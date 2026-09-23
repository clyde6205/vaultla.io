"""Chronos scanner: the daily sweep for matured vaults.

Guarantees
  * FAIL CLOSED: if trusted time cannot be established (quorum/spread) or has moved
    backwards, nothing matures and the run raises so EventBridge/alerting sees it.
  * IDEMPOTENT + CONCURRENCY-SAFE: rows are claimed with FOR UPDATE SKIP LOCKED inside a
    transaction, so overlapping invocations never double-process a vault.
  * ISOLATED: a corrupt trigger on one vault is audited and deferred; it cannot block others.
  * AUDITED: every state change writes a hash-chained audit record stamped with trusted time.
"""
from __future__ import annotations

import logging
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping, Protocol, Sequence
from uuid import UUID

from app.chronos.time_source import TimeSource
from app.chronos.triggers import build_trigger
from app.core.errors import TimeRegressionError, VaultlaError

log = logging.getLogger("vaultla.chronos.scanner")

REGRESSION_TOLERANCE = timedelta(seconds=5)
ERROR_BACKOFF = timedelta(days=1)


@dataclass(frozen=True, slots=True)
class DueVault:
    id: UUID
    tenant_id: UUID
    trigger: str
    trigger_config: Mapping[str, Any]
    last_checkin_at: datetime | None
    attestations: int
    releasable_bps: int


class ChronosTx(Protocol):
    def last_trusted_time(self) -> datetime | None: ...
    def record_trusted_time(self, t: datetime) -> None: ...
    def fetch_due(self, now: datetime, limit: int) -> Sequence[DueVault]: ...
    def apply(self, vault_id: UUID, *, matured: bool, releasable_bps: int,
              next_eval_at: datetime | None, now: datetime) -> None: ...
    def audit(self, tenant_id: UUID, vault_id: UUID | None, event_type: str,
              trusted_time: datetime, payload: Mapping[str, Any]) -> None: ...


class ChronosRepository(Protocol):
    def transaction(self) -> AbstractContextManager[ChronosTx]: ...


@dataclass(slots=True)
class ScanReport:
    trusted_time: datetime
    examined: int = 0
    matured: int = 0
    advanced: int = 0          # progressive vaults whose releasable % increased
    deferred: int = 0          # not yet due after evaluation
    errors: int = 0
    matured_ids: list[UUID] = field(default_factory=list)


class ChronosScanner:
    def __init__(self, repo: ChronosRepository, time_source: TimeSource, *,
                 batch_size: int = 500, max_batches: int = 200,
                 on_matured: Callable[[DueVault, datetime], None] | None = None) -> None:
        if not (1 <= batch_size <= 5000):
            raise ValueError("batch_size must be 1..5000")
        self._repo, self._time = repo, time_source
        self._batch, self._max_batches = batch_size, max_batches
        self._on_matured = on_matured

    # ------------------------------------------------------------------
    def _trusted_now(self) -> datetime:
        now = self._time.now()          # raises TimeConsensusError -> propagates (fail closed)
        with self._repo.transaction() as tx:
            last = tx.last_trusted_time()
            if last is not None and now < last - REGRESSION_TOLERANCE:
                raise TimeRegressionError(
                    f"Trusted time {now.isoformat()} is earlier than last recorded {last.isoformat()}.")
            tx.record_trusted_time(max(now, last) if last else now)
        return now

    def run(self) -> ScanReport:
        now = self._trusted_now()
        report = ScanReport(trusted_time=now)
        for _ in range(self._max_batches):
            newly_matured: list[DueVault] = []
            with self._repo.transaction() as tx:
                due = tx.fetch_due(now, self._batch)
                if not due:
                    break
                for v in due:
                    report.examined += 1
                    if self._process(tx, v, now, report):
                        newly_matured.append(v)
            # Side effects (e.g. S3 restore requests) run AFTER commit, never while holding row locks.
            for v in newly_matured:
                self._fire_hook(v, now)
            if len(due) < self._batch:
                break
        log.info("chronos scan complete: %s", report)
        return report

    # ------------------------------------------------------------------
    def _fire_hook(self, v: DueVault, now: datetime) -> None:
        if not self._on_matured:
            return
        try:
            self._on_matured(v, now)
        except Exception:  # noqa: BLE001 - post-commit hook failures must not fail the scan
            log.exception("on_matured hook failed for vault %s", v.id)

    def _process(self, tx: ChronosTx, v: DueVault, now: datetime, report: ScanReport) -> bool:
        """Returns True if the vault matured in this pass."""
        try:
            trig = build_trigger(v.trigger, v.trigger_config)
            ev = trig.evaluate(now, last_checkin_at=v.last_checkin_at, attestations=v.attestations)
            tx.apply(v.id, matured=ev.matured, releasable_bps=ev.releasable_bps,
                     next_eval_at=ev.next_eval_at, now=now)
            if ev.matured:
                report.matured += 1
                report.matured_ids.append(v.id)
                tx.audit(v.tenant_id, v.id, "vault.matured", now,
                         {"trigger": v.trigger, "reason": ev.reason})
                return True
            elif ev.releasable_bps > v.releasable_bps:
                report.advanced += 1
                tx.audit(v.tenant_id, v.id, "vault.tranche_released", now,
                         {"releasable_bps": ev.releasable_bps})
            else:
                report.deferred += 1
        except VaultlaError as exc:
            self._defer_on_error(tx, v, now, report, exc.code)
        except Exception as exc:  # noqa: BLE001 - never let one vault abort the sweep
            log.exception("unexpected error evaluating vault %s", v.id)
            self._defer_on_error(tx, v, now, report, type(exc).__name__)
        return False

    @staticmethod
    def _defer_on_error(tx: ChronosTx, v: DueVault, now: datetime,
                        report: ScanReport, code: str) -> None:
        report.errors += 1
        # Fail closed: keep sealed, retry tomorrow so a bad row cannot hot-loop the scanner.
        tx.apply(v.id, matured=False, releasable_bps=v.releasable_bps,
                 next_eval_at=now + ERROR_BACKOFF, now=now)
        tx.audit(v.tenant_id, v.id, "vault.trigger_error", now, {"code": code})
