"""Hermes gateway adapter for the official Zalo Bot API.

Receive: long-poll ``getUpdates`` (default) or an aiohttp webhook server. Send: ``sendMessage`` /
``sendPhoto`` / ``sendChatAction``. Hermes core owns sessions, sender authorization
(ZALO_ALLOWED_USERS / ZALO_ALLOW_ALL_USERS / pairing), reply chunking, and cron delivery.
"""
from __future__ import annotations

import asyncio
import hmac
import logging
from typing import Any, Dict, Optional, Set
from urllib.parse import urlparse

from gateway.config import Platform, PlatformConfig
from gateway.platforms._shared import get_scoped_secret
from gateway.platforms.base import BasePlatformAdapter, SendResult, cache_audio_from_url, cache_image_from_url
from gateway.platforms.event import MessageEvent, MessageType

from .api import (
    EVENT_IMAGE, EVENT_STICKER, EVENT_TEXT, EVENT_VOICE, TEXT_LIMIT,
    ZaloApiError, ZaloBotApi, ZaloUpdate, chunk_text, parse_update, redact_token,
)
from .inbound import SeenIds, strip_mention

try:
    import httpx  # noqa: F401

    HTTPX_AVAILABLE = True
except ImportError:  # pragma: no cover - httpx is a Hermes core dependency
    HTTPX_AVAILABLE = False

logger = logging.getLogger(__name__)

PLATFORM_NAME = "zalo"
DEFAULT_POLL_TIMEOUT = 30
DEFAULT_WEBHOOK_HOST = "127.0.0.1"
DEFAULT_WEBHOOK_PORT = 8790
MAX_BACKOFF_SECONDS = 30.0
PHOTO_DOWNLOAD_TIMEOUT = 20.0  # seconds
VOICE_DOWNLOAD_TIMEOUT = 20.0  # seconds
DEFAULT_WEBHOOK_PATH = "/zalo/webhook"
SECRET_HEADER = "X-Bot-Api-Secret-Token"
WEBHOOK_MAX_BODY = 1024 * 1024  # bytes

# (extra key, env var, default). config.yaml ``platforms.zalo.extra.<key>`` wins over the env var.
SETTINGS = (
    ("token", "ZALO_BOT_TOKEN", ""),
    ("mode", "ZALO_MODE", "polling"),
    ("poll_timeout", "ZALO_POLL_TIMEOUT", str(DEFAULT_POLL_TIMEOUT)),
    ("webhook_url", "ZALO_WEBHOOK_URL", ""),
    ("webhook_secret", "ZALO_WEBHOOK_SECRET", ""),
    ("webhook_host", "ZALO_WEBHOOK_HOST", DEFAULT_WEBHOOK_HOST),
    ("webhook_port", "ZALO_WEBHOOK_PORT", str(DEFAULT_WEBHOOK_PORT)),
    ("allowed_groups", "ZALO_ALLOWED_GROUPS", ""),
)

PLATFORM_HINT = (
    "You are chatting via Zalo Bot. Each message is capped at 2000 characters and supports basic "
    "markdown only, so keep replies concise. Images can be sent only as public URLs."
)

PLACEHOLDER_STICKER = "[sticker]"
PLACEHOLDER_VOICE_FAILED = "[Zalo voice message could not be downloaded]"
PLACEHOLDER_UNSUPPORTED = "[Zalo could not deliver this message — please resend it as plain text]"
PLACEHOLDER_PHOTO_FAILED = "[Zalo photo could not be downloaded]"


def _setting(extra: Dict[str, Any], key: str, env: str, default: str = "") -> str:
    """``extra[key]`` wins over the env var ``env``; falls back to ``default``. Always a stripped str."""
    value = extra.get(key)
    if value is None or str(value).strip() == "":
        value = get_scoped_secret(env, None)
    if value is None or str(value).strip() == "":
        value = default
    return str(value).strip()


def _int(value: str, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _message_keys(raw: Any) -> list:
    """Sorted keys of the ``message`` dict inside a raw update (envelope or bare form), for diagnostics."""
    if not isinstance(raw, dict):
        return []
    body = raw.get("result") if isinstance(raw.get("result"), dict) else raw
    message = body.get("message") if isinstance(body.get("message"), dict) else {}
    return sorted(message.keys())


def _csv_set(value: str) -> Set[str]:
    return {item.strip() for item in (value or "").split(",") if item.strip()}


async def _send_text(api: ZaloBotApi, chat_id: str, text: str) -> str:
    """sendMessage in <=2000-char pieces with markdown; a 400 retries that piece as plain text.

    Returns the id of the last message sent.
    """
    message_id = ""
    for piece in chunk_text(text):
        try:
            message_id = await api.send_message(chat_id, piece, parse_mode="markdown")
        except ZaloApiError as exc:
            if exc.code != 400:
                raise
            message_id = await api.send_message(chat_id, piece)
    return message_id


class ZaloAdapter(BasePlatformAdapter):
    """Zalo Bot adapter: long-poll ``getUpdates`` or a webhook in; ``sendMessage`` out."""

    MAX_MESSAGE_LENGTH = TEXT_LIMIT  # Hermes core chunks replies at this length
    splits_long_messages = True  # send() chunks natively; cron delivery keeps the full payload

    def __init__(self, config: PlatformConfig):
        super().__init__(config=config, platform=Platform(PLATFORM_NAME))
        extra = config.extra or {}
        values = {key: _setting(extra, key, env, default) for key, env, default in SETTINGS}
        self._token = values["token"]
        self._mode = values["mode"].lower()
        self._poll_timeout = max(1, _int(values["poll_timeout"], DEFAULT_POLL_TIMEOUT))
        self._webhook_url = values["webhook_url"]
        self._webhook_secret = values["webhook_secret"]
        self._webhook_host = values["webhook_host"]
        self._webhook_port = _int(values["webhook_port"], DEFAULT_WEBHOOK_PORT)
        self._allowed_groups = _csv_set(values["allowed_groups"])
        self._api: Optional[ZaloBotApi] = None
        self._poll_task: Optional[asyncio.Task] = None
        self._web_runner: Any = None  # aiohttp.web.AppRunner while the webhook server runs
        self._tasks: Set[asyncio.Task] = set()  # in-flight webhook dispatches (keeps strong refs)
        self._seen = SeenIds()
        self._bot_id = ""
        self._bot_display_name = ""
        self._chat_types: Dict[str, str] = {}  # chat_id -> "dm" | "group", for get_chat_info

    # -- lifecycle ------------------------------------------------------------

    def _make_api(self) -> ZaloBotApi:
        return ZaloBotApi(self._token, timeout=self._poll_timeout + 10)

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        if not self._token:
            self._set_fatal_error("config_missing", "ZALO_BOT_TOKEN is not set", retryable=False)
            return False
        if self._mode not in ("polling", "webhook"):
            self._set_fatal_error(
                "config_invalid", f"ZALO_MODE must be 'polling' or 'webhook', not {self._mode!r}", retryable=False)
            return False
        self._api = self._make_api()
        try:
            me = await self._api.get_me()
        except ZaloApiError as exc:
            await self._close_api()
            if exc.code == 401:
                self._set_fatal_error("bad_token", "Zalo rejected the bot token (401)", retryable=False)
            else:
                self._set_fatal_error("connect_failed", str(exc), retryable=True)
            return False
        self._bot_id = str(me.get("id") or "")
        self._bot_display_name = str(me.get("display_name") or "")
        logger.info("[%s] connected as %r (id %s)", self.name, self._bot_display_name, self._bot_id or "?")
        if self._mode == "webhook":
            if not await self._start_webhook():  # Task 8
                await self._close_api()
                return False
        else:
            await self._clear_webhook_for_polling()
        self._mark_connected()
        if self._mode == "polling":
            self._poll_task = asyncio.create_task(self._poll_loop(), name="zalo-poll")
        return True

    async def _clear_webhook_for_polling(self) -> None:
        """getUpdates is blocked while a webhook exists, so polling mode removes any webhook."""
        try:
            info = await self._api.get_webhook_info()
            if info and info.get("url"):
                await self._api.delete_webhook()
                logger.warning("[%s] deleted webhook %s so getUpdates can deliver", self.name, info.get("url"))
        except ZaloApiError as exc:
            logger.warning("[%s] webhook check failed: %s", self.name, exc)

    async def _poll_loop(self) -> None:
        backoff = 1.0
        while self._running:
            try:
                update = await self._api.get_updates(self._poll_timeout)
            except asyncio.CancelledError:
                raise
            except ZaloApiError as exc:
                if exc.code == 401:
                    self._set_fatal_error("bad_token", "Zalo rejected the bot token (401)", retryable=False)
                    await self._notify_fatal_error()
                    return
                delay = MAX_BACKOFF_SECONDS if exc.code == 429 else backoff
                logger.warning("[%s] getUpdates failed: %s; retry in %.0fs", self.name, exc, delay)
                await asyncio.sleep(delay)
                backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)
                continue
            except Exception as exc:
                logger.warning("[%s] poll error: %s; retry in %.0fs", self.name, redact_token(str(exc), self._token), backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)
                continue
            backoff = 1.0
            if update is not None:
                try:
                    await self._handle_update(update)  # Task 6
                except Exception:
                    logger.exception("[%s] failed to handle message %s", self.name, update.message_id)

    async def disconnect(self) -> None:
        self._mark_disconnected()
        if self._poll_task is not None:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except (asyncio.CancelledError, Exception):
                pass
            self._poll_task = None
        await self._stop_webhook()
        await self._close_api()

    async def _stop_webhook(self) -> None:
        if self._web_runner is not None:
            try:
                await self._web_runner.cleanup()
            except Exception:
                logger.debug("[%s] webhook server cleanup failed", self.name, exc_info=True)
            self._web_runner = None

    # -- webhook mode -----------------------------------------------------------

    async def _start_webhook(self) -> bool:
        """Bind the aiohttp server and register the public URL with Zalo. Sets a fatal error on failure."""
        if not self._webhook_url or not self._webhook_secret:
            self._set_fatal_error(
                "webhook_config", "webhook mode needs ZALO_WEBHOOK_URL and ZALO_WEBHOOK_SECRET", retryable=False)
            return False
        if not self._webhook_url.lower().startswith("https://"):
            self._set_fatal_error("webhook_config", "ZALO_WEBHOOK_URL must be an https:// URL", retryable=False)
            return False
        if not (8 <= len(self._webhook_secret) <= 256):
            self._set_fatal_error("webhook_config", "ZALO_WEBHOOK_SECRET must be 8 to 256 characters", retryable=False)
            return False
        try:
            from aiohttp import web
        except ImportError:
            self._set_fatal_error("missing_dep", "webhook mode needs aiohttp: pip install aiohttp", retryable=False)
            return False
        runner = None
        try:
            app = web.Application(client_max_size=WEBHOOK_MAX_BODY)
            app.router.add_post(self._webhook_path(), self._handle_webhook)
            app.router.add_get("/health", self._handle_health)
            runner = web.AppRunner(app)
            await runner.setup()
            await web.TCPSite(runner, self._webhook_host, self._webhook_port).start()
        except OSError as exc:
            if runner is not None:
                await runner.cleanup()
            self._set_fatal_error(
                "bind_failed", f"cannot bind {self._webhook_host}:{self._webhook_port}: {exc}", retryable=True)
            return False
        except Exception as exc:
            if runner is not None:
                await runner.cleanup()
            self._set_fatal_error("webhook_start_failed", f"cannot start webhook server: {exc}", retryable=False)
            return False
        self._web_runner = runner
        try:
            await self._api.set_webhook(self._webhook_url, self._webhook_secret)
        except ZaloApiError as exc:
            await self._stop_webhook()
            self._set_fatal_error("webhook_register_failed", str(exc), retryable=(exc.code != 400))
            return False
        logger.info("[%s] webhook %s registered; listening on %s:%s%s", self.name, self._webhook_url,
                    self._webhook_host, self._webhook_port, self._webhook_path())
        return True

    def _webhook_path(self) -> str:
        return urlparse(self._webhook_url).path or DEFAULT_WEBHOOK_PATH

    async def _handle_health(self, request):
        from aiohttp import web

        return web.Response(text="ok")

    async def _handle_webhook(self, request):
        """Check the secret, parse the update, acknowledge at once, and dispatch in the background."""
        from aiohttp import web

        provided = request.headers.get(SECRET_HEADER, "")
        if not hmac.compare_digest(provided.encode(), self._webhook_secret.encode()):
            logger.warning("[%s] webhook call rejected: bad secret", self.name)
            return web.Response(status=403, text="forbidden")
        try:
            payload = await request.json()
        except Exception:
            return web.Response(status=400, text="invalid json")
        update = parse_update(payload)
        if update is None:
            logger.debug("[%s] webhook payload without a message ignored", self.name)
        else:
            task = asyncio.create_task(self._dispatch_webhook_update(update))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        return web.Response(status=200, text="ok")

    async def _dispatch_webhook_update(self, update: ZaloUpdate) -> None:
        try:
            await self._handle_update(update)
        except Exception:
            logger.exception("[%s] failed to handle webhook message %s", self.name, update.message_id)

    async def _close_api(self) -> None:
        if self._api is not None:
            try:
                await self._api.close()
            except Exception:
                pass
            self._api = None

    # -- inbound --------------------------------------------------------------

    async def _handle_update(self, update: ZaloUpdate) -> None:
        """Dedup, gate groups, normalize the event, and hand it to the Hermes runner."""
        if update.message_id and not self._seen.add(update.message_id):
            logger.debug("[%s] duplicate message %s dropped", self.name, update.message_id)
            return
        if update.is_group and self._allowed_groups and update.chat_id not in self._allowed_groups:
            logger.warning("[%s] dropped message from group %s: not in ZALO_ALLOWED_GROUPS", self.name, update.chat_id)
            return
        text, message_type, media_paths = await self._event_content(update)
        chat_type = "group" if update.is_group else "dm"
        self._chat_types[update.chat_id] = chat_type
        source = self.build_source(
            chat_id=update.chat_id,
            chat_name=update.chat_id if update.is_group else update.user_name,
            chat_type=chat_type,
            user_id=update.user_id,
            user_name=update.user_name,
            is_bot=update.is_bot,
            message_id=update.message_id or None,
        )
        event = MessageEvent(
            text=text,
            message_type=message_type,
            source=source,
            raw_message=update.raw,
            message_id=update.message_id or None,
            media_urls=media_paths,
            media_types=["audio/aac" if message_type is MessageType.VOICE else "image/jpeg"] * len(media_paths),
        )
        logger.info("[%s] %s from user %s in %s %s", self.name, update.event_name or "message",
                    update.user_id, chat_type, update.chat_id)
        await self.handle_message(event)

    async def _event_content(self, update: ZaloUpdate):
        """Return ``(text, MessageType, media_paths)`` for one update."""
        if update.event_name == EVENT_TEXT:
            text = strip_mention(update.text, self._bot_display_name)
            return text, (MessageType.COMMAND if text.startswith("/") else MessageType.TEXT), []
        if update.event_name == EVENT_IMAGE:
            caption = strip_mention(update.text, self._bot_display_name)
            if not update.photo_url:
                logger.warning("[%s] image event has no photo URL; message keys: %s", self.name,
                               _message_keys(update.raw))
                text = f"{caption}\n{PLACEHOLDER_PHOTO_FAILED}" if caption else PLACEHOLDER_PHOTO_FAILED
                return text, MessageType.TEXT, []
            try:
                path = await asyncio.wait_for(cache_image_from_url(update.photo_url or ""), timeout=PHOTO_DOWNLOAD_TIMEOUT)
            except Exception as exc:
                logger.warning("[%s] photo download failed: %s", self.name, exc)
                text = f"{caption}\n{PLACEHOLDER_PHOTO_FAILED}" if caption else PLACEHOLDER_PHOTO_FAILED
                return text, MessageType.TEXT, []
            return caption, MessageType.PHOTO, [path]
        if update.event_name == EVENT_STICKER:
            return PLACEHOLDER_STICKER, MessageType.STICKER, []
        if update.event_name == EVENT_VOICE:
            if not update.voice_url:
                logger.warning("[%s] voice event has no voice URL; message keys: %s", self.name,
                               _message_keys(update.raw))
                return PLACEHOLDER_VOICE_FAILED, MessageType.TEXT, []
            try:
                path = await asyncio.wait_for(cache_audio_from_url(update.voice_url, ext=".aac"),
                                              timeout=VOICE_DOWNLOAD_TIMEOUT)
            except Exception as exc:
                logger.warning("[%s] voice download failed: %s", self.name, exc)
                return PLACEHOLDER_VOICE_FAILED, MessageType.TEXT, []
            return "", MessageType.VOICE, [path]  # Hermes core transcribes audio/* media on VOICE events
        return PLACEHOLDER_UNSUPPORTED, MessageType.TEXT, []

    # -- outbound -------------------------------------------------------------

    async def send(self, chat_id: str, content: str, reply_to: Optional[str] = None,
                   metadata: Optional[Dict[str, Any]] = None) -> SendResult:
        """Send text. ``reply_to`` is ignored: the Zalo Bot API has no reply parameter."""
        if not (content or "").strip():
            return SendResult(success=True)
        if self._api is None:
            return SendResult(success=False, error="Zalo adapter is not connected")
        try:
            message_id = await _send_text(self._api, str(chat_id), content)
        except ZaloApiError as exc:
            logger.warning("[%s] sendMessage failed: %s", self.name, exc)
            retryable = exc.code in (0, 429) or exc.code >= 500
            return SendResult(success=False, error=str(exc), retryable=retryable)
        return SendResult(success=True, message_id=message_id or None)

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        if self._api is None:
            return
        try:
            await self._api.send_chat_action(str(chat_id), "typing")
        except Exception as exc:
            logger.debug("[%s] typing indicator failed: %s", self.name, exc)

    async def send_image(self, chat_id: str, image_url: str, caption: Optional[str] = None,
                         reply_to: Optional[str] = None, metadata: Optional[Dict[str, Any]] = None) -> SendResult:
        if not str(image_url).lower().startswith(("http://", "https://")):
            return SendResult(success=False, error="Zalo needs a public image URL; local files are not supported")
        if self._api is None:
            return SendResult(success=False, error="Zalo adapter is not connected")
        try:
            message_id = await self._api.send_photo(str(chat_id), image_url, caption)
        except ZaloApiError as exc:
            return SendResult(success=False, error=str(exc))
        return SendResult(success=True, message_id=message_id or None)

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        return {"name": str(chat_id), "type": self._chat_types.get(str(chat_id), "dm")}


# -- plugin registration --------------------------------------------------------


def check_requirements() -> bool:
    """Passive dependency probe (never installs anything). Polling needs only httpx."""
    return HTTPX_AVAILABLE


def validate_config(config) -> bool:
    return bool(_setting(getattr(config, "extra", None) or {}, "token", "ZALO_BOT_TOKEN"))


def is_connected(config) -> bool:
    return validate_config(config)


def _env_enablement() -> Optional[dict]:
    """Seed ``PlatformConfig.extra`` from env when ZALO_BOT_TOKEN is set.

    Runs during gateway config load, before adapter construction, so ``hermes status`` and cron
    see an env-only setup. Only the token and the home channel are seeded: core merges this dict
    OVER the YAML ``extra``, and every other setting is read by the adapter itself with YAML
    precedence (see ``_setting``). The special ``home_channel`` key becomes a ``HomeChannel``.
    """
    token = str(get_scoped_secret("ZALO_BOT_TOKEN", "") or "").strip()
    if not token:
        return None
    seed: dict = {"token": token}
    home = str(get_scoped_secret("ZALO_HOME_CHANNEL", "") or "").strip()
    if home:
        name = str(get_scoped_secret("ZALO_HOME_CHANNEL_NAME", "") or "").strip() or home
        seed["home_channel"] = {"chat_id": home, "name": name}
    return seed


async def _standalone_send(pconfig, chat_id: str, message: str, *, thread_id: Optional[str] = None,
                           media_files: Optional[list] = None, force_document: bool = False) -> dict:
    """Out-of-process delivery for cron and ``hermes send`` when no gateway adapter is live."""
    token = _setting(getattr(pconfig, "extra", None) or {}, "token", "ZALO_BOT_TOKEN")
    if not token:
        return {"error": "ZALO_BOT_TOKEN is not set"}
    if media_files:
        logger.info("[zalo] standalone send ignores %d media file(s): Zalo needs public URLs", len(media_files))
    api = ZaloBotApi(token, timeout=20.0)
    try:
        message_id = await _send_text(api, str(chat_id), message)
    except ZaloApiError as exc:
        return {"error": str(exc)}
    finally:
        await api.close()
    return {"success": True, "platform": PLATFORM_NAME, "chat_id": str(chat_id), "message_id": message_id}


def register(ctx) -> None:
    """Plugin entry point: register the Zalo platform with the Hermes gateway."""
    ctx.register_platform(
        name=PLATFORM_NAME,
        label="Zalo Bot",
        adapter_factory=lambda cfg: ZaloAdapter(cfg),
        check_fn=check_requirements,
        validate_config=validate_config,
        is_connected=is_connected,
        required_env=["ZALO_BOT_TOKEN"],
        install_hint="pip install httpx   # polling needs only httpx; webhook mode also needs aiohttp",
        env_enablement_fn=_env_enablement,
        cron_deliver_env_var="ZALO_HOME_CHANNEL",
        standalone_sender_fn=_standalone_send,
        allowed_users_env="ZALO_ALLOWED_USERS",
        allow_all_env="ZALO_ALLOW_ALL_USERS",
        max_message_length=TEXT_LIMIT,
        emoji="💬",
        pii_safe=False,
        allow_update_command=True,
        platform_hint=PLATFORM_HINT,
    )
