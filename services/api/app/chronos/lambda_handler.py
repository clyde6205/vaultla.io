"""AWS Lambda entrypoint, invoked once daily by EventBridge Scheduler.

Raising on failure is deliberate: EventBridge Scheduler retries with backoff and sends the
final failure to the DLQ, which pages the on-call. A silent success on bad time would be worse.
"""
from __future__ import annotations

import logging
from typing import Any

from app.adapters.aws import S3ObjectStore
from app.adapters.postgres_chronos import PostgresChronosRepository
from app.chronos.scanner import ChronosScanner, DueVault
from app.chronos.time_source import QuorumTimeSource
from app.core.config import Settings

log = logging.getLogger("vaultla.chronos")
logging.getLogger().setLevel(logging.INFO)


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    cfg = Settings.from_env()
    store = S3ObjectStore(cfg.s3_bucket, cfg.aws_region, cfg.kms_key_id)

    def on_matured(v: DueVault, now) -> None:
        # Deep Archive objects need a restore (12-48h) before they can be downloaded.
        # Best-effort here; the vault API also lazily requests restore on first access.
        try:
            store.request_restore_prefix(f"tenants/{v.tenant_id}/vaults/{v.id}/")
        except Exception:  # noqa: BLE001
            log.exception("restore request failed for vault %s (will retry on access)", v.id)

    scanner = ChronosScanner(
        PostgresChronosRepository(cfg.chronos_database_url),
        QuorumTimeSource(cfg.ntp_servers, min_sources=cfg.ntp_min_sources,
                         max_spread_seconds=cfg.ntp_max_spread_seconds),
        on_matured=on_matured,
    )
    report = scanner.run()
    return {"trusted_time": report.trusted_time.isoformat(), "examined": report.examined,
            "matured": report.matured, "advanced": report.advanced, "errors": report.errors}
