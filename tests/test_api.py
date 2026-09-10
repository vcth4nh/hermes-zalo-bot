"""Tests for zalo/api.py. No Hermes needed."""
import asyncio
import json

import httpx
import pytest

from zalo.api import (
    API_BASE, EVENT_IMAGE, EVENT_TEXT, TEXT_LIMIT, ZaloUpdate, chunk_text, parse_update, redact_token,
)

TOKEN = "123:secret-part"


# -- redact_token -----------------------------------------------------------

def test_redact_token_replaces_every_occurrence():
    text = f"POST https://x/bot{TOKEN}/getMe failed; token={TOKEN}"
    out = redact_token(text, TOKEN)
    assert TOKEN not in out
    assert out.count("<TOKEN>") == 2


def test_redact_token_without_token_is_identity():
    assert redact_token("abc", "") == "abc"


# -- chunk_text -------------------------------------------------------------

def test_chunk_text_short_is_single_piece():
    assert chunk_text("hello", 10) == ["hello"]


def test_chunk_text_empty():
    assert chunk_text("", 10) == []


def test_chunk_text_prefers_newline_boundaries():
    pieces = chunk_text("line one\nline two\nline three", 12)
    assert pieces == ["line one", "line two", "line three"]


def test_chunk_text_falls_back_to_spaces_then_hard_cuts():
    assert chunk_text("aaaa bbbb cccc", 10) == ["aaaa bbbb", "cccc"]
    assert chunk_text("a" * 25, 10) == ["a" * 10, "a" * 10, "a" * 5]


def test_chunk_text_default_limit_is_zalo_limit():
    assert TEXT_LIMIT == 2000
    assert all(len(p) <= 2000 for p in chunk_text("x" * 4500))


# -- parse_update -----------------------------------------------------------

WEBHOOK_TEXT = {
    "ok": True,
    "result": {
        "event_name": "message.text.received",
        "message": {
            "message_id": "m1",
            "date": 1749632637199,
            "text": "hello",
            "from": {"id": "u1", "display_name": "Alice", "is_bot": False},
            "chat": {"id": "u1", "chat_type": "PRIVATE"},
        },
    },
}


def test_parse_update_envelope_form():
    u = parse_update(WEBHOOK_TEXT)
    assert isinstance(u, ZaloUpdate)
    assert (u.event_name, u.message_id, u.chat_id, u.chat_type) == (EVENT_TEXT, "m1", "u1", "PRIVATE")
    assert (u.user_id, u.user_name, u.is_bot, u.text) == ("u1", "Alice", False, "hello")
    assert u.date_ms == 1749632637199
    assert u.photo_url is None and u.voice_url is None and u.sticker is None
    assert u.raw is WEBHOOK_TEXT
    assert u.is_group is False


def test_parse_update_bare_form():
    u = parse_update(WEBHOOK_TEXT["result"])
    assert u is not None and u.text == "hello" and u.event_name == EVENT_TEXT


def test_parse_update_group_caption_and_photo():
    payload = {
        "event_name": EVENT_IMAGE,
        "message": {
            "message_id": "m2", "caption": "look", "photo": "https://cdn/img.jpg",
            "from": {"id": "u2", "display_name": "Bob"}, "chat": {"id": "g1", "chat_type": "group"},
        },
    }
    u = parse_update(payload)
    assert u.is_group and u.chat_type == "GROUP" and u.chat_id == "g1"
    assert u.text == "look" and u.photo_url == "https://cdn/img.jpg"


def test_parse_update_voice_and_sticker_fields():
    base = {"from": {"id": "u"}, "chat": {"id": "u"}}
    voice = parse_update({"event_name": "message.voice.received", "message": {**base, "voice_url": "https://cdn/v.aac"}})
    sticker = parse_update({"event_name": "message.sticker.received", "message": {**base, "sticker": "s42"}})
    assert voice.voice_url == "https://cdn/v.aac" and voice.text == ""
    assert sticker.sticker == "s42"


def test_parse_update_reads_photo_url_key_and_keeps_photo_fallback():
    base = {"from": {"id": "u"}, "chat": {"id": "u"}}
    real = parse_update({"event_name": EVENT_IMAGE, "message": {**base, "photo_url": "https://cdn/real.jpg"}})
    documented = parse_update({"event_name": EVENT_IMAGE, "message": {**base, "photo": "https://cdn/doc.jpg"}})
    assert real.photo_url == "https://cdn/real.jpg"
    assert documented.photo_url == "https://cdn/doc.jpg"


def test_parse_update_coerces_ids_and_falls_back_user_name_to_id():
    u = parse_update({"message": {"from": {"id": 77}, "chat": {"id": 77}}})
    assert u.user_id == "77" and u.user_name == "77" and u.chat_id == "77"
    assert u.event_name == "" and u.message_id == "" and u.chat_type == "PRIVATE"


@pytest.mark.parametrize("payload", [
    None, "x", 42, {}, {"ok": True, "result": {}}, {"message": "nope"}, {"message": {"chat": {}}},
])
def test_parse_update_rejects_payloads_without_a_chat(payload):
    assert parse_update(payload) is None


# -- ZaloBotApi client ------------------------------------------------------

from zalo.api import ZaloApiError, ZaloBotApi  # noqa: E402


def _run(coro):
    return asyncio.run(coro)


class Recorder:
    """httpx.MockTransport handler: records requests, replies from a script."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if not self.responses:
            return httpx.Response(200, json={"ok": True, "result": {}})
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _api(recorder: Recorder, token: str = TOKEN) -> ZaloBotApi:
    return ZaloBotApi(token, client=httpx.AsyncClient(transport=httpx.MockTransport(recorder)))


def ok(result=None) -> httpx.Response:
    return httpx.Response(200, json={"ok": True, "result": {} if result is None else result, "error_code": 0})


def err(code: int, description: str = "bad", status: int = 200) -> httpx.Response:
    return httpx.Response(status, json={"ok": False, "error_code": code, "description": description})


def _body(request: httpx.Request) -> dict:
    return json.loads(request.content)


def test_url_embeds_token_and_method():
    assert _api(Recorder()).url("getMe") == f"{API_BASE}/bot{TOKEN}/getMe"


def test_call_posts_json_and_returns_result():
    rec = Recorder(ok({"id": "42"}))
    assert _run(_api(rec).call("getMe")) == {"id": "42"}
    req = rec.requests[0]
    assert req.method == "POST"
    assert req.url.path == f"/bot{TOKEN}/getMe"
    assert req.headers["content-type"] == "application/json"
    assert _body(req) == {}


def test_call_raises_with_zalo_error_code():
    with pytest.raises(ZaloApiError) as info:
        _run(_api(Recorder(err(401, "Unauthorized"))).call("getMe"))
    assert info.value.code == 401
    assert info.value.method == "getMe"
    assert "Unauthorized" in str(info.value)


def test_call_raises_on_non_json_http_error():
    with pytest.raises(ZaloApiError) as info:
        _run(_api(Recorder(httpx.Response(502, text="<html>bad gateway</html>"))).call("getMe"))
    assert info.value.code == 502


def test_call_maps_timeout_to_408_and_transport_errors_to_0():
    with pytest.raises(ZaloApiError) as timeout:
        _run(_api(Recorder(httpx.ReadTimeout("slow"))).call("getMe"))
    with pytest.raises(ZaloApiError) as transport:
        _run(_api(Recorder(httpx.ConnectError("refused"))).call("getMe"))
    assert timeout.value.code == 408
    assert "timed out" in str(timeout.value)
    assert transport.value.code == 0


def test_call_redacts_token_from_errors():
    with pytest.raises(ZaloApiError) as info:
        _run(_api(Recorder(err(400, f"bad url https://x/bot{TOKEN}/getMe"))).call("getMe"))
    assert TOKEN not in str(info.value)
    assert "<TOKEN>" in str(info.value)


def test_call_redacts_before_truncating():
    description = "x" * 193 + TOKEN
    with pytest.raises(ZaloApiError) as info:
        _run(_api(Recorder(err(400, description))).call("getMe"))
    assert TOKEN not in str(info.value)
    assert "<TOKEN>" in str(info.value)


def test_get_me_returns_result_dict():
    assert _run(_api(Recorder(ok({"id": "42", "display_name": "Bot"}))).get_me()) == {"id": "42", "display_name": "Bot"}


def test_get_updates_sends_timeout_and_parses_update():
    rec = Recorder(ok({
        "event_name": EVENT_TEXT,
        "message": {"message_id": "m1", "text": "hi", "from": {"id": "u"}, "chat": {"id": "u", "chat_type": "PRIVATE"}},
    }))
    update = _run(_api(rec).get_updates(timeout=25))
    assert update is not None and update.text == "hi" and update.message_id == "m1"
    assert _body(rec.requests[0]) == {"timeout": 25}


def test_get_updates_returns_none_on_408_and_on_empty_result():
    assert _run(_api(Recorder(err(408, "timeout"))).get_updates()) is None
    assert _run(_api(Recorder(ok({}))).get_updates()) is None


def test_get_updates_propagates_other_errors():
    with pytest.raises(ZaloApiError):
        _run(_api(Recorder(err(401))).get_updates())


def test_send_message_params_and_message_id():
    rec = Recorder(ok({"message_id": "abc", "date": 1}), ok({"message_id": "def"}))
    api = _api(rec)
    assert _run(api.send_message("c1", "hello", parse_mode="markdown")) == "abc"
    assert _run(api.send_message("c1", "plain")) == "def"
    assert _body(rec.requests[0]) == {"chat_id": "c1", "text": "hello", "parse_mode": "markdown"}
    assert _body(rec.requests[1]) == {"chat_id": "c1", "text": "plain"}


def test_send_photo_and_chat_action_params():
    rec = Recorder(ok({"message_id": "p1"}), httpx.Response(200, json={"ok": True}))
    api = _api(rec)
    assert _run(api.send_photo("c1", "https://cdn/x.jpg", caption="cap")) == "p1"
    _run(api.send_chat_action("c1"))
    assert _body(rec.requests[0]) == {"chat_id": "c1", "photo": "https://cdn/x.jpg", "caption": "cap"}
    assert _body(rec.requests[1]) == {"chat_id": "c1", "action": "typing"}


def test_webhook_methods():
    rec = Recorder(
        ok({"url": "https://h/x"}), err(404, "Not Found"),
        httpx.Response(200, json={"ok": True}), httpx.Response(200, json={"ok": True}),
    )
    api = _api(rec)
    assert _run(api.get_webhook_info()) == {"url": "https://h/x"}
    assert _run(api.get_webhook_info()) is None
    _run(api.set_webhook("https://h/x", "s3cr3t-token"))
    _run(api.delete_webhook())
    assert _body(rec.requests[2]) == {"url": "https://h/x", "secret_token": "s3cr3t-token"}
    assert rec.requests[3].url.path.endswith("/deleteWebhook")


def test_close_closes_client():
    class Closing(httpx.AsyncClient):
        closed = False

        async def aclose(self):
            self.closed = True

    client = Closing(transport=httpx.MockTransport(Recorder()))
    _run(ZaloBotApi(TOKEN, client=client).close())
    assert client.closed
