import json
import unittest
import tempfile
from pathlib import Path

from bot import FlareSolverrClient, FaceitRating
from test_bot import FakeHTTPSession, FakeResponse, PLAYER_ID
from unittest.mock import patch


class RatingRecoveryTests(unittest.TestCase):
    def test_successful_http_html_response_is_retried_for_actual_rating_json(self):
        payload={"payload":{"cs2":{"teams":[{"players":[{"player_id":PLAYER_ID,
                "stats":{"faceit_rating":1.55,"faceit_rating_swing":0.02}}]}]}}}
        session=FakeHTTPSession([
            FakeResponse(200,{"status":"ok","session":"faceit-bot-test"}),
            FakeResponse(200,{"status":"ok","solution":{"status":200,"response":"<html><body>Temporary error</body></html>"}}),
            FakeResponse(200,{"status":"ok","solution":{"status":200,"response":json.dumps(payload)}}),
        ])
        client=FlareSolverrClient(session,'http://localhost:8191/v1',120000)
        with patch('bot.time.sleep'):
            ratings=client.match_ratings('test-match','cs2')
        self.assertEqual(ratings,{PLAYER_ID:FaceitRating(1.55,0.02)})

    def test_ready_values_survive_restart_and_invalid_response(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'ratings.json'
            payload={"payload":{"cs2":{"teams":[{"players":[{"player_id":PLAYER_ID,
                    "stats":{"faceit_rating":1.55,"faceit_rating_swing":0.02}}]}]}}}
            success=FakeHTTPSession([
                FakeResponse(200,{'status':'ok','session':'one'}),
                FakeResponse(200,{'status':'ok','solution':{'status':200,'response':json.dumps(payload)}}),
            ])
            expected={PLAYER_ID:FaceitRating(1.55,0.02)}
            first=FlareSolverrClient(success,'http://localhost:8191/v1',120000,cache_file=path)
            self.assertEqual(first.match_ratings('test-match','cs2'),expected)
            self.assertEqual(path.stat().st_mode & 0o777,0o600)
            original=path.read_bytes()
            bad=FakeResponse(200,{'status':'ok','solution':{'status':200,'response':'<html>temporary error</html>'}})
            failure=FakeHTTPSession([
                FakeResponse(200,{'status':'ok','session':'two'}),bad,bad,
                FakeResponse(200,{'status':'ok'}),
            ])
            second=FlareSolverrClient(failure,'http://localhost:8191/v1',120000,cache_file=path)
            with patch('bot.time.sleep'):
                self.assertEqual(second.match_ratings('test-match','cs2'),expected)
            self.assertEqual(path.read_bytes(),original)
