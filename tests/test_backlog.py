import json
from pathlib import Path
import tempfile
import unittest
import importlib.util

from bot import FaceitClient, SENT_MATCHES_KEY, run_once
from test_bot import PLAYER_ID, FakeResponse, FakeTelegram, FakeFaceit, make_config

spec=importlib.util.spec_from_file_location('backlog_replay',Path(__file__).resolve().parents[1]/'deploy/replay.py')
replay=importlib.util.module_from_spec(spec);spec.loader.exec_module(replay)


class HistorySession:
    def __init__(self):
        self.history = [
            {"match_id": "newest-match", "finished_at": 1700002000, "status": "finished"},
            {"match_id": "missed-match", "finished_at": 1700001000, "status": "finished"},
            {"match_id": "baseline-match", "finished_at": 1700000000, "status": "finished"},
        ]
        self.requested_stats = []

    def get(self, url, *, params=None, **kwargs):
        if url.endswith('/history'):
            offset = (params or {}).get('offset', 0)
            limit = (params or {}).get('limit', 1)
            return FakeResponse(200, {"items": self.history[offset:offset+limit]})
        if url.endswith('/stats'):
            self.requested_stats.append(url.split('/')[-2])
            return FakeResponse(200, FakeFaceit('unused').match_stats('unused'))
        return FakeResponse(200, FakeFaceit('unused').match_details('unused'))


class BacklogTests(unittest.TestCase):
    def test_all_matches_after_saved_baseline_are_notified_oldest_first(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)/'state.json'
            state.write_text(json.dumps({PLAYER_ID: 'baseline-match'}))
            session = HistorySession()
            telegram = FakeTelegram()
            self.assertEqual(run_once(make_config(state), FaceitClient(session,'test-key',15),telegram), 0)
            self.assertEqual(len(telegram.messages), 2)
            self.assertEqual(session.requested_stats, ['missed-match','newest-match'])

    def test_sent_ledger_prevents_duplicates_when_baseline_is_rewound(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)/'state.json'
            state.write_text(json.dumps({PLAYER_ID: 'baseline-match', SENT_MATCHES_KEY:['newest-match']}))
            session=HistorySession(); telegram=FakeTelegram()
            self.assertEqual(run_once(make_config(state),FaceitClient(session,'test-key',15),telegram),0)
            self.assertEqual(session.requested_stats,['missed-match'])
            saved=json.loads(state.read_text())
            self.assertEqual(saved[PLAYER_ID],'newest-match')
            self.assertEqual(set(saved[SENT_MATCHES_KEY]),{'missed-match','newest-match'})
            self.assertEqual(run_once(make_config(state),FaceitClient(session,'test-key',15),telegram),0)
            self.assertEqual(len(telegram.messages),1)

    def test_failed_older_match_blocks_later_sends_to_preserve_order(self):
        class FailOne(FakeTelegram):
            def send_message(self,text):
                if 'missed-match/scoreboard' in text:return None
                return super().send_message(text)
        with tempfile.TemporaryDirectory() as directory:
            state=Path(directory)/'state.json'
            state.write_text(json.dumps({PLAYER_ID:'baseline-match'}))
            api=FaceitClient(HistorySession(),'test-key',15)
            self.assertEqual(run_once(make_config(state),api,FailOne()),1)
            saved=json.loads(state.read_text())
            self.assertEqual(saved[PLAYER_ID],'baseline-match')
            self.assertNotIn(SENT_MATCHES_KEY,saved)
            telegram=FakeTelegram()
            self.assertEqual(run_once(make_config(state),api,telegram),0)
            self.assertEqual(len(telegram.messages),2)
            self.assertIn('missed-match/scoreboard',telegram.messages[0])
            self.assertEqual(json.loads(state.read_text())[PLAYER_ID],'newest-match')

    def test_successful_prefix_is_not_repeated_after_later_send_failure(self):
        class FailNewest(FakeTelegram):
            def send_message(self,text):
                if 'newest-match/scoreboard' in text:return None
                return super().send_message(text)
        with tempfile.TemporaryDirectory() as directory:
            state=Path(directory)/'state.json';state.write_text(json.dumps({PLAYER_ID:'baseline-match'}))
            api=FaceitClient(HistorySession(),'key',15)
            self.assertEqual(run_once(make_config(state),api,FailNewest()),1)
            saved=json.loads(state.read_text())
            self.assertEqual(saved[PLAYER_ID],'missed-match')
            telegram=FakeTelegram()
            self.assertEqual(run_once(make_config(state),api,telegram),0)
            self.assertEqual(len(telegram.messages),1)
            self.assertIn('newest-match/scoreboard',telegram.messages[0])

    def test_pagination_reaches_cursor_on_second_page(self):
        session=HistorySession()
        session.history=[{'match_id':f'm-{i}', 'finished_at':1700000000+i,'status':'finished'} for i in range(105,0,-1)]
        session.history.append({'match_id':'baseline-match'})
        result=FaceitClient(session,'key',15).matches_since(PLAYER_ID,'cs2','baseline-match')
        self.assertTrue(result.ok)
        self.assertEqual(len(result.matches),105)
        self.assertEqual(result.matches[0][0],'m-1')
        self.assertEqual(result.matches[-1][0],'m-105')

    def test_missing_cursor_or_failed_later_page_preserves_state(self):
        class FailSecond(HistorySession):
            def get(self,url,*,params=None,**kwargs):
                if (params or {}).get('offset',0):return FakeResponse(503,{})
                return super().get(url,params=params,**kwargs)
        for session in (HistorySession(), FailSecond()):
            if isinstance(session,FailSecond):
                session.history=[{'match_id':f'm-{i}','finished_at':1700000000+i,'status':'finished'} for i in range(105,0,-1)]
            with self.subTest(session=type(session).__name__), tempfile.TemporaryDirectory() as directory:
                state=Path(directory)/'state.json';original=json.dumps({PLAYER_ID:'unavailable-cursor'})
                state.write_text(original);telegram=FakeTelegram()
                self.assertEqual(run_once(make_config(state),FaceitClient(session,'key',15),telegram),1)
                self.assertEqual(telegram.messages,[])
                self.assertEqual(state.read_text(),original)

    def test_frozen_replay_excludes_new_matches_then_regular_poll_delivers_them(self):
        with tempfile.TemporaryDirectory() as directory:
            state=Path(directory)/'state.json';state.write_text(json.dumps({PLAYER_ID:'baseline-match'}))
            session=HistorySession()
            session.history.insert(0,{'match_id':'future-match','finished_at':1700003000,'status':'finished'})
            config=make_config(state);api=FaceitClient(session,'key',15)
            snapshot={'players':{PLAYER_ID:{'matches':[{'match_id':'missed-match'},{'match_id':'newest-match'}]}}}
            selected=replay.prepare_replay(api,config,snapshot)
            telegram=FakeTelegram()
            self.assertEqual(run_once(config,selected,telegram),0)
            self.assertEqual(session.requested_stats,['missed-match','newest-match'])
            self.assertEqual(json.loads(state.read_text())[PLAYER_ID],'newest-match')
            self.assertEqual(run_once(config,api,telegram),0)
            self.assertEqual(session.requested_stats[-1],'future-match')
            self.assertEqual(len(telegram.messages),3)
