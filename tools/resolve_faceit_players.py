#!/usr/bin/env python3
"""Resolve FACEIT nicknames to player IDs without printing the API key."""

from __future__ import annotations

import argparse
import os
import sys

import requests


API_URL = "https://open.faceit.com/data/v4/players"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Resolve FACEIT nicknames and print a FACEIT_PLAYERS value."
    )
    parser.add_argument("nicknames", nargs="+", help="one or more FACEIT nicknames")
    parser.add_argument(
        "--game", default=os.getenv("FACEIT_GAME", "cs2"), help="FACEIT game ID"
    )
    args = parser.parse_args()

    api_key = os.getenv("FACEIT_API_KEY", "").strip()
    if not api_key:
        print("FACEIT_API_KEY is not set.", file=sys.stderr)
        return 2

    resolved: list[str] = []
    session = requests.Session()
    session.headers.update(
        {
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
            "User-Agent": "faceit-telegram-match-bot/player-resolver",
        }
    )

    for requested_nickname in args.nicknames:
        try:
            response = session.get(
                API_URL,
                params={"nickname": requested_nickname, "game": args.game},
                timeout=15,
            )
        except requests.RequestException as exc:
            print(
                f"Could not resolve {requested_nickname!r} ({type(exc).__name__}).",
                file=sys.stderr,
            )
            continue

        if response.status_code != 200:
            print(
                f"Could not resolve {requested_nickname!r}: HTTP {response.status_code}.",
                file=sys.stderr,
            )
            continue

        try:
            payload = response.json()
        except requests.JSONDecodeError:
            print(
                f"Could not resolve {requested_nickname!r}: invalid JSON.",
                file=sys.stderr,
            )
            continue

        player_id = payload.get("player_id") if isinstance(payload, dict) else None
        nickname = payload.get("nickname") if isinstance(payload, dict) else None
        if not player_id or not nickname:
            print(
                f"Could not resolve {requested_nickname!r}: player not found.",
                file=sys.stderr,
            )
            continue

        entry = f"{player_id}:{nickname}"
        resolved.append(entry)
        print(f"{nickname}: {player_id}")

    if resolved:
        print("\nAdd this line to your environment file:")
        print(f"FACEIT_PLAYERS={','.join(resolved)}")

    return 0 if len(resolved) == len(args.nicknames) else 1


if __name__ == "__main__":
    sys.exit(main())
