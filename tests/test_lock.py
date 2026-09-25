#
# This source file is part of the SchmiedmayerLab ci-setup open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT
#

"""Lock tests use temporary files and tiny synthetic processes only.

No setup entrypoint, Homebrew command, runner binary or launchctl is invoked.
"""

import fcntl
import json
import os
from pathlib import Path
import select
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from cisetup import util


class LockTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.state = self.root / "state"
        state = mock.patch.object(util, "STATE_DIR", self.state)
        state.start()
        self.addCleanup(state.stop)
        descriptor = mock.patch.object(util, "_lock_fd", None)
        descriptor.start()
        self.addCleanup(descriptor.stop)
        environment = mock.patch.dict(os.environ, {}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        self.addCleanup(util.release_lock)

    @property
    def path(self):
        return self.state / "setup.lock"

    def test_acquire_is_idempotent_and_records_holder(self):
        self.assertIsNone(util.acquire_lock())
        descriptor = util._lock_fd
        self.assertIn(f"pid {os.getpid()}, started ", self.path.read_text())
        self.assertFalse(os.get_inheritable(descriptor))
        self.assertIsNone(util.acquire_lock())
        self.assertEqual(util._lock_fd, descriptor)

    def test_release_closes_descriptor_and_allows_new_acquisition(self):
        util.acquire_lock()
        descriptor = util._lock_fd
        util.release_lock()
        self.assertIsNone(util._lock_fd)
        with self.assertRaises(OSError):
            os.fstat(descriptor)
        self.assertIsNone(util.acquire_lock())
        util.release_lock()
        util.release_lock()  # Repeated cleanup is harmless.

    def test_contending_process_preserves_holder_record_and_closes_losing_fd(self):
        self.state.mkdir()
        self.path.write_text("holder from synthetic child")
        child_code = """
import fcntl, os, sys
fd = os.open(sys.argv[1], os.O_RDWR)
fcntl.flock(fd, fcntl.LOCK_EX)
print("locked", flush=True)
sys.stdin.read()
os.close(fd)
"""
        process = subprocess.Popen(
            [sys.executable, "-c", child_code, str(self.path)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True,
        )

        def cleanup():
            if process.poll() is None:
                process.terminate()
            process.communicate(timeout=5)

        self.addCleanup(cleanup)
        ready, _, _ = select.select([process.stdout], [], [], 5)
        self.assertTrue(ready, "synthetic holder did not become ready")
        self.assertEqual(process.stdout.readline().strip(), "locked")
        opened = []
        original_open = os.open

        def track_open(*args, **kwargs):
            descriptor = original_open(*args, **kwargs)
            opened.append(descriptor)
            return descriptor

        with mock.patch.object(util.os, "open", side_effect=track_open):
            self.assertEqual(util.acquire_lock(), "holder from synthetic child")
        self.assertIsNone(util._lock_fd)
        self.assertEqual(len(opened), 1)
        with self.assertRaises(OSError):
            os.fstat(opened[0])
        self.assertEqual(self.path.read_text(), "holder from synthetic child")
        process.communicate(input="", timeout=5)
        self.assertEqual(process.returncode, 0)
        self.assertIsNone(util.acquire_lock())

    def test_reexec_requires_owned_lock(self):
        with self.assertRaisesRegex(util.SetupError, "without its lock"):
            util.reexec_environment()

    def test_reexec_marks_only_handoff_descriptor_inheritable(self):
        util.acquire_lock()
        environment = util.reexec_environment()
        self.assertEqual(environment["CI_SETUP_LOCK_FD"], str(util._lock_fd))
        self.assertTrue(os.get_inheritable(util._lock_fd))
        self.assertNotIn("CI_SETUP_LOCK_FD", os.environ)

    def test_new_interpreter_reclaims_same_lock_through_synthetic_shell_exec(self):
        util.acquire_lock()
        descriptor = util._lock_fd
        environment = util.reexec_environment()
        repo = str(Path(__file__).resolve().parent.parent)
        # Match the shell-interpreter handoff without invoking the real setup
        # wrapper: the shell only execs this small, non-operational program.
        # macOS exercises setup's zsh path; Linux CI may have only /bin/sh.
        shell = ["/bin/zsh", "-f"] if Path("/bin/zsh").exists() else ["/bin/sh"]
        child_code = """
import fcntl, json, os, pathlib, sys
sys.path.insert(0, sys.argv[1])
from cisetup import util
util.STATE_DIR = pathlib.Path(sys.argv[2])
holder = util.acquire_lock()
fd = util._lock_fd
other = os.open(util.STATE_DIR / "setup.lock", os.O_RDWR)
blocked = False
try:
    fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    blocked = True
os.close(other)
print(json.dumps({"holder": holder, "fd": fd,
                  "inheritable": os.get_inheritable(fd),
                  "environment_consumed": "CI_SETUP_LOCK_FD" not in os.environ,
                  "competitor_blocked": blocked}))
util.release_lock()
"""
        result = subprocess.run(
            [*shell, "-c", 'exec "$@"', "lock-test", sys.executable,
             "-c", child_code, repo, str(self.state)],
            pass_fds=(descriptor,), env=environment, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10, check=True,
        )
        value = json.loads(result.stdout)
        self.assertIsNone(value["holder"])
        self.assertEqual(value["fd"], descriptor)
        self.assertFalse(value["inheritable"])
        self.assertTrue(value["environment_consumed"])
        self.assertTrue(value["competitor_blocked"])
        # Closing the child's inherited reference did not release the
        # original process's still-open reference to the same lock.
        other = os.open(self.path, os.O_RDWR)
        try:
            with self.assertRaises(BlockingIOError):
                fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(other)

    def test_rejects_inherited_descriptor_for_another_file(self):
        self.state.mkdir()
        self.path.touch()
        foreign = os.open(self.root / "unrelated", os.O_CREAT | os.O_RDWR, 0o600)
        self.addCleanup(os.close, foreign)
        os.environ["CI_SETUP_LOCK_FD"] = str(foreign)
        with self.assertRaisesRegex(util.SetupError, "wrong lock file"):
            util.acquire_lock()
        self.assertIsNone(util._lock_fd)
        self.assertNotIn("CI_SETUP_LOCK_FD", os.environ)
        os.fstat(foreign)  # A rejected foreign descriptor belongs to its caller.

    def test_rejects_invalid_inherited_descriptor(self):
        os.environ["CI_SETUP_LOCK_FD"] = "not-an-integer"
        with self.assertRaisesRegex(util.SetupError, "retain setup lock"):
            util.acquire_lock()
        self.assertIsNone(util._lock_fd)


if __name__ == "__main__":
    unittest.main()
