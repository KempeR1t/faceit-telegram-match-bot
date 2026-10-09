#!/usr/bin/env python3
"""Replay an explicitly selected audit snapshot through normal delivery/state code.

Rewinding the cursors and clearing delivery receipts is a separate operator action.
This entry point never includes matches outside the selected snapshot.
"""
import json
import logging
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from bot import (Config, FaceitClient, FlareSolverrClient, MatchHistoryResult,
                 SENT_MATCHES_KEY, TelegramClient, build_http_session,
                 configure_logging, load_state, run_once)


class ReplayClient:
    def __init__(self, client, histories):
        self.client, self.histories = client, histories

    def __getattr__(self, name):
        return getattr(self.client, name)

    def matches_since(self, player_id, game_id, previous_match_id):
        return self.histories[player_id]


def prepare_replay(client, config, snapshot):
    selected = {m['match_id'] for p in snapshot['players'].values() for m in p['matches']}
    if not selected or not all(isinstance(mid, str) and mid for mid in selected):
        raise ValueError('Replay snapshot must select match IDs.')
    state, is_new = load_state(config.state_file)
    if is_new or any(pid not in state for pid in config.players):
        raise ValueError('A saved cursor is required for every tracked player.')
    histories = {}; found = set()
    for pid in config.players:
        result = client.matches_since(pid, config.game_id, state[pid])
        if not result.ok:
            raise ValueError('Cannot verify complete history for replay.')
        matches = tuple((mid, t) for mid, t in result.matches if mid in selected)
        histories[pid] = MatchHistoryResult(True, matches)
        found.update(mid for mid, _ in matches)
    if not selected.issubset(found | set(state.get(SENT_MATCHES_KEY, []))):
        raise ValueError('Selected matches are not covered by history or delivery receipts.')
    return ReplayClient(client, histories)


def main():
    configure_logging()
    if len(sys.argv) != 2:
        raise SystemExit('Usage: replay.py <audit-snapshot.json>')
    config = Config.from_env()
    snapshot = json.loads(Path(sys.argv[1]).read_text())
    provider = None
    with build_http_session() as session:
        client = FaceitClient(session, config.faceit_api_key, config.request_timeout)
        replay = prepare_replay(client, config, snapshot)
        telegram = TelegramClient(session, config.telegram_bot_token, config.telegram_chat_id,
                                  config.request_timeout, config.telegram_proxy_url)
        if config.flaresolverr_enabled:
            provider = FlareSolverrClient(session, config.flaresolverr_url,
                                         config.flaresolverr_max_timeout_ms, cleanup_stale_sessions=True)
        return run_once(config, replay, telegram, provider)


if __name__ == '__main__':
    raise SystemExit(main())
