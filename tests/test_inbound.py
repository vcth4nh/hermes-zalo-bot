"""Tests for zalo/inbound.py. No Hermes needed."""
from zalo.inbound import SeenIds, strip_mention


def test_seen_ids_new_then_duplicate():
    seen = SeenIds(ttl=10)
    assert seen.add("m1", now=100.0) is True
    assert seen.add("m1", now=105.0) is False


def test_seen_ids_expires_after_ttl():
    seen = SeenIds(ttl=10)
    seen.add("m1", now=100.0)
    assert seen.add("m1", now=111.0) is True


def test_seen_ids_is_bounded_and_evicts_oldest():
    seen = SeenIds(ttl=1000, max_size=3)
    for index, key in enumerate(["a", "b", "c", "d"]):
        assert seen.add(key, now=float(index)) is True
    assert len(seen._seen) <= 3
    assert seen.add("a", now=5.0) is True  # "a" was evicted, so it counts as new again
    assert seen.add("d", now=5.0) is False


def test_strip_mention_removes_leading_bot_mention_case_insensitively():
    assert strip_mention("@Bot Mockup: hello", "Bot Mockup") == "hello"
    assert strip_mention("@bot mockup hello", "Bot Mockup") == "hello"


def test_strip_mention_keeps_text_without_leading_mention():
    assert strip_mention("hello @Bot Mockup", "Bot Mockup") == "hello @Bot Mockup"
    assert strip_mention("  plain  ", "") == "plain"
    assert strip_mention("", "Bot Mockup") == ""
