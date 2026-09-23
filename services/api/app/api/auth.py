"""JWT verification. tenant_id and user_id come ONLY from the verified token, never from
request bodies, query strings, or headers a client can set."""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from uuid import UUID

import jwt
from fastapi import Header
from jwt import PyJWKClient

from app.core.config import Settings
from app.core.errors import NotAuthorized


@dataclass(frozen=True, slots=True)
class Principal:
    user_id: UUID
    tenant_id: UUID
    role: str


@lru_cache
def _settings() -> Settings:
    return Settings.from_env()


@lru_cache
def _jwks() -> PyJWKClient:
    return PyJWKClient(_settings().jwks_url, cache_keys=True, lifespan=3600)


def current_principal(authorization: str = Header(default="")) -> Principal:
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise NotAuthorized("Missing bearer token.")
    cfg = _settings()
    try:
        key = _jwks().get_signing_key_from_jwt(token).key
        claims = jwt.decode(token, key, algorithms=["RS256", "ES256"], audience=cfg.jwt_audience,
                            issuer=cfg.jwt_issuer, options={"require": ["exp", "iat", "sub"]})
        return Principal(UUID(claims["custom:user_id"]), UUID(claims["custom:tenant_id"]),
                         str(claims.get("custom:role", "member")))
    except (jwt.PyJWTError, KeyError, ValueError) as exc:
        raise NotAuthorized("Invalid or expired token.") from exc
