"""Referral engine: storage-for-invites, the proven consumer growth loop.

Design goals: reward REAL activation, resist farming.
  * Reward fires only when the referee (a) verified their email AND (b) SEALED a first vault.
    Signing up alone earns nothing, so fake-account farms gain nothing.
  * Both sides win: referrer and referee each get +1 GB (referee's arrives at signup as a
    welcome bonus; the referrer's on qualification).
  * Hard cap on lifetime bonus (see entitlements.REFERRAL_BONUS_CAP_BYTES); never unlimited.
  * No self-referral (same tenant, same verified email, or same device fingerprint hash).
  * Velocity limit hook: too many referrals from one referrer in 24 h is held for review.
  * Idempotent: a referee can qualify exactly once.
"""
from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Protocol
from uuid import UUID

from app.billing.catalog import GB
from app.billing.entitlements import REFERRAL_BONUS_CAP_BYTES
from app.core.errors import Conflict, ValidationFailed

_ALPHABET = "ABCDEFGHJKMNPQRSTVWXYZ23456789"     # no 0/O/1/I/L
CODE_LEN = 8
REWARD_BYTES = 1 * GB
WELCOME_BYTES = 1 * GB
VELOCITY_LIMIT_PER_DAY = 25


def new_code() -> str:
    return "".join(secrets.choice(_ALPHABET) for _ in range(CODE_LEN))


def normalise_code(raw: str) -> str:
    code = raw.strip().upper().replace("-", "").replace(" ", "")
    if len(code) != CODE_LEN or any(c not in _ALPHABET for c in code):
        raise ValidationFailed("invalid referral code")
    return code


class Status(str, Enum):
    PENDING = "pending"          # signed up, not yet activated
    QUALIFIED = "qualified"      # rewarded
    HELD = "held"                # velocity/abuse review
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class Referral:
    referrer_tenant: UUID
    referee_tenant: UUID
    status: Status
    referrer_email_hash: str
    referee_email_hash: str
    referrer_device_hash: str | None = None
    referee_device_hash: str | None = None


class ReferralStore(Protocol):
    def tenant_for_code(self, code: str) -> tuple[UUID, str, str | None] | None:
        """-> (tenant_id, verified_email_hash, device_hash) of the code's owner."""
    def get_for_referee(self, referee_tenant: UUID) -> Referral | None: ...
    def create(self, r: Referral) -> None: ...
    def set_status(self, referee_tenant: UUID, status: Status, now: datetime) -> None: ...
    def bonus_bytes(self, tenant: UUID) -> int: ...
    def add_bonus(self, tenant: UUID, delta: int) -> None: ...
    def count_since(self, referrer_tenant: UUID, since: datetime) -> int: ...


def register_referral(store: ReferralStore, *, code: str, referee_tenant: UUID,
                      referee_email_hash: str, referee_device_hash: str | None,
                      now: datetime) -> Referral:
    """Called at signup. Grants the welcome bonus; the referrer is rewarded later."""
    owner = store.tenant_for_code(normalise_code(code))
    if owner is None:
        raise ValidationFailed("unknown referral code")
    referrer, r_email, r_device = owner
    if store.get_for_referee(referee_tenant):
        raise Conflict("this account already used a referral code")
    if referrer == referee_tenant or r_email == referee_email_hash or (
            r_device and referee_device_hash and r_device == referee_device_hash):
        raise ValidationFailed("self-referral is not allowed")
    status = Status.PENDING
    if store.count_since(referrer, now - timedelta(days=1)) >= VELOCITY_LIMIT_PER_DAY:
        status = Status.HELD
    ref = Referral(referrer, referee_tenant, status, r_email, referee_email_hash, r_device, referee_device_hash)
    store.create(ref)
    if status is Status.PENDING:
        store.add_bonus(referee_tenant, min(WELCOME_BYTES, _room(store, referee_tenant)))
    return ref


def _room(store: ReferralStore, tenant: UUID) -> int:
    return max(REFERRAL_BONUS_CAP_BYTES - store.bonus_bytes(tenant), 0)


def qualify_referral(store: ReferralStore, *, referee_tenant: UUID, email_verified: bool,
                     sealed_first_vault: bool, now: datetime) -> bool:
    """Called when the referee seals a vault. Returns True iff the referrer was just rewarded."""
    ref = store.get_for_referee(referee_tenant)
    if ref is None or ref.status is not Status.PENDING:
        return False                                  # none, held, rejected, or already qualified
    if not (email_verified and sealed_first_vault):
        return False
    grant = min(REWARD_BYTES, _room(store, ref.referrer_tenant))
    if grant > 0:
        store.add_bonus(ref.referrer_tenant, grant)
    store.set_status(referee_tenant, Status.QUALIFIED, now)
    return True
