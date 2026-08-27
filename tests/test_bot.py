from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from zoneinfo import ZoneInfo

from bot import (
    Config,
    ConfigurationError,
    LatestMatchResult,
    format_duration,
    load_players_file,
    load_state,
    run_once,
)


PLAYER_ID = "11111111-1111-4111-8111-111111111111"


class FakeFaceit:
    def __init__(self, match_id: str) -> None:
        self.match_id = match_id

    def latest_match(self, player_id: str, game_id: str) -> LatestMatchResult:
        return LatestMatchResult(ok=True, match_id=self.match_id)

    def match_details(self, match_id: str) -> dict:
        return {"started_at": 1_700_000_000, "finished_at": 1_700_001_000}

    def match_stats(self, match_id: str) -> dict:
        return {
            "rounds": [
                {
                    "round_stats": {"Map": "de_mirage", "Score": "13 / 10"},
                    "teams": [
                        {
                            "team_stats": {"Team Win": "1"},
                            "players": [
                                {
                                    "player_id": PLAYER_ID,
                                    "player_stats": {
                                        "Kills": "20",
                                        "Deaths": "10",
                                        "K/D Ratio": "2.0",
                                        "ADR": "100",
                                        "MVPs": "4",
                                    },
                                }
                            ],
                        }
                    ],
                }
            ]
        }


class FakeTelegram:
    def __init__(self, result: bool = True) -> None:
        self.result = result
        self.messages: list[str] = []

    def send_message(self, text: str) -> bool:
        self.messages.append(text)
        return self.result


def make_config(state_file: Path, notify_on_first_run: bool = False) -> Config:
    return Config(
        faceit_api_key="test-faceit-key",
        telegram_bot_token="test-telegram-token",
        telegram_chat_id="123",
        players={PLAYER_ID: "Player<One>"},
        game_id="cs2",
        timezone=ZoneInfo("Europe/Moscow"),
        timezone_name="Europe/Moscow",
        state_file=state_file,
        request_timeout=15,
        notify_on_first_run=notify_on_first_run,
        log_level="INFO",
    )


class ConfigurationTests(unittest.TestCase):
    def test_load_players_file_rejects_missing_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            players_file = Path(temporary_directory) / "missing.json"

            with self.assertRaises(ConfigurationError):
                load_players_file(players_file)

    def test_load_players_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            players_file = Path(temporary_directory) / "players.json"
            players_file.write_text(
                json.dumps({PLAYER_ID: "PlayerOne"}), encoding="utf-8"
            )

            self.assertEqual(
                load_players_file(players_file), {PLAYER_ID: "PlayerOne"}
            )

    def test_load_players_file_rejects_invalid_id(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            players_file = Path(temporary_directory) / "players.json"
            players_file.write_text(
                json.dumps({"not-a-player-id": "PlayerOne"}), encoding="utf-8"
            )

            with self.assertRaises(ConfigurationError):
                load_players_file(players_file)

    def test_load_players_file_rejects_duplicate_keys(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            players_file = Path(temporary_directory) / "players.json"
            players_file.write_text(
                f'{{"{PLAYER_ID}": "One", "{PLAYER_ID}": "Two"}}',
                encoding="utf-8",
            )

            with self.assertRaises(ConfigurationError):
                load_players_file(players_file)

    def test_load_players_file_rejects_empty_object(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            players_file = Path(temporary_directory) / "players.json"
            players_file.write_text("{}", encoding="utf-8")

            with self.assertRaises(ConfigurationError):
                load_players_file(players_file)

    def test_format_duration(self) -> None:
        self.assertEqual(format_duration(125), "2м 5с")


class PollingTests(unittest.TestCase):
    def test_first_run_creates_baseline_without_sending(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            state_file = Path(temporary_directory) / "state.json"
            telegram = FakeTelegram()

            result = run_once(
                make_config(state_file), FakeFaceit("new-match"), telegram
            )

            self.assertEqual(result, 0)
            self.assertEqual(telegram.messages, [])
            state, _ = load_state(state_file)
            self.assertEqual(state[PLAYER_ID], "new-match")

    def test_new_match_is_sent_and_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            state_file = Path(temporary_directory) / "state.json"
            state_file.write_text(
                json.dumps({PLAYER_ID: "old-match"}), encoding="utf-8"
            )
            telegram = FakeTelegram()

            result = run_once(
                make_config(state_file), FakeFaceit("new-match"), telegram
            )

            self.assertEqual(result, 0)
            self.assertEqual(len(telegram.messages), 1)
            self.assertIn("Player&lt;One&gt;", telegram.messages[0])
            state, _ = load_state(state_file)
            self.assertEqual(state[PLAYER_ID], "new-match")

    def test_failed_telegram_send_does_not_advance_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            state_file = Path(temporary_directory) / "state.json"
            state_file.write_text(
                json.dumps({PLAYER_ID: "old-match"}), encoding="utf-8"
            )
            telegram = FakeTelegram(result=False)

            result = run_once(
                make_config(state_file), FakeFaceit("new-match"), telegram
            )

            self.assertEqual(result, 1)
            state, _ = load_state(state_file)
            self.assertEqual(state[PLAYER_ID], "old-match")


if __name__ == "__main__":
    unittest.main()
