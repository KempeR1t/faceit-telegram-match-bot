from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from bot import (
    Config,
    ConfigurationError,
    FaceitRating,
    FlareSolverrClient,
    LatestMatchResult,
    TelegramClient,
    build_message,
    decode_flaresolverr_json,
    extract_faceit_ratings,
    format_faceit_rating,
    format_duration,
    load_players_file,
    load_state,
    normalize_telegram_proxy_url,
    run_once,
)


PLAYER_ID = "11111111-1111-4111-8111-111111111111"
SECOND_PLAYER_ID = "22222222-2222-4222-8222-222222222222"


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
        telegram_proxy_url=None,
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
    def test_config_loads_optional_telegram_proxy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            players_file = Path(temporary_directory) / "players.json"
            players_file.write_text(
                json.dumps({PLAYER_ID: "PlayerOne"}), encoding="utf-8"
            )
            environment = {
                "FACEIT_API_KEY": "test-faceit-key",
                "TELEGRAM_BOT_TOKEN": "test-telegram-token",
                "TELEGRAM_CHAT_ID": "123",
                "TELEGRAM_PROXY_URL": "socks5h://127.0.0.1:1080",
                "FACEIT_PLAYERS_FILE": str(players_file),
            }

            with patch.dict(os.environ, environment, clear=True):
                config = Config.from_env()

        self.assertEqual(
            config.telegram_proxy_url,
            "socks5h://127.0.0.1:1080",
        )

    def test_empty_telegram_proxy_is_disabled(self) -> None:
        self.assertIsNone(normalize_telegram_proxy_url("  "))

    def test_telegram_socks_proxy_is_accepted(self) -> None:
        proxy_url = "socks5h://127.0.0.1:1080"
        self.assertEqual(normalize_telegram_proxy_url(proxy_url), proxy_url)

    def test_invalid_telegram_proxy_is_rejected(self) -> None:
        invalid_values = (
            "ftp://127.0.0.1:1080",
            "socks5h://127.0.0.1",
            "socks5h://127.0.0.1:0",
            "socks5h://127.0.0.1:70000",
            "socks5h://127.0.0.1:1080/path",
        )
        for proxy_url in invalid_values:
            with self.subTest(proxy_url=proxy_url):
                with self.assertRaises(ConfigurationError):
                    normalize_telegram_proxy_url(proxy_url)

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
            "🔻 Swing: <code>-0.71%</code>\n",
        )

    def test_match_result_uses_first_configured_player_and_is_shown_once(self) -> None:
        match_details = {
            "started_at": 1_700_000_000,
            "finished_at": 1_700_001_000,
        }
        stats_data = {
            "rounds": [
                {
                    "round_stats": {"Map": "de_mirage", "Score": "13 / 10"},
                    "teams": [
                        {
                            "team_stats": {"Team Win": "1"},
                            "players": [
                                {
                                    "player_id": SECOND_PLAYER_ID,
                                    "player_stats": {},
                                }
                            ],
                        },
                        {
                            "team_stats": {"Team Win": "0"},
                            "players": [
                                {"player_id": PLAYER_ID, "player_stats": {}}
                            ],
                        },
                    ],
                }
            ]
        }

        message = build_message(
            "1-test-match",
            match_details,
            stats_data,
            {PLAYER_ID: "First", SECOND_PLAYER_ID: "Second"},
            "cs2",
            ZoneInfo("Europe/Moscow"),
        )

        self.assertIsNotNone(message)
        assert message is not None
        result_line = "🏁 Результат: 🔴 <b>ПОРАЖЕНИЕ</b> 😡"
        self.assertEqual(message.count("🏁 Результат:"), 1)
        self.assertIn(result_line, message)
        self.assertLess(message.index("(длительность:"), message.index(result_line))
        self.assertLess(message.index(result_line), message.index("👤"))
        self.assertNotIn("ВЫИГРАЛ", message)
        self.assertNotIn("ПРОИГРАЛ", message)
        self.assertIn(
            'href="https://www.faceit.com/ru/cs2/room/1-test-match/scoreboard"',
            message,
        )

    def test_players_are_sorted_by_rating_with_kd_fallback(self) -> None:
        match_details = {
            "started_at": 1_700_000_000,
            "finished_at": 1_700_001_000,
        }
        stats_data = {
            "rounds": [
                {
                    "round_stats": {"Map": "de_mirage", "Score": "13 / 10"},
                    "teams": [
                        {
                            "team_stats": {"Team Win": "1"},
                            "players": [
                                {
                                    "player_id": PLAYER_ID,
                                    "player_stats": {"K/D Ratio": "2.00"},
                                },
                                {
                                    "player_id": SECOND_PLAYER_ID,
                                    "player_stats": {"K/D Ratio": "0.80"},
                                },
                            ],
                        }
                    ],
                }
            ]
        }
        players = {PLAYER_ID: "HighKD", SECOND_PLAYER_ID: "HighRating"}

        message_by_rating = build_message(
            "1-test-match",
            match_details,
            stats_data,
            players,
            "cs2",
            ZoneInfo("Europe/Moscow"),
            {
                PLAYER_ID: FaceitRating(rating=1.10, swing=0.0),
                SECOND_PLAYER_ID: FaceitRating(rating=1.50, swing=0.0),
            },
        )
        message_by_kd = build_message(
            "1-test-match",
            match_details,
            stats_data,
            players,
            "cs2",
            ZoneInfo("Europe/Moscow"),
        )

        self.assertIsNotNone(message_by_rating)
        self.assertIsNotNone(message_by_kd)
        assert message_by_rating is not None
        assert message_by_kd is not None
        self.assertLess(
            message_by_rating.index("👤 <b>HighRating</b>"),
            message_by_rating.index("👤 <b>HighKD</b>"),
        )
        self.assertLess(
            message_by_kd.index("👤 <b>HighKD</b>"),
            message_by_kd.index("👤 <b>HighRating</b>"),
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

    def test_no_new_match_does_not_contact_flaresolverr(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            state_file = Path(temporary_directory) / "state.json"
            state_file.write_text(
                json.dumps({PLAYER_ID: "current-match"}), encoding="utf-8"
            )
            session = FakeHTTPSession([])
            flaresolverr = FlareSolverrClient(
                session,
                "http://127.0.0.1:8191/v1",
                120000,
                cleanup_stale_sessions=True,
            )

            with self.assertLogs("faceit_match_bot", level="INFO") as logs:
                result = run_once(
                    make_config(state_file),
                    FakeFaceit("current-match"),
                    FakeTelegram(),
                    flaresolverr,
                )
                flaresolverr.close()

            self.assertEqual(result, 0)
            self.assertEqual(session.requests, [])
            self.assertEqual(
                logs.output,
                ["INFO:faceit_match_bot:No new matches found."],
            )

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

    def test_match_younger_than_fifteen_minutes_is_deferred(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            state_file = Path(temporary_directory) / "state.json"
            state_file.write_text(
                json.dumps({PLAYER_ID: "old-match"}), encoding="utf-8"
            )
            telegram = FakeTelegram()
            flaresolverr = FakeFlareSolverr(
                {PLAYER_ID: FaceitRating(rating=1.5522096, swing=0.07237932)}
            )

            with patch("bot.time.time", return_value=1_700_001_000 + 14 * 60):
                result = run_once(
                    make_config(state_file),
                    FakeFaceit("new-match"),
                    telegram,
                    flaresolverr,
                )

            self.assertEqual(result, 0)
            self.assertEqual(telegram.messages, [])
            self.assertEqual(flaresolverr.requests, [])
            state, _ = load_state(state_file)
            self.assertEqual(state[PLAYER_ID], "old-match")

    def test_fifteen_minute_old_match_is_processed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            state_file = Path(temporary_directory) / "state.json"
            state_file.write_text(
                json.dumps({PLAYER_ID: "old-match"}), encoding="utf-8"
            )
            telegram = FakeTelegram()
            flaresolverr = FakeFlareSolverr(
                {PLAYER_ID: FaceitRating(rating=1.5522096, swing=0.07237932)}
            )

            with patch("bot.time.time", return_value=1_700_001_000 + 15 * 60):
                result = run_once(
                    make_config(state_file),
                    FakeFaceit("new-match"),
                    telegram,
                    flaresolverr,
                )

            self.assertEqual(result, 0)
            self.assertEqual(len(telegram.messages), 1)
            self.assertEqual(flaresolverr.requests, [("new-match", "cs2")])
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
                "💚 Swing: <code>+7.24%</code>"
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


class TelegramClientTests(unittest.TestCase):
    def test_sends_only_telegram_request_through_configured_proxy(self) -> None:
        session = FakeHTTPSession([FakeResponse(200, {"ok": True})])
        proxy_url = "socks5h://127.0.0.1:1080"
        client = TelegramClient(
            session,
            "test-token",
            "123",
            15,
            proxy_url,
        )

        self.assertTrue(client.send_message("test"))
        self.assertEqual(
            session.requests[0]["proxies"],
            {"https": proxy_url},
        )

    def test_direct_telegram_request_has_no_proxy_option(self) -> None:
        session = FakeHTTPSession([FakeResponse(200, {"ok": True})])
        client = TelegramClient(session, "test-token", "123", 15)

        self.assertTrue(client.send_message("test"))
        self.assertNotIn("proxies", session.requests[0])


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
            ["sessions.create", "request.get", "sessions.destroy"],
        )
        self.assertIn("scoreboard-summary", session.requests[1]["json"]["url"])

    def test_retries_http_500_in_the_same_session(self) -> None:
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
                FakeResponse(200, {"status": "ok", "session": "test-session"}),
                FakeResponse(
                    500,
                    {
                        "status": "error",
                        "message": "Error solving the challenge. Timeout.",
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

        with patch("bot.time.sleep") as sleep:
            ratings = client.match_ratings("1-test-match", "cs2")
        client.close()

        self.assertEqual(
            ratings[PLAYER_ID], FaceitRating(rating=1.5522096, swing=0.07237932)
        )
        self.assertEqual(sleep.call_args.args, (5.0,))
        first_request = session.requests[1]["json"]
        second_request = session.requests[2]["json"]
        self.assertEqual(first_request["session"], "test-session")
        self.assertEqual(second_request["session"], "test-session")
        self.assertEqual(first_request["url"], second_request["url"])

    def test_waits_in_the_same_session_until_rating_is_ready(self) -> None:
        empty_scoreboard = {"payload": {"cs2": {"teams": []}}}
        ready_scoreboard = {
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
                FakeResponse(200, {"status": "ok", "session": "test-session"}),
                FakeResponse(
                    200,
                    {
                        "status": "ok",
                        "solution": {
                            "status": 200,
                            "response": json.dumps(empty_scoreboard),
                        },
                    },
                ),
                FakeResponse(
                    200,
                    {
                        "status": "ok",
                        "solution": {
                            "status": 200,
                            "response": json.dumps(ready_scoreboard),
                        },
                    },
                ),
                FakeResponse(200, {"status": "ok", "message": "removed"}),
            ]
        )
        client = FlareSolverrClient(session, "http://127.0.0.1:8191/v1", 120000)

        with patch("bot.time.sleep") as sleep:
            ratings = client.match_ratings("1-test-match", "cs2")
        client.close()

        self.assertEqual(
            ratings[PLAYER_ID], FaceitRating(rating=1.5522096, swing=0.07237932)
        )
        sleep.assert_called_once_with(60.0)
        scoreboard_requests = [
            request["json"]
            for request in session.requests
            if request["json"]["cmd"] == "request.get"
        ]
        self.assertEqual(len(scoreboard_requests), 2)
        self.assertEqual(scoreboard_requests[0]["session"], "test-session")
        self.assertEqual(scoreboard_requests[1]["session"], "test-session")
        self.assertEqual(
            scoreboard_requests[0]["url"], scoreboard_requests[1]["url"]
        )

    def test_stops_waiting_after_ten_empty_scoreboards(self) -> None:
        empty_scoreboard = {"payload": {"cs2": {"teams": []}}}
        responses = [
            FakeResponse(200, {"status": "ok", "session": "test-session"})
        ]
        responses.extend(
            FakeResponse(
                200,
                {
                    "status": "ok",
                    "solution": {
                        "status": 200,
                        "response": json.dumps(empty_scoreboard),
                    },
                },
            )
            for _ in range(10)
        )
        responses.append(FakeResponse(200, {"status": "ok", "message": "removed"}))
        session = FakeHTTPSession(responses)
        client = FlareSolverrClient(session, "http://127.0.0.1:8191/v1", 120000)

        with patch("bot.time.sleep") as sleep:
            self.assertEqual(client.match_ratings("1-test-match", "cs2"), {})
        client.close()

        self.assertEqual(sleep.call_count, 9)
        self.assertTrue(
            all(call.args == (60.0,) for call in sleep.call_args_list)
        )
        commands = [request["json"]["cmd"] for request in session.requests]
        self.assertEqual(commands.count("request.get"), 10)

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

        with patch("bot.time.sleep") as sleep:
            self.assertEqual(client.match_ratings("1-test-match", "cs2"), {})
        client.close()

        self.assertEqual(sleep.call_args.args, (5.0,))
        commands = [request["json"]["cmd"] for request in session.requests]
        self.assertEqual(
            commands,
            ["sessions.create", "request.get", "request.get", "sessions.destroy"],
        )


if __name__ == "__main__":
    unittest.main()
