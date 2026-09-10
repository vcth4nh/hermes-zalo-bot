#!/usr/bin/env python3
"""Hermes-free live check for the Zalo Bot API: wait for one message and echo it back.

Usage:
    ZALO_BOT_TOKEN=... python scripts/smoke_echo.py [--wait 60] [--delete-webhook]

getUpdates is blocked while a webhook is registered. Pass --delete-webhook to remove it first.
Exit codes: 0 echoed a message, 1 nothing arrived, 2 no token, 3 a webhook blocks polling, 4 Zalo API error.
"""
import argparse
import asyncio
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from zalo.api import ZaloApiError, ZaloBotApi  # noqa: E402

POLL_SECONDS = 30  # Zalo's default long-poll timeout


async def main(wait: int, delete_webhook: bool) -> int:
    token = os.environ.get("ZALO_BOT_TOKEN", "").strip()
    if not token:
        print("ZALO_BOT_TOKEN is not set", file=sys.stderr)
        return 2
    api = ZaloBotApi(token, timeout=POLL_SECONDS + 10)
    try:
        try:
            me = await api.get_me()
            print(f"bot: {me.get('display_name')!r} id={me.get('id')} can_join_groups={me.get('can_join_groups')}")
            info = await api.get_webhook_info()
            if info and info.get("url"):
                if not delete_webhook:
                    print(f"webhook is set to {info['url']}; getUpdates is blocked. Re-run with --delete-webhook.")
                    return 3
                await api.delete_webhook()
                print(f"deleted webhook {info['url']}")
            print(f"waiting up to {wait}s for one message: send the bot a DM now")
            deadline = time.monotonic() + wait
            update = None
            while update is None and time.monotonic() < deadline:
                remaining = max(1, int(deadline - time.monotonic()))
                update = await api.get_updates(timeout=min(POLL_SECONDS, remaining))
            if update is None:
                print("no update received")
                return 1
            print(f"received {update.event_name} from {update.user_id} ({update.user_name}) "
                  f"in {update.chat_type} {update.chat_id}: text={update.text!r} photo={update.photo_url}")
            reply = f"Echo: {update.text}" if update.text else f"Echo: [{update.event_name}]"
            try:
                message_id = await api.send_message(update.chat_id, reply, parse_mode="markdown")
            except ZaloApiError as exc:
                if exc.code != 400:
                    raise
                message_id = await api.send_message(update.chat_id, reply)
            print(f"sent message_id={message_id}")
            return 0
        except ZaloApiError as exc:
            print(f"Zalo API error: {exc}", file=sys.stderr)
            return 4
    finally:
        await api.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--wait", type=int, default=60, help="seconds to wait for one update (default 60)")
    parser.add_argument("--delete-webhook", action="store_true", help="remove a registered webhook so polling works")
    args = parser.parse_args()
    sys.exit(asyncio.run(main(args.wait, args.delete_webhook)))
