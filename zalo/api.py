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
        photo_url=_opt_str(message.get("photo_url") or message.get("photo")),  # live payloads use photo_url; docs say photo
        voice_url=_opt_str(message.get("voice_url")),
        sticker=_opt_str(message.get("sticker")),
        date_ms=int(date) if isinstance(date, (int, float)) and not isinstance(date, bool) else None,
        raw=payload,
    )


try:
    import httpx
except ImportError:  # pragma: no cover - httpx is a Hermes core dependency
    httpx = None  # type: ignore[assignment]


class ZaloApiError(Exception):
    """A Zalo API call failed. ``code`` is Zalo's ``error_code``, else the HTTP status, else 0."""

    def __init__(self, method: str, code: int, description: str):
        self.method = method
        self.code = code
        self.description = description
        super().__init__(f"Zalo {method} failed ({code}): {description}")


class ZaloBotApi:
    """Async client for ``https://bot-api.zaloplatforms.com/bot<TOKEN>/<method>``.

    ``client`` is injectable for tests: ``httpx.AsyncClient(transport=httpx.MockTransport(...))``.
    httpx is required (not aiohttp): Zalo's edge sends duplicate ``Server`` headers, which
    aiohttp's strict client parser rejects.
    """

    def __init__(self, token: str, *, client: Optional["httpx.AsyncClient"] = None, timeout: float = 35.0):
        if httpx is None:
            raise RuntimeError("httpx is required for the Zalo adapter")
        self._token = token
        self._timeout = timeout
        self._client = client or httpx.AsyncClient(timeout=timeout)

    async def close(self) -> None:
        await self._client.aclose()

    def url(self, method: str) -> str:
        return f"{API_BASE}/bot{self._token}/{method}"

    def _redact(self, text: str) -> str:
        return redact_token(text, self._token)

    async def call(self, method: str, params: Optional[dict] = None, *, read_timeout: Optional[float] = None) -> Any:
        """POST ``params`` as JSON and return the ``result`` field. Raises ``ZaloApiError`` on failure."""
        try:
            response = await self._client.post(self.url(method), json=params or {}, timeout=read_timeout or self._timeout)
        except httpx.TimeoutException as exc:
            raise ZaloApiError(method, 408, self._redact("request timed out")) from exc
        except httpx.HTTPError as exc:
            raise ZaloApiError(method, 0, self._redact(str(exc))) from exc
        try:
            payload = response.json()
        except ValueError:
            raise ZaloApiError(method, response.status_code, self._redact(response.text)[:200]) from None
        if not isinstance(payload, dict) or not payload.get("ok"):
            code = payload.get("error_code") if isinstance(payload, dict) else None
            description = payload.get("description") if isinstance(payload, dict) else None
            raise ZaloApiError(method, int(code or response.status_code or 0), self._redact(str(description or payload))[:200])
        return payload.get("result")

    async def get_me(self) -> dict:
        result = await self.call("getMe")
        return result if isinstance(result, dict) else {}

    async def get_updates(self, timeout: int = 30) -> Optional[ZaloUpdate]:
        """Long-poll once. Returns the single update, or ``None`` when there is none (empty result or 408)."""
        try:
            result = await self.call("getUpdates", {"timeout": timeout}, read_timeout=timeout + 10)
        except ZaloApiError as exc:
            if exc.code == 408:
                return None
            raise
        return parse_update(result) if isinstance(result, dict) else None

    async def send_message(self, chat_id: str, text: str, parse_mode: Optional[str] = None) -> str:
        params: dict = {"chat_id": str(chat_id), "text": text}
        if parse_mode:
            params["parse_mode"] = parse_mode
        result = await self.call("sendMessage", params)
        return str(result.get("message_id") or "") if isinstance(result, dict) else ""

    async def send_photo(self, chat_id: str, photo_url: str, caption: Optional[str] = None) -> str:
        params: dict = {"chat_id": str(chat_id), "photo": photo_url}
        if caption:
            params["caption"] = caption[:TEXT_LIMIT]
        result = await self.call("sendPhoto", params)
        return str(result.get("message_id") or "") if isinstance(result, dict) else ""

    async def send_chat_action(self, chat_id: str, action: str = "typing") -> None:
        await self.call("sendChatAction", {"chat_id": str(chat_id), "action": action})

    async def get_webhook_info(self) -> Optional[dict]:
        """The current webhook, or ``None`` when Zalo answers 404 (no webhook set)."""
        try:
            result = await self.call("getWebhookInfo")
        except ZaloApiError as exc:
            if exc.code == 404:
                return None
            raise
        return result if isinstance(result, dict) else None

    async def set_webhook(self, url: str, secret_token: str) -> None:
        await self.call("setWebhook", {"url": url, "secret_token": secret_token})

    async def delete_webhook(self) -> None:
        await self.call("deleteWebhook")
