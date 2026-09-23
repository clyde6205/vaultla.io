"""Time-lock trigger logic. PURE functions: no I/O, no clocks, no database.

`now` is always injected and MUST come from a TimeSource (trusted quorum time).
Each evaluation returns:
  * matured          - the vault as a whole may now release keys (fixed / liveness)
  * releasable_bps   - basis points (0..10000) of items releasable (progressive)
  * next_eval_at     - earliest instant this trigger could change outcome; the scanner
                       stores it so the daily scan touches only genuinely due vaults.

Progressive releases use integer basis points (5% == 500) to avoid float drift over a
100-year horizon. Calendar math uses whole-year steps anchored to the start date, so a
Feb-29 anchor lands on Feb-28 in non-leap years (documented, deterministic).
"""
from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Mapping

from app.core.errors import ValidationFailed

BPS_FULL = 10_000
MAX_HORIZON_YEARS = 150   # sanity cap: 100-year products plus headroom


class TriggerType(str, Enum):
    FIXED_DATE = "fixed_date"
    PROGRESSIVE = "progressive"
    LIVENESS = "liveness"


@dataclass(frozen=True, slots=True)
class Evaluation:
    matured: bool
    releasable_bps: int
    next_eval_at: datetime | None
    reason: str


# ------------------------------------------------------------------ helpers
def _aware(dt: datetime, name: str) -> datetime:
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValidationFailed(f"{name} must be timezone-aware")
    return dt.astimezone(timezone.utc)


def add_years(dt: datetime, years: int) -> datetime:
    """Add whole calendar years; Feb 29 -> Feb 28 in non-leap target years."""
    y = dt.year + years
    day = dt.day
    if dt.month == 2 and day == 29 and not calendar.isleap(y):
        day = 28
    return dt.replace(year=y, day=day)


def parse_dt(value: Any, name: str) -> datetime:
    if isinstance(value, datetime):
        return _aware(value, name)
    if isinstance(value, str):
        try:
            return _aware(datetime.fromisoformat(value.replace("Z", "+00:00")), name)
        except ValueError as exc:
            raise ValidationFailed(f"{name} is not a valid ISO-8601 timestamp") from exc
    raise ValidationFailed(f"{name} is required")


# ------------------------------------------------------------------ fixed date
@dataclass(frozen=True, slots=True)
class FixedDate:
    unlock_at: datetime

    @staticmethod
    def from_config(cfg: Mapping[str, Any]) -> "FixedDate":
        return FixedDate(parse_dt(cfg.get("unlock_at"), "unlock_at"))

    def evaluate(self, now: datetime, **_: Any) -> Evaluation:
        now = _aware(now, "now")
        if now >= self.unlock_at:
            return Evaluation(True, BPS_FULL, None, "fixed date reached")
        return Evaluation(False, 0, self.unlock_at, "waiting for fixed date")


# ------------------------------------------------------------------ progressive
@dataclass(frozen=True, slots=True)
class Progressive:
    """`step_bps` released every `every_years` starting at `start_at`.
    e.g. 5% per year: step_bps=500, every_years=1  -> fully released after 20 years."""
    start_at: datetime
    step_bps: int
    every_years: int = 1
    first_step_at_start: bool = False   # if True, the first tranche opens at start_at

    def __post_init__(self) -> None:
        if not (1 <= self.step_bps <= BPS_FULL):
            raise ValidationFailed("step_bps must be between 1 and 10000")
        if not (1 <= self.every_years <= MAX_HORIZON_YEARS):
            raise ValidationFailed("every_years out of range")
        steps_needed = -(-BPS_FULL // self.step_bps)
        if steps_needed * self.every_years > MAX_HORIZON_YEARS:
            raise ValidationFailed("schedule exceeds maximum supported horizon")

    @staticmethod
    def from_config(cfg: Mapping[str, Any]) -> "Progressive":
        try:
            step = int(cfg["step_bps"])
            yrs = int(cfg.get("every_years", 1))
        except (KeyError, TypeError, ValueError) as exc:
            raise ValidationFailed("progressive trigger needs integer step_bps") from exc
        return Progressive(parse_dt(cfg.get("start_at"), "start_at"), step, yrs,
                           bool(cfg.get("first_step_at_start", False)))

    def _steps_elapsed(self, now: datetime) -> int:
        # Step k (1-based) opens at start + (k - 1 + lag) * every_years, where lag is 0 when
        # the first tranche opens at start_at and 1 otherwise. Bounded loop, exact calendar math.
        n = 0
        lag = 0 if self.first_step_at_start else 1
        while n < MAX_HORIZON_YEARS:
            if now < add_years(self.start_at, (n + lag) * self.every_years):
                break
            n += 1
        return n

    def evaluate(self, now: datetime, **_: Any) -> Evaluation:
        now = _aware(now, "now")
        steps = self._steps_elapsed(now)
        bps = min(steps * self.step_bps, BPS_FULL)
        if bps >= BPS_FULL:
            return Evaluation(True, BPS_FULL, None, "progressive schedule complete")
        lag = 0 if self.first_step_at_start else 1
        nxt = add_years(self.start_at, (steps + lag) * self.every_years)
        return Evaluation(False, bps, nxt, f"{bps / 100:.2f}% released")


# ------------------------------------------------------------------ liveness (dead man's switch)
@dataclass(frozen=True, slots=True)
class Liveness:
    """Matures when the owner misses check-ins for `interval_days + grace_days`, AND (if
    configured) `required_attestations` independent verified confirmations exist. The
    attestation requirement is the guard against a lapsed email or forgotten app
    releasing a vault by accident."""
    interval_days: int
    grace_days: int = 14
    required_attestations: int = 0
    created_at: datetime | None = None

    def __post_init__(self) -> None:
        if not (1 <= self.interval_days <= 3660):
            raise ValidationFailed("interval_days must be 1..3660")
        if not (0 <= self.grace_days <= 365):
            raise ValidationFailed("grace_days must be 0..365")
        if not (0 <= self.required_attestations <= 20):
            raise ValidationFailed("required_attestations must be 0..20")

    @staticmethod
    def from_config(cfg: Mapping[str, Any]) -> "Liveness":
        try:
            return Liveness(int(cfg["interval_days"]), int(cfg.get("grace_days", 14)),
                            int(cfg.get("required_attestations", 0)),
                            parse_dt(cfg["created_at"], "created_at") if cfg.get("created_at") else None)
        except (KeyError, TypeError, ValueError) as exc:
            raise ValidationFailed("liveness trigger needs integer interval_days") from exc

    def deadline(self, last_checkin_at: datetime | None) -> datetime:
        anchor = last_checkin_at or self.created_at
        if anchor is None:
            raise ValidationFailed("liveness vault has no check-in or creation anchor")
        return _aware(anchor, "last_checkin_at") + timedelta(days=self.interval_days + self.grace_days)

    def evaluate(self, now: datetime, *, last_checkin_at: datetime | None = None,
                 attestations: int = 0, **_: Any) -> Evaluation:
        now = _aware(now, "now")
        deadline = self.deadline(last_checkin_at)
        if now < deadline:
            return Evaluation(False, 0, deadline, "owner check-in window still open")
        if attestations < self.required_attestations:
            # Deadline passed but not enough confirmations. Re-evaluate daily.
            return Evaluation(False, 0, now + timedelta(days=1),
                              f"awaiting attestations ({attestations}/{self.required_attestations})")
        return Evaluation(True, BPS_FULL, None, "liveness deadline missed and attested")


# ------------------------------------------------------------------ factory
Trigger = FixedDate | Progressive | Liveness


def build_trigger(kind: str, cfg: Mapping[str, Any]) -> Trigger:
    try:
        t = TriggerType(kind)
    except ValueError as exc:
        raise ValidationFailed(f"unknown trigger type: {kind!r}") from exc
    return {
        TriggerType.FIXED_DATE: FixedDate,
        TriggerType.PROGRESSIVE: Progressive,
        TriggerType.LIVENESS: Liveness,
    }[t].from_config(cfg)


def releasable_item_count(total_items: int, releasable_bps: int) -> int:
    """How many items (ordered by release_rank) may release keys at `releasable_bps`.
    Floor division: never releases early; the final tranche always reaches 100%."""
    if total_items < 0 or not (0 <= releasable_bps <= BPS_FULL):
        raise ValidationFailed("invalid release arguments")
    return (total_items * releasable_bps) // BPS_FULL
