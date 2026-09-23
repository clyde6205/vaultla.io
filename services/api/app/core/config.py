"""Environment-driven settings. No secrets live in code; production values come from
AWS Secrets Manager / SSM injected as environment variables by the task or Lambda role."""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _req(name: str) -> str:
    v = os.environ.get(name)
    if not v:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return v


@dataclass(frozen=True, slots=True)
class Settings:
    env: str
    database_url: str
    chronos_database_url: str
    s3_bucket: str
    kms_key_id: str
    aws_region: str
    jwks_url: str
    jwt_issuer: str
    jwt_audience: str
    ntp_servers: tuple[str, ...] = field(default=(
        "time.aws.com", "time.cloudflare.com", "time.google.com", "pool.ntp.org",
    ))
    ntp_min_sources: int = 3
    ntp_max_spread_seconds: float = 2.0
    presign_ttl_seconds: int = 900
    max_item_bytes: int = 5 * 1024**3          # single-PUT ceiling (5 GiB); larger files are chunked into items

    @staticmethod
    def from_env() -> "Settings":
        return Settings(
            env=os.environ.get("VAULTLA_ENV", "production"),
            database_url=_req("DATABASE_URL"),
            chronos_database_url=_req("CHRONOS_DATABASE_URL"),
            s3_bucket=_req("VAULT_BUCKET"),
            kms_key_id=_req("KMS_KEY_ID"),
            aws_region=_req("AWS_REGION"),
            jwks_url=_req("JWKS_URL"),
            jwt_issuer=_req("JWT_ISSUER"),
            jwt_audience=_req("JWT_AUDIENCE"),
        )
