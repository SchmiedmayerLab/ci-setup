#
# This source file is part of the SchmiedmayerLab ci-setup open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT
#

"""Boot-agent tests use temporary files and mocked launchctl exclusively."""

import os
import plistlib
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from cisetup import boot
from cisetup.config import Config


class BootTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.state = self.root / "state"
        self.logs = self.root / "logs"
        self.cfg = Config(repo_root=self.root / "checkout", boot_label="test.ci.setup")
        self.patch("pathlib.Path.home", return_value=self.root)
        self.patch("cisetup.boot.STATE_DIR", new=self.state)
        self.patch("cisetup.boot._LABEL_STATE", new=self.state / "boot-agent-label")
        self.patch("cisetup.boot.LOG_PATH", new=self.logs)
        self.patch("cisetup.boot.BOOTSTRAP_LOG", new=self.logs / "bootstrap.log")
        self.patch("subprocess.run", side_effect=AssertionError("host commands forbidden"))
        self.patch("subprocess.Popen", side_effect=AssertionError("host processes forbidden"))
        environment = mock.patch.dict(os.environ, {}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        self.commands = []
        self.loaded = True
        self.bootstrap_error = ""
        self.run = self.patch("cisetup.boot.run", side_effect=self.command)
        self.warning = self.patch("cisetup.boot.warn")

    def patch(self, target, **kwargs):
        patcher = mock.patch(target, **kwargs)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def command(self, argv, **kwargs):
        self.commands.append(argv)
        self.assertEqual(argv[0], "launchctl")
        self.assertIn(argv[1], {"print", "bootout", "enable", "bootstrap"})
        if argv[1] == "print":
            return subprocess.CompletedProcess(argv, 0 if self.loaded else 1, "", "")
        if argv[1] == "bootstrap" and self.bootstrap_error:
            return subprocess.CompletedProcess(argv, 1, "", self.bootstrap_error)
        return subprocess.CompletedProcess(argv, 0, "", "")

    def install_current_files(self):
        target = boot._plist_path(self.cfg.boot_label)
        target.parent.mkdir(parents=True)
        target.write_bytes(boot._plist_bytes(self.cfg))
        boot._record_label(self.cfg.boot_label)
        return target

    def verbs(self):
        return [command[1] for command in self.commands]

    def test_initial_install_writes_expected_schedule_and_bootstraps(self):
        boot.ensure(self.cfg)
        data = plistlib.loads(boot._plist_path(self.cfg.boot_label).read_bytes())
        self.assertEqual(data["ProgramArguments"], [str(self.cfg.repo_root / "setup"), "converge", "--non-interactive"])
        self.assertEqual(data["EnvironmentVariables"], {boot._MARKER_ENV: self.cfg.boot_label})
        self.assertEqual(data["StartInterval"], 21600)
        self.assertTrue(data["RunAtLoad"])
        self.assertEqual(data["StandardOutPath"], str(self.logs / "bootstrap.log"))
        self.assertEqual(self.verbs(), ["bootout", "enable", "bootstrap"])
        self.assertEqual(boot._recorded_label(), self.cfg.boot_label)

    def test_unchanged_loaded_agent_is_not_reloaded(self):
        self.install_current_files()
        boot.ensure(self.cfg)
        self.assertEqual(self.verbs(), ["print"])

    def test_force_reload_restarts_unchanged_agent_during_manual_run(self):
        target = self.install_current_files()
        old_contents = target.read_bytes()
        boot.ensure(self.cfg, force_reload=True)
        self.assertEqual(self.verbs(), ["bootout", "enable", "bootstrap"])
        self.assertEqual(target.read_bytes(), old_contents)

    def test_missing_loaded_agent_is_bootstrapped_without_bootout(self):
        self.install_current_files()
        self.loaded = False
        boot.ensure(self.cfg)
        self.assertEqual(self.verbs(), ["print", "enable", "bootstrap"])

    def test_own_agent_never_boots_itself_out_even_when_force_reload_requested(self):
        self.install_current_files()
        os.environ[boot._MARKER_ENV] = self.cfg.boot_label
        boot.ensure(self.cfg, force_reload=True)
        self.run.assert_not_called()

    def test_own_agent_updates_definition_without_unloading_itself(self):
        target = self.install_current_files()
        os.environ[boot._MARKER_ENV] = self.cfg.boot_label
        self.cfg.repo_root = self.root / "moved-checkout"
        boot.ensure(self.cfg, force_reload=True)
        self.assertEqual(plistlib.loads(target.read_bytes())["WorkingDirectory"], str(self.cfg.repo_root))
        self.run.assert_not_called()

    def test_disabled_agent_removes_installed_definition_during_manual_run(self):
        target = self.install_current_files()
        self.cfg.boot_install = False
        boot.ensure(self.cfg)
        self.assertEqual(self.verbs(), ["bootout"])
        self.assertFalse(target.exists())
        self.assertIsNone(boot._recorded_label())

    def test_disabled_absent_agent_performs_no_launchctl_commands(self):
        self.cfg.boot_install = False
        boot.ensure(self.cfg)
        self.run.assert_not_called()
        self.assertFalse(self.state.exists())

    def test_disabling_own_agent_never_unloads_current_process(self):
        target = self.install_current_files()
        self.cfg.boot_install = False
        os.environ[boot._MARKER_ENV] = self.cfg.boot_label
        boot.ensure(self.cfg)
        self.run.assert_not_called()
        self.warning.assert_called_once()
        self.assertFalse(target.exists())
        self.assertEqual(boot._recorded_label(), self.cfg.boot_label)
        # A later manual converge can still find and unload the live agent,
        # despite its plist already being removed to prevent future logins.
        os.environ.pop(boot._MARKER_ENV)
        boot.ensure(self.cfg)
        self.assertEqual(self.verbs(), ["bootout"])
        self.assertIsNone(boot._recorded_label())

    def test_manual_label_rename_removes_old_agent(self):
        old_target = self.install_current_files()
        old_label = self.cfg.boot_label
        self.cfg.boot_label = "test.ci.renamed"
        boot.ensure(self.cfg)
        self.assertFalse(old_target.exists())
        self.assertTrue(boot._plist_path(self.cfg.boot_label).exists())
        self.assertEqual(boot._recorded_label(), self.cfg.boot_label)
        self.assertEqual(self.commands[0], ["launchctl", "bootout", f"{boot._domain()}/{old_label}"])

    def test_rename_in_old_agent_retains_label_until_manual_migration(self):
        old_target = self.install_current_files()
        old_label = self.cfg.boot_label
        os.environ[boot._MARKER_ENV] = old_label
        self.cfg.boot_label = "test.ci.renamed"
        boot.ensure(self.cfg, force_reload=True)
        self.run.assert_not_called()
        self.warning.assert_called_once()
        self.assertEqual(boot._recorded_label(), old_label)
        self.assertTrue(old_target.exists())
        self.assertFalse(boot._plist_path(self.cfg.boot_label).exists())
        os.environ.pop(boot._MARKER_ENV)
        boot.ensure(self.cfg)
        self.assertFalse(old_target.exists())
        self.assertEqual(boot._recorded_label(), self.cfg.boot_label)

    def test_bootstrap_failure_leaves_definition_for_next_login_and_warns(self):
        self.bootstrap_error = "no GUI session"
        boot.ensure(self.cfg)
        self.assertTrue(boot._plist_path(self.cfg.boot_label).exists())
        self.warning.assert_called_once()
        self.assertIn("no GUI session", self.warning.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
