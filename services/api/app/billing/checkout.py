"""Pure builders for Stripe API parameters. No network: fully unit-testable.

Global-payments decisions encoded here (from Stripe docs, verified Sept 2026):
  * We NEVER pass `payment_method_types`. Setting it forces card-only and silently disables
    Adaptive Pricing and every local method (iDEAL, UPI, PIX, ...). Dynamic payment methods are
    managed in the Stripe Dashboard instead.
  * `adaptive_pricing.enabled` lets Stripe present local currencies; tax is computed on our
    base price by Stripe Tax.
  * `automatic_tax` + `tax_id_collection` handle VAT/GST and B2B reverse charge.
  * Enterprise deals are INVOICED (net-30, PO number), not card-checkout.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any, Mapping
from uuid import UUID

from app.billing.catalog import BASE_CURRENCY, ENTERPRISE_MIN_ANNUAL_USD, Interval, Plan, get_plan, stripe_locale, to_minor_units
from app.core.errors import ValidationFailed


def _https(url: str, field: str) -> str:
    if not url.startswith("https://"):
        raise ValidationFailed(f"{field} must be an https URL")
    return url


def price_create_params(plan: Plan, product_id: str) -> dict[str, Any]:
    """Params for stripe.Price.create: USD base + explicit local `currency_options`."""
    params: dict[str, Any] = {
        "product": product_id,
        "currency": BASE_CURRENCY.lower(),
        "unit_amount": plan.minor(BASE_CURRENCY),
        "lookup_key": plan.key,
        "transfer_lookup_key": True,
        "metadata": {"plan": plan.key, "tier": plan.tier.value},
        "currency_options": {
            cur.lower(): {"unit_amount": plan.minor(cur)}
            for cur in plan.prices if cur != BASE_CURRENCY
        },
    }
    # Tax behaviour is intentionally NOT set per price: Stripe recommends the Dashboard default
    # "automatic" (tax-exclusive for USD/CAD, tax-inclusive elsewhere, matching local convention).
    if plan.interval in (Interval.MONTH, Interval.YEAR):
        params["recurring"] = {"interval": plan.interval.value}
    return params


def checkout_session_params(*, plan_key: str, price_id: str, tenant_id: UUID, user_id: UUID,
                            success_url: str, cancel_url: str, locale: str = "en",
                            customer_id: str | None = None, customer_email: str | None = None,
                            referral_code: str | None = None) -> dict[str, Any]:
    plan = get_plan(plan_key)
    if plan.key == "free":
        raise ValidationFailed("the free plan needs no checkout")
    meta = {"tenant_id": str(tenant_id), "user_id": str(user_id), "plan": plan.key}
    if referral_code:
        meta["referral_code"] = referral_code[:16]

    params: dict[str, Any] = {
        "mode": "payment" if plan.interval is Interval.ONCE else "subscription",
        "line_items": [{"price": price_id, "quantity": 1}],
        "client_reference_id": str(tenant_id),
        "metadata": meta,
        "success_url": _https(success_url, "success_url"),
        "cancel_url": _https(cancel_url, "cancel_url"),
        "locale": stripe_locale(locale),
        "automatic_tax": {"enabled": True},
        "tax_id_collection": {"enabled": True},
        "billing_address_collection": "auto",
        "allow_promotion_codes": True,
        "adaptive_pricing": {"enabled": True},
    }
    if customer_id:
        params["customer"] = customer_id
        params["customer_update"] = {"address": "auto", "name": "auto"}
    elif customer_email:
        params["customer_email"] = customer_email
    if params["mode"] == "subscription":
        params["subscription_data"] = {"metadata": meta}
    else:
        params["invoice_creation"] = {"enabled": True, "invoice_data": {"metadata": meta}}
        params["customer_creation"] = "always"
    assert "payment_method_types" not in params        # regression guard: see module docstring
    return params


def enterprise_invoice_params(*, customer_id: str, tenant_id: UUID, annual_usd: Decimal,
                              po_number: str | None = None, days_until_due: int = 30,
                              seats_note: str = "") -> dict[str, Any]:
    """Params for a sales-assisted Enterprise subscription paid by invoice (net terms)."""
    if annual_usd < ENTERPRISE_MIN_ANNUAL_USD:
        raise ValidationFailed(f"Enterprise starts at ${ENTERPRISE_MIN_ANNUAL_USD}/year")
    if not (7 <= days_until_due <= 90):
        raise ValidationFailed("days_until_due must be 7..90")
    return {
        "customer": customer_id,
        "collection_method": "send_invoice",
        "days_until_due": days_until_due,
        "automatic_tax": {"enabled": True},
        "items": [{"price_data": {
            "currency": BASE_CURRENCY.lower(),
            "product": "REPLACE_WITH_ENTERPRISE_PRODUCT_ID",
            "unit_amount": to_minor_units(BASE_CURRENCY, annual_usd),
            "recurring": {"interval": "year"},
        }}],
        # PO number: also set it on the customer's invoice_settings.custom_fields so it prints on
        # every invoice (Stripe does not accept custom_fields on Subscription create).
        "metadata": {"tenant_id": str(tenant_id), "plan": "enterprise", "note": seats_note[:200],
                     **({"po_number": po_number[:30]} if po_number else {})},
    }
