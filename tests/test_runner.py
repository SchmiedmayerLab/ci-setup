# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
# SPDX-License-Identifier: MIT

"""Runner lifecycle tests; never execute runner/service commands on the host."""

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cisetup import runner
from cisetup.brew import BrewEnv
from cisetup.config import Config
from cisetup.util import SetupError


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cfg = Config(repo_root=self.root, runner_dir=self.root / "runner")
        self.cfg.runner_dir.mkdir()
        self.addCleanup(patch.stopall)
        # This guard makes an accidentally unmocked command fail before it
        # can touch launchd, GitHub, Homebrew or any existing runner.
        self.run = patch.object(runner, "run", side_effect=AssertionError("unexpected host command")).start()

    def touch(self, name, content=""):
        path = self.cfg.runner_dir / name
        path.write_text(content)
        return path

    def test_stop_failure_is_not_silenced(self):
        with patch.object(runner, "service_running", return_value=True), patch.object(
            runner, "_svc", side_effect=SetupError("unload failed")
        ):
            with self.assertRaisesRegex(SetupError, "unload failed"):
                runner.stop_service(self.cfg)

    def test_changed_environment_restart_survives_busy_run(self):
        self.touch("svc.sh")
        self.touch(".service")
        env = BrewEnv("/fake/brew", "/fake/java", None)
        self.assertTrue(runner.ensure_job_env(self.cfg, env, None))
        self.assertFalse(runner.ensure_job_env(self.cfg, env, None))
        marker = self.cfg.runner_dir / runner._RESTART_MARKER
        with patch.object(runner, "service_running", return_value=True), patch.object(
            runner.util, "runner_busy", return_value=True
        ), patch.object(runner, "_svc") as svc:
            with self.assertRaisesRegex(SetupError, "pending restart"):
                runner.ensure_service(self.cfg)
            svc.assert_not_called()
        self.assertTrue(marker.exists())
        # The next run notices the marker even though .env/.path are already
        # current and ensure_job_env no longer returns changed=True.
        with patch.object(runner, "service_running", side_effect=[True, True, False]), patch.object(
            runner.util, "runner_busy", return_value=False
        ), patch.object(runner, "stop_service") as stop, patch.object(runner, "_svc") as svc:
            runner.ensure_service(self.cfg)
        stop.assert_called_once_with(self.cfg)
        svc.assert_called_once_with(self.cfg, "start")
        self.assertFalse(marker.exists())

    def test_failed_start_keeps_pending_restart(self):
        self.touch("svc.sh")
        self.touch(".service")
        self.touch(runner._RESTART_MARKER)
        with patch.object(runner, "service_running", return_value=False), patch.object(
            runner, "_svc", side_effect=SetupError("start failed")
        ):
            with self.assertRaisesRegex(SetupError, "start failed"):
                runner.ensure_service(self.cfg)
        self.assertTrue((self.cfg.runner_dir / runner._RESTART_MARKER).exists())

    def test_drift_without_pat_is_incomplete_and_preserves_registration(self):
        self.touch(".runner", "registration")
        self.touch(runner._STATE_FILE, json.dumps({"name": "old"}))
        with patch.object(runner.util, "INTERACTIVE", False), patch.object(runner, "deregister") as remove:
            with self.assertRaisesRegex(SetupError, "store a PAT"):
                runner.ensure_registered(self.cfg)
        remove.assert_not_called()
        self.assertEqual((self.cfg.runner_dir / ".runner").read_text(), "registration")

    def test_unchanged_registration_is_not_recreated(self):
        self.touch(".runner")
        self.touch(runner._STATE_FILE, json.dumps(runner.desired_state(self.cfg)))
        with patch.object(runner, "_register") as register, patch.object(runner, "deregister") as remove:
            runner.ensure_registered(self.cfg)
        register.assert_not_called()
        remove.assert_not_called()

    def test_failed_deregistration_does_not_delete_local_credentials(self):
        for name in (".runner", ".credentials", ".credentials_rsaparams", runner._STATE_FILE):
            self.touch(name, "preserve me")
        self.run.side_effect = None
        self.run.return_value = subprocess.CompletedProcess([], 1)
        with patch.object(runner, "_removal_token", return_value="secret"), patch.object(
            runner, "uninstall_service"
        ):
            with self.assertRaisesRegex(SetupError, "preserved"):
                runner.deregister(self.cfg)
        for name in (".runner", ".credentials", ".credentials_rsaparams", runner._STATE_FILE):
            self.assertEqual((self.cfg.runner_dir / name).read_text(), "preserve me")

    def test_uninstall_already_stopped_service_removes_only_its_plist(self):
        agents = self.root / "Library/LaunchAgents"
        agents.mkdir(parents=True)
        plist = agents / "actions.runner.example.plist"
        plist.write_text("plist")
        self.touch(".service", str(plist))
        self.touch(".credentials", "preserve me")
        with patch.object(runner, "service_running", return_value=False), patch.object(
            runner.Path, "home", return_value=self.root
        ), patch.object(runner, "_svc") as svc:
            runner.uninstall_service(self.cfg)
        svc.assert_not_called()
        self.assertFalse(plist.exists())
        self.assertFalse((self.cfg.runner_dir / ".service").exists())
        self.assertTrue((self.cfg.runner_dir / ".credentials").exists())

    def test_uninstall_rejects_unexpected_plist_path(self):
        self.touch(".service", "/unrelated/user/file")
        with patch.object(runner, "service_running", return_value=False):
            with self.assertRaisesRegex(SetupError, "unexpected runner service plist"):
                runner.uninstall_service(self.cfg)


if __name__ == "__main__":
    unittest.main()
