"""Adapter tests. They need a Hermes checkout: set HERMES_AGENT_SRC (see tests/conftest.py)."""
import asyncio
from unittest.mock import AsyncMock

import pytest

pytest.importorskip("gateway.platforms.base", reason="set HERMES_AGENT_SRC to a hermes-agent checkout")

from gateway.config import PlatformConfig  # noqa: E402
from gateway.platform_registry import PlatformEntry, platform_registry  # noqa: E402
from gateway.platforms.event import MessageType  # noqa: E402

from zalo import adapter as zadapter  # noqa: E402
from zalo.api import (  # noqa: E402
    EVENT_IMAGE, EVENT_STICKER, EVENT_TEXT, EVENT_UNSUPPORTED, EVENT_VOICE, ZaloApiError, ZaloUpdate,
)

TOKEN = "123:secret"


class RegisteringCtx:
    """Minimal PluginContext: turns register_platform kwargs into a real PlatformEntry."""

    def __init__(self):
        self.kwargs = None

    def register_platform(self, **kwargs):
        self.kwargs = kwargs
        platform_registry.register(PlatformEntry(**kwargs))


@pytest.fixture(scope="module", autouse=True)
def registered_platform():
    """Platform("zalo") only resolves once the registry knows the name."""
    ctx = RegisteringCtx()
    zadapter.register(ctx)
    yield ctx
    platform_registry.unregister("zalo")


class FakeApi:
    """Scripted stand-in for ZaloBotApi."""

    def __init__(self, *, me=None, webhook_info=None, updates=(), send_results=None):
        self.me = {"id": "42", "display_name": "Bot Mockup"} if me is None else me
        self.webhook_info = webhook_info
        self.updates = list(updates)  # per get_updates call: ZaloUpdate | Exception | None
        self.send_results = list(send_results or [])  # per send_message call: str | Exception
        self.calls = []
        self.closed = False
        self.on_exhausted = lambda: None

    async def get_me(self):
        self.calls.append(("get_me",))
        if isinstance(self.me, Exception):
            raise self.me
        return self.me

    async def get_webhook_info(self):
        self.calls.append(("get_webhook_info",))
        return self.webhook_info

    async def delete_webhook(self):
        self.calls.append(("delete_webhook",))

    async def set_webhook(self, url, secret_token):
        self.calls.append(("set_webhook", url, secret_token))

    async def get_updates(self, timeout=30):
        self.calls.append(("get_updates", timeout))
        if not self.updates:
            self.on_exhausted()
            return None
        item = self.updates.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    async def send_message(self, chat_id, text, parse_mode=None):
        self.calls.append(("send_message", chat_id, text, parse_mode))
        item = self.send_results.pop(0) if self.send_results else "sent-1"
        if isinstance(item, Exception):
            raise item
        return item

    async def send_photo(self, chat_id, photo_url, caption=None):
        self.calls.append(("send_photo", chat_id, photo_url, caption))
        return "photo-1"

    async def send_chat_action(self, chat_id, action="typing"):
        self.calls.append(("send_chat_action", chat_id, action))

    async def close(self):
        self.closed = True


def _run(coro):
    return asyncio.run(coro)


def make_adapter(extra=None, api=None):
    """Adapter with a scripted API, a captured handle_message, and a token in ``extra``."""
    config = PlatformConfig(enabled=True, extra={"token": TOKEN, **(extra or {})})
    adapter = zadapter.ZaloAdapter(config)
    api = api or FakeApi()
    adapter._make_api = lambda: api
    adapter.handle_message = AsyncMock()
    return adapter, api


def _update(**overrides):
    fields = dict(event_name=EVENT_TEXT, message_id="m1", chat_id="u1", chat_type="PRIVATE", user_id="u1",
                  user_name="Alice", is_bot=False, text="hello", raw={"k": "v"})
    fields.update(overrides)
    return ZaloUpdate(**fields)


def _stop_when_exhausted(adapter, api):
    api.on_exhausted = lambda: setattr(adapter, "_running", False)


# -- registration -----------------------------------------------------------

def test_register_builds_expected_entry(registered_platform):
    kwargs = registered_platform.kwargs
    entry = platform_registry.get("zalo")
    assert entry is not None and entry.name == "zalo" and entry.label == "Zalo Bot"
    assert kwargs["allowed_users_env"] == "ZALO_ALLOWED_USERS"
    assert kwargs["allow_all_env"] == "ZALO_ALLOW_ALL_USERS"
    assert kwargs["max_message_length"] == 2000
    assert kwargs["required_env"] == ["ZALO_BOT_TOKEN"]
    assert kwargs["check_fn"] is zadapter.check_requirements
    assert kwargs["validate_config"] is zadapter.validate_config
    assert kwargs["is_connected"] is zadapter.is_connected
    assert "2000" in kwargs["platform_hint"]
    assert kwargs["install_hint"].startswith("pip install httpx")
    built = entry.adapter_factory(PlatformConfig(enabled=True, extra={"token": TOKEN}))
    assert isinstance(built, zadapter.ZaloAdapter)


def test_check_and_validate_config(monkeypatch):
    monkeypatch.delenv("ZALO_BOT_TOKEN", raising=False)
    assert zadapter.check_requirements() is True
    assert zadapter.validate_config(PlatformConfig(enabled=True, extra={"token": TOKEN})) is True
    assert zadapter.validate_config(PlatformConfig(enabled=True, extra={})) is False
    monkeypatch.setenv("ZALO_BOT_TOKEN", TOKEN)
    assert zadapter.is_connected(PlatformConfig(enabled=True, extra={})) is True


# -- settings ---------------------------------------------------------------

def test_settings_extra_wins_over_env(monkeypatch):
    monkeypatch.setenv("ZALO_MODE", "webhook")
    monkeypatch.setenv("ZALO_POLL_TIMEOUT", "5")
    adapter, _ = make_adapter({"mode": "polling", "webhook_port": 9999})
    assert adapter._mode == "polling"
    assert adapter._poll_timeout == 5  # env used because extra has no poll_timeout
    assert adapter._webhook_port == 9999


def test_settings_defaults(monkeypatch):
    for _key, env, _default in zadapter.SETTINGS:
        monkeypatch.delenv(env, raising=False)
    adapter, _ = make_adapter()
    assert adapter._mode == "polling" and adapter._poll_timeout == 30
    assert (adapter._webhook_host, adapter._webhook_port) == ("127.0.0.1", 8790)
    assert adapter._allowed_groups == set()
    assert adapter.MAX_MESSAGE_LENGTH == 2000
    assert adapter.splits_long_messages is True


def test_allowed_groups_csv():
    adapter, _ = make_adapter({"allowed_groups": " g1, g2 ,,"})
    assert adapter._allowed_groups == {"g1", "g2"}


def test_poll_timeout_is_clamped_to_one_second():
    assert make_adapter({"poll_timeout": 0})[0]._poll_timeout == 1
    assert make_adapter({"poll_timeout": -5})[0]._poll_timeout == 1


# -- connect / disconnect (polling) -----------------------------------------

def test_connect_polling_clears_stale_webhook_and_starts_loop():
    api = FakeApi(webhook_info={"url": "https://old.example/hook"})
    adapter, _ = make_adapter(api=api)
    _stop_when_exhausted(adapter, api)

    async def scenario():
        assert await adapter.connect() is True
        assert adapter._bot_display_name == "Bot Mockup" and adapter._bot_id == "42"
        await asyncio.sleep(0)  # let the poll task run once
        await adapter.disconnect()

    _run(scenario())
    names = [call[0] for call in api.calls]
    assert names[:3] == ["get_me", "get_webhook_info", "delete_webhook"]
    assert "get_updates" in names
    assert api.closed and adapter._poll_task is None and adapter._api is None


def test_connect_polling_without_webhook_skips_delete():
    api = FakeApi(webhook_info=None)
    adapter, _ = make_adapter(api=api)
    _stop_when_exhausted(adapter, api)

    async def scenario():
        assert await adapter.connect() is True
        await asyncio.sleep(0)
        await adapter.disconnect()

    _run(scenario())
    assert ("delete_webhook",) not in api.calls


def test_connect_requires_token(monkeypatch):
    monkeypatch.delenv("ZALO_BOT_TOKEN", raising=False)
    adapter, api = make_adapter({"token": ""})
    assert _run(adapter.connect()) is False
    assert adapter._fatal_error_code == "config_missing"
    assert api.calls == []


def test_connect_rejects_bad_mode():
    adapter, _ = make_adapter({"mode": "carrier-pigeon"})
    assert _run(adapter.connect()) is False
    assert adapter._fatal_error_code == "config_invalid"


def test_connect_bad_token_is_fatal_and_not_retryable():
    api = FakeApi(me=ZaloApiError("getMe", 401, "Unauthorized"))
    adapter, _ = make_adapter(api=api)
    assert _run(adapter.connect()) is False
    assert adapter._fatal_error_code == "bad_token"
    assert adapter._fatal_error_retryable is False
    assert api.closed and adapter._api is None


def test_connect_network_error_is_retryable():
    api = FakeApi(me=ZaloApiError("getMe", 0, "connection refused"))
    adapter, _ = make_adapter(api=api)
    assert _run(adapter.connect()) is False
    assert adapter._fatal_error_code == "connect_failed"
    assert adapter._fatal_error_retryable is True


# -- poll loop --------------------------------------------------------------

def test_poll_loop_dispatches_updates():
    update = _update()
    api = FakeApi(updates=[None, update])
    adapter, _ = make_adapter(api=api)
    adapter._api = api
    adapter._handle_update = AsyncMock()
    _stop_when_exhausted(adapter, api)
    adapter._running = True
    _run(adapter._poll_loop())
    adapter._handle_update.assert_awaited_once_with(update)
    assert [call for call in api.calls if call[0] == "get_updates"] == [("get_updates", 30)] * 3


def test_poll_loop_401_sets_fatal_and_stops():
    api = FakeApi(updates=[ZaloApiError("getUpdates", 401, "Unauthorized"), _update()])
    adapter, _ = make_adapter(api=api)
    adapter._api = api
    adapter._handle_update = AsyncMock()
    handler = AsyncMock()
    adapter.set_fatal_error_handler(handler)
    adapter._running = True
    _run(adapter._poll_loop())
    assert adapter._fatal_error_code == "bad_token" and adapter._running is False
    adapter._handle_update.assert_not_awaited()
    handler.assert_awaited_once_with(adapter)


def test_poll_loop_backs_off_on_errors(monkeypatch):
    sleeps = []

    async def fake_sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(zadapter.asyncio, "sleep", fake_sleep)
    api = FakeApi(updates=[
        ZaloApiError("getUpdates", 500, "boom"), RuntimeError("net"), ZaloApiError("getUpdates", 429, "quota"), _update(),
    ])
    adapter, _ = make_adapter(api=api)
    adapter._api = api
    adapter._handle_update = AsyncMock()
    _stop_when_exhausted(adapter, api)
    adapter._running = True
    _run(adapter._poll_loop())
    assert sleeps == [1.0, 2.0, 30.0]
    adapter._handle_update.assert_awaited_once()


def test_poll_loop_isolates_handler_errors():
    api = FakeApi(updates=[_update(message_id="a"), _update(message_id="b")])
    adapter, _ = make_adapter(api=api)
    adapter._api = api
    adapter._handle_update = AsyncMock(side_effect=[RuntimeError("handler"), None])
    _stop_when_exhausted(adapter, api)
    adapter._running = True
    _run(adapter._poll_loop())
    assert adapter._handle_update.await_count == 2


# -- outbound ---------------------------------------------------------------

def test_send_uses_markdown_then_plain_on_400():
    api = FakeApi(send_results=[ZaloApiError("sendMessage", 400, "bad markup"), "id-2"])
    adapter, _ = make_adapter(api=api)
    adapter._api = api
    result = _run(adapter.send("u1", "**hi**"))
    assert result.success and result.message_id == "id-2"
    assert api.calls == [("send_message", "u1", "**hi**", "markdown"), ("send_message", "u1", "**hi**", None)]


def test_send_chunks_long_text():
    adapter, api = make_adapter()
    adapter._api = api
    result = _run(adapter.send("u1", "x" * 4100))
    sent = [call for call in api.calls if call[0] == "send_message"]
    assert result.success and len(sent) == 3
    assert all(len(call[2]) <= 2000 for call in sent)


def test_send_reports_failure_and_retryability():
    api = FakeApi(send_results=[ZaloApiError("sendMessage", 500, "down")])
    adapter, _ = make_adapter(api=api)
    adapter._api = api
    result = _run(adapter.send("u1", "hi"))
    assert result.success is False and "down" in result.error and result.retryable is True


def test_send_timeout_is_not_retryable():
    api = FakeApi(send_results=[ZaloApiError("sendMessage", 408, "request timed out")])
    adapter, _ = make_adapter(api=api)
    adapter._api = api
    result = _run(adapter.send("u1", "hi"))
    assert result.success is False and result.retryable is False


def test_send_empty_is_noop_and_unconnected_fails():
    adapter, api = make_adapter()
    assert _run(adapter.send("u1", "   ")).success is True
    assert _run(adapter.send("u1", "hi")).success is False  # _api is None before connect
    assert api.calls == []


def test_send_typing_calls_chat_action_and_swallows_errors():
    adapter, api = make_adapter()
    adapter._api = api
    _run(adapter.send_typing("u1"))
    assert api.calls == [("send_chat_action", "u1", "typing")]

    async def boom(chat_id, action="typing"):
        raise RuntimeError("no")

    api.send_chat_action = boom
    _run(adapter.send_typing("u1"))  # must not raise


def test_send_image_url_vs_local_path():
    adapter, api = make_adapter()
    adapter._api = api
    ok = _run(adapter.send_image("u1", "https://cdn/x.png", caption="cap"))
    bad = _run(adapter.send_image("u1", "/tmp/x.png"))
    assert ok.success and ok.message_id == "photo-1"
    assert api.calls == [("send_photo", "u1", "https://cdn/x.png", "cap")]
    assert bad.success is False and "public image URL" in bad.error


def test_get_chat_info_defaults_to_dm():
    adapter, _ = make_adapter()
    adapter._chat_types["g1"] = "group"
    assert _run(adapter.get_chat_info("g1")) == {"name": "g1", "type": "group"}
    assert _run(adapter.get_chat_info("u9")) == {"name": "u9", "type": "dm"}


# -- inbound dispatch -------------------------------------------------------

def _dispatch(adapter, update):
    """Run _handle_update and return the captured handle_message mock."""
    _run(adapter._handle_update(update))
    return adapter.handle_message


def test_handle_update_builds_dm_event():
    adapter, _ = make_adapter()
    adapter._bot_display_name = "Bot Mockup"
    handle = _dispatch(adapter, _update(text="@Bot Mockup hi there"))
    handle.assert_awaited_once()
    event = handle.await_args.args[0]
    assert event.text == "hi there" and event.message_type is MessageType.TEXT
    assert event.message_id == "m1" and event.raw_message == {"k": "v"}
    assert event.media_urls == [] and event.media_types == []
    source = event.source
    assert (source.chat_id, source.chat_type, source.user_id, source.user_name) == ("u1", "dm", "u1", "Alice")
    assert source.chat_name == "Alice" and source.platform.value == "zalo"


def test_handle_update_group_source_and_chat_info():
    adapter, _ = make_adapter()
    event = _dispatch(adapter, _update(chat_id="g1", chat_type="GROUP")).await_args.args[0]
    assert event.source.chat_type == "group" and event.source.chat_name == "g1" and event.source.user_id == "u1"
    assert _run(adapter.get_chat_info("g1")) == {"name": "g1", "type": "group"}


def test_handle_update_group_allowlist_blocks_other_groups():
    adapter, _ = make_adapter({"allowed_groups": "g-ok"})
    _dispatch(adapter, _update(message_id="x1", chat_id="g-bad", chat_type="GROUP"))
    adapter.handle_message.assert_not_awaited()
    _dispatch(adapter, _update(message_id="x2", chat_id="g-ok", chat_type="GROUP"))
    adapter.handle_message.assert_awaited_once()


def test_handle_update_group_allowlist_does_not_touch_dms():
    adapter, _ = make_adapter({"allowed_groups": "g-ok"})
    _dispatch(adapter, _update())
    adapter.handle_message.assert_awaited_once()


def test_handle_update_dedups_message_ids():
    adapter, _ = make_adapter()
    _dispatch(adapter, _update(message_id="same"))
    _dispatch(adapter, _update(message_id="same"))
    assert adapter.handle_message.await_count == 1


def test_handle_update_command_type():
    adapter, _ = make_adapter()
    event = _dispatch(adapter, _update(text="/new")).await_args.args[0]
    assert event.message_type is MessageType.COMMAND and event.is_command()


def test_handle_update_photo_downloads_to_cache(monkeypatch):
    seen = {}

    async def fake_cache(url):
        seen["url"] = url
        return "/cache/img.jpg"

    monkeypatch.setattr(zadapter, "cache_image_from_url", fake_cache)
    adapter, _ = make_adapter()
    update = _update(event_name=EVENT_IMAGE, text="look", photo_url="https://cdn/a.jpg")
    event = _dispatch(adapter, update).await_args.args[0]
    assert seen["url"] == "https://cdn/a.jpg"
    assert event.message_type is MessageType.PHOTO and event.text == "look"
    assert event.media_urls == ["/cache/img.jpg"] and event.media_types == ["image/jpeg"]


def test_handle_update_photo_download_failure_becomes_placeholder(monkeypatch):
    async def fake_cache(url):
        raise ValueError("blocked")

    monkeypatch.setattr(zadapter, "cache_image_from_url", fake_cache)
    adapter, _ = make_adapter()
    update = _update(event_name=EVENT_IMAGE, text="", photo_url="https://cdn/a.jpg")
    event = _dispatch(adapter, update).await_args.args[0]
    assert event.text == zadapter.PLACEHOLDER_PHOTO_FAILED
    assert event.media_urls == [] and event.message_type is MessageType.TEXT


def test_handle_update_image_without_url_skips_download(monkeypatch, caplog):
    calls = []

    async def fake_cache(url):
        calls.append(url)
        return "/cache/img.jpg"

    monkeypatch.setattr(zadapter, "cache_image_from_url", fake_cache)
    adapter, _ = make_adapter()
    update = _update(event_name=EVENT_IMAGE, text="", photo_url=None, raw={"message": {"photo_url": "", "chat": {}}})
    with caplog.at_level("WARNING"):
        event = _dispatch(adapter, update).await_args.args[0]
    assert calls == []
    assert event.text == zadapter.PLACEHOLDER_PHOTO_FAILED and event.media_urls == []
    assert "no photo URL" in caplog.text and "photo_url" in caplog.text


def test_handle_update_photo_download_timeout_becomes_placeholder(monkeypatch):
    async def slow_cache(url):
        await asyncio.sleep(1)
        return "/cache/img.jpg"

    monkeypatch.setattr(zadapter, "PHOTO_DOWNLOAD_TIMEOUT", 0.05)
    monkeypatch.setattr(zadapter, "cache_image_from_url", slow_cache)
    adapter, _ = make_adapter()
    update = _update(event_name=EVENT_IMAGE, text="", photo_url="https://cdn/a.jpg")
    event = _dispatch(adapter, update).await_args.args[0]
    assert event.text == zadapter.PLACEHOLDER_PHOTO_FAILED
    assert event.media_urls == []


def test_handle_update_voice_downloads_clip_as_audio(monkeypatch):
    seen = {}

    async def fake_cache(url, ext=".ogg"):
        seen["url"], seen["ext"] = url, ext
        return "/cache/voice.aac"

    monkeypatch.setattr(zadapter, "cache_audio_from_url", fake_cache)
    adapter, _ = make_adapter()
    update = _update(event_name=EVENT_VOICE, text="", voice_url="https://cdn/v.aac")
    event = _dispatch(adapter, update).await_args.args[0]
    assert seen == {"url": "https://cdn/v.aac", "ext": ".aac"}
    assert event.message_type is MessageType.VOICE and event.text == ""
    assert event.media_urls == ["/cache/voice.aac"] and event.media_types == ["audio/aac"]


def test_handle_update_voice_download_failure_becomes_placeholder(monkeypatch):
    async def fake_cache(url, ext=".ogg"):
        raise ValueError("blocked")

    monkeypatch.setattr(zadapter, "cache_audio_from_url", fake_cache)
    adapter, _ = make_adapter()
    update = _update(event_name=EVENT_VOICE, text="", voice_url="https://cdn/v.aac")
    event = _dispatch(adapter, update).await_args.args[0]
    assert event.text == zadapter.PLACEHOLDER_VOICE_FAILED
    assert event.media_urls == [] and event.message_type is MessageType.TEXT


def test_handle_update_voice_without_url_skips_download(monkeypatch, caplog):
    calls = []

    async def fake_cache(url, ext=".ogg"):
        calls.append(url)
        return "/cache/voice.aac"

    monkeypatch.setattr(zadapter, "cache_audio_from_url", fake_cache)
    adapter, _ = make_adapter()
    update = _update(event_name=EVENT_VOICE, text="", voice_url=None, raw={"message": {"voice": "", "chat": {}}})
    with caplog.at_level("WARNING"):
        event = _dispatch(adapter, update).await_args.args[0]
    assert calls == []
    assert event.text == zadapter.PLACEHOLDER_VOICE_FAILED and event.media_urls == []
    assert "no voice URL" in caplog.text and "voice" in caplog.text


def test_handle_update_voice_download_timeout_becomes_placeholder(monkeypatch):
    async def slow_cache(url, ext=".ogg"):
        await asyncio.sleep(1)
        return "/cache/voice.aac"

    monkeypatch.setattr(zadapter, "VOICE_DOWNLOAD_TIMEOUT", 0.05)
    monkeypatch.setattr(zadapter, "cache_audio_from_url", slow_cache)
    adapter, _ = make_adapter()
    update = _update(event_name=EVENT_VOICE, text="", voice_url="https://cdn/v.aac")
    event = _dispatch(adapter, update).await_args.args[0]
    assert event.text == zadapter.PLACEHOLDER_VOICE_FAILED
    assert event.media_urls == []


@pytest.mark.parametrize("event_name, expected_text, expected_type", [
    (EVENT_STICKER, "PLACEHOLDER_STICKER", MessageType.STICKER),
    (EVENT_UNSUPPORTED, "PLACEHOLDER_UNSUPPORTED", MessageType.TEXT),
    ("something.new", "PLACEHOLDER_UNSUPPORTED", MessageType.TEXT),
])
def test_handle_update_placeholders(event_name, expected_text, expected_type):
    adapter, _ = make_adapter()
    event = _dispatch(adapter, _update(event_name=event_name, text="")).await_args.args[0]
    assert event.text == getattr(zadapter, expected_text)
    assert event.message_type is expected_type


# -- env enablement and standalone send ------------------------------------

def test_env_enablement_none_without_token(monkeypatch):
    monkeypatch.delenv("ZALO_BOT_TOKEN", raising=False)
    assert zadapter._env_enablement() is None


def test_env_enablement_seeds_only_token_and_home_channel(monkeypatch):
    for _key, env, _default in zadapter.SETTINGS:
        monkeypatch.delenv(env, raising=False)
    monkeypatch.setenv("ZALO_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("ZALO_MODE", "webhook")
    monkeypatch.setenv("ZALO_HOME_CHANNEL", "u1")
    monkeypatch.setenv("ZALO_HOME_CHANNEL_NAME", "Me")
    seed = zadapter._env_enablement()
    assert seed == {"token": TOKEN, "home_channel": {"chat_id": "u1", "name": "Me"}}


def test_env_enablement_home_channel_name_defaults_to_id(monkeypatch):
    monkeypatch.setenv("ZALO_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("ZALO_HOME_CHANNEL", "u1")
    monkeypatch.delenv("ZALO_HOME_CHANNEL_NAME", raising=False)
    assert zadapter._env_enablement()["home_channel"] == {"chat_id": "u1", "name": "u1"}


def test_register_wires_env_enablement_and_standalone_send(registered_platform):
    kwargs = registered_platform.kwargs
    assert kwargs["env_enablement_fn"] is zadapter._env_enablement
    assert kwargs["standalone_sender_fn"] is zadapter._standalone_send
    assert kwargs["cron_deliver_env_var"] == "ZALO_HOME_CHANNEL"


def test_standalone_send_success(monkeypatch):
    api = FakeApi(send_results=["cron-1"])
    monkeypatch.setattr(zadapter, "ZaloBotApi", lambda token, **kw: api)
    pconfig = PlatformConfig(enabled=True, extra={"token": TOKEN})
    result = _run(zadapter._standalone_send(pconfig, "u1", "report", media_files=["/tmp/x.png"]))
    assert result == {"success": True, "platform": "zalo", "chat_id": "u1", "message_id": "cron-1"}
    assert api.calls == [("send_message", "u1", "report", "markdown")]
    assert api.closed


def test_standalone_send_errors(monkeypatch):
    monkeypatch.delenv("ZALO_BOT_TOKEN", raising=False)
    missing = _run(zadapter._standalone_send(PlatformConfig(enabled=True, extra={}), "u1", "x"))
    assert "ZALO_BOT_TOKEN" in missing["error"]

    api = FakeApi(send_results=[ZaloApiError("sendMessage", 500, "down")])
    monkeypatch.setattr(zadapter, "ZaloBotApi", lambda token, **kw: api)
    failed = _run(zadapter._standalone_send(PlatformConfig(enabled=True, extra={"token": TOKEN}), "u1", "x"))
    assert "down" in failed["error"] and api.closed


# -- webhook mode -----------------------------------------------------------

import httpx  # noqa: E402

try:
    from aiohttp import web as aiohttp_web
    from aiohttp.test_utils import TestClient, TestServer
    AIOHTTP_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised only in aiohttp-free installs
    aiohttp_web = TestClient = TestServer = None
    AIOHTTP_AVAILABLE = False

needs_aiohttp = pytest.mark.skipif(not AIOHTTP_AVAILABLE, reason="aiohttp is needed for webhook tests")

WEBHOOK_EXTRA = {
    "mode": "webhook", "webhook_url": "https://example.com/zalo/webhook",
    "webhook_secret": "s3cr3t-token", "webhook_port": 0,  # port 0 = any free port
}
WEBHOOK_PAYLOAD = {
    "ok": True,
    "result": {
        "event_name": EVENT_TEXT,
        "message": {
            "message_id": "w1", "text": "hi",
            "from": {"id": "u1", "display_name": "Alice"}, "chat": {"id": "u1", "chat_type": "PRIVATE"},
        },
    },
}
SECRET_HEADERS = {"X-Bot-Api-Secret-Token": "s3cr3t-token"}


def _webhook_client(adapter):
    app = aiohttp_web.Application()
    app.router.add_post("/zalo/webhook", adapter._handle_webhook)
    return TestClient(TestServer(app))


def test_connect_webhook_requires_url_and_secret():
    adapter, api = make_adapter({"mode": "webhook", "webhook_url": "", "webhook_secret": ""})
    assert _run(adapter.connect()) is False
    assert adapter._fatal_error_code == "webhook_config"
    assert api.closed and adapter._api is None


@needs_aiohttp
def test_connect_webhook_rejects_non_https_url():
    adapter, api = make_adapter({
        "mode": "webhook", "webhook_url": "example.com/zalo/webhook", "webhook_secret": "s3cr3t-token",
    })
    assert _run(adapter.connect()) is False
    assert adapter._fatal_error_code == "webhook_config"
    assert api.closed and adapter._web_runner is None


@needs_aiohttp
@pytest.mark.parametrize("secret", ["short", "x" * 300])
def test_connect_webhook_rejects_bad_secret_length(secret):
    adapter, api = make_adapter({**WEBHOOK_EXTRA, "webhook_secret": secret})
    assert _run(adapter.connect()) is False
    assert adapter._fatal_error_code == "webhook_config"
    assert adapter._web_runner is None and api.closed


@needs_aiohttp
def test_connect_webhook_registers_and_serves_health():
    adapter, api = make_adapter(WEBHOOK_EXTRA)

    async def scenario():
        assert await adapter.connect() is True
        host, port = adapter._web_runner.addresses[0][:2]
        async with httpx.AsyncClient() as client:
            response = await client.get(f"http://{host}:{port}/health")
        assert response.status_code == 200 and response.text == "ok"
        await adapter.disconnect()

    _run(scenario())
    assert ("set_webhook", "https://example.com/zalo/webhook", "s3cr3t-token") in api.calls
    assert ("get_updates", 30) not in api.calls
    assert adapter._web_runner is None and adapter._poll_task is None and api.closed


@needs_aiohttp
def test_connect_webhook_register_failure_stops_server():
    api = FakeApi()

    async def failing(url, secret_token):
        raise ZaloApiError("setWebhook", 400, "bad url")

    api.set_webhook = failing
    adapter, _ = make_adapter(WEBHOOK_EXTRA, api=api)
    assert _run(adapter.connect()) is False
    assert adapter._fatal_error_code == "webhook_register_failed"
    assert adapter._web_runner is None and api.closed
    assert adapter._fatal_error_retryable is False


@needs_aiohttp
def test_connect_webhook_start_failure_is_fatal_not_raised():
    adapter, api = make_adapter({**WEBHOOK_EXTRA, "webhook_port": 99999})
    assert _run(adapter.connect()) is False
    assert adapter._fatal_error_code == "webhook_start_failed"
    assert adapter._web_runner is None and api.closed


def test_webhook_path_from_url_or_default():
    adapter, _ = make_adapter({"webhook_url": "https://h.example/hooks/zalo"})
    assert adapter._webhook_path() == "/hooks/zalo"
    bare, _ = make_adapter({"webhook_url": "https://h.example"})
    assert bare._webhook_path() == "/zalo/webhook"


@needs_aiohttp
def test_webhook_rejects_bad_secret_and_bad_json():
    adapter, _ = make_adapter(WEBHOOK_EXTRA)

    async def scenario():
        async with _webhook_client(adapter) as client:
            wrong = await client.post("/zalo/webhook", json=WEBHOOK_PAYLOAD, headers={"X-Bot-Api-Secret-Token": "wrong"})
            missing = await client.post("/zalo/webhook", json=WEBHOOK_PAYLOAD)
            broken = await client.post("/zalo/webhook", data=b"{not json", headers=SECRET_HEADERS)
            return wrong.status, missing.status, broken.status

    assert _run(scenario()) == (403, 403, 400)
    adapter.handle_message.assert_not_awaited()


@needs_aiohttp
def test_webhook_accepts_and_dispatches():
    adapter, _ = make_adapter(WEBHOOK_EXTRA)

    async def scenario():
        async with _webhook_client(adapter) as client:
            response = await client.post("/zalo/webhook", json=WEBHOOK_PAYLOAD, headers=SECRET_HEADERS)
            assert response.status == 200
            if adapter._tasks:
                await asyncio.gather(*adapter._tasks)

    _run(scenario())
    adapter.handle_message.assert_awaited_once()
    assert adapter.handle_message.await_args.args[0].text == "hi"


@needs_aiohttp
def test_webhook_ignores_payload_without_message():
    adapter, _ = make_adapter(WEBHOOK_EXTRA)

    async def scenario():
        async with _webhook_client(adapter) as client:
            response = await client.post("/zalo/webhook", json={"ok": True, "result": {}}, headers=SECRET_HEADERS)
            return response.status

    assert _run(scenario()) == 200
    adapter.handle_message.assert_not_awaited()
