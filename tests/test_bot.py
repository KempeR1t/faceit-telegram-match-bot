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
    PENDING_MESSAGES_KEY,
    StateError,
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
        self.edits: list[tuple[int, str, str]] = []
        self.edit_ok = True

    def send_message(self, text: str) -> int | None:
        self.messages.append(text)
        return len(self.messages) if self.result else None

    def edit_message(self, message_id: int, text: str, *, chat_id: str) -> bool:
        self.edits.append((message_id, text, chat_id))
        return self.edit_ok


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

    def test_table_escapes_external_values_and_contains_all_columns(self) -> None:
        faceit = FakeFaceit("test-match")
        stats = faceit.match_stats("test-match")
        stats["rounds"][0]["round_stats"]["Map"] = "<b>map</b>"
        stats["rounds"][0]["teams"][0]["players"][0]["player_stats"]["ADR"] = "<bad>"
        message = build_message(
            "test-match", faceit.match_details("test-match"), stats,
            {PLAYER_ID: "<b>Player & One</b>"}, "cs2", ZoneInfo("UTC"),
            {PLAYER_ID: FaceitRating(1.92, 0.093)},
        )
        self.assertIn("<table bordered striped compact>", message)
        upper, details = message.split("<details>")
        self.assertIn("<th>Игрок</th><th>Rating</th><th>Swing</th><th>K/D</th>", upper)
        self.assertNotIn("<th>ADR</th>", upper)
        self.assertNotIn("<th>K/D Ratio</th>", upper)
        self.assertIn("<summary>K/D Ratio, ADR и MVP</summary>", details)
        self.assertIn("<th>Игрок</th><th>K/D Ratio</th><th>ADR</th><th>MVP</th>", details)
        self.assertNotIn("<th>K/D</th>", details)
        self.assertIn("<td>&lt;b&gt;Player &amp; One&lt;/b&gt;</td>", message)
        self.assertIn("<td>🟠 1.92</td><td>💚 +9.30%</td><td>20/10</td>", message)
        self.assertIn("<td>&lt;bad&gt;</td>", message)
        self.assertNotIn("<b>map</b>", message)
        self.assertEqual(message.count("<tr>"), 4)

    def test_table_missing_ratings_use_merged_status_or_dashes(self) -> None:
        faceit = FakeFaceit("test-match")
        args = (
            "test-match", faceit.match_details("test-match"),
            faceit.match_stats("test-match"), {PLAYER_ID: "Player"},
            "cs2", ZoneInfo("UTC"),
        )
        waiting = build_message(*args, rating_status="⏳ Rating и Swing рассчитываются")
        self.assertIn('<td colspan="2">⏳ Rating и Swing рассчитываются</td>', waiting)
        self.assertIn("<td>20/10</td>", waiting)
        self.assertIn("<td>—</td><td>—</td>", build_message(*args))

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
            "<td>⚪ 1.07</td><td>🔻 -0.71%</td>",
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
        self.assertLess(message.index("⏱"), message.index(result_line))
        self.assertLess(message.index(result_line), message.index("<table"))
        self.assertNotIn("ВЫИГРАЛ", message)
        self.assertNotIn("ПРОИГРАЛ", message)
        self.assertIn(
            'href="https://www.faceit.com/ru/cs2/room/1-test-match/scoreboard"',
            message,
        )

    def test_rating_markers_follow_displayed_value_at_boundaries(self) -> None:
        cases = [
            (0.0, "🔴", "0.00"),
            (0.99, "🔴", "0.99"),
            (1.0, "⚪", "1.00"),
            (1.29, "⚪", "1.29"),
            (1.30, "🟢", "1.30"),
            (1.79, "🟢", "1.79"),
            (1.80, "🟠", "1.80"),
            (2.1, "🟠", "2.10"),
            (0.999, "⚪", "1.00"),
            (1.299, "🟢", "1.30"),
            (1.799, "🟠", "1.80"),
        ]
        for value, marker, displayed in cases:
            with self.subTest(value=value):
                self.assertEqual(
                    format_faceit_rating(FaceitRating(value, 0.0)),
                    f"<td>{marker} {displayed}</td><td>+0.00%</td>",
                )
        self.assertEqual(format_faceit_rating(None), "<td>—</td><td>—</td>")

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
            message_by_rating.index("<td>HighRating</td>"),
            message_by_rating.index("<td>HighKD</td>"),
        )
        self.assertLess(
            message_by_kd.index("<td>HighKD</td>"),
            message_by_kd.index("<td>HighRating</td>"),
        )
        for message, first, second in (
            (message_by_rating, "HighRating", "HighKD"),
            (message_by_kd, "HighKD", "HighRating"),
        ):
            details = message.split("<details>")[1]
            self.assertLess(details.index(first), details.index(second))


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

    def test_recent_match_is_sent_immediately(self) -> None:
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
            self.assertEqual(len(telegram.messages), 1)
            self.assertEqual(flaresolverr.requests, [("new-match", "cs2")])
            state, _ = load_state(state_file)
            self.assertEqual(state[PLAYER_ID], "new-match")

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
                "<td>🟢 1.55</td><td>💚 +7.24%</td>"
            )
            self.assertIn(rating_line, message)
            self.assertLess(message.index(rating_line), message.index("<td>20/10</td>"))
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
            self.assertNotIn("Rating: <code>", telegram.messages[0])
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


class PendingMessageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "state.json"
        self.path.write_text(json.dumps({PLAYER_ID: "old-match"}), encoding="utf-8")
        self.config = make_config(self.path)
        self.telegram = FakeTelegram()
        self.provider = FakeFlareSolverr({})
        self.faceit = FakeFaceit("new-match")

    def run_at(self, timestamp: float) -> int:
        with patch("bot.time.time", return_value=timestamp):
            return run_once(self.config, self.faceit, self.telegram, self.provider)

    def test_updates_original_message_after_restart_without_resending(self) -> None:
        self.assertEqual(self.run_at(10000), 0)
        self.assertIn("⏳ Rating и Swing рассчитываются", self.telegram.messages[0])
        state, _ = load_state(self.path)
        self.assertEqual(state[PLAYER_ID], "new-match")
        self.assertEqual(state[PENDING_MESSAGES_KEY]["new-match"]["message_id"], 1)
        self.provider = FakeFlareSolverr({PLAYER_ID: FaceitRating(1.5, 0.0)})
        self.assertEqual(self.run_at(10900), 0)
        self.assertEqual(len(self.telegram.messages), 1)
        self.assertEqual(self.telegram.edits[0][0], 1)
        self.assertIn("<td>+0.00%</td>", self.telegram.edits[0][1])
        self.assertNotIn("рассчитываются", self.telegram.edits[0][1])
        state, _ = load_state(self.path)
        self.assertEqual(state[PENDING_MESSAGES_KEY], {})
        self.run_at(11800)
        self.assertEqual(len(self.telegram.edits), 1)

    def test_new_match_does_not_replace_pending_old_match(self) -> None:
        self.run_at(10000)
        self.faceit = FakeFaceit("next-match")
        self.run_at(10900)
        state, _ = load_state(self.path)
        self.assertEqual(set(state[PENDING_MESSAGES_KEY]), {"new-match", "next-match"})
        self.provider.ratings = {PLAYER_ID: FaceitRating(1.5, 0.03)}
        self.run_at(11800)
        self.assertEqual(len(self.telegram.messages), 2)
        self.assertEqual({edit[0] for edit in self.telegram.edits}, {1, 2})
        self.assertEqual(load_state(self.path)[0][PENDING_MESSAGES_KEY], {})

    def test_legacy_text_pending_message_becomes_table_without_resending(self) -> None:
        self.run_at(10000)
        state, _ = load_state(self.path)
        state[PENDING_MESSAGES_KEY]["new-match"]["last_text"] = (
            "🎮 <b>Матч на FACEIT завершён!</b>\n• ⏳ Rating и Swing рассчитываются"
        )
        self.path.write_text(json.dumps(state), encoding="utf-8")
        self.assertEqual(self.run_at(10900), 0)
        self.assertEqual(len(self.telegram.messages), 1)
        self.assertEqual(self.telegram.edits[0][0], 1)
        self.assertIn("<table bordered striped compact>", self.telegram.edits[0][1])
        self.assertIn("new-match", load_state(self.path)[0][PENDING_MESSAGES_KEY])

    def test_unchanged_pending_message_is_not_edited(self) -> None:
        self.run_at(10000)
        self.run_at(10900)
        self.assertEqual(self.telegram.edits, [])
        self.assertEqual(len(self.telegram.messages), 1)

    def test_complete_initial_ratings_do_not_create_update_job(self) -> None:
        self.provider.ratings = {PLAYER_ID: FaceitRating(1.5, 0.0)}
        self.run_at(10000)
        self.assertNotIn("рассчитываются", self.telegram.messages[0])
        self.assertNotIn(PENDING_MESSAGES_KEY, load_state(self.path)[0])
        self.run_at(10900)
        self.assertEqual(len(self.provider.requests), 1)

    def test_failed_send_does_not_create_update_job(self) -> None:
        self.telegram.result = False
        self.assertEqual(self.run_at(10000), 1)
        state, _ = load_state(self.path)
        self.assertEqual(state[PLAYER_ID], "old-match")
        self.assertNotIn(PENDING_MESSAGES_KEY, state)

    def test_disabled_flaresolverr_does_not_add_waiting_marker(self) -> None:
        self.provider = None
        self.run_at(10000)
        self.assertNotIn("рассчитываются", self.telegram.messages[0])
        self.assertNotIn(PENDING_MESSAGES_KEY, load_state(self.path)[0])

    def test_expiry_stops_rating_requests_but_retries_failed_final_edit(self) -> None:
        self.run_at(10000)
        self.provider.requests.clear()
        self.telegram.edit_ok = False
        self.assertEqual(self.run_at(17200), 1)
        self.assertEqual(self.provider.requests, [])
        self.assertIn("недоступны", self.telegram.edits[-1][1])
        self.assertIn("new-match", load_state(self.path)[0][PENDING_MESSAGES_KEY])
        self.telegram.edit_ok = True
        self.assertEqual(self.run_at(18100), 0)
        self.assertEqual(self.provider.requests, [])
        self.assertEqual(load_state(self.path)[0][PENDING_MESSAGES_KEY], {})

    def test_failed_edit_preserves_ratings_for_next_run(self) -> None:
        self.run_at(10000)
        self.provider.ratings = {PLAYER_ID: FaceitRating(1.5, 0.03)}
        self.telegram.edit_ok = False
        self.assertEqual(self.run_at(10900), 1)
        self.provider.ratings = {}
        self.telegram.edit_ok = True
        self.assertEqual(self.run_at(11800), 0)
        self.assertIn("<td>🟢 1.50</td><td>💚 +3.00%</td>", self.telegram.edits[-1][1])
        self.assertEqual(len(self.telegram.messages), 1)
        self.assertEqual(load_state(self.path)[0][PENDING_MESSAGES_KEY], {})

    def test_partial_ratings_accumulate_and_resort_players(self) -> None:
        from dataclasses import replace
        self.config = replace(self.config, players={
            PLAYER_ID: "First", SECOND_PLAYER_ID: "Second",
        })
        self.path.write_text(json.dumps({
            PLAYER_ID: "old-match", SECOND_PLAYER_ID: "old-match",
        }), encoding="utf-8")
        stats = self.faceit.match_stats("new-match")
        stats["rounds"][0]["teams"][0]["players"].append({
            "player_id": SECOND_PLAYER_ID, "player_stats": {"K/D Ratio": "0.5"},
        })
        with patch.object(self.faceit, "match_stats", return_value=stats):
            self.run_at(10000)
        self.provider.ratings = {SECOND_PLAYER_ID: FaceitRating(3.0, 0.0)}
        self.run_at(10900)
        text = self.telegram.edits[-1][1]
        self.assertLess(text.index("<td>Second</td>"), text.index("<td>First</td>"))
        self.assertEqual(text.count("рассчитываются"), 1)
        self.provider.ratings = {PLAYER_ID: FaceitRating(1.1, -0.01)}
        self.run_at(11800)
        text = self.telegram.edits[-1][1]
        self.assertIn("<td>🟠 3.00</td>", text)
        self.assertIn("<td>⚪ 1.10</td>", text)
        self.assertNotIn("рассчитываются", text)
        self.assertEqual(load_state(self.path)[0][PENDING_MESSAGES_KEY], {})

    def test_queue_keeps_original_chat_after_config_changes(self) -> None:
        from dataclasses import replace
        original_chat = self.config.telegram_chat_id
        self.run_at(10000)
        self.config = replace(self.config, telegram_chat_id="another-chat")
        self.provider.ratings = {PLAYER_ID: FaceitRating(1.5, 0.0)}
        self.run_at(10900)
        self.assertEqual(self.telegram.edits[-1][2], original_chat)

    def test_corrupt_queue_is_rejected_before_network_calls(self) -> None:
        self.path.write_text(json.dumps({PENDING_MESSAGES_KEY: {"bad": {}}}))
        with self.assertRaises(StateError):
            self.run_at(10000)
        self.assertEqual(self.telegram.messages, [])
        self.assertEqual(self.provider.requests, [])

    def test_overlapping_run_is_skipped(self) -> None:
        import fcntl
        with self.path.with_suffix(".json.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(self.run_at(10000), 0)
        self.assertEqual(self.telegram.messages, [])
        self.assertEqual(self.provider.requests, [])


class TelegramClientTests(unittest.TestCase):
    def test_send_returns_message_id(self) -> None:
        session = FakeHTTPSession([FakeResponse(200, {
            "ok": True, "result": {"message_id": 123},
        })])
        client = TelegramClient(session, "test-token", "123", 15)
        self.assertEqual(client.send_message("test"), 123)
        self.assertTrue(session.requests[0]["url"].endswith("/sendRichMessage"))
        self.assertEqual(session.requests[0]["json"]["rich_message"], {"html": "test"})
        self.assertNotIn("text", session.requests[0]["json"])
        self.assertNotIn("parse_mode", session.requests[0]["json"])

    def test_edit_uses_saved_destination_and_proxy(self) -> None:
        session = FakeHTTPSession([FakeResponse(200, {"ok": True})])
        client = TelegramClient(session, "test-token", "new-chat", 15,
                                "socks5h://127.0.0.1:1080")
        self.assertTrue(client.edit_message(99, "updated", chat_id="old-chat"))
        request = session.requests[0]
        self.assertTrue(request["url"].endswith("/editMessageText"))
        self.assertEqual(request["json"]["chat_id"], "old-chat")
        self.assertEqual(request["json"]["message_id"], 99)
        self.assertEqual(request["json"]["rich_message"], {"html": "updated"})
        self.assertNotIn("text", request["json"])
        self.assertNotIn("parse_mode", request["json"])
        self.assertEqual(request["proxies"], {"https": "socks5h://127.0.0.1:1080"})

    def test_already_applied_edit_is_success(self) -> None:
        session = FakeHTTPSession([FakeResponse(400, {
            "ok": False, "description": "Bad Request: message is not modified",
        })])
        client = TelegramClient(session, "test-token", "123", 15)
        self.assertTrue(client.edit_message(99, "same", chat_id="123"))

    def test_sends_only_telegram_request_through_configured_proxy(self) -> None:
        session = FakeHTTPSession([FakeResponse(200, {"ok": True, "result": {"message_id": 42}})])
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
        session = FakeHTTPSession([FakeResponse(200, {"ok": True, "result": {"message_id": 42}})])
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

    def test_empty_scoreboard_returns_without_waiting(self) -> None:
        session = FakeHTTPSession([
            FakeResponse(200, {"status": "ok", "session": "test-session"}),
            FakeResponse(200, {"status": "ok", "solution": {
                "status": 200, "response": '{"payload":{"cs2":{"teams":[]}}}'
            }}),
            FakeResponse(200, {"status": "ok"}),
        ])
        client = FlareSolverrClient(session, "http://127.0.0.1:8191/v1", 120000)
        with patch("bot.time.sleep") as sleep:
            self.assertEqual(client.match_ratings("new-match", "cs2"), {})
        client.close()
        sleep.assert_not_called()
        self.assertEqual(
            [r["json"]["cmd"] for r in session.requests],
            ["sessions.create", "request.get", "sessions.destroy"],
        )

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
