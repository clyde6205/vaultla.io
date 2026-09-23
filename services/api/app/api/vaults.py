from __future__ import annotations

from typing import Callable, TypeVar
from uuid import UUID

from fastapi import APIRouter, Depends

from app.adapters.aws import KmsShareSealer, S3ObjectStore
from app.adapters.postgres_vault import tenant_session
from app.api.auth import Principal, current_principal
from app.api.schemas import CreatedItemOut, CreatedVaultOut, CreateVaultBody, ShareOut, UploadOut
from app.chronos.time_source import QuorumTimeSource
from app.core.config import Settings
from app.vault.handler import CreateVaultIn, ItemIn, VaultHandler, WrappedShareIn

T = TypeVar("T")
router = APIRouter(prefix="/v1/vaults", tags=["vaults"])
_cfg = Settings.from_env()
_clock = QuorumTimeSource(_cfg.ntp_servers, min_sources=_cfg.ntp_min_sources,
                          max_spread_seconds=_cfg.ntp_max_spread_seconds)
_store = S3ObjectStore(_cfg.s3_bucket, _cfg.aws_region, _cfg.kms_key_id)
_kms = KmsShareSealer(_cfg.kms_key_id, _cfg.aws_region)


def _run(p: Principal, fn: Callable[[VaultHandler], T]) -> T:
    """One tenant-scoped transaction per request. The commit happens when the `with` block
    exits, i.e. BEFORE the response is returned, so a client never sees success for work
    that was rolled back."""
    with tenant_session(_cfg.database_url, p.tenant_id) as repo:
        return fn(VaultHandler(repo, _store, _kms, _clock, presign_ttl=_cfg.presign_ttl_seconds))


@router.post("", response_model=CreatedVaultOut, status_code=201)
def create_vault(body: CreateVaultBody, p: Principal = Depends(current_principal)) -> CreatedVaultOut:
    cmd = CreateVaultIn(
        p.tenant_id, p.user_id, body.trigger, body.trigger_config, body.envelope_version,
        [ItemIn(i.size_bytes, i.sha256_hex, i.server_share_b64,
                [WrappedShareIn(w.holder, w.holder_user_id, w.blob_b64) for w in i.wrapped_shares])
         for i in body.items])
    out = _run(p, lambda h: h.create_vault(cmd))
    return CreatedVaultOut(vault_id=out.vault_id, items=[
        CreatedItemOut(item_id=i.item_id, release_rank=i.release_rank,
                       upload=UploadOut(url=i.upload.url, headers=dict(i.upload.headers),
                                        expires_in=i.upload.expires_in)) for i in out.items])


@router.post("/{vault_id}/seal")
def seal_vault(vault_id: UUID, p: Principal = Depends(current_principal)) -> dict[str, str]:
    return {"state": _run(p, lambda h: h.finalize_vault(p.tenant_id, vault_id, p.user_id)).state}


@router.post("/{vault_id}/checkin")
def checkin(vault_id: UUID, p: Principal = Depends(current_principal)) -> dict[str, str]:
    deadline = _run(p, lambda h: h.record_checkin(p.tenant_id, vault_id, p.user_id))
    return {"next_deadline": deadline.isoformat()}


@router.post("/{vault_id}/items/{item_id}/key", response_model=ShareOut)
def release_key(vault_id: UUID, item_id: UUID, p: Principal = Depends(current_principal)) -> ShareOut:
    r = _run(p, lambda h: h.release_server_share(p.tenant_id, vault_id, item_id, p.user_id))
    return ShareOut(item_id=r.item_id, share_b64=r.share_b64)


@router.get("/{vault_id}/items/{item_id}/download")
def download(vault_id: UUID, item_id: UUID, p: Principal = Depends(current_principal)) -> dict[str, str]:
    return {"url": _run(p, lambda h: h.download_url(p.tenant_id, vault_id, item_id, p.user_id))}
