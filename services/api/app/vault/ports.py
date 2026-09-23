"""Ports (interfaces) the vault handler depends on. AWS/Postgres adapters implement these;
tests use in-memory fakes. Keeping the crypto-sensitive logic behind ports means it can be
unit-tested and audited without cloud dependencies."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping, Protocol, Sequence
from uuid import UUID


@dataclass(frozen=True, slots=True)
class ObjectHead:
    size_bytes: int
    checksum_sha256_b64: str | None


@dataclass(frozen=True, slots=True)
class PresignedUpload:
    url: str
    headers: Mapping[str, str]       # client MUST send these exactly (incl. checksum + length)
    expires_in: int


class ObjectStore(Protocol):
    def presign_put(self, key: str, *, size_bytes: int, sha256_b64: str, ttl: int) -> PresignedUpload: ...
    def head(self, key: str) -> ObjectHead | None: ...
    def presign_get(self, key: str, *, ttl: int) -> str: ...
    def request_restore_prefix(self, prefix: str, days: int = 7) -> None: ...


class KmsSealer(Protocol):
    """Envelope-encrypts the server-held key share. The encryption context binds the
    ciphertext to (tenant, vault, item): a sealed share copied to another row won't open."""
    def seal(self, plaintext: bytes, context: Mapping[str, str]) -> bytes: ...
    def unseal(self, sealed: bytes, context: Mapping[str, str]) -> bytes: ...


@dataclass(frozen=True, slots=True)
class WrappedShare:
    holder: str                      # 'owner' | 'guardian' | 'beneficiary'
    holder_user_id: UUID | None
    blob: bytes                      # opaque to the server


@dataclass(frozen=True, slots=True)
class NewItem:
    item_id: UUID
    release_rank: int
    size_bytes: int
    storage_key: str
    sha256: bytes
    server_share_sealed: bytes
    wrapped_shares: Sequence[WrappedShare]


@dataclass(frozen=True, slots=True)
class VaultRecord:
    id: UUID
    tenant_id: UUID
    owner_id: UUID
    state: str
    trigger: str
    trigger_config: Mapping[str, Any]
    releasable_bps: int
    last_checkin_at: datetime | None
    storage_class: str


@dataclass(frozen=True, slots=True)
class ItemRecord:
    id: UUID
    vault_id: UUID
    release_rank: int
    size_bytes: int
    storage_key: str
    sha256: bytes
    server_share_sealed: bytes
    upload_confirmed: bool


class VaultRepository(Protocol):
    """Every method is implicitly scoped to `tenant_id` (and, in Postgres, by RLS)."""
    def quota_and_usage(self, tenant_id: UUID) -> tuple[int, int]: ...
    def insert_draft(self, *, vault_id: UUID, tenant_id: UUID, owner_id: UUID, trigger: str,
                     trigger_config: Mapping[str, Any], unlock_at: datetime | None,
                     total_size_bytes: int, envelope_version: int, items: Sequence[NewItem],
                     trusted_now: datetime) -> None: ...
    def get_vault(self, tenant_id: UUID, vault_id: UUID) -> VaultRecord | None: ...
    def get_items(self, tenant_id: UUID, vault_id: UUID) -> Sequence[ItemRecord]: ...
    def confirm_item(self, tenant_id: UUID, item_id: UUID) -> None: ...
    def seal(self, tenant_id: UUID, vault_id: UUID, *, next_eval_at: datetime | None,
             releasable_bps: int, matured: bool, now: datetime) -> None: ...
    def is_authorized_recipient(self, tenant_id: UUID, vault_id: UUID, user_id: UUID) -> bool: ...
    def mark_item_released(self, tenant_id: UUID, item_id: UUID, now: datetime) -> None: ...
    def record_checkin(self, tenant_id: UUID, vault_id: UUID, *, source: str,
                       attested_by: str | None, trusted_time: datetime,
                       next_eval_at: datetime) -> None: ...
    def audit(self, tenant_id: UUID, vault_id: UUID | None, actor: str, event_type: str,
              trusted_time: datetime, payload: Mapping[str, Any]) -> None: ...
