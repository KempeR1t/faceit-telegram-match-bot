import contextlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from bot import MatchHistoryResult, build_http_session
from test_bot import PLAYER_ID, SECOND_PLAYER_ID, make_config

spec = importlib.util.spec_from_file_location(
    "deployment_preview", Path(__file__).resolve().parents[1] / "deploy/preview.py"
)
preview = importlib.util.module_from_spec(spec)
spec.loader.exec_module(preview)


class DeploymentTests(unittest.TestCase):
    def test_direct_connection_ignores_ambient_proxy_environment(self):
        with patch.dict("os.environ", {"HTTPS_PROXY": "http://127.0.0.1:1"}):
            with build_http_session() as session:
                self.assertFalse(session.trust_env)

    def test_preview_groups_new_matches_without_changing_baseline(self):
        with tempfile.TemporaryDirectory() as directory:
            state_file = Path(directory) / "last_matches.json"
            baseline = {PLAYER_ID: "previous-one", SECOND_PLAYER_ID: "previous-two"}
            state_file.write_text(json.dumps(baseline))
            original = state_file.read_bytes()
            config = make_config(state_file)
            config.players[SECOND_PLAYER_ID] = "Player Two"
            output = io.StringIO()
            with patch.object(preview.Config, "from_env", return_value=config), \
                    patch.object(preview.FaceitClient, "matches_since", return_value=MatchHistoryResult(True, (("shared-new-match", 1.0),))), \
                    contextlib.redirect_stdout(output):
                preview.main()
            result = json.loads(output.getvalue())
            self.assertEqual(result["notification_count"], 1)
            self.assertTrue(all(p["would_notify"] for p in result["players"]))
            self.assertEqual(state_file.read_bytes(), original)

    def test_preview_requires_seed_for_every_player(self):
        with tempfile.TemporaryDirectory() as directory:
            state_file = Path(directory) / "last_matches.json"
            state_file.write_text("{}")
            with patch.object(preview.Config, "from_env", return_value=make_config(state_file)):
                with self.assertRaises(SystemExit):
                    preview.main()
