"""Stripe webhook verification and idempotent event handling.

Security: the signature scheme is implemented with the standard library so it can be audited
and tested offline: header `t=<unix>,v1=<hex>[,v1=...]`, signed payload `"{t}.{raw_body}"`,
HMAC-SHA256 with the endpoint secret, constant-time comparison, 5-minute replay tolerance.
ALWAYS verify against the RAW request bytes, never re-serialised JSON.

Handling is idempotent (Stripe retries and may deliver out of order): each event id is
recorded once, and state is derived from the object's current status, not from event order.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Protocol
from uuid import UUID

from app.billing.catalog import PLANS, Tier
from app.core.errors import NotAuthorized, ValidationFailed

log = logging.getLogger("vaultla.billing")
TOLERANCE_SECONDS = 300


def verify_signature(payload: bytes, header: str, secret: str, *,
                     now: float | None = None, tolerance: int = TOLERANCE_SECONDS) -> None:
    """Raises NotAuthorized unless the header authenticates `payload`."""
    try:
        parts = [kv.split("=", 1) for kv in header.split(",")]
        ts = int(next(v for k, v in parts if k == "t"))
        sigs = [v for k, v in parts if k == "v1"]
    except (StopIteration, ValueError) as exc:
        raise NotAuthorized("Malformed signature header.") from exc
    if not sigs:
        raise NotAuthorized("No v1 signature present.")
    if abs((now if now is not None else time.time()) - ts) > tolerance:
        raise NotAuthorized("Signature timestamp outside tolerance.")
    expected = hmac.new(secret.encode(), f"{ts}.".encode() + payload, hashlib.sha256).hexdigest()
    if not any(hmac.compare_digest(expected, s) for s in sigs):
        raise NotAuthorized("Signature mismatch.")


class BillingStore(Protocol):
    def claim_event(self, event_id: str, event_type: str) -> bool:
        """Atomically record the event id; return False if it was already processed."""
    def tenant_for_customer(self, customer_id: str) -> UUID | None: ...
    def upsert_subscription(self, tenant_id: UUID, **fields: Any) -> None: ...
    def audit(self, tenant_id: UUID, event_type: str, payload: Mapping[str, Any]) -> None: ...


@dataclass(frozen=True, slots=True)
class HandleResult:
    handled: bool
    reason: str
    retry: bool = False      # True => respond non-2xx so Stripe redelivers (e.g. tenant not linked yet)


def _tenant_from(obj: Mapping[str, Any], store: BillingStore) -> UUID | None:
    raw = (obj.get("metadata") or {}).get("tenant_id") or obj.get("client_reference_id")
    if raw:
        try:
            return UUID(str(raw))
        except ValueError:
            return None
    cust = obj.get("customer")
    return store.tenant_for_customer(cust) if isinstance(cust, str) else None


def _dt(ts: int | None) -> datetime | None:
    return datetime.fromtimestamp(ts, tz=timezone.utc) if ts else None


def handle_event(event: Mapping[str, Any], store: BillingStore, *, now: datetime) -> HandleResult:
    etype, eid = event.get("type"), event.get("id")
    if not isinstance(etype, str) or not isinstance(eid, str):
        raise ValidationFailed("malformed event")
    obj = (event.get("data") or {}).get("object") or {}

    if etype not in _HANDLED:
        return HandleResult(False, "ignored")

    # Resolve the tenant BEFORE claiming the event id. Stripe can deliver a subscription event
    # before the checkout event that links customer -> tenant; if we claimed first, the retry
    # Stripe sends later would be swallowed as a duplicate and the purchase would be lost.
    tenant = _tenant_from(obj, store)
    if tenant is None:
        log.warning("billing event %s (%s) has no resolvable tenant yet", eid, etype)
        return HandleResult(False, "no_tenant", retry=True)
    if not store.claim_event(eid, etype):
        return HandleResult(False, "duplicate")
    return _HANDLED[etype](obj, tenant, store, now)


def _checkout_completed(obj, tenant, store, now) -> HandleResult:
    plan_key = (obj.get("metadata") or {}).get("plan")
    cust = obj.get("customer")
    if cust:
        store.upsert_subscription(tenant, provider_customer_id=cust)
    if obj.get("mode") == "payment" and plan_key == "lifetime":
        if obj.get("payment_status") != "paid":
            return HandleResult(False, "lifetime_not_paid_yet")
        store.upsert_subscription(tenant, tier=Tier.LIFETIME.value, is_lifetime=True, status="active",
                                  storage_quota_bytes=PLANS["lifetime"].storage_bytes,
                                  currency=(obj.get("currency") or "usd").upper(), past_due_since=None)
        store.audit(tenant, "billing.lifetime_activated", {"session": obj.get("id")})
        return HandleResult(True, "lifetime_activated")
    return HandleResult(True, "customer_linked")   # subscription state arrives via subscription events


def _plan_from_subscription(obj: Mapping[str, Any]) -> str | None:
    meta_plan = (obj.get("metadata") or {}).get("plan")
    if meta_plan:
        return meta_plan
    items = ((obj.get("items") or {}).get("data")) or []
    return ((items[0].get("price") or {}).get("lookup_key")) if items else None


def _subscription_changed(obj, tenant, store, now) -> HandleResult:
    status = obj.get("status", "")
    plan_key = _plan_from_subscription(obj)
    fields: dict[str, Any] = dict(
        status=status, stripe_subscription_id=obj.get("id"),
        provider_customer_id=obj.get("customer"), current_period_end=_dt(obj.get("current_period_end")),
        cancel_at_period_end=bool(obj.get("cancel_at_period_end")))
    if status == "past_due":
        fields["past_due_since"] = now       # store keeps the earliest value (COALESCE) on conflict
    elif status in ("active", "trialing"):
        fields["past_due_since"] = None
    if plan_key == "enterprise":
        fields["tier"] = Tier.ENTERPRISE.value
    elif plan_key in PLANS and PLANS[plan_key].tier is Tier.PREMIUM:
        fields.update(tier=Tier.PREMIUM.value, storage_quota_bytes=PLANS[plan_key].storage_bytes)
    store.upsert_subscription(tenant, **fields)
    store.audit(tenant, "billing.subscription_changed", {"status": status, "plan": plan_key})
    return HandleResult(True, f"subscription_{status}")


def _subscription_deleted(obj, tenant, store, now) -> HandleResult:
    # Data is NEVER deleted here; entitlements simply fall back to free-tier creation limits.
    store.upsert_subscription(tenant, status="canceled", cancel_at_period_end=False)
    store.audit(tenant, "billing.subscription_canceled", {})
    return HandleResult(True, "subscription_canceled")


def _payment_failed(obj, tenant, store, now) -> HandleResult:
    store.upsert_subscription(tenant, status="past_due", past_due_since=now)
    store.audit(tenant, "billing.payment_failed", {"invoice": obj.get("id")})
    return HandleResult(True, "payment_failed")


_HANDLED = {
    "checkout.session.completed": _checkout_completed,
    "checkout.session.async_payment_succeeded": _checkout_completed,   # delayed methods (bank debits, vouchers)
    "customer.subscription.created": _subscription_changed,
    "customer.subscription.updated": _subscription_changed,
    "customer.subscription.deleted": _subscription_deleted,
    "invoice.payment_failed": _payment_failed,
}


def process_webhook(payload: bytes, signature_header: str, secret: str, store: BillingStore,
                    *, now: datetime | None = None) -> HandleResult:
    verify_signature(payload, signature_header, secret)
    try:
        event = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ValidationFailed("payload is not JSON") from exc
    return handle_event(event, store, now=now or datetime.now(timezone.utc))
