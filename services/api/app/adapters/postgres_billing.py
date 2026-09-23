"""PostgreSQL BillingStore for Stripe webhooks (psycopg 3), connecting as `vaultla_system`.
Not exercised by unit tests: integration-test against Postgres."""
from __future__ import annotations

import json
from contextlib import contextmanager
from typing import Any, Iterator, Mapping
from uuid import UUID

import psycopg
from psycopg.rows import dict_row

# Whitelist: webhook payloads can never write arbitrary columns.
_COLUMNS = {"tier", "status", "stripe_subscription_id", "provider_customer_id", "current_period_end",
            "cancel_at_period_end", "storage_quota_bytes", "is_lifetime", "currency", "past_due_since"}


class PostgresBillingStore:
    def __init__(self, conn: psycopg.Connection) -> None:
        self._c = conn

    def claim_event(self, event_id: str, event_type: str) -> bool:
        cur = self._c.execute(
            "INSERT INTO billing_events (event_id, event_type) VALUES (%s, %s) "
            "ON CONFLICT (event_id) DO NOTHING", (event_id, event_type))
        return cur.rowcount == 1

    def tenant_for_customer(self, customer_id: str) -> UUID | None:
        row = self._c.execute("SELECT tenant_id FROM subscriptions WHERE provider_customer_id = %s",
                              (customer_id,)).fetchone()
        return row["tenant_id"] if row else None

    def upsert_subscription(self, tenant_id: UUID, **fields: Any) -> None:
        unknown = set(fields) - _COLUMNS
        if unknown:
            raise ValueError(f"unexpected subscription fields: {sorted(unknown)}")
        sets, params = [], []
        for col, val in fields.items():
            if col == "past_due_since" and val is not None:
                sets.append("past_due_since = COALESCE(subscriptions.past_due_since, %s)")  # keep the EARLIEST
            else:
                sets.append(f"{col} = %s")
            params.append(val)
        if not sets:
            return
        # Column names come from the whitelist above, never from input.
        self._c.execute(f"UPDATE subscriptions SET {', '.join(sets)} WHERE tenant_id = %s",
                        (*params, tenant_id))

    def audit(self, tenant_id: UUID, event_type: str, payload: Mapping[str, Any]) -> None:
        self._c.execute(
            "INSERT INTO audit_log (tenant_id, actor, event_type, trusted_time, payload) "
            "VALUES (%s, 'billing', %s, now(), %s::jsonb)",
            (tenant_id, event_type, json.dumps(payload, sort_keys=True)))


@contextmanager
def billing_store(dsn: str) -> Iterator[PostgresBillingStore]:
    with psycopg.connect(dsn, row_factory=dict_row) as conn:   # one transaction: claim + effects commit together
        yield PostgresBillingStore(conn)
