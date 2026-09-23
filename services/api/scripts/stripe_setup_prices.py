"""One-time (idempotent by lookup_key) creation of Stripe Products and multi-currency Prices.

Usage:  STRIPE_API_KEY=sk_test_... python scripts/stripe_setup_prices.py [--dry-run]
Run against TEST mode first. Requires: pip install stripe. Never commit keys.
"""
from __future__ import annotations

import json
import os
import sys

from app.billing.catalog import PLANS
from app.billing.checkout import price_create_params


def main(dry: bool) -> None:
    if dry:
        for plan in PLANS.values():
            if plan.key != "free":
                print(json.dumps(price_create_params(plan, "prod_DRYRUN"), indent=2, default=str))
        return
    import stripe  # noqa: PLC0415
    stripe.api_key = os.environ["STRIPE_API_KEY"]
    product = stripe.Product.create(name="Vaultla.io Premium", metadata={"app": "vaultla"})
    for plan in PLANS.values():
        if plan.key == "free":
            continue
        existing = stripe.Price.list(lookup_keys=[plan.key], limit=1).data
        if existing:
            print(f"{plan.key}: exists ({existing[0].id})")
            continue
        price = stripe.Price.create(**price_create_params(plan, product.id))
        print(f"{plan.key}: created {price.id}")


if __name__ == "__main__":
    main("--dry-run" in sys.argv)
