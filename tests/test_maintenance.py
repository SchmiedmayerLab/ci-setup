# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
# SPDX-License-Identifier: MIT

"""Isolated maintenance tests: all process/service operations are mocked."""

import json
import os
import signal
import tempfile
import unittest
from dataclasses import asdict, replace
from pathlib import Path
from unittest.mock import patch

from cisetup import maintenance, runner
from cisetup.config import Config
from cisetup.util import SetupError


class RestartRequested(BaseException):
    """The caller's non-error unwind immediately before exec."""


class MaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cfg = Config(repo_root=self.root, runner_dir=self.root / "runner")
        self.cfg.runner_dir.mkdir()
        self.listener = maintenance.Process(
            123, 10, os.getuid(), "S", "Thu Sep 24 10:00:00 2026",
            str(self.cfg.runner_dir / "bin/Runner.Listener"),
        )
        self.processes = [self.listener]
        self.running = True
        self.calls = []
        self.addCleanup(patch.stopall)
        patch.object(maintenance.util, "STATE_DIR", self.root / "state").start()
        patch.object(maintenance.signal, "signal").start()
        patch.object(maintenance, "_processes", side_effect=lambda: self.processes).start()
        self.kill = patch.object(maintenance.os, "kill", side_effect=self.signal_process).start()
        patch.object(runner, "service_running", side_effect=lambda cfg: self.running).start()
        patch.object(runner, "is_registered", return_value=True).start()
        self.stop = patch.object(runner, "stop_service", side_effect=self.stop_service).start()
        self.start = patch.object(runner, "ensure_service", side_effect=self.start_service).start()

    @property
    def marker(self):
        return self.root / "state/maintenance.json"

    def signal_process(self, pid, sig):
        self.calls.append(sig)
        self.processes = [
            replace(process, state="T" if sig == signal.SIGSTOP else "S")
            if process.pid == pid else process
            for process in self.processes
        ]

    def stop_service(self, cfg):
        self.assertTrue(all("T" in p.state for p in self.processes))
        self.calls.append("stop")
        self.running = False
        self.processes = []

    def start_service(self, cfg):
        self.calls.append("start")
        self.running = True

    def test_pauses_before_body_and_restores_on_success(self):
        with maintenance.paused(self.cfg):
            self.assertFalse(self.running)
            self.assertTrue(self.marker.exists())
            self.calls.append("body")
        self.assertEqual(self.calls, [signal.SIGSTOP, "stop", "body", "start"])
        self.assertFalse(self.marker.exists())

    def test_busy_defers_without_signalling_or_mutating(self):
        self.processes.append(replace(self.listener, pid=124, ppid=123, executable="Runner.Worker"))
        with self.assertRaises(maintenance.MaintenanceDeferred):
            with maintenance.paused(self.cfg):
                self.fail("maintenance body ran while busy")
        self.kill.assert_not_called()
        self.stop.assert_not_called()
        self.assertTrue(self.running)

    def test_just_forked_child_defers_and_resumes_listener(self):
        original_signal = self.signal_process

        def signal_and_spawn(pid, sig):
            original_signal(pid, sig)
            if sig == signal.SIGSTOP:
                self.processes.append(replace(self.listener, pid=124, ppid=123))

        self.kill.side_effect = signal_and_spawn
        with self.assertRaises(maintenance.MaintenanceDeferred):
            with maintenance.paused(self.cfg):
                self.fail("maintenance body ran after a job started")
        self.stop.assert_not_called()
        self.assertIn(signal.SIGCONT, self.calls)
        self.assertTrue(self.running)

    def test_stop_failure_resumes_listener_and_does_not_enter_body(self):
        self.stop.side_effect = SetupError("unload failed")
        with self.assertRaisesRegex(SetupError, "unload failed"):
            with maintenance.paused(self.cfg):
                self.fail("maintenance body ran after stop failure")
        self.assertEqual(self.calls, [signal.SIGSTOP, signal.SIGCONT])
        self.start.assert_not_called()
        self.assertFalse(self.marker.exists())

    def test_phase_failure_restores_service(self):
        with self.assertRaisesRegex(SetupError, "upgrade failed"):
            with maintenance.paused(self.cfg):
                raise SetupError("upgrade failed")
        self.start.assert_called_once_with(self.cfg)
        self.assertFalse(self.marker.exists())

    def test_signal_exit_restores_service(self):
        with self.assertRaises(SystemExit) as raised:
            with maintenance.paused(self.cfg):
                maintenance._interrupted(signal.SIGTERM, None)
        self.assertEqual(raised.exception.code, 143)
        self.start.assert_called_once_with(self.cfg)

    def test_previously_stopped_service_stays_stopped(self):
        self.running = False
        self.processes = []
        with maintenance.paused(self.cfg):
            pass
        self.stop.assert_not_called()
        self.start.assert_not_called()

    def test_interrupted_maintenance_preserves_restart_obligation(self):
        self.running = False
        self.processes = []
        self.marker.parent.mkdir()
        self.marker.write_text(json.dumps({
            "runner_dir": str(self.cfg.runner_dir), "was_running": True,
            "listeners": [asdict(self.listener)],
        }))
        with maintenance.paused(self.cfg):
            self.assertFalse(self.running)
        self.start.assert_called_once_with(self.cfg)
        self.assertFalse(self.marker.exists())

    def test_failed_restore_keeps_recovery_marker(self):
        self.start.side_effect = SetupError("start failed")
        with self.assertRaisesRegex(SetupError, "start failed"):
            with maintenance.paused(self.cfg):
                pass
        self.assertTrue(self.marker.exists())

    def test_listener_stop_timeout_aborts_before_mutation(self):
        wait = maintenance._wait_for

        def fail_stop(predicate, description, timeout):
            if description == "the runner listener to stop":
                raise SetupError("listener did not stop")
            return wait(predicate, description, timeout)

        with patch.object(maintenance, "_wait_for", side_effect=fail_stop):
            with self.assertRaisesRegex(SetupError, "listener did not stop"):
                with maintenance.paused(self.cfg):
                    self.fail("maintenance body ran before listener exit")
        self.start.assert_called_once_with(self.cfg)
        self.assertNotIn(signal.SIGKILL, self.calls)

    def test_service_without_verified_listener_defers(self):
        self.processes = []
        with self.assertRaisesRegex(maintenance.MaintenanceDeferred, "cannot be verified"):
            with maintenance.paused(self.cfg):
                self.fail("unverified service entered maintenance")
        self.stop.assert_not_called()

    def test_missing_registration_retains_recovery_marker(self):
        with patch.object(runner, "is_registered", return_value=False):
            with self.assertRaisesRegex(SetupError, "registration is missing"):
                with maintenance.paused(self.cfg):
                    pass
        self.start.assert_not_called()
        self.assertTrue(self.marker.exists())

    def test_recovery_does_not_signal_reused_pid(self):
        original = self.listener
        self.processes = [replace(original, started="Thu Sep 24 11:00:00 2026")]
        maintenance._resume([original])
        self.kill.assert_not_called()

    def handoff(self):
        with self.assertRaises(RestartRequested):
            with maintenance.paused(self.cfg) as pause:
                pause.handoff()
                raise RestartRequested()

    def test_handoff_keeps_intake_stopped_and_forgets_exited_pids(self):
        self.handoff()
        self.assertFalse(self.running)
        self.start.assert_not_called()
        state = json.loads(self.marker.read_text())
        self.assertTrue(state["was_running"])
        self.assertEqual(state["listeners"], [])

    def test_next_process_inherits_restore_obligation_without_reopening_intake(self):
        self.handoff()
        with maintenance.paused(self.cfg):
            self.assertFalse(self.running)
            self.start.assert_not_called()
        self.start.assert_called_once_with(self.cfg)
        self.assertFalse(self.marker.exists())

    def test_failed_exec_can_recover_handed_off_service(self):
        self.handoff()
        maintenance.recover(self.cfg)
        self.start.assert_called_once_with(self.cfg)
        self.assertTrue(self.running)
        self.assertFalse(self.marker.exists())

    def test_handoff_recovery_failure_retains_obligation(self):
        self.handoff()
        self.start.side_effect = SetupError("start failed")
        with self.assertRaisesRegex(SetupError, "start failed"):
            maintenance.recover(self.cfg)
        self.assertTrue(self.marker.exists())

    def test_handoff_followed_by_ordinary_exception_still_restores(self):
        with self.assertRaisesRegex(SetupError, "unexpected failure"):
            with maintenance.paused(self.cfg) as pause:
                pause.handoff()
                raise SetupError("unexpected failure")
        self.start.assert_called_once_with(self.cfg)
        self.assertFalse(self.marker.exists())

    def test_handoff_followed_by_signal_still_restores(self):
        with self.assertRaises(SystemExit):
            with maintenance.paused(self.cfg) as pause:
                pause.handoff()
                maintenance._interrupted(signal.SIGTERM, None)
        self.start.assert_called_once_with(self.cfg)
        self.assertFalse(self.marker.exists())

    def test_recovery_rejects_invalid_marker_before_process_operations(self):
        self.marker.parent.mkdir()
        self.marker.write_text('{"was_running":true}')
        with self.assertRaisesRegex(SetupError, "invalid maintenance recovery marker"):
            maintenance.recover(self.cfg)
        self.start.assert_not_called()
        self.kill.assert_not_called()

    def test_recovery_works_when_updated_configuration_cannot_load(self):
        self.handoff()
        maintenance.recover()
        self.start.assert_called_once()
        recovered_cfg = self.start.call_args.args[0]
        self.assertEqual(recovered_cfg.runner_dir, self.cfg.runner_dir)
        self.assertIsNone(recovered_cfg.pat)
        self.assertTrue(self.running)
        self.assertFalse(self.marker.exists())

    def test_recovery_rejects_relative_stored_runner_directory(self):
        self.marker.parent.mkdir()
        self.marker.write_text(json.dumps({
            "runner_dir": "relative/runner", "was_running": True, "listeners": [],
        }))
        with self.assertRaisesRegex(SetupError, "invalid maintenance recovery marker"):
            maintenance.recover()
        self.start.assert_not_called()
        self.kill.assert_not_called()
        self.assertTrue(self.marker.exists())

    def test_recovery_without_marker_is_a_noop(self):
        maintenance.recover()
        self.start.assert_not_called()
        self.kill.assert_not_called()


class ProcessParsingTests(unittest.TestCase):
    def test_ps_output_with_spaces_in_runner_path(self):
        with patch.object(maintenance, "run") as run:
            run.return_value.stdout = (
                " 123 10 501 T Thu Sep 24 10:00:00 2026 /Users/ci/my runner/bin/Runner.Listener\n"
            )
            result = maintenance._processes()
        self.assertEqual(result[0].started, "Thu Sep 24 10:00:00 2026")
        self.assertEqual(result[0].executable, "/Users/ci/my runner/bin/Runner.Listener")

    def test_invalid_process_inventory_fails_closed(self):
        with patch.object(maintenance, "run") as run:
            run.return_value.stdout = "unexpected output\n"
            with self.assertRaises(SetupError):
                maintenance._processes()


if __name__ == "__main__":
    unittest.main()
