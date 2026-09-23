"""What a tenant may do. Pure function of (subscription state, referral bonus).

TRUST PRINCIPLE (product promise, enforced here): non-payment NEVER deletes or locks sealed
vaults. A lapsed account drops to free-tier *creation* limits and read-only over-quota state.
Existing capsules keep maturing and remain openable. This is the promise a 100-year product
must make, and a key reason to buy.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from app.billing.catalog import GB, PLANS, Tier

PAST_DUE_GRACE = timedelta(days=14)
REFERRAL_BONUS_CAP_BYTES = 20 * GB


@dataclass(frozen=True, slots=True)
class Entitlements:
    tier: Tier
    storage_quota_bytes: int
    can_create: bool                 # False => over quota or lapsed: read-only for NEW uploads
    triggers: frozenset[str]
    max_guardians: int
    max_active_event_pages: int
    white_label: bool
    sso: bool
    audit_export: bool
    data_residency: bool
    max_sub_capsules: int
    priority_support: bool


_FREE = dict(triggers=frozenset({"fixed_date"}), max_guardians=1, max_active_event_pages=1,
             white_label=False, sso=False, audit_export=False, data_residency=False,
             max_sub_capsules=1, priority_support=False)
_PAID = dict(triggers=frozenset({"fixed_date", "progressive", "liveness"}), max_guardians=5,
             max_active_event_pages=10, white_label=False, sso=False, audit_export=False,
             data_residency=False, max_sub_capsules=1, priority_support=True)
_ENT = dict(triggers=frozenset({"fixed_date", "progressive", "liveness"}), max_guardians=20,
            max_active_event_pages=1000, white_label=True, sso=True, audit_export=True,
            data_residency=True, max_sub_capsules=50_000, priority_support=True)


def compute_entitlements(*, tier: Tier, status: str, storage_quota_bytes: int | None,
                         used_bytes: int, is_lifetime: bool = False,
                         past_due_since: datetime | None = None, now: datetime,
                         referral_bonus_bytes: int = 0) -> Entitlements:
    lapsed = False
    eff = tier
    if is_lifetime:
        eff = Tier.LIFETIME
    elif status in ("canceled", "unpaid", "incomplete_expired"):
        eff, lapsed = Tier.FREE, True
    elif status == "past_due" and past_due_since and now - past_due_since > PAST_DUE_GRACE:
        eff, lapsed = Tier.FREE, True

    if eff is Tier.FREE:
        base, feat = PLANS["free"].storage_bytes, _FREE
    elif eff is Tier.ENTERPRISE:
        base, feat = storage_quota_bytes or 0, _ENT
    else:
        base, feat = PLANS["premium_monthly"].storage_bytes, _PAID

    bonus = min(max(referral_bonus_bytes, 0), REFERRAL_BONUS_CAP_BYTES)
    quota = base + bonus
    return Entitlements(tier=eff, storage_quota_bytes=quota,
                        can_create=(used_bytes < quota) and not (lapsed and used_bytes >= quota),
                        **feat)
