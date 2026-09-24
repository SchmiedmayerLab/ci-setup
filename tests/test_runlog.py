#
# This source file is part of the SchmiedmayerLab ci-setup open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT
#

"""Run logging checks with synthetic Python children and temporary files only."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cisetup.runlog import _Redactor, _prune, register_secret  # noqa: E402


class RunLogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.logs = self.directory / "logs"

    def run_script(self, body: str, *, input: str = "", check: bool = True):
        script = "from cisetup.runlog import RunLog, register_secret\n"
        script += "import os, sys, subprocess, time\nfrom pathlib import Path\n"
        script += f"logs = Path({str(self.logs)!r})\n"
        script += textwrap.dedent(body)
        return subprocess.run(
            [sys.executable, "-c", script],
            input=input,
            text=True,
            capture_output=True,
            check=check,
            timeout=10,
            cwd=self.directory,
            env=dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parent.parent)),
        )

    def log_text(self):
        return "".join(path.read_text() for path in sorted(self.logs.glob("setup-*.log")))

    def test_captures_python_and_inherited_child_output_without_stdin(self):
        result = self.run_script(r"""
            with RunLog("converge", directory=logs) as logger:
                print("python stdout")
                print("python stderr", file=sys.stderr)
                subprocess.run([sys.executable, "-c",
                                "import os; os.write(1, b'child stdout\\n'); "
                                "os.write(2, b'child stderr\\n')"], check=True)
                entered = sys.stdin.readline()
                os.write(1, b"partial final line")
            print("after capture")
        """, input="stdin must never enter the log\n")
        text = self.log_text()
        for message in ("python stdout", "python stderr", "child stdout", "child stderr", "partial final line"):
            self.assertIn(message, text)
        self.assertIn("python stdout", result.stdout)
        self.assertIn("child stderr", result.stderr)
        self.assertIn("after capture", result.stdout)
        self.assertNotIn("after capture", text)
        self.assertNotIn("stdin must never", text)
        self.assertRegex(text, r"\d{4}-\d{2}-\d{2}T.+\[\d+-[0-9a-f]{8}\] START command=converge")
        self.assertIn("END command=converge exit=0 duration=", text)
        self.assertEqual((next(self.logs.glob("setup-*.log")).stat().st_mode & 0o777), 0o600)

    def test_prints_are_logged_while_run_is_still_active(self):
        self.run_script(r"""
            with RunLog("live", directory=logs) as logger:
                print("live output without explicit flush")
                deadline = time.monotonic() + 2
                while "live output" not in logger.path.read_text():
                    assert time.monotonic() < deadline, "stdout was buffered until exit"
                    time.sleep(0.01)
        """)

    def test_redacts_secrets_split_across_reads_in_log_and_console(self):
        result = self.run_script(r"""
            register_secret("secret-token-123")
            register_secret("multi\nline-secret")
            with RunLog("secrets", directory=logs):
                os.write(1, b"value=secret-to")
                time.sleep(0.1)
                os.write(1, b"ken-123 done\n")
                os.write(2, b"multi\n")
                time.sleep(0.1)
                os.write(2, b"line-secret\n")
        """)
        text = self.log_text()
        for output in (text, result.stdout, result.stderr):
            self.assertNotIn("secret-token-123", output)
            self.assertNotIn("multi\nline-secret", output)
        self.assertIn("value=<redacted> done", text)
        self.assertIn("value=<redacted> done", result.stdout)
        self.assertIn("<redacted>", result.stderr)

    def test_returned_failure_and_exception_statuses_are_recorded(self):
        result = self.run_script(r"""
            with RunLog("returned-failure", directory=logs) as logger:
                code = logger.finish(7)
            try:
                with RunLog("exception", directory=logs):
                    raise RuntimeError("simulated command failure")
            except RuntimeError:
                pass
            try:
                with RunLog("interrupt", directory=logs):
                    raise KeyboardInterrupt()
            except KeyboardInterrupt:
                pass
            try:
                with RunLog("system-exit", directory=logs):
                    raise SystemExit(4)
            except SystemExit:
                pass
            print("descriptors restored")
            sys.exit(code)
        """, check=False)
        self.assertEqual(result.returncode, 7)
        text = self.log_text()
        for command, code in (("returned-failure", 7), ("exception", 1), ("interrupt", 130), ("system-exit", 4)):
            self.assertIn(f"END command={command} exit={code}", text)
        self.assertIn("descriptors restored", result.stdout)
        self.assertNotIn("descriptors restored", text)

    def test_retention_and_size_cap_during_a_noisy_run(self):
        self.logs.mkdir()
        old = self.logs / f"setup-{date.today() - timedelta(days=30)}.log"
        old.write_text("too old\n")
        yesterday = self.logs / f"setup-{date.today() - timedelta(days=1)}.log"
        yesterday.write_text("recent previous run\n")
        foreign = self.logs / "user-notes.log"
        foreign.write_text("not owned\n")
        result = self.run_script(r"""
            with RunLog("noisy", directory=logs, max_bytes=4096):
                print("x" * 100_000)
                print("newest output")
        """)
        self.assertIn("newest output", result.stdout)
        self.assertFalse(old.exists())
        self.assertEqual(foreign.read_text(), "not owned\n")
        self.assertLessEqual(sum(path.stat().st_size for path in self.logs.glob("setup-*.log")), 4096)
        self.assertIn("newest output", self.log_text())
        self.assertIn("END command=noisy exit=0", self.log_text())

    def test_long_unicode_records_remain_valid_utf8_when_truncated(self):
        self.run_script(r"""
            with RunLog("unicode", directory=logs, max_bytes=1024):
                print("🌳" * 1000)
        """)
        text = self.log_text()  # Strict UTF-8 decoding must remain valid.
        self.assertIn("END command=unicode exit=0", text)
        self.assertLessEqual(sum(path.stat().st_size for path in self.logs.glob("setup-*.log")), 1024)

    def test_does_not_wait_for_descendant_to_close_inherited_pipe(self):
        self.run_script(r"""
            child = None
            try:
                started = time.monotonic()
                with RunLog("descendant", directory=logs):
                    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(5)"])
                    print("parent complete")
                assert time.monotonic() - started < 3, "waited for detached child"
            finally:
                if child is not None:
                    child.terminate()
                    child.wait(timeout=3)
        """)
        self.assertIn("parent complete", self.log_text())
        self.assertIn("END command=descendant exit=0", self.log_text())

    def test_unattended_capture_can_avoid_duplicating_output_to_launchd_log(self):
        result = self.run_script(r"""
            with RunLog("unattended", directory=logs, console=False):
                print("captured stdout")
                print("captured stderr", file=sys.stderr)
        """)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "")
        self.assertIn("captured stdout", self.log_text())
        self.assertIn("captured stderr", self.log_text())

    def test_close_before_reexec_is_idempotent(self):
        result = self.run_script(r"""
            with RunLog("before-reexec", directory=logs) as logger:
                print("before close")
                logger.close(0)
                print("after close")
        """)
        self.assertIn("before close", result.stdout)
        self.assertIn("after close", result.stdout)
        self.assertNotIn("after close", self.log_text())
        self.assertEqual(self.log_text().count("END command="), 1)

    def test_logging_failure_is_reported_after_restoring_descriptors(self):
        result = self.run_script(r"""
            try:
                with RunLog("write-failure", directory=logs) as logger:
                    def fail(*args):
                        raise OSError("simulated full disk")
                    logger._record = fail
                    print("output remains visible")
            except OSError as error:
                print("caught: " + str(error))
            else:
                raise AssertionError("logging failure was swallowed")
            print("after failure")
        """)
        self.assertIn("output remains visible", result.stdout)
        self.assertIn("caught: simulated full disk", result.stdout)
        self.assertIn("after failure", result.stdout)


class RetentionTests(unittest.TestCase):
    def test_size_trimming_leaves_headroom_for_subsequent_records(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            today = date.today()
            path = directory / f"setup-{today}.log"
            maximum = 64 * 1024
            path.write_bytes(b"old\n" * (maximum // 4))
            trims = []
            original_open = Path.open

            def tracked_open(file, *args, **kwargs):
                if args and args[0] == "r+b":
                    trims.append(file)
                return original_open(file, *args, **kwargs)

            with patch.object(Path, "open", tracked_open):
                for index in range(100):
                    with path.open("ab") as stream:
                        stream.write(f"new-{index:03}\n".encode())
                    _prune(directory, retention_days=30, max_bytes=maximum, today=today)
            self.assertEqual(len(trims), 1, "small appends must not rewrite the retained log repeatedly")
            self.assertLessEqual(path.stat().st_size, maximum)
            self.assertIn("new-099", path.read_text())

    def test_uses_calendar_day_window_and_preserves_unowned_files_and_symlinks(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            today = date(2026, 9, 24)
            paths = {}
            for days in (0, 29, 30):
                paths[days] = directory / f"setup-{today - timedelta(days=days)}.log"
                paths[days].write_text("entry\n")
            unrelated = directory / "setup-invalid-date.log"
            unrelated.write_text("keep me\n")
            target = directory / "external.txt"
            target.write_text("untouched\n")
            link = directory / "setup-2020-01-01.log"
            link.symlink_to(target)
            _prune(directory, retention_days=30, max_bytes=1024, today=today)
            self.assertTrue(paths[0].exists())
            self.assertTrue(paths[29].exists())
            self.assertFalse(paths[30].exists())
            self.assertTrue(link.is_symlink())
            self.assertEqual(target.read_text(), "untouched\n")
            self.assertEqual(unrelated.read_text(), "keep me\n")

    def test_streaming_redaction_handles_overlapping_values_and_partial_final_output(self):
        register_secret("unit-secret-long")
        register_secret("unit-secret")
        stream = _Redactor()
        self.assertEqual(stream.feed("a unit-se"), "a ")
        self.assertEqual(stream.feed("cret-long b"), "<redacted> b")
        self.assertEqual(stream.feed(" unit-se"), " ")
        self.assertEqual(stream.feed("", final=True), "unit-se")
        overlap = _Redactor()
        self.assertEqual(overlap.feed("unit-secret"), "")
        self.assertEqual(overlap.feed("-long!"), "<redacted>!")


if __name__ == "__main__":
    unittest.main()
