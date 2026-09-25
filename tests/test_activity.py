# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
# SPDX-License-Identifier: MIT

"""Job inspection uses synthetic logs and mocks every process operation."""

import copy
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from cisetup import activity  # noqa: E402
from cisetup.config import Config  # noqa: E402
from cisetup.maintenance import Process  # noqa: E402
from cisetup.util import SetupError  # noqa: E402


class ActivityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = pathlib.Path(temporary.name)
        self.cfg = Config(repo_root=self.root, runner_dir=self.root / "runner", owner="example", scope="org")
        self.diag = self.cfg.runner_dir / "_diag"
        self.diag.mkdir(parents=True)
        self.listener = self.process(100, "Runner.Listener")
        self.worker = self.process(101, "Runner.Worker")
        self.processes = self.patch("cisetup.activity._processes", return_value=[self.listener, self.worker])
        self.run = self.patch("cisetup.activity.run", side_effect=AssertionError("unexpected command"))
        self.patch("subprocess.Popen", side_effect=AssertionError("real process launch is forbidden"))
        self.payload = {
            "jobId": "job-guid", "jobDisplayName": "Build (macOS)", "jobName": "build",
            "variables": {"SECRET": {"value": "never-export-this"}},
            "resources": {"endpoints": [{"token": "never-export-this"}]},
            "contextData": {"github": {"t": 2, "d": [
                {"k": key, "v": value} for key, value in {
                    "repository": "example/project", "workflow": "Continuous integration",
                    "run_id": "123", "run_attempt": "2", "ref": "refs/heads/main",
                    "server_url": "https://attacker.invalid", "token": "never-export-this",
                    "event": {"private": "never-export-this"},
                }.items()
            ]}},
        }

    def patch(self, target, **kwargs):
        patcher = mock.patch(target, **kwargs)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def process(self, pid, name):
        return Process(pid, 100 if name == "Runner.Worker" else 1, 501, "S",
                       "Fri Sep 25 10:00:00 2026", str(self.cfg.runner_dir / "bin" / name))

    def record(self, payload=None, timestamp="2026-09-25 10:00:00Z"):
        if payload is None:
            payload = self.payload
        return f"[{timestamp} INFO Worker] Job message:\n {json.dumps(payload)}\n"

    def log(self, contents=None, path=None):
        path = path or self.diag / "Worker_20260925-100000-utc.log"
        path.write_text(self.record() if contents is None else contents)
        self.run.side_effect = None
        self.run.return_value = subprocess.CompletedProcess([], 0, f"p101\nn{path}\n")
        return path

    def test_reads_upstream_encoded_metadata_with_allowlist_and_trusted_url(self):
        self.log()
        clock = self.patch("cisetup.activity.datetime", wraps=datetime)
        clock.now.return_value = datetime(2026, 9, 25, 10, 2, 3, tzinfo=timezone.utc)
        snapshot = activity.collect(self.cfg)
        self.assertEqual(snapshot["state"], "busy")
        self.assertEqual(snapshot["listener_pids"], [100])
        self.assertEqual(snapshot["workers"][0], {"pid": 101, "job": {
            "id": "job-guid", "name": "Build (macOS)", "key": "build",
            "repository": "example/project", "workflow": "Continuous integration",
            "run_id": "123", "run_attempt": "2", "ref": "refs/heads/main",
            "url": "https://github.com/example/project/actions/runs/123/attempts/2",
            "started_at": "2026-09-25T10:00:00+00:00", "elapsed_seconds": 123,
        }})
        self.assertNotIn("never-export-this", json.dumps(snapshot))
        self.assertNotIn("attacker.invalid", json.dumps(snapshot))
        self.run.assert_called_once_with(
            ["/usr/sbin/lsof", "-nP", "-a", "-p", "101", "-Fn"],
            capture=True, check=False, timeout=5,
        )
        self.assertEqual(self.processes.call_count, 2)

    def test_unrelated_runner_and_zombie_do_not_mark_this_runner_busy(self):
        other = replace(self.worker, executable=str(self.root / "other/bin/Runner.Worker"))
        zombie = replace(self.worker, state="Z")
        self.processes.return_value = [self.listener, other, zombie]
        snapshot = activity.collect(self.cfg)
        self.assertEqual(snapshot["state"], "idle")
        self.assertEqual(snapshot["workers"], [])
        self.run.assert_not_called()

    def test_old_log_cannot_resurrect_a_job_when_idle_or_offline(self):
        self.log()
        for processes, state in [([self.listener], "idle"), ([], "offline")]:
            with self.subTest(state=state):
                self.processes.return_value = processes
                snapshot = activity.collect(self.cfg)
                self.assertEqual(snapshot["state"], state)
                self.assertEqual(snapshot["workers"], [])
        self.run.assert_not_called()

    def test_stopped_listener_is_paused(self):
        self.processes.return_value = [replace(self.listener, state="T")]
        self.assertEqual(activity.collect(self.cfg)["state"], "paused")

    def test_worker_exit_discards_metadata_and_reports_current_state(self):
        self.log()
        self.processes.side_effect = [[self.listener, self.worker], [self.listener]]
        snapshot = activity.collect(self.cfg)
        self.assertEqual(snapshot["state"], "idle")
        self.assertEqual(snapshot["workers"], [])

    def test_reused_pid_does_not_inherit_previous_jobs_details(self):
        self.log()
        replacement = replace(self.worker, started="Fri Sep 25 10:01:00 2026")
        self.processes.side_effect = [[self.worker], [replacement]]
        snapshot = activity.collect(self.cfg)
        self.assertEqual(snapshot["state"], "busy")
        self.assertNotIn("job", snapshot["workers"][0])
        self.assertIn("started during inspection", snapshot["workers"][0]["details_unavailable"])

    def test_missing_rotated_malformed_and_truncated_logs_remain_busy(self):
        cases = {
            "rotated": "[2026-09-25 10:03:00Z INFO Worker] Waiting for the job to complete.\n",
            "truncated": '[2026-09-25 10:00:00Z INFO Worker] Job message:\n {"jobId":',
            "nonobject": self.record(["unexpected"]),
            "no_metadata": self.record({"variables": {"secret": "never-export-this"}}),
            "unanchored": "untrusted text " + self.record(),
        }
        for name, contents in cases.items():
            with self.subTest(name=name):
                self.log(contents)
                snapshot = activity.collect(self.cfg)
                self.assertEqual(snapshot["state"], "busy")
                self.assertNotIn("job", snapshot["workers"][0])
                self.assertIn("details_unavailable", snapshot["workers"][0])
        self.run.return_value = subprocess.CompletedProcess([], 0, "p101\n")
        self.assertIn("details_unavailable", activity.collect(self.cfg)["workers"][0])

    def test_oversized_payload_is_bounded_and_stays_unavailable(self):
        payload = copy.deepcopy(self.payload)
        payload["variables"]["huge"] = "x" * (activity._LOG_MAX_BYTES + 1)
        self.log(self.record(payload))
        snapshot = activity.collect(self.cfg)
        self.assertEqual(snapshot["state"], "busy")
        self.assertNotIn("job", snapshot["workers"][0])

    def test_reused_log_selects_latest_job_message(self):
        previous = self.record()
        self.payload["jobId"] = "second-job"
        self.payload["jobDisplayName"] = "Second job"
        self.log(previous + "[2026-09-25 10:00:00Z INFO Worker] Job completed.\n"
                 + "[2026-09-25 10:00:00Z INFO Worker] Version: 2.337.0\n" + self.record())
        job = activity.collect(self.cfg)["workers"][0]["job"]
        self.assertEqual(job["id"], "second-job")
        self.assertEqual(job["name"], "Second job")

    def test_later_startup_or_completion_cannot_report_old_job(self):
        for boundary in ("Worker] Version: 2.337.0", "Worker] Job completed.",
                         "JobRunner] Job result after all job steps finish: Succeeded"):
            with self.subTest(boundary=boundary):
                self.log(self.record() + f"[2026-09-25 10:00:00Z INFO {boundary}\n")
                snapshot = activity.collect(self.cfg)
                self.assertEqual(snapshot["state"], "busy")
                self.assertNotIn("job", snapshot["workers"][0])

    def test_later_job_beyond_size_cap_never_returns_old_metadata(self):
        self.log(self.record() + "x" * activity._LOG_MAX_BYTES + "\n" + self.record())
        snapshot = activity.collect(self.cfg)
        self.assertEqual(snapshot["state"], "busy")
        self.assertNotIn("job", snapshot["workers"][0])

    def test_wrong_runner_log_or_symlink_outside_diag_is_rejected(self):
        foreign = self.root / "Worker_foreign.log"
        foreign.write_text(self.record())
        link = self.diag / "Worker_link.log"
        link.symlink_to(foreign)
        for path in (foreign, link):
            with self.subTest(path=path):
                self.run.side_effect = None
                self.run.return_value = subprocess.CompletedProcess([], 0, f"n{path}\n")
                self.assertNotIn("job", activity.collect(self.cfg)["workers"][0])

    def test_lsof_failures_do_not_hide_busy_state(self):
        for error in (SetupError("inspection timed out"), OSError("permission denied")):
            with self.subTest(error=error):
                self.run.side_effect = error
                snapshot = activity.collect(self.cfg)
                self.assertEqual(snapshot["state"], "busy")
                self.assertIn("details_unavailable", snapshot["workers"][0])

    def test_process_probe_failures_are_unknown_even_after_reading_metadata(self):
        self.log()
        for responses in ([SetupError("ps failed")],
                          [[self.listener, self.worker], OSError("ps failed")]):
            with self.subTest(responses=responses):
                self.processes.side_effect = responses
                snapshot = activity.collect(self.cfg)
                self.assertEqual(snapshot["state"], "unknown")
                self.assertEqual(snapshot["workers"], [])
                self.assertIn("ps failed", snapshot["error"])

    def test_display_text_removes_controls_and_limits_workflow_strings(self):
        self.payload["jobDisplayName"] = "\x1b[31mBuild\n\r\t\x00\u202e"
        self.payload["jobName"] = "a" * 900
        self.log()
        job = activity.collect(self.cfg)["workers"][0]["job"]
        self.assertEqual(job["name"], "[31mBuild")
        self.assertEqual(len(job["key"]), 500)
        self.assertTrue(all(char.isprintable() for char in job["name"]))

    def test_bad_url_components_do_not_create_a_run_link(self):
        for key, value in [("repository", "../private/path"), ("repository", "evil.invalid#org/repo"),
                           ("run_id", "123?token=secret")]:
            with self.subTest(key=key):
                payload = copy.deepcopy(self.payload)
                for entry in payload["contextData"]["github"]["d"]:
                    if entry["k"] == key:
                        entry["v"] = value
                self.log(self.record(payload))
                self.assertNotIn("url", activity.collect(self.cfg)["workers"][0]["job"])

    def test_invalid_timestamp_is_omitted_and_future_elapsed_is_zero(self):
        for timestamp in ("invalid", "2026-09-25 10:00:00"):
            with self.subTest(timestamp=timestamp):
                self.log(self.record(timestamp=timestamp))
                job = activity.collect(self.cfg)["workers"][0]["job"]
                self.assertNotIn("started_at", job)
                self.assertNotIn("elapsed_seconds", job)
        self.log(self.record(timestamp="2099-01-01 00:00:00Z"))
        self.assertEqual(activity.collect(self.cfg)["workers"][0]["job"]["elapsed_seconds"], 0)


if __name__ == "__main__":
    unittest.main()
