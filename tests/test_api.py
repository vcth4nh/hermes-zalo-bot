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


def test_parse_update_coerces_ids_and_falls_back_user_name_to_id():
    u = parse_update({"message": {"from": {"id": 77}, "chat": {"id": 77}}})
    assert u.user_id == "77" and u.user_name == "77" and u.chat_id == "77"
    assert u.event_name == "" and u.message_id == "" and u.chat_type == "PRIVATE"


@pytest.mark.parametrize("payload", [
    None, "x", 42, {}, {"ok": True, "result": {}}, {"message": "nope"}, {"message": {"chat": {}}},
])
def test_parse_update_rejects_payloads_without_a_chat(payload):
    assert parse_update(payload) is None
