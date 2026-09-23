"""Event pages (weddings, reunions, corporate events): QR entryway for guest uploads.

Access model: a 256-bit random token is embedded in the QR URL's fragment (#t=...), which
browsers do not send in requests or Referer headers; the page's JavaScript reads it and presents
it in a header. Only its SHA-256 is stored, so a database leak cannot forge entryways. Guests need
no account.

OPEN DESIGN ITEM: for zero-knowledge guest uploads the browser must encrypt to the vault's PUBLIC
key (hybrid encryption) so guests never hold a decryption key. That client flow is not built yet.

Abuse controls live here as pure, testable rules: time window, per-guest and per-event byte
caps, per-token rate limit, and a media-type allow-list.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import datetime
from typing import Sequence

from app.core.errors import NotAuthorized, QuotaExceeded, ValidationFailed

ALLOWED_TYPES = frozenset({"image/jpeg", "image/png", "image/webp", "image/heic", "video/mp4",
                           "video/quicktime", "audio/mpeg", "audio/mp4", "audio/webm", "text/plain"})
MAX_FILE_BYTES = 500 * 1024 * 1024
RATE_LIMIT_PER_MINUTE = 30


def new_token() -> tuple[str, bytes]:
    """-> (token for the QR URL, sha256 digest to store)."""
    token = secrets.token_urlsafe(32)
    return token, hashlib.sha256(token.encode()).digest()


def verify_token(presented: str, stored_hash: bytes) -> bool:
    return hmac.compare_digest(hashlib.sha256(presented.encode()).digest(), stored_hash)


@dataclass(frozen=True, slots=True)
class EventPage:
    slug: str
    token_hash: bytes
    opens_at: datetime
    closes_at: datetime
    allow_anonymous: bool
    max_guest_bytes: int          # per guest
    max_event_bytes: int          # whole event
    used_event_bytes: int = 0


def join_url(base: str, slug: str, token: str, locale: str | None = None) -> str:
    if not base.startswith("https://"):
        raise ValidationFailed("base URL must be https")
    prefix = f"/{locale}" if locale else ""
    return f"{base.rstrip('/')}{prefix}/e/{slug}#t={token}"


def authorize_upload(page: EventPage, *, presented_token: str, now: datetime, is_authenticated: bool,
                     content_type: str, size_bytes: int, guest_used_bytes: int,
                     requests_last_minute: int) -> None:
    """Raise if the upload must be refused. Order: cheapest, least-informative checks first."""
    if not verify_token(presented_token, page.token_hash):
        raise NotAuthorized("This event link is not valid.")
    if not (page.opens_at <= now < page.closes_at):
        raise NotAuthorized("This event is not accepting uploads right now.")
    if not page.allow_anonymous and not is_authenticated:
        raise NotAuthorized("Sign in to add to this event.")
    if requests_last_minute >= RATE_LIMIT_PER_MINUTE:
        raise QuotaExceeded("Too many uploads; please slow down.")
    if content_type not in ALLOWED_TYPES:
        raise ValidationFailed("This file type is not allowed.")
    if not (0 < size_bytes <= MAX_FILE_BYTES):
        raise ValidationFailed("File is empty or too large.")
    if guest_used_bytes + size_bytes > page.max_guest_bytes:
        raise QuotaExceeded("You have reached your upload limit for this event.")
    if page.used_event_bytes + size_bytes > page.max_event_bytes:
        raise QuotaExceeded("This event has reached its storage limit.")
