import copy
import json
from pathlib import Path
import tempfile
import unittest
from zoneinfo import ZoneInfo

from bot import (
    FlareSolverrClient, MatchHistoryResult, PENDING_MESSAGES_KEY,
    build_message, run_once, scoreboard_match_stats,
)
from test_bot import (
    PLAYER_ID, SECOND_PLAYER_ID, FakeFaceit, FakeHTTPSession, FakeResponse,
    FakeTelegram, make_config,
)


def scoreboard_case(match_id):
    details = {
        'match_id': match_id, 'best_of': 1,
        'started_at': 1700000000, 'finished_at': 1700001000,
        'voting': {'map': {'pick': ['de_dust2']}},
        'results': {'winner': 'faction2', 'score': {'faction2': 19, 'faction1': 16}},
        'teams': {
            'faction1': {'roster': [{'player_id': SECOND_PLAYER_ID}]},
            'faction2': {'roster': [{'player_id': PLAYER_ID}]},
        },
    }
    # The website lists the winning faction2 before faction1.
    payload = {'payload': {'id': match_id, 'game': 'cs2', 'cs2': {'teams': [
        {'id': PLAYER_ID, 'players': [{'player_id': PLAYER_ID, 'stats': {
            'kills': 12, 'deaths': 28, 'kd': 12 / 28, 'adr': 54.142857,
            'mvps': 1, 'faceit_rating': .728668, 'faceit_rating_swing': -.06148173,
        }}]},
        {'id': SECOND_PLAYER_ID, 'players': [{'player_id': SECOND_PLAYER_ID, 'stats': {
            'kills': 19, 'deaths': 25, 'kd': .76, 'adr': 64.37143,
            'mvps': 2, 'faceit_rating': 1.007652, 'faceit_rating_swing': -.017115712,
        }}]},
    ]}}}
    return details, payload


def solution(payload):
    return FakeResponse(200, {'status': 'ok', 'solution': {
        'status': 200, 'response': json.dumps(payload),
    }})


class MissingStatsFaceit(FakeFaceit):
    def matches_since(self, player_id, game_id, previous_match_id):
        matches = [('missing-match', 1.0), ('next-match', 2.0)]
        if previous_match_id == 'missing-match':
            matches = matches[1:]
        elif previous_match_id == 'next-match':
            matches = []
        return MatchHistoryResult(True, tuple(matches))

    def match_details(self, match_id):
        return scoreboard_case(match_id)[0]

    def match_stats(self, match_id):
        return None if match_id == 'missing-match' else super().match_stats(match_id)


class StatsFallbackTests(unittest.TestCase):
    def test_reversed_team_order_preserves_score_winner_and_player_stats(self):
        details, payload = scoreboard_case('match')
        stats = scoreboard_match_stats(payload, details, 'cs2')
        message = build_message('match', details, stats, {PLAYER_ID: 'Player'},
                                'cs2', ZoneInfo('Europe/Moscow'))
        self.assertIn('Dust2 · 16:19', message)
        self.assertIn('ПОБЕДА', message)
        self.assertIn('<td>12/28</td><td>0.43</td><td>54.1</td><td>1</td>', message)
        payload['payload']['cs2']['teams'].reverse()
        self.assertEqual(scoreboard_match_stats(payload, details, 'cs2'), stats)

    def test_incomplete_mismatched_or_series_statistics_are_rejected(self):
        details, payload = scoreboard_case('match')
        variants = []
        for key, value in [('kills', None), ('adr', float('nan')), ('deaths', -1), ('mvps', 1.5)]:
            bad = copy.deepcopy(payload)
            bad['payload']['cs2']['teams'][0]['players'][0]['stats'][key] = value
            variants.append((details, bad))
        bad = copy.deepcopy(payload)
        bad['payload']['id'] = 'another-match'
        variants.append((details, bad))
        bad = copy.deepcopy(payload)
        bad['payload']['cs2']['teams'][0]['players'][0]['player_id'] = 'wrong-player'
        variants.append((details, bad))
        bad = copy.deepcopy(details)
        bad['voting']['map']['pick'].append('de_mirage')
        bad['best_of'] = 3
        variants.append((bad, payload))
        variants.append((details, {'errors': [{'code': 'err_f0'}]}))
        for bad_details, bad_payload in variants:
            with self.subTest(details=bad_details, payload=bad_payload):
                self.assertIsNone(scoreboard_match_stats(bad_payload, bad_details, 'cs2'))

    def test_missing_data_api_stats_do_not_block_later_matches_when_fallback_works(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / 'state.json'
            state.write_text(json.dumps({PLAYER_ID: 'baseline'}))
            session = FakeHTTPSession([
                FakeResponse(200, {'status': 'ok', 'session': 'private'}),
                solution(scoreboard_case('missing-match')[1]),
                solution(scoreboard_case('next-match')[1]),
                FakeResponse(200, {'status': 'ok'}),
            ])
            provider = FlareSolverrClient(session, 'http://localhost:8191/v1', 120000)
            telegram = FakeTelegram()
            self.assertEqual(run_once(make_config(state), MissingStatsFaceit('unused'), telegram, provider), 0)
            self.assertEqual(len(telegram.messages), 2)
            self.assertIn('missing-match/scoreboard', telegram.messages[0])
            self.assertIn('next-match/scoreboard', telegram.messages[1])
            self.assertIn('🔻 -6.15%', telegram.messages[0])
            self.assertEqual(json.loads(state.read_text())[PLAYER_ID], 'next-match')
            # Fallback statistics and Rating/Swing share one website request.
            requests = [r for r in session.requests if r['json']['cmd'] == 'request.get']
            self.assertEqual(len(requests), 2)
            self.assertEqual(len([r for r in requests if 'missing-match/' in r['json']['url']]), 1)

    def test_failed_fallback_preserves_cursor_and_does_not_send_later_match(self):
        for fallback in (None, {'payload': {}}, scoreboard_case('different-match')[1]):
            with self.subTest(fallback=fallback), tempfile.TemporaryDirectory() as directory:
                state = Path(directory) / 'state.json'
                original = json.dumps({PLAYER_ID: 'baseline'})
                state.write_text(original)
                provider = None if fallback is None else FlareSolverrClient(
                    FakeHTTPSession([
                        FakeResponse(200, {'status': 'ok', 'session': 'private'}),
                        solution(fallback), FakeResponse(200, {'status': 'ok'}),
                    ]), 'http://localhost:8191/v1', 120000,
                )
                telegram = FakeTelegram()
                self.assertEqual(run_once(make_config(state), MissingStatsFaceit('unused'), telegram, provider), 1)
                self.assertEqual(telegram.messages, [])
                self.assertEqual(state.read_text(), original)

    def test_complete_basic_stats_can_be_sent_before_ratings_are_ready(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / 'state.json'
            state.write_text(json.dumps({PLAYER_ID: 'baseline'}))
            payload = scoreboard_case('missing-match')[1]
            for team in payload['payload']['cs2']['teams']:
                for player in team['players']:
                    player['stats'].pop('faceit_rating')
                    player['stats'].pop('faceit_rating_swing')
            provider = FlareSolverrClient(FakeHTTPSession([
                FakeResponse(200, {'status': 'ok', 'session': 'private'}),
                solution(payload), solution(scoreboard_case('next-match')[1]),
                FakeResponse(200, {'status': 'ok'}),
            ]), 'http://localhost:8191/v1', 120000)
            telegram = FakeTelegram()
            self.assertEqual(run_once(make_config(state), MissingStatsFaceit('unused'), telegram, provider), 0)
            self.assertEqual(len(telegram.messages), 2)
            saved = json.loads(state.read_text())
            self.assertEqual(saved[PLAYER_ID], 'next-match')
            self.assertEqual(saved[PENDING_MESSAGES_KEY]['missing-match']['message_id'], 1)
            self.assertIn('12/28', telegram.messages[0])
            self.assertIn('рассчитываются', telegram.messages[0])
