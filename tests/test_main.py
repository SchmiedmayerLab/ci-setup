#
# This source file is part of the SchmiedmayerLab ci-setup open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT
#

"""Integration of phase coordination with every machine operation mocked.

These tests never invoke ./setup, a runner binary, Homebrew or any real
maintenance context. Filesystem effects are confined to temporary directories.
An additional subprocess guard fails any accidental external command.
"""

import argparse
from contextlib import contextmanager
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cisetup import main  # noqa: E402
from cisetup.brew import BrewEnv, BrewError  # noqa: E402
from cisetup.config import Config  # noqa: E402
from cisetup.maintenance import MaintenanceDeferred  # noqa: E402
from cisetup.report import RunReport  # noqa: E402
from cisetup.util import SetupError  # noqa: E402
from cisetup.xcode import XcodeSetupError  # noqa: E402


class ExecRequested(BaseException):
    """Sentinel replacing a real exec without being caught as a setup error."""


class MainTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.state = self.root / "state"
        self.cfg = Config(repo_root=self.root, runner_dir=self.root / "runner",
                          runner_name="test-runner", owner="test-org", scope="org")
        self.cfg.runner_dir.mkdir()
        (self.cfg.runner_dir / "config.sh").touch()
        self.env = BrewEnv("/fake/homebrew", "/fake/homebrew/opt/openjdk", None)
        self.args = argparse.Namespace(command="converge", non_interactive=False,
                                       skip_brew=False, skip_xcode=False)
        self.events = []
        self.in_maintenance = False
        self.handed_off = False
        self.stdout = io.StringIO()
        self.patch("sys.stdout", new=self.stdout)
        self.patch("sys.stderr", new=io.StringIO())
        self.patch("subprocess.run", side_effect=AssertionError("external command forbidden in main tests"))
        self.patch("subprocess.Popen", side_effect=AssertionError("external process forbidden in main tests"))
        self.patch("os.execv", side_effect=AssertionError("real exec forbidden in main tests"))
        self.execve = self.patch("os.execve", side_effect=AssertionError("unexpected exec"))
        self.patch("pathlib.Path.home", return_value=self.root)
        environment = mock.patch.dict(os.environ, {}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        self.patch("cisetup.util.STATE_DIR", new=self.state)
        self.patch("cisetup.util.INTERACTIVE", new=True)
        self.patch("cisetup.util.WARNINGS", new=[])
        self.patch("cisetup.main.sys.stdin.isatty", return_value=True)
        self.patch("cisetup.runlog.LOG_DIR", new=self.root / "logs")
        self.load = self.patch("cisetup.config.load", return_value=self.cfg)
        self.resolve_pat = self.patch("cisetup.config.resolve_pat", return_value="test-pat")
        self.prepare_adoption = self.patch("cisetup.adoption.prepare", return_value=self.cfg)
        self.save_adoption = self.patch("cisetup.adoption.save")
        self.retire_legacy = self.patch("cisetup.legacy.retire_homebrew_autoupdate")
        self.patch("cisetup.runlog.register_secret")
        self.acquire = self.patch("cisetup.util.acquire_lock", return_value=None)
        self.release = self.patch("cisetup.util.release_lock")
        self.reexec_env = self.patch("cisetup.util.reexec_environment", return_value={
            "CI_SETUP_UPDATED": "stale", "CI_SETUP_PY_REEXEC": "stale", "CI_SETUP_LOCK_FD": "42",
        })
        self.pause = self.patch("cisetup.maintenance.paused", side_effect=self.paused)
        self.recover = self.patch("cisetup.maintenance.recover", side_effect=self.recover_service)
        self.handoff = mock.Mock(side_effect=self.handoff_service)
        self.pull = self.patch("cisetup.update.pull", return_value=False)
        self.brew = self.patch("cisetup.brew.ensure", return_value=self.env)
        self.probe = self.patch("cisetup.brew.probe", return_value=self.env)
        self.xcode = self.patch("cisetup.xcode.ensure", return_value=Path("/fake/Ready/Developer"))
        self.sudo = self.patch("cisetup.xcode.ensure_sudoless_select")
        self.certificate = self.patch("cisetup.xcode.ensure_wwdr_certificate")
        self.install = self.patch("cisetup.runner.ensure_installed")
        self.register = self.patch("cisetup.runner.ensure_registered")
        self.job_env = self.patch("cisetup.runner.ensure_job_env")
        self.start = self.patch("cisetup.runner.ensure_service")
        self.patch("cisetup.runner.is_registered", return_value=True)
        self.patch("cisetup.runner.service_running", return_value=False)
        self.activity = self.patch("cisetup.activity.collect", return_value={"state": "idle", "workers": []})
        self.boot = self.patch("cisetup.boot.ensure")
        self.spotlight = self.patch("cisetup.power.ensure_spotlight_exclusions")
        self.power = self.patch("cisetup.power.ensure")
        self.inventory = self.patch("cisetup.inventory.collect", return_value={"errors": [], "comparable": {}})
        self.save_inventory = self.patch("cisetup.inventory.save")
        self.logger = mock.MagicMock()
        self.logger.run_id = "test-run"
        self.logger.__enter__.side_effect = self.log_enter
        self.logger.__exit__.side_effect = self.log_exit
        self.runlog = self.patch("cisetup.runlog.RunLog", return_value=self.logger)
        self.mutations = [self.brew, self.sudo, self.certificate, self.xcode, self.install,
                          self.register, self.job_env, self.start, self.boot, self.spotlight, self.power]

    def patch(self, target, **kwargs):
        patcher = mock.patch(target, **kwargs)
        self.addCleanup(patcher.stop)
        return patcher.start()

    @contextmanager
    def paused(self, cfg):
        self.assertIs(cfg, self.cfg)
        self.events.append("pause")
        self.in_maintenance = True
        error = None
        try:
            yield SimpleNamespace(handoff=self.handoff)
        except BaseException as caught:
            error = caught
            raise
        finally:
            if self.handed_off and isinstance(error, main.Restart):
                self.events.append("pause-retained")
            else:
                self.in_maintenance = False
                self.events.append("restore")

    def handoff_service(self):
        self.assertTrue(self.in_maintenance)
        self.handed_off = True
        self.events.append("handoff")

    def recover_service(self):
        self.release.assert_not_called()
        if self.in_maintenance:
            self.in_maintenance = False
            self.events.append("recover")

    def log_enter(self):
        self.events.append("log-open")
        return self.logger

    def log_exit(self, *args):
        self.events.append("log-closed")
        return False

    def converge(self):
        self.report = RunReport("phase-test", "converge")
        return main.cmd_converge(self.args, self.cfg, self.report)

    def test_failed_homebrew_still_prepares_xcode_runner_and_boot(self):
        self.brew.side_effect = BrewError("firebase upgrade failed", self.env)
        self.assertEqual(self.converge(), 1)
        self.xcode.assert_called_once_with(self.cfg)
        self.install.assert_called_once_with(self.cfg)
        self.register.assert_called_once_with(self.cfg)
        self.boot.assert_called_once_with(self.cfg, force_reload=False)
        self.job_env.assert_called_once_with(self.cfg, self.env, Path("/fake/Ready/Developer"))
        self.start.assert_not_called()
        self.assertEqual(self.events, ["pause", "restore"])
        self.assertEqual(next(p for p in self.report.data["phases"] if p["name"] == "homebrew")["status"], "failed")

    def test_adoption_preserves_identity_inside_pause_without_keychain_lookup(self):
        self.cfg.adopted_registration = {"version": 1}
        self.save_adoption.side_effect = lambda cfg: self.assertTrue(self.in_maintenance)
        self.retire_legacy.side_effect = lambda: self.assertTrue(self.in_maintenance)
        self.assertEqual(main.main(["adopt", str(self.cfg.runner_dir), "--skip-xcode"]), 0)
        self.prepare_adoption.assert_called_once_with(self.cfg, self.cfg.runner_dir)
        self.save_adoption.assert_called_once_with(self.cfg)
        self.retire_legacy.assert_called_once()
        self.resolve_pat.assert_not_called()
        self.xcode.assert_not_called()
        self.register.assert_called_once_with(self.cfg)
        self.assertLess(self.events.index("restore"), self.events.index("log-closed"))
        self.assertFalse(self.in_maintenance)

    def test_busy_adoption_does_not_write_state_or_retire_scheduler(self):
        self.cfg.adopted_registration = {"version": 1}
        self.pause.side_effect = MaintenanceDeferred("a worker is running")
        self.assertEqual(main.main(["adopt", str(self.cfg.runner_dir)]), 2)
        self.save_adoption.assert_not_called()
        self.retire_legacy.assert_not_called()
        for operation in self.mutations:
            operation.assert_not_called()

    def test_failed_adoption_state_write_stops_before_tool_changes(self):
        self.args.command = "adopt"
        self.cfg.adopted_registration = {"version": 1}
        self.save_adoption.side_effect = SetupError("disk full")
        self.assertEqual(self.converge(), 1)
        self.retire_legacy.assert_not_called()
        self.brew.assert_not_called()
        self.resolve_pat.assert_not_called()
        self.assertEqual(self.events, ["pause", "restore"])

    def test_adopted_runner_always_checks_legacy_scheduler_before_maintenance(self):
        self.cfg.adopted_registration = {"version": 1}
        self.retire_legacy.side_effect = SetupError("legacy updater still running")
        self.assertEqual(self.converge(), 1)
        self.brew.assert_not_called()
        self.register.assert_not_called()
        self.assertEqual(self.events, ["pause", "restore"])

    def test_homebrew_without_paths_falls_back_to_existing_paths(self):
        self.brew.side_effect = SetupError("metadata unavailable")
        self.assertEqual(self.converge(), 1)
        self.probe.assert_called_once()
        self.job_env.assert_called_once_with(self.cfg, self.env, Path("/fake/Ready/Developer"))
        self.xcode.assert_called_once()

    def test_xcode_cleanup_failure_uses_verified_replacement_directory(self):
        ready = Path("/fake/Replacement/Developer")
        self.xcode.side_effect = XcodeSetupError("runtime cleanup failed", developer_dir=ready)
        self.assertEqual(self.converge(), 1)
        self.job_env.assert_called_once_with(self.cfg, self.env, ready)
        self.start.assert_not_called()
        self.assertFalse(self.in_maintenance)

    def test_xcode_auth_failure_preserves_runner_developer_directory(self):
        self.xcode.side_effect = XcodeSetupError("Apple ID authentication expired")
        self.assertEqual(self.converge(), 1)
        self.job_env.assert_called_once_with(self.cfg, self.env, None)
        self.boot.assert_called_once()
        self.start.assert_not_called()

    def test_independent_failures_are_all_reported_before_returning_nonzero(self):
        self.brew.side_effect = BrewError("upgrade failed", self.env)
        self.xcode.side_effect = SetupError("download failed")
        self.register.side_effect = SetupError("registration unavailable")
        self.boot.side_effect = SetupError("launchd unavailable")
        self.assertEqual(self.converge(), 1)
        failed = {p["name"] for p in self.report.data["phases"] if p["status"] == "failed"}
        self.assertEqual(failed, {"homebrew", "Xcode", "runner registration", "boot agent"})
        self.power.assert_called_once()
        self.inventory.assert_called_once()
        self.start.assert_not_called()
        self.assertEqual(self.events[-1], "restore")

    def test_unexpected_error_still_exits_pause_context(self):
        self.install.side_effect = RuntimeError("unexpected bug")
        with self.assertRaisesRegex(RuntimeError, "unexpected bug"):
            self.converge()
        self.assertEqual(self.events, ["pause", "restore"])
        self.assertFalse(self.in_maintenance)
        self.start.assert_not_called()

    def test_inventory_failure_prevents_new_service_and_restores_context(self):
        def incomplete_inventory(cfg):
            self.assertTrue(self.in_maintenance)
            return {"errors": ["active dependency is unavailable"], "comparable": {}}
        self.inventory.side_effect = incomplete_inventory
        self.assertEqual(self.converge(), 1)
        self.start.assert_not_called()
        self.save_inventory.assert_called_once()
        self.assertEqual(self.events, ["pause", "restore"])
        self.assertFalse(self.in_maintenance)
        phase = next(p for p in self.report.data["phases"] if p["name"] == "inventory")
        self.assertEqual(phase["status"], "failed")
        self.assertIn("active dependency is unavailable", phase["detail"])

    def test_fresh_runner_starts_only_after_leaving_maintenance(self):
        self.job_env.side_effect = lambda *args: self.assertTrue(self.in_maintenance)
        self.start.side_effect = lambda *args: self.assertFalse(self.in_maintenance)
        self.assertEqual(self.converge(), 0)
        self.start.assert_called_once_with(self.cfg)

    def test_busy_deferral_never_reaches_operational_mutations(self):
        self.pause.side_effect = MaintenanceDeferred("a worker is running")
        self.assertEqual(main.main(["converge"]), 2)
        for operation in self.mutations:
            operation.assert_not_called()
        self.pull.assert_not_called()
        self.resolve_pat.assert_not_called()
        self.inventory.assert_not_called()
        self.logger.finish.assert_called_once_with(2)
        self.release.assert_called_once()
        summary = json.loads((self.state / "last-run.json").read_text())
        self.assertEqual(summary["status"], "deferred")
        self.assertFalse((self.root / "Library/LaunchAgents").exists())

    def test_explicit_update_failure_restores_context_and_stops_before_setup(self):
        self.args.command = "update"
        self.pull.side_effect = SetupError("checkout is dirty")
        self.assertEqual(self.converge(), 1)
        for operation in self.mutations:
            operation.assert_not_called()
        self.assertEqual(self.events, ["pause", "restore"])

    def assert_exec_after_cleanup(self, script, argv, environment):
        self.assertEqual(self.events[-2:], ["pause-retained", "log-closed"])
        self.assertTrue(self.in_maintenance)
        self.handoff.assert_called_once()
        self.recover.assert_not_called()
        self.release.assert_not_called()
        self.assertTrue(script.endswith("/setup"))
        self.assertEqual(argv[0], script)
        self.assertEqual(environment["CI_SETUP_LOCK_FD"], "42")
        self.assertEqual(environment["CI_SETUP_UPDATED"], "1")
        self.assertEqual(json.loads((self.state / "last-run.json").read_text())["status"], "restarting")
        self.logger.finish.assert_called_once_with(0)
        raise ExecRequested()

    def test_source_reexec_closes_logger_and_retains_lock_and_update_guard(self):
        self.pull.return_value = True
        self.execve.side_effect = self.assert_exec_after_cleanup
        with self.assertRaises(ExecRequested):
            main.main(["update", "--non-interactive"])
        self.assertEqual(self.execve.call_args.args[1][1:], ["update", "--non-interactive"])
        self.recover.assert_called_once_with()
        self.brew.assert_not_called()
        self.release.assert_called_once()

    def test_python_reexec_retains_both_guards_after_environment_merge(self):
        self.env.python_changed = True
        def execute(script, argv, environment):
            self.assertEqual(environment["CI_SETUP_PY_REEXEC"], "1")
            self.assert_exec_after_cleanup(script, argv, environment)
        self.execve.side_effect = execute
        with self.assertRaises(ExecRequested):
            main.main(["converge"])
        self.reexec_env.assert_called_once()
        self.recover.assert_called_once_with()
        self.release.assert_called_once()
        self.xcode.assert_not_called()
        self.install.assert_not_called()
        self.boot.assert_not_called()

    def test_partial_brew_failure_reexecs_updated_python_before_other_phases(self):
        self.env.python_changed = True
        self.brew.side_effect = BrewError("firebase failed after Python upgrade", self.env)
        self.execve.side_effect = self.assert_exec_after_cleanup
        with self.assertRaises(ExecRequested):
            main.main(["converge"])
        self.assertEqual(self.execve.call_args.args[2]["CI_SETUP_PY_REEXEC"], "1")
        self.recover.assert_called_once_with()
        self.xcode.assert_not_called()
        self.install.assert_not_called()
        self.start.assert_not_called()

    def test_exec_failure_recovers_handed_off_service_before_unlocking(self):
        self.pull.return_value = True
        def fail_exec(*args):
            self.assertEqual(self.events[-1], "log-closed")
            self.assertTrue(self.in_maintenance)
            self.release.assert_not_called()
            raise OSError("exec failed")
        self.execve.side_effect = fail_exec
        self.assertEqual(main.main(["update"]), 1)
        self.recover.assert_called_once_with()
        self.assertFalse(self.in_maintenance)
        self.release.assert_called_once()
        summary = json.loads((self.state / "last-run.json").read_text())
        self.assertEqual(summary["status"], "failed")

    def test_log_close_failure_recovers_handed_off_service_without_exec(self):
        self.pull.return_value = True
        def fail_close(*args):
            self.events.append("log-close-failed")
            raise OSError("log flush failed")
        self.logger.__exit__.side_effect = fail_close
        self.assertEqual(main.main(["update"]), 1)
        self.execve.assert_not_called()
        self.recover.assert_called_once_with()
        self.assertFalse(self.in_maintenance)
        self.assertEqual(self.events[-2:], ["log-close-failed", "recover"])
        self.release.assert_called_once()

    def test_lock_contention_logs_deferral_without_overwriting_active_summary(self):
        self.state.mkdir()
        summary_path = self.state / "last-run.json"
        summary_path.write_text('{"status":"running","run_id":"another-process"}')
        previous = summary_path.read_text()
        self.acquire.return_value = "pid 99"
        self.assertEqual(main.main(["converge"]), 2)
        self.logger.finish.assert_called_once_with(2)
        self.assertEqual(summary_path.read_text(), previous)
        self.load.assert_not_called()
        self.pause.assert_not_called()
        self.recover.assert_not_called()
        for operation in self.mutations:
            operation.assert_not_called()
        self.release.assert_called_once()

    def test_keyboard_interrupt_restores_context_and_finishes_failed_log(self):
        self.xcode.side_effect = KeyboardInterrupt()
        self.assertEqual(main.main(["converge"]), 130)
        self.assertEqual(self.events, ["log-open", "pause", "restore", "log-closed"])
        self.logger.finish.assert_called_once_with(130)
        self.start.assert_not_called()
        self.release.assert_called_once()
        summary = json.loads((self.state / "last-run.json").read_text())
        self.assertEqual(summary["status"], "failed")
        self.assertEqual(next(p for p in summary["phases"] if p["name"] == "Xcode")["status"], "interrupted")

    def test_readonly_json_status_does_not_log_lock_update_or_mutate(self):
        snapshot = {"schema_version": 1, "host": "test", "comparable": {}, "errors": [],
                    "runner": {"activity": {"state": "busy", "workers": [{"pid": 321, "job": {"name": "Build"}}]}}}
        self.inventory.return_value = snapshot
        for command in ("status", "info"):
            with self.subTest(command=command):
                self.stdout.seek(0)
                self.stdout.truncate()
                self.assertEqual(main.main([command, "--json", "--non-interactive"]), 0)
                self.assertEqual(json.loads(self.stdout.getvalue()), snapshot)
                self.assert_read_only()

    def test_info_and_status_show_aligned_current_job_without_mutating(self):
        value = {"state": "busy", "workers": [{"pid": 321, "job": {
            "name": "Build (macOS)", "repository": "test-org/project", "workflow": "Tests",
            "ref": "refs/heads/main", "started_at": "2026-09-25T10:00:00+00:00", "elapsed_seconds": 3723,
            "url": "https://github.com/test-org/project/actions/runs/123/attempts/2",
        }}]}
        self.activity.return_value = value
        self.inventory.return_value = {"host": "test", "captured_at": "now", "errors": [],
                                       "comparable": {}, "runner": {"activity": value}}
        self.patch("cisetup.runner.installed_version", return_value=(2, 337, 0))
        self.cfg.xcode_manage = False
        for command in ("info", "status"):
            with self.subTest(command=command):
                self.stdout.seek(0)
                self.stdout.truncate()
                self.assertEqual(main.main([command]), 0)
                lines = self.stdout.getvalue().splitlines()
                for label, expected in (("activity", "busy"), ("current job", "Build (macOS)"),
                                        ("repository", "test-org/project"), ("workflow", "Tests"),
                                        ("elapsed", "1h 02m 03s"), ("run", value["workers"][0]["job"]["url"])):
                    self.assertIn(f"  {label + ':':<18} {expected}", lines)
                self.assert_read_only()

    def test_status_reports_unknown_when_activity_probe_fails(self):
        self.activity.return_value = {"state": "unknown", "error": "process probe unavailable", "workers": []}
        self.patch("cisetup.runner.installed_version", return_value=(2, 337, 0))
        self.cfg.xcode_manage = False
        self.assertEqual(main.main(["status"]), 1)
        self.assertIn("process probe unavailable", self.stdout.getvalue())
        self.assertIn("Maintenance:", self.stdout.getvalue())
        self.assert_read_only()

    def test_info_reports_actual_versions_pins_and_distinct_xcode_selections(self):
        self.inventory.return_value = {
            "schema_version": 1, "host": "example-runner", "captured_at": "2026-09-24T12:00:00Z",
            "errors": [], "comparable": {
                "setup": {"commit": "abcdef123", "dirty": True},
                "os": {"version": "27.0", "build": "26A428", "architecture": "arm64"},
                "runner_version": "2.337.0",
                "homebrew": {"formulae": {"node": {"version": "26.9.0", "pinned": True},
                                            "firebase-cli": {"version": "15.30.2", "pinned": False}},
                             "casks": {}},
                "xcodes": {"managed": True,
                           "installed": [{"version": "27.0", "build": "18A1", "prerelease": ["beta", 2]}],
                           "selected": {"version": "26.6", "build": "17F42"},
                           "runner": {"version": "26.5", "build": "17E1", "source": "DEVELOPER_DIR"}},
            },
        }
        self.assertEqual(main.main(["info", "--non-interactive"]), 0)
        output = " ".join(self.stdout.getvalue().split())
        for expected in ("example-runner", "abcdef123 (uncommitted changes)", "27.0 (26A428), arm64",
                         "v2.337.0", "node: 26.9.0 [pinned]", "firebase-cli: 15.30.2",
                         "27.0 beta 2 (18A1)", "global selection: 26.6 (17F42)",
                         "runner selection: 26.5 (17E1) via DEVELOPER_DIR", "last run: not recorded"):
            self.assertIn(expected, output)
        self.assert_read_only()

    def test_info_keeps_partial_state_when_runner_is_absent_and_probes_fail(self):
        self.cfg.runner_dir = self.root / "missing-runner"
        self.inventory.return_value = {
            "host": "example-runner", "captured_at": "2026-09-24T12:00:00Z",
            "comparable": {"setup": {"commit": "abcdef123", "dirty": False}},
            "errors": ["homebrew: command not found: brew", "xcodes: unavailable"],
        }
        self.assertEqual(main.main(["info"]), 1)
        output = " ".join(self.stdout.getvalue().split())
        for expected in ("abcdef123", "runner: not installed", "boot agent: not installed",
                         "Managed tools (Homebrew)", "Xcode:", "Incomplete checks:",
                         "homebrew: command not found: brew"):
            self.assertIn(expected, output)
        self.assert_read_only()

    def test_info_continues_after_service_probe_failure(self):
        self.patch("cisetup.runner.service_installed", return_value=True)
        self.patch("cisetup.runner.service_running", side_effect=SetupError("service status unavailable"))
        self.inventory.return_value = {
            "host": "example-runner", "captured_at": "2026-09-24T12:00:00Z", "errors": [],
            "comparable": {"homebrew": {"formulae": {"node": {"version": "26.9.0", "pinned": False}},
                                       "casks": {}}},
        }
        self.assertEqual(main.main(["info"]), 1)
        output = self.stdout.getvalue()
        self.assertIn("node: 26.9.0", output)
        self.assertIn("runner status: service status unavailable", output)
        self.assert_read_only()

    def assert_read_only(self):
        self.acquire.assert_not_called()
        self.release.assert_not_called()
        self.recover.assert_not_called()
        self.runlog.assert_not_called()
        self.pause.assert_not_called()
        self.pull.assert_not_called()
        self.resolve_pat.assert_not_called()
        self.save_inventory.assert_not_called()
        for operation in self.mutations:
            operation.assert_not_called()
        self.assertFalse(self.state.exists())
        self.assertFalse((self.root / "Library").exists())


if __name__ == "__main__":
    unittest.main()
