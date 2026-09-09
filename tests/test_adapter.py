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


def test_allowed_groups_csv():
    adapter, _ = make_adapter({"allowed_groups": " g1, g2 ,,"})
    assert adapter._allowed_groups == {"g1", "g2"}


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
