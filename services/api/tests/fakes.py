"""In-memory fakes for the ports. Used by unit tests only."""
from __future__ import annotations

import base64
import hashlib
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator, Mapping, Sequence
from uuid import UUID

from app.chronos.scanner import DueVault
from app.vault.ports import (ItemRecord, NewItem, ObjectHead, PresignedUpload, VaultRecord)

UTC = timezone.utc


class FixedClock:
    def __init__(self, t: datetime) -> None:
        self.t = t

    def now(self) -> datetime:
        return self.t

    def advance(self, **kw: float) -> None:
        self.t += timedelta(**kw)


class FakeStore:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def put(self, key: str, data: bytes) -> None:
        self.objects[key] = data

    def presign_put(self, key: str, *, size_bytes: int, sha256_b64: str, ttl: int) -> PresignedUpload:
        return PresignedUpload(f"https://s3.test/{key}", {"x-amz-checksum-sha256": sha256_b64}, ttl)

    def head(self, key: str) -> ObjectHead | None:
        d = self.objects.get(key)
        if d is None:
            return None
        return ObjectHead(len(d), base64.b64encode(hashlib.sha256(d).digest()).decode())

    def presign_get(self, key: str, *, ttl: int) -> str:
        return f"https://s3.test/get/{key}"

    def request_restore_prefix(self, prefix: str, days: int = 7) -> None:
        pass


class FakeKms:
    """Reversible toy sealing bound to the context (stand-in for AWS KMS)."""
    def seal(self, plaintext: bytes, context: Mapping[str, str]) -> bytes:
        return repr(sorted(context.items())).encode() + b"|" + plaintext[::-1]

    def unseal(self, sealed: bytes, context: Mapping[str, str]) -> bytes:
        ctx, _, body = sealed.partition(b"|")
        if ctx != repr(sorted(context.items())).encode():
            raise ValueError("encryption context mismatch")
        return body[::-1]


class FakeVaultRepo:
    def __init__(self, quota: int = 10**12, used: int = 0) -> None:
        self.quota, self.used = quota, used
        self.vaults: dict[UUID, dict[str, Any]] = {}
        self.items: dict[UUID, list[NewItem]] = {}
        self.confirmed: set[UUID] = set()
        self.recipients: set[tuple[UUID, UUID]] = set()
        self.audit_log: list[tuple[str, dict]] = []
        self.checkins: list[dict] = []

    def quota_and_usage(self, tenant_id): return self.quota, self.used

    def insert_draft(self, *, vault_id, tenant_id, owner_id, trigger, trigger_config, unlock_at,
                     total_size_bytes, envelope_version, items: Sequence[NewItem], trusted_now):
        self.vaults[vault_id] = dict(tenant_id=tenant_id, owner_id=owner_id, state="draft",
                                     trigger=trigger, cfg=dict(trigger_config), bps=0, last=None)
        self.items[vault_id] = list(items)

    def get_vault(self, tenant_id, vault_id):
        v = self.vaults.get(vault_id)
        if not v or v["tenant_id"] != tenant_id:
            return None
        return VaultRecord(vault_id, tenant_id, v["owner_id"], v["state"], v["trigger"], v["cfg"],
                           v["bps"], v["last"], "STANDARD")

    def get_items(self, tenant_id, vault_id):
        return [ItemRecord(i.item_id, vault_id, i.release_rank, i.size_bytes, i.storage_key, i.sha256,
                           i.server_share_sealed, i.item_id in self.confirmed)
                for i in self.items.get(vault_id, [])]

    def confirm_item(self, tenant_id, item_id): self.confirmed.add(item_id)

    def seal(self, tenant_id, vault_id, *, next_eval_at, releasable_bps, matured, now):
        v = self.vaults[vault_id]
        v.update(state="matured" if matured else "sealed", bps=releasable_bps, next=next_eval_at)

    def is_authorized_recipient(self, tenant_id, vault_id, user_id):
        return (vault_id, user_id) in self.recipients

    def mark_item_released(self, tenant_id, item_id, now): pass

    def record_checkin(self, tenant_id, vault_id, *, source, attested_by, trusted_time, next_eval_at):
        self.checkins.append(dict(source=source, at=trusted_time, next=next_eval_at))
        if source == "user":
            self.vaults[vault_id]["last"] = trusted_time

    def audit(self, tenant_id, vault_id, actor, event_type, trusted_time, payload):
        self.audit_log.append((event_type, dict(payload)))


class FakeChronosRepo:
    """Minimal ChronosRepository: holds DueVault rows and applies results."""
    def __init__(self, due: Sequence[DueVault], last_time: datetime | None = None) -> None:
        self.rows = {v.id: v for v in due}
        self.state: dict[UUID, dict] = {v.id: dict(state="sealed", next=None, bps=v.releasable_bps) for v in due}
        self.last_time = last_time
        self.audits: list[tuple[str, dict]] = []

    @contextmanager
    def transaction(self) -> Iterator["FakeChronosRepo"]:
        yield self

    def last_trusted_time(self): return self.last_time
    def record_trusted_time(self, t): self.last_time = t

    def fetch_due(self, now, limit):
        return [v for v in self.rows.values() if self.state[v.id]["state"] == "sealed"
                and (self.state[v.id]["next"] is None or self.state[v.id]["next"] <= now)][:limit]

    def apply(self, vault_id, *, matured, releasable_bps, next_eval_at, now):
        self.state[vault_id].update(state="matured" if matured else "sealed",
                                    next=next_eval_at, bps=releasable_bps)

    def audit(self, tenant_id, vault_id, event_type, trusted_time, payload):
        self.audits.append((event_type, dict(payload)))
