from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")      # unknown fields are rejected, not ignored


class WrappedShareBody(_Strict):
    holder: Literal["owner", "guardian", "beneficiary"]
    holder_user_id: UUID | None = None
    blob_b64: str = Field(min_length=44, max_length=8192)


class ItemBody(_Strict):
    size_bytes: int = Field(gt=0, le=5 * 1024**3)
    sha256_hex: str = Field(pattern=r"^[0-9a-f]{64}$")
    server_share_b64: str = Field(min_length=44, max_length=64)
    wrapped_shares: list[WrappedShareBody] = Field(min_length=2, max_length=10)


class CreateVaultBody(_Strict):
    trigger: Literal["fixed_date", "progressive", "liveness"]
    trigger_config: dict[str, Any]
    envelope_version: int = 1
    items: list[ItemBody] = Field(min_length=1, max_length=10_000)


class UploadOut(BaseModel):
    url: str
    headers: dict[str, str]
    expires_in: int


class CreatedItemOut(BaseModel):
    item_id: UUID
    release_rank: int
    upload: UploadOut


class CreatedVaultOut(BaseModel):
    vault_id: UUID
    items: list[CreatedItemOut]


class ShareOut(BaseModel):
    item_id: UUID
    share_b64: str
