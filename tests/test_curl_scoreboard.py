import copy
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from curl_cffi import requests as curl_requests

from bot import PENDING_MESSAGES_KEY, build_scoreboard_client, run_once
from test_bot import PLAYER_ID, FakeFaceit, FakeHTTPSession, FakeResponse, FakeTelegram, make_config
from test_stats_fallback import MissingStatsFaceit, scoreboard_case, solution


class FakeCurlSession:
    def __init__(self, responses):
        self.responses = responses
        self.requests = []
        self.closed = False

    def get(self, url, **kwargs):
        self.requests.append({"url": url, **kwargs})
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def close(self):
        self.closed = True


class InvalidJSONResponse:
    status_code = 200

    def json(self):
        raise ValueError("Invalid JSON")


def browser_responses(match_id):
    return [
        FakeResponse(200, {"status": "ok", "sessions": []}),
        FakeResponse(200, {"status": "ok", "session": "private"}),
        solution(scoreboard_case(match_id)[1]),
        FakeResponse(200, {"status": "ok"}),
        FakeResponse(200, {"status": "ok", "sessions": []}),
    ]


class CurlScoreboardTests(unittest.TestCase):
    def test_direct_scoreboard_delivers_missing_stats_and_ratings_in_order(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state.json"
            state.write_text(json.dumps({PLAYER_ID: "baseline"}))
            browser = FakeHTTPSession([])
            direct = FakeCurlSession([
                FakeResponse(200, scoreboard_case("missing-match")[1]),
                FakeResponse(200, scoreboard_case("next-match")[1]),
            ])
            telegram = FakeTelegram()
            with patch("bot.curl_requests.Session", return_value=direct):
                config = make_config(state)
                provider = build_scoreboard_client(config, browser)
                result = run_once(config, MissingStatsFaceit("unused"), telegram, provider)

            self.assertEqual(result, 0)
            self.assertEqual(len(telegram.messages), 2)
            self.assertIn("missing-match/scoreboard", telegram.messages[0])
            self.assertIn("next-match/scoreboard", telegram.messages[1])
            self.assertIn("12/28", telegram.messages[0])
            self.assertIn("🔻 -6.15%", telegram.messages[0])
            self.assertEqual(json.loads(state.read_text())[PLAYER_ID], "next-match")
            self.assertEqual(len(direct.requests), 2)
            self.assertEqual(browser.requests, [])
            self.assertTrue(direct.closed)
            self.assertTrue(direct.requests[0]["url"].startswith("https://api.faceit.com/"))

    def test_failed_or_invalid_direct_response_falls_back_and_delivers(self):
        cases = [
            FakeResponse(403, {}),
            FakeResponse(503, {}),
            InvalidJSONResponse(),
            FakeResponse(200, {"errors": [{"code": "err_f0"}]}),
            FakeResponse(200, scoreboard_case("another-match")[1]),
            curl_requests.RequestsError("Connection failed"),
        ]
        for response in cases:
            with self.subTest(response=type(response).__name__), tempfile.TemporaryDirectory() as directory:
                state = Path(directory) / "state.json"
                state.write_text(json.dumps({PLAYER_ID: "baseline"}))
                browser = FakeHTTPSession(browser_responses("match"))
                direct = FakeCurlSession([response])
                telegram = FakeTelegram()
                with patch("bot.curl_requests.Session", return_value=direct):
                    config = make_config(state)
                    provider = build_scoreboard_client(config, browser)
                    result = run_once(config, FakeFaceit("match"), telegram, provider)

                self.assertEqual(result, 0)
                self.assertEqual(len(telegram.messages), 1)
                self.assertIn("🔻 -6.15%", telegram.messages[0])
                self.assertEqual(json.loads(state.read_text())[PLAYER_ID], "match")
                commands = [request["json"]["cmd"] for request in browser.requests]
                self.assertEqual(commands.count("request.get"), 1)
                self.assertEqual(commands.count("sessions.destroy"), 1)
                self.assertTrue(direct.closed)

    def test_disabled_browser_preserves_cache_and_pauses_missing_stats(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state.json"
            original = json.dumps({PLAYER_ID: "baseline"})
            state.write_text(original)
            cache_file = state.with_name("faceit_ratings.json")
            cached = json.dumps({"cs2/missing-match": {
                PLAYER_ID: {"rating": 1.8, "swing": 0.05},
            }})
            cache_file.write_text(cached)
            browser = FakeHTTPSession([])
            direct = FakeCurlSession([FakeResponse(403, {}), FakeResponse(403, {})])
            config = replace(make_config(state), flaresolverr_enabled=False)
            telegram = FakeTelegram()
            with patch("bot.curl_requests.Session", return_value=direct):
                provider = build_scoreboard_client(config, browser)
                ratings = provider.match_ratings("missing-match", "cs2")
                result = run_once(config, MissingStatsFaceit("unused"), telegram, provider)

            self.assertEqual(ratings[PLAYER_ID].rating, 1.8)
            self.assertEqual(result, 1)
            self.assertEqual(telegram.messages, [])
            self.assertEqual(state.read_text(), original)
            self.assertEqual(cache_file.read_text(), cached)
            self.assertEqual(browser.requests, [])

    def test_pending_message_receives_ratings_on_a_later_direct_lookup(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state.json"
            state.write_text(json.dumps({PLAYER_ID: "baseline"}))
            payload = copy.deepcopy(scoreboard_case("match")[1])
            for team in payload["payload"]["cs2"]["teams"]:
                for player in team["players"]:
                    player["stats"].pop("faceit_rating")
                    player["stats"].pop("faceit_rating_swing")
            browser = FakeHTTPSession([])
            telegram = FakeTelegram()
            config = make_config(state)
            for response in [payload, scoreboard_case("match")[1]]:
                direct = FakeCurlSession([FakeResponse(200, response)])
                with patch("bot.curl_requests.Session", return_value=direct):
                    provider = build_scoreboard_client(config, browser)
                    self.assertEqual(run_once(config, FakeFaceit("match"), telegram, provider), 0)

            self.assertEqual(len(telegram.messages), 1)
            self.assertIn("рассчитываются", telegram.messages[0])
            self.assertEqual(len(telegram.edits), 1)
            self.assertIn("🔻 -6.15%", telegram.edits[0][1])
            self.assertEqual(json.loads(state.read_text())[PENDING_MESSAGES_KEY], {})
            self.assertEqual(browser.requests, [])

    def test_transport_flags_allow_legacy_mode_and_disable_scoreboard(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state.json"
            config = replace(make_config(state), curl_cffi_enabled=False)
            browser = FakeHTTPSession(browser_responses("match"))
            with patch("bot.curl_requests.Session") as direct_factory:
                provider = build_scoreboard_client(config, browser)
                ratings = provider.match_ratings("match", "cs2")
                provider.close()
                direct_factory.assert_not_called()
            self.assertEqual(ratings[PLAYER_ID].rating, 0.728668)
            disabled = replace(config, flaresolverr_enabled=False)
            self.assertIsNone(build_scoreboard_client(disabled, browser))
