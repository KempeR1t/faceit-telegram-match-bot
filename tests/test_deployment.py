import contextlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
import os
import subprocess
from unittest.mock import MagicMock, patch

from bot import MatchHistoryResult, build_http_session
from test_bot import PLAYER_ID, SECOND_PLAYER_ID, make_config

spec = importlib.util.spec_from_file_location(
    "deployment_preview", Path(__file__).resolve().parents[1] / "deploy/preview.py"
)
preview = importlib.util.module_from_spec(spec)
spec.loader.exec_module(preview)


class DeploymentTests(unittest.TestCase):
    def test_cron_uses_current_home_and_preserves_other_jobs(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            app = home / "faceit_bot"
            (app / "deploy").mkdir(parents=True)
            source = Path(__file__).resolve().parents[1] / "deploy/install-cron.sh"
            script = app / "deploy/install-cron.sh"
            script.write_bytes(source.read_bytes())
            bin_dir = home / "bin"
            bin_dir.mkdir()
            crontab = bin_dir / "crontab"
            crontab.write_text('#!/bin/sh\nif [ "$1" = -l ]; then cat "$HOME/cron"; else cp "$1" "$HOME/cron"; fi\n')
            crontab.chmod(0o700)
            other = "* * * * * /opt/other/run.sh\n"
            (home / "cron").write_text(other + f"*/5 * * * * {app}/run_bot.sh\n")
            env = dict(os.environ, HOME=str(home), PATH=str(bin_dir) + os.pathsep + os.environ["PATH"])
            for _ in range(2):
                subprocess.run(["bash", str(script)], env=env, check=True, capture_output=True)
            result = (home / "cron").read_text()
            self.assertEqual(result.count("# BEGIN CODEX MANAGED: faceit_bot"), 1)
            self.assertIn(other, result)
            self.assertIn(f"*/10 * * * * {app}/deploy/run.sh", result)
            self.assertNotIn("run_bot.sh", result)
            self.assertTrue(list((home / ".config/deployment-backups/faceit-bot-crontabs").iterdir()))

    def test_token_validation_uses_configured_proxy_and_saves_atomically(self):
        spec = importlib.util.spec_from_file_location(
            "token_helper", Path(__file__).resolve().parents[1] / "deploy/set-telegram-token.py"
        )
        helper = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(helper)
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / "faceit_bot.env"
            env_file.write_text("TELEGRAM_CHAT_ID=-100123\nTELEGRAM_BOT_TOKEN=old\nTELEGRAM_PROXY_URL=socks5h://127.0.0.1:1080\n")
            env_file.chmod(0o600)
            session = MagicMock()
            session.get.return_value.status_code = 200
            session.get.return_value.json.return_value = {"ok": True}
            with patch.object(helper, "ENV_FILE", env_file), \
                    patch.object(helper.getpass, "getpass", return_value="123:unit-test-token"), \
                    patch.object(helper.requests, "Session") as session_class, \
                    contextlib.redirect_stdout(io.StringIO()):
                session_class.return_value.__enter__.return_value = session
                helper.main()
            self.assertFalse(session.trust_env)
            self.assertEqual(session.get.call_count, 2)
            for call in session.get.call_args_list:
                self.assertEqual(call.kwargs["proxies"], {"http": "socks5h://127.0.0.1:1080", "https": "socks5h://127.0.0.1:1080"})
            self.assertIn("TELEGRAM_BOT_TOKEN=123:unit-test-token", env_file.read_text())
            self.assertEqual(env_file.stat().st_mode & 0o777, 0o600)

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
