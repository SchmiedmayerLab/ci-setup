#
# This source file is part of the SchmiedmayerLab ci-setup open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT
#

"""Unit tests for pure helpers in cisetup.util."""

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from cisetup.util import _redact, fmt_version, vtuple  # noqa: E402


class RedactTests(unittest.TestCase):
    def test_redacts_token_values(self):
        argv = ["./config.sh", "--url", "https://github.com/x/y", "--token", "AAAA", "--name", "mac"]
        self.assertEqual(
            _redact(argv),
            ["./config.sh", "--url", "https://github.com/x/y", "--token", "<redacted>", "--name", "mac"],
        )

    def test_redacts_w_flag(self):
        self.assertEqual(
            _redact(["security", "add-generic-password", "-w", "secret"]),
            ["security", "add-generic-password", "-w", "<redacted>"],
        )

    def test_no_secret_flags(self):
        argv = ["brew", "install", "jq"]
        self.assertEqual(_redact(argv), argv)

    def test_trailing_flag_without_value(self):
        self.assertEqual(_redact(["security", "-w"]), ["security", "-w"])


class VersionTests(unittest.TestCase):
    def test_roundtrip(self):
        self.assertEqual(vtuple("2.328.0"), (2, 328, 0))
        self.assertEqual(fmt_version((2, 328, 0)), "2.328.0")

    def test_comparison(self):
        self.assertLess(vtuple("2.319.1"), vtuple("2.328.0"))

    def test_invalid(self):
        with self.assertRaises(ValueError):
            vtuple("v2.328.0")


class ProcessCleanupTests(unittest.TestCase):
    """Synthetic Python processes only; no setup or machine operations."""

    def test_sigterm_stops_interactive_descendants_before_cleanup_returns(self):
        import os
        import subprocess
        import tempfile
        import textwrap

        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            descendant = (
                "import pathlib,time; time.sleep(0.5); "
                "pathlib.Path('descendant-ran-after-cleanup').touch()"
            )
            child = (
                "import pathlib,subprocess,sys,time; "
                f"subprocess.Popen([sys.executable,'-c',{descendant!r}]); "
                "pathlib.Path('ready').touch(); time.sleep(3)"
            )
            driver = textwrap.dedent("""
                import os,pathlib,signal,sys,threading,time
                from cisetup import util
                util.INTERACTIVE=True
                def interrupt(signum, frame):
                    raise SystemExit(128+signum)
                signal.signal(signal.SIGTERM, interrupt)
                def signal_when_ready():
                    deadline=time.monotonic()+2
                    while not pathlib.Path('ready').exists():
                        assert time.monotonic()<deadline
                        time.sleep(.01)
                    os.kill(os.getpid(),signal.SIGTERM)
                threading.Thread(target=signal_when_ready,daemon=True).start()
                try:
                    util.run([sys.executable,'-c',CHILD], capture=True)
                except SystemExit:
                    print('cleanup finished',flush=True)
                else:
                    raise AssertionError('expected interruption')
                time.sleep(.7)
                assert not pathlib.Path('descendant-ran-after-cleanup').exists()
            """).replace("CHILD", repr(child))
            result = subprocess.run(
                [sys.executable, "-c", driver], cwd=root,
                env=dict(os.environ, PYTHONPATH=str(pathlib.Path(__file__).resolve().parent.parent)),
                capture_output=True, text=True, timeout=5,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("cleanup finished", result.stdout)

    def test_failed_command_stops_descendants_with_captured_or_inherited_output(self):
        import os
        import subprocess
        import tempfile
        import textwrap

        for capture in (False, True):
            with self.subTest(capture=capture), tempfile.TemporaryDirectory() as directory:
                descendant = (
                    "import pathlib,time; time.sleep(1); "
                    "pathlib.Path('descendant-survived-failure').touch()"
                )
                child = (
                    "import subprocess,sys; "
                    f"subprocess.Popen([sys.executable,'-c',{descendant!r}]); sys.exit(7)"
                )
                driver = textwrap.dedent("""
                    import sys,time
                    from pathlib import Path
                    from cisetup import util
                    util.INTERACTIVE=True
                    try:
                        util.run([sys.executable,'-c',CHILD], capture=CAPTURE)
                    except util.SetupError as error:
                        assert 'exit 7' in str(error)
                    else:
                        raise AssertionError('command failure not reported')
                    time.sleep(1.2)
                    assert not Path('descendant-survived-failure').exists()
                """).replace("CHILD", repr(child)).replace("CAPTURE", repr(capture))
                result = subprocess.run(
                    [sys.executable, "-c", driver], cwd=directory,
                    env=dict(os.environ, PYTHONPATH=str(pathlib.Path(__file__).resolve().parent.parent)),
                    capture_output=True, text=True, timeout=5,
                )
                self.assertEqual(result.returncode, 0, result.stderr)

    def run_with_terminal(self, script, response, *, directory):
        import errno
        import os
        import pty
        import select
        import signal
        import time

        child, terminal = pty.fork()
        if child == 0:
            os.chdir(directory)
            environment = dict(os.environ, PYTHONPATH=str(pathlib.Path(__file__).resolve().parent.parent))
            os.execve(sys.executable, [sys.executable, "-c", script], environment)
        output = bytearray()
        responded = False
        status = None
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                ready, _, _ = select.select([terminal], [], [], .1)
                if ready:
                    try:
                        chunk = os.read(terminal, 65536)
                    except OSError as error:
                        if error.errno != errno.EIO:
                            raise
                        chunk = b""
                    if not chunk:
                        break
                    output.extend(chunk)
                    if not responded and b"PROMPT_READY" in output:
                        os.write(terminal, response)
                        responded = True
                waited, value = os.waitpid(child, os.WNOHANG)
                if waited:
                    status = value
                    break
            while status is None and time.monotonic() < deadline:
                waited, value = os.waitpid(child, os.WNOHANG)
                if waited:
                    status = value
                else:
                    time.sleep(.01)
            self.assertIsNotNone(status, bytes(output).decode(errors="replace"))
            self.assertTrue(responded, bytes(output).decode(errors="replace"))
            self.assertEqual(os.waitstatus_to_exitcode(status), 0, bytes(output).decode(errors="replace"))
            return bytes(output).decode(errors="replace")
        finally:
            os.close(terminal)
            if status is None:
                try:
                    os.kill(child, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                os.waitpid(child, 0)

    def test_terminal_prompts_and_foreground_ownership_survive_log_capture(self):
        import tempfile
        import textwrap

        with tempfile.TemporaryDirectory() as directory:
            child = "value=input('PROMPT_READY: '); assert value=='synthetic-answer'; print('accepted')"
            driver = textwrap.dedent("""
                import os,sys
                from pathlib import Path
                from cisetup import util,runlog
                util.INTERACTIVE=True
                with runlog.RunLog('terminal-test',directory=Path('logs')):
                    util.run([sys.executable,'-c',CHILD])
                    assert os.tcgetpgrp(0)==os.getpgrp(), 'setup did not regain terminal'
                    print('terminal restored',flush=True)
            """).replace("CHILD", repr(child))
            output = self.run_with_terminal(driver, b"synthetic-answer\n", directory=directory)
            self.assertIn("accepted", output)
            text = "".join(path.read_text() for path in (pathlib.Path(directory) / "logs").glob("*.log"))
            self.assertIn("terminal restored", text)
            self.assertNotIn("synthetic-answer", text, "terminal input must not be logged")

    def test_foreground_ctrl_c_interrupts_setup_and_cleans_descendants(self):
        import tempfile
        import textwrap

        with tempfile.TemporaryDirectory() as directory:
            descendant = (
                "import pathlib,signal,time; signal.signal(signal.SIGINT,signal.SIG_IGN); "
                "pathlib.Path('descendant-ready').touch(); "
                "time.sleep(.6); pathlib.Path('descendant-survived').touch()"
            )
            child = (
                "import subprocess,sys,time; "
                f"subprocess.Popen([sys.executable,'-c',{descendant!r}]); "
                "print('PROMPT_READY',flush=True); time.sleep(3)"
            )
            driver = textwrap.dedent("""
                import os,sys,threading,time
                from pathlib import Path
                from cisetup import util
                util.INTERACTIVE=True
                def announce():
                    while not Path('descendant-ready').exists():
                        time.sleep(.01)
                    print('PROMPT_READY',flush=True)
                threading.Thread(target=announce,daemon=True).start()
                try:
                    util.run([sys.executable,'-c',CHILD],capture=True)
                except KeyboardInterrupt:
                    assert os.tcgetpgrp(0)==os.getpgrp()
                    print('interrupted; terminal restored',flush=True)
                else:
                    raise AssertionError('Ctrl-C did not cancel setup')
                time.sleep(.8)
                assert not Path('descendant-survived').exists()
            """).replace("CHILD", repr(child))
            output = self.run_with_terminal(driver, b"\x03", directory=directory)
            self.assertIn("interrupted; terminal restored", output)


if __name__ == "__main__":
    unittest.main()
