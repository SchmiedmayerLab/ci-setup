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
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from cisetup.util import _redact, fmt_version, vtuple  # noqa: E402
from cisetup import runlog, util  # noqa: E402


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


class WatchdogTests(unittest.TestCase):
    """Only launch synthetic Python children, never installed CI tools."""

    def test_default_timeout_is_applied_and_explicit_none_opts_out(self):
        import time

        with patch.object(util, "INTERACTIVE", False), patch.object(util, "DEFAULT_COMMAND_TIMEOUT", .1):
            started = time.monotonic()
            with self.assertRaisesRegex(util.SetupError, "command timed out"):
                util.run([sys.executable, "-c", "import time; time.sleep(10)"], capture=True)
            self.assertLess(time.monotonic() - started, 2)
            result = util.run(
                [sys.executable, "-c", "import time; time.sleep(.2); print('finished')"],
                capture=True, timeout=None,
            )
            self.assertEqual(result.stdout.strip(), "finished")

    def test_repeated_output_does_not_extend_deadline_and_timeout_stops_descendants(self):
        import tempfile
        import time

        with tempfile.TemporaryDirectory() as directory:
            descendant = (
                "import pathlib,time; time.sleep(.7); "
                "pathlib.Path('survived-timeout').touch()"
            )
            child = (
                "import pathlib,subprocess,sys,time; "
                f"subprocess.Popen([sys.executable,'-c',{descendant!r}]); "
                "pathlib.Path('started').touch(); "
                "exec('while True:\\n print(\"repeated output\", flush=True); time.sleep(.01)')"
            )
            with patch.object(util, "INTERACTIVE", False):
                with self.assertRaisesRegex(util.SetupError, "command timed out"):
                    util.run([sys.executable, "-c", child], cwd=directory, capture=True, timeout=.3)
            self.assertTrue((pathlib.Path(directory) / "started").exists())
            time.sleep(.8)
            self.assertFalse((pathlib.Path(directory) / "survived-timeout").exists())

    def test_heartbeat_reports_elapsed_time_without_exposing_argument_secrets(self):
        runlog.register_secret("known-heartbeat-secret")
        script = "import time; time.sleep(.35)"
        with patch.object(util, "INTERACTIVE", False), patch.object(util, "log") as log:
            util.run(
                [sys.executable, "-c", script, "--token", "argument-heartbeat-secret",
                 "known-heartbeat-secret"],
                timeout=2, heartbeat_interval=.05,
            )
        messages = "\n".join(call.args[0] for call in log.call_args_list)
        self.assertIn("still running after", messages)
        self.assertIn("timeout 2s", messages)
        self.assertIn("<redacted>", messages)
        self.assertNotIn("argument-heartbeat-secret", messages)
        self.assertNotIn("known-heartbeat-secret", messages)

    def test_captured_commands_never_emit_heartbeats_or_captured_output(self):
        with patch.object(util, "INTERACTIVE", False), patch.object(util, "log") as log:
            result = util.run(
                [sys.executable, "-c", "import time; print('private-capture'); time.sleep(.35)"],
                capture=True, heartbeat_interval=.01,
            )
        log.assert_not_called()
        self.assertEqual(result.stdout.strip(), "private-capture")

    def test_escaped_descendant_cannot_keep_timeout_cleanup_waiting_for_pipe_eof(self):
        import os
        import signal
        import tempfile
        import time

        with tempfile.TemporaryDirectory() as directory:
            pidfile = pathlib.Path(directory) / "escaped-pid"
            child = (
                "import pathlib,subprocess,sys,time; "
                "escaped=subprocess.Popen([sys.executable,'-c','import time; time.sleep(3)'], "
                "start_new_session=True); "
                "pathlib.Path('escaped-pid').write_text(str(escaped.pid)); time.sleep(10)"
            )
            try:
                with patch.object(util, "INTERACTIVE", False), patch.object(util, "PROCESS_CLEANUP_TIMEOUT", .1):
                    started = time.monotonic()
                    with self.assertRaisesRegex(util.SetupError, "command timed out"):
                        util.run([sys.executable, "-c", child], cwd=directory, capture=True, timeout=.3)
                    self.assertLess(time.monotonic() - started, 2)
                self.assertTrue(pidfile.exists())
            finally:
                if pidfile.exists():
                    try:
                        os.kill(int(pidfile.read_text()), signal.SIGKILL)
                    except ProcessLookupError:
                        pass

    def test_sudo_gives_interactive_prompt_more_time_without_overriding_explicit_limit(self):
        with patch.object(util, "INTERACTIVE", True), patch.object(util, "run") as run:
            util.sudo_run(["synthetic-command"])
            self.assertEqual(run.call_args.kwargs["timeout"], 600)
            util.sudo_run(["synthetic-command"], timeout=1800)
            self.assertEqual(run.call_args.kwargs["timeout"], 1800)


class DownloadDeadlineTests(unittest.TestCase):
    """Fake response data and clocks only; never make a network request."""

    def test_slow_trickle_is_limited_even_when_each_read_returns_data(self):
        import tempfile

        response = MagicMock()
        response.headers = {}
        response.read1.side_effect = [b"first", b"late"]
        response.__enter__.return_value = response
        with tempfile.TemporaryDirectory() as directory:
            target = pathlib.Path(directory) / "download"
            with patch.object(util.urllib.request, "urlopen", return_value=response) as urlopen, \
                    patch.object(util, "DOWNLOAD_TIMEOUT", 1), \
                    patch.object(util.time, "monotonic", side_effect=[0, 0, .4, .4, 1.1]):
                with self.assertRaisesRegex(util.SetupError, "download timed out after 1s"):
                    util.download("https://example.invalid/artifact", target)
            self.assertEqual(target.read_bytes(), b"first")
            self.assertEqual(response.read1.call_count, 2)
            response.read.assert_not_called()
            self.assertEqual(urlopen.call_args.kwargs["timeout"], 1)
            response.__exit__.assert_called_once()

    def test_complete_download_with_explicit_deadline(self):
        import tempfile

        response = MagicMock()
        response.headers = {"Content-Length": "8"}
        response.read1.side_effect = [b"complete", b""]
        response.__enter__.return_value = response
        with tempfile.TemporaryDirectory() as directory:
            target = pathlib.Path(directory) / "download"
            with patch.object(util.urllib.request, "urlopen", return_value=response) as urlopen, \
                    patch.object(util.time, "monotonic", return_value=0), \
                    patch.object(util.sys.stdout, "isatty", return_value=False):
                util.download("https://example.invalid/artifact", target, timeout=120)
            self.assertEqual(target.read_bytes(), b"complete")
            self.assertEqual(urlopen.call_args.kwargs["timeout"], 60)

    def test_download_heartbeat_reports_received_bytes_without_url(self):
        import tempfile

        response = MagicMock()
        response.headers = {"Content-Length": "16"}
        response.read1.side_effect = [b"received", b""]
        response.__enter__.return_value = response
        with tempfile.TemporaryDirectory() as directory:
            target = pathlib.Path(directory) / "download"
            with patch.object(util.urllib.request, "urlopen", return_value=response), \
                    patch.object(util.time, "monotonic", side_effect=[0, 0, 301, 301, 302]), \
                    patch.object(util.sys.stdout, "isatty", return_value=False), \
                    patch.object(util, "log") as log:
                util.download("https://example.invalid/artifact?private=token", target)
            log.assert_called_once_with("downloaded 8/16 bytes after 301s")


if __name__ == "__main__":
    unittest.main()
