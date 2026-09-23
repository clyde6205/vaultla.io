"""AWS adapters: S3 (ciphertext store) and KMS (server-share sealing).
boto3 clients use the task/Lambda IAM role — never static keys. Not exercised by unit tests."""
from __future__ import annotations

import base64
from typing import Mapping

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from app.core.errors import ExternalServiceError
from app.vault.ports import ObjectHead, PresignedUpload

_CFG = Config(signature_version="s3v4", retries={"max_attempts": 5, "mode": "adaptive"})


class S3ObjectStore:
    def __init__(self, bucket: str, region: str, kms_key_id: str) -> None:
        self._bucket, self._kms_key = bucket, kms_key_id
        self._s3 = boto3.client("s3", region_name=region, config=_CFG)

    def presign_put(self, key: str, *, size_bytes: int, sha256_b64: str, ttl: int) -> PresignedUpload:
        # Signing ContentLength + ChecksumSHA256 means S3 rejects any upload that differs from
        # what was declared. SSE-KMS is defence-in-depth; content is ALREADY client-encrypted.
        params = {"Bucket": self._bucket, "Key": key, "ContentLength": size_bytes,
                  "ChecksumSHA256": sha256_b64, "ServerSideEncryption": "aws:kms",
                  "SSEKMSKeyId": self._kms_key}
        url = self._s3.generate_presigned_url("put_object", Params=params, ExpiresIn=ttl)
        headers = {"x-amz-checksum-sha256": sha256_b64,
                   "x-amz-server-side-encryption": "aws:kms",
                   "x-amz-server-side-encryption-aws-kms-key-id": self._kms_key,
                   "content-length": str(size_bytes)}
        return PresignedUpload(url, headers, ttl)

    def head(self, key: str) -> ObjectHead | None:
        try:
            r = self._s3.head_object(Bucket=self._bucket, Key=key, ChecksumMode="ENABLED")
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
                return None
            raise ExternalServiceError("Object store unavailable.") from exc
        return ObjectHead(int(r["ContentLength"]), r.get("ChecksumSHA256"))

    def presign_get(self, key: str, *, ttl: int) -> str:
        return self._s3.generate_presigned_url(
            "get_object", Params={"Bucket": self._bucket, "Key": key}, ExpiresIn=ttl)

    def request_restore_prefix(self, prefix: str, days: int = 7) -> None:
        """Deep Archive objects must be restored (12-48h, Bulk tier) before download."""
        pager = self._s3.get_paginator("list_objects_v2")
        for page in pager.paginate(Bucket=self._bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                if obj.get("StorageClass") != "DEEP_ARCHIVE":
                    continue
                try:
                    self._s3.restore_object(Bucket=self._bucket, Key=obj["Key"], RestoreRequest={
                        "Days": days, "GlacierJobParameters": {"Tier": "Bulk"}})
                except ClientError as exc:
                    if exc.response.get("Error", {}).get("Code") != "RestoreAlreadyInProgress":
                        raise


class KmsShareSealer:
    def __init__(self, key_id: str, region: str) -> None:
        self._key, self._kms = key_id, boto3.client("kms", region_name=region, config=_CFG)

    def seal(self, plaintext: bytes, context: Mapping[str, str]) -> bytes:
        try:
            return self._kms.encrypt(KeyId=self._key, Plaintext=plaintext,
                                     EncryptionContext=dict(context))["CiphertextBlob"]
        except ClientError as exc:
            raise ExternalServiceError("Key service unavailable.") from exc

    def unseal(self, sealed: bytes, context: Mapping[str, str]) -> bytes:
        try:
            return self._kms.decrypt(CiphertextBlob=sealed, KeyId=self._key,
                                     EncryptionContext=dict(context))["Plaintext"]
        except ClientError as exc:
            raise ExternalServiceError("Key service unavailable.") from exc
