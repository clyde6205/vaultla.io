"""Public 'sealed capsule' teaser: the shareable, viral surface.

A capsule owner can share a link that shows a live countdown and an OPTIONAL public message.
Zero-knowledge is preserved: the teaser is built from an explicit allow-list of fields. It can
never include file names, sizes, owner email, recipient lists, or any content. The optional
message is plaintext ONLY because the owner chose to make it public.
"""
from __future__ import annotations

import html
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping

from app.core.errors import ValidationFailed

MAX_MESSAGE = 140
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


@dataclass(frozen=True, slots=True)
class TeaserView:
    state: str                 # 'sealed' | 'open'
    opens_at: datetime | None
    seconds_remaining: int | None
    public_message: str | None
    og_title: str
    og_description: str
    noindex: bool = True       # teasers are shareable but never search-indexed


def sanitize_public_message(raw: str | None) -> str | None:
    if raw is None or not raw.strip():
        return None
    cleaned = _CTRL.sub("", raw).strip()
    if len(cleaned) > MAX_MESSAGE:
        raise ValidationFailed(f"public message is limited to {MAX_MESSAGE} characters")
    return cleaned                      # escaping happens at render time (html.escape below)


def build_teaser(vault: Mapping[str, Any], now: datetime) -> TeaserView:
    """`vault` is a DB row; ONLY the keys below are ever read."""
    if not vault.get("teaser_enabled"):
        raise ValidationFailed("teaser is not enabled for this capsule")
    if vault.get("state") == "revoked":
        raise ValidationFailed("capsule unavailable")
    opens: datetime | None = vault.get("unlock_at")
    msg = sanitize_public_message(vault.get("teaser_message"))
    if opens and opens > now:
        remaining = int((opens - now).total_seconds())
        return TeaserView("sealed", opens, remaining, msg,
                          og_title="A sealed time capsule",
                          og_description=html.escape(msg) if msg else "Something is waiting to be opened.")
    return TeaserView("open", opens, 0 if opens else None, msg,
                      og_title="A time capsule is ready to open",
                      og_description="The wait is over.")
