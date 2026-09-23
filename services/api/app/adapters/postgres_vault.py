"""PostgreSQL implementation of VaultRepository (psycopg 3).

Every request runs inside ONE transaction that first executes
    SELECT set_config('app.tenant_id', <tenant>, true)
so RLS scopes every statement. Queries ALSO filter on tenant_id explicitly (belt and braces).
Connects as `vaultla_app`. NOT exercised by unit tests: integration-test against Postgres."""
from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Iterator, Mapping, Sequence
from uuid import UUID

import psycopg
from psycopg.rows import dict_row

from app.vault.ports import ItemRecord, NewItem, VaultRecord


@contextmanager
def tenant_session(dsn: str, tenant_id: UUID) -> Iterator["PostgresVaultRepository"]:
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        conn.execute("SELECT set_config('app.tenant_id', %s, true)", (str(tenant_id),))
        yield PostgresVaultRepository(conn)


class PostgresVaultRepository:
    def __init__(self, conn: psycopg.Connection) -> None:
        self._c = conn

    def quota_and_usage(self, tenant_id: UUID) -> tuple[int, int]:
        q = self._c.execute("SELECT storage_quota_bytes FROM subscriptions WHERE tenant_id = %s",
                            (tenant_id,)).fetchone()
        u = self._c.execute("SELECT COALESCE(SUM(total_size_bytes), 0) AS used FROM vaults "
                            "WHERE tenant_id = %s AND state <> 'revoked'", (tenant_id,)).fetchone()
        return int(q["storage_quota_bytes"]) if q else 0, int(u["used"])

    def insert_draft(self, *, vault_id: UUID, tenant_id: UUID, owner_id: UUID, trigger: str,
                     trigger_config: Mapping[str, Any], unlock_at: datetime | None,
                     total_size_bytes: int, envelope_version: int, items: Sequence[NewItem],
                     trusted_now: datetime) -> None:
        self._c.execute(
            """INSERT INTO vaults (id, tenant_id, owner_id, state, trigger, trigger_config,
                                   unlock_at, total_size_bytes, envelope_version)
               VALUES (%s,%s,%s,'draft',%s,%s::jsonb,%s,%s,%s)""",
            (vault_id, tenant_id, owner_id, trigger, json.dumps(trigger_config, sort_keys=True),
             unlock_at, total_size_bytes, envelope_version))
        for it in items:
            self._c.execute(
                """INSERT INTO vault_items (id, tenant_id, vault_id, release_rank, size_bytes,
                        storage_key, ciphertext_sha256, server_share_sealed)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
                (it.item_id, tenant_id, vault_id, it.release_rank, it.size_bytes, it.storage_key,
                 it.sha256, it.server_share_sealed))
            for w in it.wrapped_shares:
                self._c.execute(
                    """INSERT INTO vault_key_envelopes (tenant_id, vault_id, item_id, holder,
                            holder_user_id, wrapped_share) VALUES (%s,%s,%s,%s,%s,%s)""",
                    (tenant_id, vault_id, it.item_id, w.holder, w.holder_user_id, w.blob))

    def get_vault(self, tenant_id: UUID, vault_id: UUID) -> VaultRecord | None:
        r = self._c.execute(
            """SELECT id, tenant_id, owner_id, state::text, trigger::text AS trigger, trigger_config,
                      releasable_bps, last_checkin_at, storage_class::text AS storage_class
                 FROM vaults WHERE tenant_id = %s AND id = %s""", (tenant_id, vault_id)).fetchone()
        return None if r is None else VaultRecord(
            r["id"], r["tenant_id"], r["owner_id"], r["state"], r["trigger"], r["trigger_config"],
            r["releasable_bps"], r["last_checkin_at"], r["storage_class"])

    def get_items(self, tenant_id: UUID, vault_id: UUID) -> Sequence[ItemRecord]:
        rows = self._c.execute(
            """SELECT id, vault_id, release_rank, size_bytes, storage_key, ciphertext_sha256,
                      server_share_sealed, upload_confirmed_at IS NOT NULL AS confirmed
                 FROM vault_items WHERE tenant_id = %s AND vault_id = %s ORDER BY release_rank""",
            (tenant_id, vault_id)).fetchall()
        return [ItemRecord(r["id"], r["vault_id"], r["release_rank"], r["size_bytes"], r["storage_key"],
                           bytes(r["ciphertext_sha256"]), bytes(r["server_share_sealed"]), r["confirmed"])
                for r in rows]

    def confirm_item(self, tenant_id: UUID, item_id: UUID) -> None:
        self._c.execute("UPDATE vault_items SET upload_confirmed_at = now() "
                        "WHERE tenant_id = %s AND id = %s", (tenant_id, item_id))

    def seal(self, tenant_id: UUID, vault_id: UUID, *, next_eval_at: datetime | None,
             releasable_bps: int, matured: bool, now: datetime) -> None:
        self._c.execute(
            """UPDATE vaults SET state = %s, next_eval_at = %s, releasable_bps = %s, sealed_at = %s
                WHERE tenant_id = %s AND id = %s AND state = 'draft'""",
            ("matured" if matured else "sealed", next_eval_at, releasable_bps, now, tenant_id, vault_id))

    def is_authorized_recipient(self, tenant_id: UUID, vault_id: UUID, user_id: UUID) -> bool:
        return self._c.execute(
            """SELECT 1 FROM vault_key_envelopes WHERE tenant_id = %s AND vault_id = %s
                  AND holder_user_id = %s AND holder IN ('guardian','beneficiary') LIMIT 1""",
            (tenant_id, vault_id, user_id)).fetchone() is not None

    def mark_item_released(self, tenant_id: UUID, item_id: UUID, now: datetime) -> None:
        self._c.execute("UPDATE vault_items SET released_at = COALESCE(released_at, %s) "
                        "WHERE tenant_id = %s AND id = %s", (now, tenant_id, item_id))

    def record_checkin(self, tenant_id: UUID, vault_id: UUID, *, source: str,
                       attested_by: str | None, trusted_time: datetime, next_eval_at: datetime) -> None:
        self._c.execute(
            """INSERT INTO liveness_checkins (tenant_id, vault_id, source, attested_by, trusted_time)
               VALUES (%s,%s,%s,%s,%s)""", (tenant_id, vault_id, source, attested_by, trusted_time))
        if source == "user":
            self._c.execute("UPDATE vaults SET last_checkin_at = %s, next_eval_at = %s "
                            "WHERE tenant_id = %s AND id = %s", (trusted_time, next_eval_at, tenant_id, vault_id))
        else:
            self._c.execute("UPDATE vaults SET next_eval_at = LEAST(next_eval_at, %s) "
                            "WHERE tenant_id = %s AND id = %s", (next_eval_at, tenant_id, vault_id))

    def audit(self, tenant_id: UUID, vault_id: UUID | None, actor: str, event_type: str,
              trusted_time: datetime, payload: Mapping[str, Any]) -> None:
        self._c.execute(
            """INSERT INTO audit_log (tenant_id, vault_id, actor, event_type, trusted_time, payload)
               VALUES (%s,%s,%s,%s,%s,%s::jsonb)""",
            (tenant_id, vault_id, actor, event_type, trusted_time, json.dumps(payload, sort_keys=True)))
