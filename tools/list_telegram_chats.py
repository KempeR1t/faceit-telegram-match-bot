#!/usr/bin/env python3
"""List chat IDs visible in recent Telegram bot updates."""

from __future__ import annotations

import os
import sys
from typing import Any

import requests


def extract_chat(update: dict[str, Any]) -> dict[str, Any] | None:
    for field in ("message", "edited_message", "channel_post", "edited_channel_post"):
        event = update.get(field)
        if isinstance(event, dict) and isinstance(event.get("chat"), dict):
            return event["chat"]
    return None


def main() -> int:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        print("TELEGRAM_BOT_TOKEN is not set.", file=sys.stderr)
        return 2

    try:
        response = requests.get(
            f"https://api.telegram.org/bot{token}/getUpdates",
            params={"limit": 100, "timeout": 0},
            timeout=15,
        )
    except requests.RequestException as exc:
        # Do not print the raw exception: the request URL contains the bot token.
        print(f"Telegram request failed ({type(exc).__name__}).", file=sys.stderr)
        return 1

    if response.status_code != 200:
        print(f"Telegram returned HTTP {response.status_code}.", file=sys.stderr)
        return 1

    try:
        payload = response.json()
    except requests.JSONDecodeError:
        print("Telegram returned invalid JSON.", file=sys.stderr)
        return 1

    updates = payload.get("result") if isinstance(payload, dict) else None
    if not isinstance(updates, list):
        print("Telegram returned an unexpected response.", file=sys.stderr)
        return 1

    chats: dict[str, tuple[str, str]] = {}
    for update in updates:
        if not isinstance(update, dict):
            continue
        chat = extract_chat(update)
        if not chat or chat.get("id") is None:
            continue
        chat_id = str(chat["id"])
        chat_type = str(chat.get("type", "unknown"))
        title = str(chat.get("title") or chat.get("username") or chat_type)
        chats[chat_id] = (chat_type, title)

    if not chats:
        print(
            "No chats found. Send the bot a private message or a command in the "
            "target group, then run this tool again."
        )
        return 1

    print("Visible Telegram chats:")
    for chat_id, (chat_type, title) in chats.items():
        print(f"  id={chat_id}  type={chat_type}  name={title}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
