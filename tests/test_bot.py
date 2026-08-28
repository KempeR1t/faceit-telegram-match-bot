from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from zoneinfo import ZoneInfo

from bot import (
    Config,
    ConfigurationError,
    FaceitRating,
    FlareSolverrClient,
    LatestMatchResult,
    decode_flaresolverr_json,
    extract_faceit_ratings,
    format_faceit_rating,
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


class FakeFlareSolverr:
    def __init__(self, ratings: dict[str, FaceitRating]) -> None:
        self.ratings = ratings
        self.requests: list[tuple[str, str]] = []

    def match_ratings(
        self, match_id: str, game_id: str
    ) -> dict[str, FaceitRating]:
        self.requests.append((match_id, game_id))
        return self.ratings


class FailingFlareSolverr:
    def match_ratings(
        self, match_id: str, game_id: str
    ) -> dict[str, FaceitRating]:
        raise RuntimeError("test failure")


class FakeResponse:
    def __init__(self, status_code: int, payload: dict) -> None:
        self.status_code = status_code
        self.payload = payload

    def json(self) -> dict:
        return self.payload


class FakeHTTPSession:
    def __init__(self, responses: list[FakeResponse]) -> None:
        self.responses = responses
        self.requests: list[dict] = []

    def post(self, url: str, **kwargs) -> FakeResponse:
        self.requests.append({"url": url, **kwargs})
        return self.responses.pop(0)


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
        flaresolverr_enabled=True,
        flaresolverr_url="http://127.0.0.1:8191/v1",
        flaresolverr_max_timeout_ms=120000,
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

    def test_extract_faceit_ratings(self) -> None:
        payload = {
            "payload": {
                "cs2": {
                    "teams": [
                        {
                            "players": [
                                {
                                    "player_id": PLAYER_ID,
                                    "stats": {
                                        "faceit_rating": 1.5522096,
                                        "faceit_rating_swing": 0.07237932,
                                    },
                                }
                            ]
                        }
                    ]
                }
            }
        }

        self.assertEqual(
            extract_faceit_ratings(payload, "cs2")[PLAYER_ID],
            FaceitRating(rating=1.5522096, swing=0.07237932),
        )

    def test_decode_flaresolverr_json_from_browser_pre(self) -> None:
        body = '<html><body><pre>{&quot;payload&quot;: {}}</pre></body></html>'
        self.assertEqual(decode_flaresolverr_json(body), {"payload": {}})

    def test_format_faceit_rating_marks_negative_swing(self) -> None:
        line = format_faceit_rating(FaceitRating(rating=1.0737851, swing=-0.0070559285))

        self.assertEqual(
            line,
            "• Rating: <code>1.07</code> | "
            "🔴 Swing: <code>-0.71%</code>\n",
        )


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

    def test_optional_rating_is_rendered_before_kills(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            state_file = Path(temporary_directory) / "state.json"
            state_file.write_text(
                json.dumps({PLAYER_ID: "old-match"}), encoding="utf-8"
            )
            telegram = FakeTelegram()
            flaresolverr = FakeFlareSolverr(
                {PLAYER_ID: FaceitRating(rating=1.5522096, swing=0.07237932)}
            )

            result = run_once(
                make_config(state_file),
                FakeFaceit("new-match"),
                telegram,
                flaresolverr,
            )

            self.assertEqual(result, 0)
            message = telegram.messages[0]
            rating_line = (
                "• Rating: <code>1.55</code> | "
                "Swing: <code>+7.24%</code>"
            )
            self.assertIn(rating_line, message)
            self.assertLess(message.index(rating_line), message.index("• Kills:"))
            self.assertEqual(flaresolverr.requests, [("new-match", "cs2")])

    def test_missing_optional_rating_does_not_block_message(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            state_file = Path(temporary_directory) / "state.json"
            state_file.write_text(
                json.dumps({PLAYER_ID: "old-match"}), encoding="utf-8"
            )
            telegram = FakeTelegram()

            result = run_once(
                make_config(state_file),
                FakeFaceit("new-match"),
                telegram,
                FakeFlareSolverr({}),
            )

            self.assertEqual(result, 0)
            self.assertEqual(len(telegram.messages), 1)
            self.assertNotIn("Rating:", telegram.messages[0])
            self.assertNotIn("Swing:", telegram.messages[0])

    def test_unexpected_optional_rating_error_does_not_block_message(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            state_file = Path(temporary_directory) / "state.json"
            state_file.write_text(
                json.dumps({PLAYER_ID: "old-match"}), encoding="utf-8"
            )
            telegram = FakeTelegram()

            result = run_once(
                make_config(state_file),
                FakeFaceit("new-match"),
                telegram,
                FailingFlareSolverr(),
            )

            self.assertEqual(result, 0)
            self.assertEqual(len(telegram.messages), 1)
            self.assertNotIn("Rating:", telegram.messages[0])

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


class FlareSolverrClientTests(unittest.TestCase):
    def test_fetches_scoreboard_in_private_session(self) -> None:
        scoreboard = {
            "payload": {
                "cs2": {
                    "teams": [
                        {
                            "players": [
                                {
                                    "player_id": PLAYER_ID,
                                    "stats": {
                                        "faceit_rating": 1.5522096,
                                        "faceit_rating_swing": 0.07237932,
                                    },
                                }
                            ]
                        }
                    ]
                }
            }
        }
        session = FakeHTTPSession(
            [
                FakeResponse(
                    200,
                    {
                        "status": "ok",
                        "message": "Session created successfully.",
                        "session": "test-session",
                    },
                ),
                FakeResponse(
                    200,
                    {
                        "status": "ok",
                        "solution": {"status": 200, "response": "<html></html>"},
                    },
                ),
                FakeResponse(
                    200,
                    {
                        "status": "ok",
                        "solution": {
                            "status": 200,
                            "response": json.dumps(scoreboard),
                        },
                    },
                ),
                FakeResponse(200, {"status": "ok", "message": "removed"}),
            ]
        )
        client = FlareSolverrClient(session, "http://127.0.0.1:8191/v1", 120000)

        ratings = client.match_ratings("1-test-match", "cs2")
        client.close()

        self.assertEqual(
            ratings[PLAYER_ID], FaceitRating(rating=1.5522096, swing=0.07237932)
        )
        commands = [request["json"]["cmd"] for request in session.requests]
        self.assertEqual(
            commands,
            ["sessions.create", "request.get", "request.get", "sessions.destroy"],
        )
        self.assertEqual(session.requests[1]["json"]["url"], "https://www.faceit.com/")
        self.assertIn("scoreboard-summary", session.requests[2]["json"]["url"])

    def test_http_500_returns_no_ratings_and_cleans_up(self) -> None:
        session = FakeHTTPSession(
            [
                FakeResponse(
                    200,
                    {"status": "ok", "session": "test-session"},
                ),
                FakeResponse(
                    500,
                    {
                        "status": "error",
                        "message": "Error solving the challenge. Timeout.",
                    },
                ),
                FakeResponse(200, {"status": "ok", "message": "removed"}),
            ]
        )
        client = FlareSolverrClient(session, "http://127.0.0.1:8191/v1", 120000)

        self.assertEqual(client.match_ratings("1-test-match", "cs2"), {})
        client.close()

        commands = [request["json"]["cmd"] for request in session.requests]
        self.assertEqual(
            commands, ["sessions.create", "request.get", "sessions.destroy"]
        )


if __name__ == "__main__":
    unittest.main()
