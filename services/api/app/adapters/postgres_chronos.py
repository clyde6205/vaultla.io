"""PostgreSQL implementation of ChronosRepository (psycopg 3).
Connects as the restricted `vaultla_chronos` role (see migration). NOT exercised by the
unit tests in this scaffold; integration-test against a real Postgres before production."""
from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Iterator, Mapping, Sequence
from uuid import UUID

import psycopg
from psycopg.rows import dict_row

from app.chronos.scanner import ChronosTx, DueVault

_FETCH_DUE = """
SELECT v.id, v.tenant_id, v.trigger::text AS trigger, v.trigger_config,
       v.last_checkin_at, v.releasable_bps,
       COALESCE((
         SELECT count(DISTINCT c.attested_by) FROM liveness_checkins c
          WHERE c.vault_id = v.id AND c.source IN ('webhook', 'trustee')
            AND c.trusted_time > COALESCE(v.last_checkin_at, '-infinity'::timestamptz)
       ), 0) AS attestations
  FROM vaults v
 WHERE v.state = 'sealed' AND v.next_eval_at <= %s
 ORDER BY v.next_eval_at
 LIMIT %s
 FOR UPDATE OF v SKIP LOCKED
"""


class _Tx:
    def __init__(self, conn: psycopg.Connection) -> None:
        self._c = conn

    def last_trusted_time(self) -> datetime | None:
        row = self._c.execute(
            "SELECT last_trusted_time FROM chronos_state WHERE id = true FOR UPDATE").fetchone()
        return row["last_trusted_time"] if row else None

    def record_trusted_time(self, t: datetime) -> None:
        self._c.execute(
            """INSERT INTO chronos_state (id, last_trusted_time) VALUES (true, %s)
               ON CONFLICT (id) DO UPDATE
                 SET last_trusted_time = GREATEST(chronos_state.last_trusted_time, EXCLUDED.last_trusted_time),
                     updated_at = now()""", (t,))

    def fetch_due(self, now: datetime, limit: int) -> Sequence[DueVault]:
        rows = self._c.execute(_FETCH_DUE, (now, limit)).fetchall()
        return [DueVault(id=r["id"], tenant_id=r["tenant_id"], trigger=r["trigger"],
                         trigger_config=r["trigger_config"], last_checkin_at=r["last_checkin_at"],
                         attestations=int(r["attestations"]), releasable_bps=r["releasable_bps"])
                for r in rows]

    def apply(self, vault_id: UUID, *, matured: bool, releasable_bps: int,
              next_eval_at: datetime | None, now: datetime) -> None:
        if matured:
            self._c.execute(
                """UPDATE vaults SET state = 'matured', matured_at = %s, releasable_bps = %s,
                          next_eval_at = NULL WHERE id = %s AND state = 'sealed'""",
                (now, releasable_bps, vault_id))
        else:
            self._c.execute(
                """UPDATE vaults SET releasable_bps = %s, next_eval_at = %s
                    WHERE id = %s AND state = 'sealed'""",
                (releasable_bps, next_eval_at, vault_id))

    def audit(self, tenant_id: UUID, vault_id: UUID | None, event_type: str,
              trusted_time: datetime, payload: Mapping[str, Any]) -> None:
        self._c.execute(
            """INSERT INTO audit_log (tenant_id, vault_id, actor, event_type, trusted_time, payload)
               VALUES (%s, %s, 'chronos', %s, %s, %s::jsonb)""",
            (tenant_id, vault_id, event_type, trusted_time, json.dumps(payload, sort_keys=True)))


class PostgresChronosRepository:
    def __init__(self, dsn: str) -> None:
        self._dsn = dsn

    @contextmanager
    def transaction(self) -> Iterator[ChronosTx]:
        with psycopg.connect(self._dsn, row_factory=dict_row) as conn:  # commits/rolls back on exit
            yield _Tx(conn)
