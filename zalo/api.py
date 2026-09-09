"""Official Zalo Bot API: thin async client and update parser.

No Hermes imports live here, so this module is testable and reusable on its own.
API reference: https://bot.zapps.me/docs/
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, List, Optional

logger = logging.getLogger(__name__)

API_BASE = "https://bot-api.zaloplatforms.com"
TEXT_LIMIT = 2000  # sendMessage text: 1..2000 characters

EVENT_TEXT = "message.text.received"
EVENT_IMAGE = "message.image.received"
EVENT_STICKER = "message.sticker.received"
EVENT_VOICE = "message.voice.received"
EVENT_UNSUPPORTED = "message.unsupported.received"


def redact_token(text: str, token: str) -> str:
    """Replace every occurrence of ``token`` in ``text`` with ``<TOKEN>``."""
    if not token:
        return text
    return text.replace(token, "<TOKEN>")


def chunk_text(text: str, limit: int = TEXT_LIMIT) -> List[str]:
    """Split ``text`` into pieces of at most ``limit`` characters.

    Cuts at the last newline in range, else the last space, else hard at ``limit``.
    """
    text = text or ""
    pieces: List[str] = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit + 1)
        if cut < limit // 2:
            cut = text.rfind(" ", 0, limit + 1)
        if cut < limit // 2:
            cut = limit
        pieces.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    if text:
        pieces.append(text)
    return pieces


@dataclass
class ZaloUpdate:
    """One inbound event, normalized from the webhook / getUpdates payload."""

    event_name: str
    message_id: str
    chat_id: str
    chat_type: str  # "PRIVATE" or "GROUP"
    user_id: str
    user_name: str
    is_bot: bool
    text: str  # message.text, else message.caption, else ""
    photo_url: Optional[str] = None
    voice_url: Optional[str] = None
    sticker: Optional[str] = None
    date_ms: Optional[int] = None
    raw: Any = field(default=None, repr=False)

    @property
    def is_group(self) -> bool:
        return self.chat_type == "GROUP"


def _opt_str(value: Any) -> Optional[str]:
    text = str(value).strip() if value is not None else ""
    return text or None


def parse_update(payload: Any) -> Optional[ZaloUpdate]:
    """Parse ``{ok, result: {event_name, message}}`` or a bare ``{event_name, message}``.

    Returns ``None`` when there is no ``message`` dict or no ``chat.id``.
    """
    if not isinstance(payload, dict):
        return None
    body = payload["result"] if isinstance(payload.get("result"), dict) else payload
    message = body.get("message")
    if not isinstance(message, dict):
        return None
    chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}
    sender = message.get("from") if isinstance(message.get("from"), dict) else {}
    chat_id = _opt_str(chat.get("id"))
    if not chat_id:
        return None
    user_id = _opt_str(sender.get("id")) or ""
    date = message.get("date")
    return ZaloUpdate(
        event_name=str(body.get("event_name") or ""),
        message_id=_opt_str(message.get("message_id")) or "",
        chat_id=chat_id,
        chat_type=str(chat.get("chat_type") or "PRIVATE").upper(),
        user_id=user_id,
        user_name=_opt_str(sender.get("display_name")) or user_id,
        is_bot=bool(sender.get("is_bot")),
        text=str(message.get("text") or message.get("caption") or ""),
        photo_url=_opt_str(message.get("photo")),
        voice_url=_opt_str(message.get("voice_url")),
        sticker=_opt_str(message.get("sticker")),
        date_ms=int(date) if isinstance(date, (int, float)) and not isinstance(date, bool) else None,
        raw=payload,
    )
