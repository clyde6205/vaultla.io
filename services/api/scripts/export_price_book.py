"""Export the price book (minor units) for the web app so backend and frontend never drift.
Usage (from services/api):  PYTHONPATH=. python scripts/export_price_book.py > ../../apps/web/src/lib/price-book.json
A unit test asserts the committed file matches this output."""
from __future__ import annotations

import json

from app.billing.catalog import ENTERPRISE_MIN_ANNUAL_USD, PLANS, to_minor_units


def build() -> dict:
    return {
        "plans": {k: {cur: p.minor(cur) for cur in p.prices} for k, p in PLANS.items() if k != "free"},
        "enterprise_min_annual_usd_minor": to_minor_units("USD", ENTERPRISE_MIN_ANNUAL_USD),
    }


if __name__ == "__main__":
    print(json.dumps(build(), indent=2, sort_keys=True))
