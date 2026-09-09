"""Inbound helpers with no Hermes dependency: message-id dedup and mention stripping."""
from __future__ import annotations

import time
from typing import Dict, Optional

DEDUP_TTL_SECONDS = 600.0
DEDUP_MAX = 2048


class SeenIds:
    """Bounded, time-limited set of message ids (webhook retries redeliver the same id)."""

    def __init__(self, ttl: float = DEDUP_TTL_SECONDS, max_size: int = DEDUP_MAX):
        self._ttl = ttl
        self._max = max_size
        self._seen: Dict[str, float] = {}  # key -> time first seen (insertion ordered)

    def add(self, key: str, now: Optional[float] = None) -> bool:
        """Record ``key``. Return True when it was not seen within the TTL."""
        now = time.monotonic() if now is None else now
        cutoff = now - self._ttl
        stamp = self._seen.get(key)
        if stamp is not None and stamp > cutoff:
            return False
        if len(self._seen) >= self._max:
            self._seen = {k: t for k, t in self._seen.items() if t > cutoff}
            while len(self._seen) >= self._max:
                self._seen.pop(next(iter(self._seen)))
        self._seen[key] = now
        return True


def strip_mention(text: str, bot_name: str) -> str:
    """Remove a leading ``@<bot_name>`` (case-insensitive) and the separators after it."""
    text = (text or "").strip()
    name = (bot_name or "").strip()
    if name and text.lower().startswith(("@" + name).lower()):
        text = text[len(name) + 1:].lstrip(" \t:,")
    return text
