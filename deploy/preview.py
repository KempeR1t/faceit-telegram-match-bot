#!/usr/bin/env python3
"""Read-only preview of notifications; never sends or changes state."""
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from bot import Config, FaceitClient, SENT_MATCHES_KEY, PENDING_MESSAGES_KEY, build_http_session, load_state

def main():
    # The preview does not make any Telegram calls.
    if not os.environ.get("TELEGRAM_BOT_TOKEN"):
        os.environ["TELEGRAM_BOT_TOKEN"] = "preview-only"
    config = Config.from_env()
    state, new_state = load_state(config.state_file)
    if new_state or any(pid not in state for pid in config.players):
        raise SystemExit("Seed a baseline for every player before previewing.")
    with build_http_session() as session:
        faceit = FaceitClient(session, config.faceit_api_key, config.request_timeout)
        sent = set(state.get(SENT_MATCHES_KEY, [])) | set(state.get(PENDING_MESSAGES_KEY, {}))
        result = []
        for pid, name in config.players.items():
            history = faceit.matches_since(pid, config.game_id, state[pid])
            if not history.ok:
                raise SystemExit("FACEIT lookup failed for " + name)
            pending = [mid for mid, _ in history.matches if mid not in sent]
            result.append({"nickname": name, "player_id": pid,
                           "previous_match_id": state[pid],
                           "pending_match_ids": pending,
                           "would_notify": bool(pending)})
    pending = {mid for p in result for mid in p["pending_match_ids"]}
    print(json.dumps({"players": result, "notification_count": len(pending),
                      "state_changed": False, "telegram_called": False}, ensure_ascii=False, indent=2))

if __name__ == "__main__":
    main()
