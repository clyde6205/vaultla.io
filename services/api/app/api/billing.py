"""Billing routes: Stripe Checkout, customer portal, and the webhook endpoint."""
from __future__ import annotations

import os
from functools import lru_cache
from uuid import UUID

import stripe
from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from app.adapters.postgres_billing import billing_store
from app.api.auth import Principal, current_principal
from app.billing.catalog import get_plan
from app.billing.checkout import checkout_session_params
from app.billing.webhooks import process_webhook
from app.core.errors import ExternalServiceError, ValidationFailed

router = APIRouter(tags=["billing"])
stripe.api_key = os.environ.get("STRIPE_API_KEY", "")
_SITE = os.environ.get("SITE_URL", "https://vaultla.io")


class CheckoutBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    plan: str = Field(pattern=r"^(premium_monthly|premium_yearly|lifetime)$")
    locale: str = Field(default="en", max_length=8)
    referral_code: str | None = Field(default=None, max_length=16)


@lru_cache(maxsize=16)
def _price_id(lookup_key: str) -> str:
    prices = stripe.Price.list(lookup_keys=[lookup_key], active=True, limit=1).data
    if not prices:
        raise ExternalServiceError("Pricing is temporarily unavailable.")
    return prices[0].id


@router.post("/v1/billing/checkout")
def create_checkout(body: CheckoutBody, p: Principal = Depends(current_principal)) -> dict[str, str]:
    plan = get_plan(body.plan)
    params = checkout_session_params(
        plan_key=plan.key, price_id=_price_id(plan.key), tenant_id=p.tenant_id, user_id=p.user_id,
        success_url=f"{_SITE}/{body.locale}/welcome?session_id={{CHECKOUT_SESSION_ID}}",
        cancel_url=f"{_SITE}/{body.locale}/pricing", locale=body.locale, referral_code=body.referral_code)
    try:
        session = stripe.checkout.Session.create(**params)
    except stripe.StripeError as exc:
        raise ExternalServiceError("Could not start checkout.") from exc
    return {"url": session.url}


@router.post("/webhooks/stripe", include_in_schema=False)
async def stripe_webhook(request: Request, stripe_signature: str = Header(default="")) -> JSONResponse:
    raw = await request.body()                         # RAW bytes: required for signature verification
    with billing_store(os.environ["SYSTEM_DATABASE_URL"]) as store:
        result = process_webhook(raw, stripe_signature, os.environ["STRIPE_WEBHOOK_SECRET"], store)
    # Non-2xx makes Stripe retry: used when a tenant link has not arrived yet.
    return JSONResponse({"ok": result.handled, "reason": result.reason}, status_code=409 if result.retry else 200)
