#!/usr/bin/env python3
"""Resolve FACEIT nicknames to player IDs without printing the API key."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import requests


API_URL = "https://open.faceit.com/data/v4/players"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Resolve FACEIT nicknames and create a players JSON object."
    )
    parser.add_argument("nicknames", nargs="+", help="one or more FACEIT nicknames")
    parser.add_argument(
        "--game", default=os.getenv("FACEIT_GAME", "cs2"), help="FACEIT game ID"
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="write the JSON object to this file, for example players.json",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="allow --output to replace an existing file",
    )
    args = parser.parse_args()

    if args.force and args.output is None:
        parser.error("--force requires --output")

    api_key = os.getenv("FACEIT_API_KEY", "").strip()
    if not api_key:
        print("FACEIT_API_KEY is not set.", file=sys.stderr)
        return 2

    resolved: dict[str, str] = {}
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

        resolved[str(player_id)] = str(nickname)
        print(f"{nickname}: {player_id}")

    if len(resolved) != len(args.nicknames):
        print(
            "No file was written because not all players were resolved.",
            file=sys.stderr,
        )
        return 1

    json_text = json.dumps(resolved, ensure_ascii=False, indent=2) + "\n"
    if args.output is None:
        print("\nplayers.json:")
        print(json_text, end="")
        return 0

    output_path = args.output.expanduser()
    mode = "w" if args.force else "x"
    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with output_path.open(mode, encoding="utf-8") as file_handle:
            file_handle.write(json_text)
        os.chmod(output_path, 0o600)
    except FileExistsError:
        print(
            f"Output file already exists: {output_path}. Use --force to replace it.",
            file=sys.stderr,
        )
        return 2
    except OSError as exc:
        print(
            f"Could not write {output_path} ({type(exc).__name__}).",
            file=sys.stderr,
        )
        return 2

    print(f"\nWrote {len(resolved)} player(s) to {output_path}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
