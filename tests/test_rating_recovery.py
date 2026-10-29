import json
import unittest
import tempfile
import logging
from pathlib import Path

from bot import FlareSolverrClient, FaceitRating, TelegramTokenFilter
from test_bot import FakeHTTPSession, FakeResponse, PLAYER_ID
from unittest.mock import patch


class RatingRecoveryTests(unittest.TestCase):
    def test_anonymous_access_error_is_not_reported_as_empty_statistics(self):
        body = {'errors': [{'code': 'err_f0', 'message':
                            'this content is not available for anonymous users'}]}
        session = FakeHTTPSession([
            FakeResponse(200, {'status': 'ok', 'session': 'private-session'}),
            FakeResponse(200, {'status': 'ok', 'solution': {'status': 200,
                'response': json.dumps(body)}}),
            FakeResponse(200, {'status': 'ok'}),
        ])
        client = FlareSolverrClient(session, 'http://localhost:8191/v1', 120000)
        with self.assertLogs('faceit_match_bot', level='INFO') as logs, patch('bot.time.sleep') as sleep:
            self.assertEqual(client.match_ratings('restricted-match', 'cs2'), {})
        sleep.assert_not_called()
        text = '\n'.join(logs.output)
        self.assertIn('restricted-match', text)
        self.assertIn('err_f0', text)
        self.assertIn('requires a signed-in account', text)
        self.assertNotIn('Loaded FACEIT Rating', text)

    def test_retry_warning_redacts_telegram_token_in_relative_url(self):
        record = logging.LogRecord('urllib3.connectionpool', logging.WARNING,
            '', 1, 'Retrying after failure: %s',
            ('/bot123456789:EXAMPLE_NOT_A_REAL_TOKEN/sendRichMessage',), None)
        self.assertTrue(TelegramTokenFilter().filter(record))
        self.assertNotIn('EXAMPLE_NOT_A_REAL_TOKEN', record.getMessage())
        self.assertIn('/bot<redacted>/sendRichMessage', record.getMessage())

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
