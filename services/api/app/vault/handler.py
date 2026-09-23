"""Module A — Cryptographic Vault Handler (server side).

WHAT THE SERVER NEVER SEES: plaintext, the data-encryption key (DEK), passphrases, or any
client-held key share. Encryption happens in the browser (Web Crypto AES-256-GCM); the
DEK is split with Shamir 2-of-3:

    share 1  -> server (sealed under KMS, released ONLY after the Chronos trigger matures)
    share 2  -> owner  (wrapped client-side under the owner's key; server stores opaque bytes)
    share 3  -> guardian/beneficiary recovery kit (wrapped client-side; opaque)

Any ONE share is information-theoretically useless. The server alone cannot decrypt;
the owner alone cannot open the vault before maturity without a second party; after
maturity, server share + one holder share reconstruct the DEK in the client.

What the server DOES store (per spec): ciphertext (S3), size, unlock date/trigger, owner id.

HONEST LIMIT: the time-lock is enforced by server policy (share withholding), not by
mathematics. Someone who could subvert BOTH the Chronos service and the KMS sealing could
release early. For a cryptographic time-lock, layer drand/tlock on top (see docs).
"""
from __future__ import annotations

import base64
import binascii
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Mapping, Sequence
from uuid import UUID, uuid4

from app.chronos.time_source import TimeSource
from app.chronos.triggers import (BPS_FULL, Liveness, TriggerType, build_trigger,
                                  releasable_item_count)
from app.core.errors import (Conflict, IntegrityError, NotAuthorized, NotFound, QuotaExceeded,
                             ValidationFailed, VaultStillSealed)
from app.vault.ports import (KmsSealer, NewItem, ObjectStore, PresignedUpload, VaultRecord,
                             VaultRepository, WrappedShare)

log = logging.getLogger("vaultla.vault")

SUPPORTED_ENVELOPE_VERSIONS = frozenset({1})   # v1 = AES-256-GCM chunked, Shamir(GF256) 2-of-3
SHARE_LEN = 33                                  # 1 x-coordinate byte + 32 y bytes
MIN_WRAPPED, MAX_WRAPPED = 32, 4096
MAX_ITEMS_PER_VAULT = 10_000
MAX_ITEM_BYTES = 5 * 1024**3
HOLDERS = frozenset({"owner", "guardian", "beneficiary"})
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


# ------------------------------------------------------------------ request DTOs
@dataclass(frozen=True, slots=True)
class WrappedShareIn:
    holder: str
    holder_user_id: UUID | None
    blob_b64: str


@dataclass(frozen=True, slots=True)
class ItemIn:
    size_bytes: int
    sha256_hex: str                 # SHA-256 of the CIPHERTEXT object
    server_share_b64: str           # plaintext share, TLS-protected; sealed immediately below
    wrapped_shares: Sequence[WrappedShareIn]


@dataclass(frozen=True, slots=True)
class CreateVaultIn:
    tenant_id: UUID
    owner_id: UUID
    trigger: str
    trigger_config: Mapping[str, Any]
    envelope_version: int
    items: Sequence[ItemIn]


@dataclass(frozen=True, slots=True)
class CreatedItem:
    item_id: UUID
    release_rank: int
    upload: PresignedUpload


@dataclass(frozen=True, slots=True)
class CreatedVault:
    vault_id: UUID
    items: Sequence[CreatedItem]


@dataclass(frozen=True, slots=True)
class ReleasedShare:
    item_id: UUID
    share_b64: str


# ------------------------------------------------------------------ helpers
def _b64(value: str, field: str) -> bytes:
    try:
        return base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValidationFailed(f"{field} is not valid base64") from exc


def _enc_ctx(tenant_id: UUID, vault_id: UUID, item_id: UUID) -> dict[str, str]:
    return {"tenant_id": str(tenant_id), "vault_id": str(vault_id), "item_id": str(item_id)}


def storage_key(tenant_id: UUID, vault_id: UUID, item_id: UUID) -> str:
    return f"tenants/{tenant_id}/vaults/{vault_id}/{item_id}.enc"


# ------------------------------------------------------------------ handler
class VaultHandler:
    def __init__(self, repo: VaultRepository, store: ObjectStore, kms: KmsSealer,
                 clock: TimeSource, *, presign_ttl: int = 900) -> None:
        self._repo, self._store, self._kms, self._clock = repo, store, kms, clock
        self._ttl = presign_ttl

    # ---- create -------------------------------------------------------
    def create_vault(self, req: CreateVaultIn) -> CreatedVault:
        """Validate, seal server shares, persist a DRAFT, and hand back presigned uploads."""
        if req.envelope_version not in SUPPORTED_ENVELOPE_VERSIONS:
            raise ValidationFailed(f"unsupported envelope_version {req.envelope_version}")
        if not (1 <= len(req.items) <= MAX_ITEMS_PER_VAULT):
            raise ValidationFailed(f"a vault holds 1..{MAX_ITEMS_PER_VAULT} items")

        now = self._clock.now()                      # trusted time, never client time
        trig_cfg = dict(req.trigger_config)
        if req.trigger == TriggerType.LIVENESS.value:
            trig_cfg["created_at"] = now.isoformat()  # server-stamped anchor
        trigger = build_trigger(req.trigger, trig_cfg)   # raises ValidationFailed if malformed
        self._validate_schedule(req.trigger, trigger, now)

        total = sum(i.size_bytes for i in req.items)
        quota, used = self._repo.quota_and_usage(req.tenant_id)
        if used + total > quota:
            raise QuotaExceeded("Storage quota exceeded for this account.")

        vault_id = uuid4()
        new_items: list[NewItem] = []
        for rank, item in enumerate(req.items):
            new_items.append(self._build_item(req, vault_id, rank, item))

        unlock_at = getattr(trigger, "unlock_at", None) or getattr(trigger, "start_at", None)
        self._repo.insert_draft(
            vault_id=vault_id, tenant_id=req.tenant_id, owner_id=req.owner_id, trigger=req.trigger,
            trigger_config=trig_cfg, unlock_at=unlock_at, total_size_bytes=total,
            envelope_version=req.envelope_version, items=new_items, trusted_now=now)
        self._repo.audit(req.tenant_id, vault_id, f"user:{req.owner_id}", "vault.created", now,
                         {"items": len(new_items), "bytes": total, "trigger": req.trigger})

        uploads = [
            CreatedItem(n.item_id, n.release_rank, self._store.presign_put(
                n.storage_key, size_bytes=n.size_bytes,
                sha256_b64=base64.b64encode(n.sha256).decode(), ttl=self._ttl))
            for n in new_items
        ]
        return CreatedVault(vault_id, uploads)

    def _validate_schedule(self, kind: str, trigger: Any, now: datetime) -> None:
        if kind == TriggerType.FIXED_DATE.value and trigger.unlock_at <= now + timedelta(minutes=1):
            raise ValidationFailed("unlock_at must be in the future")
        if kind == TriggerType.PROGRESSIVE.value and trigger.start_at < now - timedelta(minutes=1):
            raise ValidationFailed("start_at must not be in the past")

    def _build_item(self, req: CreateVaultIn, vault_id: UUID, rank: int, item: ItemIn) -> NewItem:
        if not (0 < item.size_bytes <= MAX_ITEM_BYTES):
            raise ValidationFailed(f"item {rank}: size_bytes must be 1..{MAX_ITEM_BYTES}")
        if not _HEX64.match(item.sha256_hex):
            raise ValidationFailed(f"item {rank}: sha256_hex must be 64 lowercase hex chars")
        share = _b64(item.server_share_b64, f"item {rank}: server_share")
        if len(share) != SHARE_LEN:
            raise ValidationFailed(f"item {rank}: server share must be {SHARE_LEN} bytes")
        wrapped = self._validate_wrapped(rank, item.wrapped_shares)

        item_id = uuid4()
        sealed = self._kms.seal(share, _enc_ctx(req.tenant_id, vault_id, item_id))
        return NewItem(item_id=item_id, release_rank=rank, size_bytes=item.size_bytes,
                       storage_key=storage_key(req.tenant_id, vault_id, item_id),
                       sha256=bytes.fromhex(item.sha256_hex), server_share_sealed=sealed,
                       wrapped_shares=wrapped)

    @staticmethod
    def _validate_wrapped(rank: int, shares: Sequence[WrappedShareIn]) -> list[WrappedShare]:
        if not (2 <= len(shares) <= 10):
            raise ValidationFailed(f"item {rank}: provide 2..10 wrapped client shares")
        out: list[WrappedShare] = []
        for s in shares:
            if s.holder not in HOLDERS:
                raise ValidationFailed(f"item {rank}: unknown holder {s.holder!r}")
            blob = _b64(s.blob_b64, f"item {rank}: wrapped share")
            if not (MIN_WRAPPED <= len(blob) <= MAX_WRAPPED):
                raise ValidationFailed(f"item {rank}: wrapped share size out of range")
            out.append(WrappedShare(s.holder, s.holder_user_id, blob))
        return out

    # ---- finalize (seal) ------------------------------------------------
    def finalize_vault(self, tenant_id: UUID, vault_id: UUID, actor_id: UUID) -> VaultRecord:
        """Verify every ciphertext object landed intact, then move DRAFT -> SEALED."""
        vault = self._must_get(tenant_id, vault_id)
        if vault.owner_id != actor_id:
            raise NotAuthorized("Only the vault owner can seal it.")
        if vault.state != "draft":
            raise Conflict(f"Vault is {vault.state}; only drafts can be sealed.")

        for item in self._repo.get_items(tenant_id, vault_id):
            head = self._store.head(item.storage_key)
            if head is None:
                raise IntegrityError(f"Item {item.release_rank} has not been uploaded.")
            if head.size_bytes != item.size_bytes:
                raise IntegrityError(f"Item {item.release_rank} size mismatch.")
            expected = base64.b64encode(item.sha256).decode()
            if head.checksum_sha256_b64 != expected:
                raise IntegrityError(f"Item {item.release_rank} checksum mismatch.")
            self._repo.confirm_item(tenant_id, item.id)

        now = self._clock.now()
        trigger = build_trigger(vault.trigger, vault.trigger_config)
        ev = trigger.evaluate(now, last_checkin_at=None, attestations=0)
        if ev.matured:
            raise ValidationFailed("Trigger is already satisfied; refusing to seal an open vault.")
        self._repo.seal(tenant_id, vault_id, next_eval_at=ev.next_eval_at,
                        releasable_bps=ev.releasable_bps, matured=False, now=now)
        self._repo.audit(tenant_id, vault_id, f"user:{actor_id}", "vault.sealed", now,
                         {"next_eval_at": ev.next_eval_at.isoformat() if ev.next_eval_at else None})
        return self._must_get(tenant_id, vault_id)

    # ---- liveness check-in ---------------------------------------------
    def record_checkin(self, tenant_id: UUID, vault_id: UUID, actor_id: UUID,
                       *, source: str = "user", attested_by: str | None = None) -> datetime:
        vault = self._must_get(tenant_id, vault_id)
        if vault.trigger != TriggerType.LIVENESS.value:
            raise Conflict("Check-ins apply to liveness vaults only.")
        if vault.state != "sealed":
            raise Conflict(f"Vault is {vault.state}; check-ins are closed.")
        if source == "user" and vault.owner_id != actor_id:
            raise NotAuthorized("Only the owner can check in.")
        now = self._clock.now()
        trig = build_trigger(vault.trigger, vault.trigger_config)
        assert isinstance(trig, Liveness)
        # Only an OWNER check-in resets the clock; attestations are recorded but don't extend it.
        anchor = now if source == "user" else vault.last_checkin_at
        deadline = trig.deadline(anchor)
        self._repo.record_checkin(tenant_id, vault_id, source=source, attested_by=attested_by,
                                  trusted_time=now, next_eval_at=deadline if source == "user" else now)
        self._repo.audit(tenant_id, vault_id, f"{source}:{attested_by or actor_id}",
                         "vault.checkin", now, {"source": source})
        return deadline

    # ---- key release -----------------------------------------------------
    def release_server_share(self, tenant_id: UUID, vault_id: UUID, item_id: UUID,
                             requester_id: UUID) -> ReleasedShare:
        """Return the server-held share ONLY if the trigger allows and the caller is entitled.
        Secure default: any doubt -> refuse."""
        vault = self._must_get(tenant_id, vault_id)
        if vault.state in ("draft", "revoked"):
            raise VaultStillSealed("This vault is not available.")
        if not (requester_id == vault.owner_id or
                self._repo.is_authorized_recipient(tenant_id, vault_id, requester_id)):
            raise NotAuthorized("You are not a recipient of this vault.")

        items = self._repo.get_items(tenant_id, vault_id)
        item = next((i for i in items if i.id == item_id), None)
        if item is None:
            raise NotFound("Item not found.")

        if vault.state == "sealed":
            # Only progressive vaults release partially while still 'sealed'.
            if vault.trigger != TriggerType.PROGRESSIVE.value:
                raise VaultStillSealed("This vault has not matured yet.")
            if item.release_rank >= releasable_item_count(len(items), vault.releasable_bps):
                raise VaultStillSealed("This portion has not been released yet.")
        elif vault.state not in ("matured", "released"):
            raise VaultStillSealed("This vault is not available.")

        share = self._kms.unseal(item.server_share_sealed,
                                 _enc_ctx(tenant_id, vault_id, item.id))
        now = self._clock.now()
        self._repo.mark_item_released(tenant_id, item.id, now)
        self._repo.audit(tenant_id, vault_id, f"user:{requester_id}", "share.released", now,
                         {"item_id": str(item.id)})
        return ReleasedShare(item.id, base64.b64encode(share).decode())

    def download_url(self, tenant_id: UUID, vault_id: UUID, item_id: UUID, requester_id: UUID) -> str:
        """Presigned GET for ciphertext. Requires the same entitlement as key release."""
        self.release_server_share(tenant_id, vault_id, item_id, requester_id)  # authz + timing gate
        item = next(i for i in self._repo.get_items(tenant_id, vault_id) if i.id == item_id)
        return self._store.presign_get(item.storage_key, ttl=self._ttl)

    # ---- internal --------------------------------------------------------
    def _must_get(self, tenant_id: UUID, vault_id: UUID) -> VaultRecord:
        v = self._repo.get_vault(tenant_id, vault_id)
        if v is None:                 # same error for "absent" and "other tenant" (no oracle)
            raise NotFound("Vault not found.")
        return v
