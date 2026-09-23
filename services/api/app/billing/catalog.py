"""Plan catalog and multi-currency price book.

Prices are PROPOSED LAUNCH VALUES for the currencies where we set explicit local prices
(purchasing-power adjusted, charm-priced). For every other market, Stripe Adaptive Pricing
converts from USD at checkout. Stripe skips conversion only for currencies present in a price's
`currency_options`, so the two mechanisms coexist. Validate all numbers with real price tests.

Amounts are Decimals in MAJOR units and converted to minor units per currency (JPY has none).
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from enum import Enum
from typing import Mapping

from app.core.errors import ValidationFailed

GB = 1024**3
ZERO_DECIMAL = frozenset({"JPY", "KRW", "VND", "CLP", "PYG", "UGX", "XAF", "XOF"})
BASE_CURRENCY = "USD"


class Tier(str, Enum):
    FREE = "free"
    PREMIUM = "premium"
    LIFETIME = "lifetime"
    ENTERPRISE = "enterprise"


class Interval(str, Enum):
    MONTH = "month"
    YEAR = "year"
    ONCE = "once"           # lifetime, one-time payment


def to_minor_units(currency: str, amount: Decimal) -> int:
    cur = currency.upper()
    if amount < 0:
        raise ValidationFailed("negative price")
    if cur in ZERO_DECIMAL:
        return int(amount.quantize(Decimal(1), rounding=ROUND_HALF_UP))
    return int((amount * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))


@dataclass(frozen=True, slots=True)
class Plan:
    key: str                          # stable id, also stored in Stripe metadata
    tier: Tier
    interval: Interval
    name: str
    prices: Mapping[str, Decimal]     # currency -> major-unit amount; must include USD
    storage_bytes: int

    def __post_init__(self) -> None:
        if BASE_CURRENCY not in self.prices:
            raise ValueError(f"plan {self.key} needs a {BASE_CURRENCY} base price")

    def minor(self, currency: str) -> int:
        return to_minor_units(currency, self.prices[currency.upper()])


D = Decimal
PLANS: dict[str, Plan] = {p.key: p for p in [
    Plan("free", Tier.FREE, Interval.ONCE, "Free", {"USD": D("0")}, 1 * GB),
    Plan("premium_monthly", Tier.PREMIUM, Interval.MONTH, "Premium (monthly)", {
        "USD": D("4.99"), "EUR": D("4.99"), "GBP": D("3.99"), "CAD": D("6.49"), "AUD": D("7.49"),
        "INR": D("199"), "BRL": D("14.90"), "MXN": D("79"), "PHP": D("149"), "JPY": D("690"),
    }, 50 * GB),
    # Proposed: annual at ~2 months free. Not in the original spec; remove if unwanted.
    Plan("premium_yearly", Tier.PREMIUM, Interval.YEAR, "Premium (yearly)", {
        "USD": D("49.99"), "EUR": D("49.99"), "GBP": D("39.99"), "CAD": D("64.99"), "AUD": D("74.99"),
        "INR": D("1999"), "BRL": D("149"), "MXN": D("790"), "PHP": D("1490"), "JPY": D("6900"),
    }, 50 * GB),
    Plan("lifetime", Tier.LIFETIME, Interval.ONCE, "Lifetime (50 GB)", {
        "USD": D("149"), "EUR": D("149"), "GBP": D("119"), "CAD": D("199"), "AUD": D("229"),
        "INR": D("5999"), "BRL": D("449"), "MXN": D("2499"), "PHP": D("4499"), "JPY": D("21900"),
    }, 50 * GB),
]}

ENTERPRISE_MIN_ANNUAL_USD = Decimal("5000")

# Stripe Checkout locale codes we are confident about; everything else falls back to "auto".
_STRIPE_LOCALE = {"en": "en", "es": "es", "fr": "fr", "de": "de", "it": "it", "pt-BR": "pt-BR",
                  "tr": "tr", "ru": "ru", "ja": "ja", "ko": "ko", "id": "id", "vi": "vi",
                  "zh-CN": "zh", "tl": "fil"}


def stripe_locale(app_locale: str) -> str:
    return _STRIPE_LOCALE.get(app_locale, "auto")


def get_plan(key: str) -> Plan:
    try:
        return PLANS[key]
    except KeyError as exc:
        raise ValidationFailed(f"unknown plan {key!r}") from exc
