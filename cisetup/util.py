#
# This source file is part of the SchmiedmayerLab ci-setup open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT
#

"""Shared helpers: logging, subprocess wrappers, prompts, download, locking."""

from __future__ import annotations

import fcntl
import errno
import os
import signal
import shlex
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path


class SetupError(Exception):
    """Fatal, user-facing error. Raised instead of printing a traceback."""


# main() sets this; when False, nothing may prompt and nothing may sudo.
INTERACTIVE = True
WARNINGS: list[str] = []

# Callers with legitimately long work supply an explicit limit. Keep these
# values injectable so watchdog behavior can be tested without waiting minutes.
DEFAULT_COMMAND_TIMEOUT = 120.0
DEFAULT_HEARTBEAT_INTERVAL = 300.0
DEFAULT_SUDO_TIMEOUT = 600.0
PROCESS_CLEANUP_TIMEOUT = 5.0
DOWNLOAD_TIMEOUT = 30 * 60.0
DOWNLOAD_READ_TIMEOUT = 60.0
DOWNLOAD_HEARTBEAT_INTERVAL = 300.0


class _DefaultTimeout:
    pass


_DEFAULT_TIMEOUT = _DefaultTimeout()

_USE_COLOR = sys.stdout.isatty()


def _c(code: str) -> str:
    return code if _USE_COLOR else ""


BLUE = _c("\033[34m")
GREEN = _c("\033[32m")
YELLOW = _c("\033[33m")
RED = _c("\033[31m")
BOLD = _c("\033[1m")
OFF = _c("\033[0m")


def log(msg: str) -> None:
    print(f"{BLUE}==>{OFF} {msg}", flush=True)


def ok(msg: str) -> None:
    print(f"{GREEN}  ✓{OFF} {msg}", flush=True)


def warn(msg: str) -> None:
    WARNINGS.append(msg)
    print(f"{YELLOW}  !{OFF} {msg}", flush=True)


def err(msg: str) -> None:
    print(f"{RED}  ✗{OFF} {msg}", file=sys.stderr, flush=True)


def _redact(argv: list[str]) -> list[str]:
    """Blank out secret values (registration tokens, passwords) so they never
    end up in error messages or the boot-agent log."""
    redacted = []
    hide_next = False
    for arg in argv:
        if hide_next:
            redacted.append("<redacted>")
            hide_next = False
        else:
            if any(arg.startswith(flag + "=") for flag in ("--token", "--password", "--pat")):
                redacted.append(arg.split("=", 1)[0] + "=<redacted>")
                continue
            redacted.append(arg)
            if arg in ("--token", "--password", "--pat", "-w"):
                hide_next = True
    return redacted


def _stop_process_group(pid: int) -> None:
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _drain_stopped_process(child: subprocess.Popen) -> tuple[str | None, str | None]:
    """Do not let an escaped descendant's pipe defeat the command deadline."""
    try:
        return child.communicate(timeout=PROCESS_CLEANUP_TIMEOUT)
    except subprocess.TimeoutExpired as error:
        # A descendant can create its own session while retaining a captured
        # pipe. Our process group is stopped; do not wait for that pipe's EOF.
        for stream in (child.stdin, child.stdout, child.stderr):
            if stream is not None:
                stream.close()
        child.wait(timeout=PROCESS_CLEANUP_TIMEOUT)

        def decoded(value):
            return value.decode(errors="replace") if isinstance(value, bytes) else value

        return decoded(error.output), decoded(error.stderr)


def run(
    cmd: list,
    *,
    check: bool = True,
    capture: bool = False,
    cwd=None,
    env: dict | None = None,
    stdin_devnull: bool = False,
    timeout: float | None | _DefaultTimeout = _DEFAULT_TIMEOUT,
    heartbeat_interval: float | None = DEFAULT_HEARTBEAT_INTERVAL,
    input: str | None = None,
    start_new_session: bool = False,
) -> subprocess.CompletedProcess:
    """Run a command with a default deadline, or explicit ``None`` to opt out.

    Captured commands never emit heartbeat/command output. Other long-running
    commands report elapsed time, which is liveness information, not progress.
    """
    if timeout is _DEFAULT_TIMEOUT:
        timeout = DEFAULT_COMMAND_TIMEOUT
    if heartbeat_interval is not None and heartbeat_interval <= 0:
        raise ValueError("heartbeat interval must be positive or None")
    argv = [str(c) for c in cmd]
    # Captured Keychain/API output is deliberately never logged. Known
    # argument secrets are also scrubbed if a child echoes its arguments.
    from . import runlog
    hide_next = False
    for arg in argv:
        if hide_next:
            runlog.register_secret(arg)
            hide_next = False
        elif arg in ("--token", "--password", "--pat", "-w"):
            hide_next = True
        elif any(arg.startswith(flag + "=") for flag in ("--token", "--password", "--pat")):
            runlog.register_secret(arg.split("=", 1)[1])
    kwargs: dict = {"cwd": str(cwd) if cwd else None, "env": env, "text": True}
    if capture:
        kwargs["stdout"] = subprocess.PIPE
        kwargs["stderr"] = subprocess.PIPE
    if input is not None:
        kwargs["stdin"] = subprocess.PIPE
    elif stdin_devnull or not INTERACTIVE:
        # Unattended runs must never block on a child reading stdin.
        kwargs["stdin"] = subprocess.DEVNULL
    detached = start_new_session or not INTERACTIVE
    if detached:
        # Detach the controlling terminal too: tools that prompt on /dev/tty
        # (sudo, most password prompts) then fail fast instead of hanging.
        kwargs["start_new_session"] = True
    else:
        # A distinct group lets cancellation stop descendants without also
        # signalling setup. Unlike setsid(), it retains the controlling TTY.
        kwargs["process_group"] = 0
    shown = runlog.redact(shlex.join(_redact(argv)))
    tty_fd = None
    foreground = None
    previous_ttou = None
    if not detached:
        try:
            tty_fd = os.open("/dev/tty", os.O_RDWR | os.O_CLOEXEC)
            foreground = os.tcgetpgrp(tty_fd)
            if foreground != os.getpgrp():
                # A background invocation must never steal another job's TTY.
                os.close(tty_fd)
                tty_fd = None
        except OSError:
            if tty_fd is not None:
                os.close(tty_fd)
            tty_fd = None
    try:
        with subprocess.Popen(argv, **kwargs) as child:
            try:
                if tty_fd is not None:
                    # While the child is foreground, setup's logging thread
                    # still writes the terminal. Ignore background-write stops
                    # only in the parent (the child has already been spawned).
                    previous_ttou = signal.getsignal(signal.SIGTTOU)
                    signal.signal(signal.SIGTTOU, signal.SIG_IGN)
                    try:
                        os.tcsetpgrp(tty_fd, child.pid)
                        # The child may have read stdin before the handoff and
                        # stopped on SIGTTIN; safely resume its whole group.
                        os.killpg(child.pid, signal.SIGCONT)
                    except OSError as error:
                        if error.errno not in (errno.ESRCH, errno.EPERM) or child.poll() is None:
                            raise
                started = time.monotonic()
                deadline = started + timeout if timeout is not None else None
                heartbeat = (started + heartbeat_interval
                             if not capture and heartbeat_interval is not None else None)
                first_communication = True
                while True:
                    remaining = None if deadline is None else max(0, deadline - time.monotonic())
                    # A foreground Ctrl-C reaches the child group, not setup.
                    # Poll captured commands too: a surviving descendant's
                    # pipe must not conceal a failed/interrupted child's exit.
                    interval = min(0.2, remaining if remaining is not None else 0.2)
                    try:
                        stdout, stderr = child.communicate(
                            input=input if first_communication else None, timeout=interval
                        )
                        break
                    except subprocess.TimeoutExpired:
                        first_communication = False
                        returncode = child.poll()
                        if returncode in (-signal.SIGINT, 128 + signal.SIGINT):
                            raise KeyboardInterrupt
                        if check and returncode is not None and returncode != 0:
                            _stop_process_group(child.pid)
                            stdout, stderr = _drain_stopped_process(child)
                            break
                        now = time.monotonic()
                        if deadline is not None and now >= deadline:
                            raise subprocess.TimeoutExpired(argv, timeout)
                        if heartbeat is not None and now >= heartbeat:
                            limit = f"timeout {timeout:g}s" if timeout is not None else "no deadline"
                            log(f"still running after {now - started:.0f}s ({limit}): {shown}")
                            heartbeat = now + heartbeat_interval
                if child.returncode in (-signal.SIGINT, 128 + signal.SIGINT):
                    raise KeyboardInterrupt
                if check and child.returncode != 0:
                    _stop_process_group(child.pid)
            except BaseException:
                # Both interactive and unattended commands own a process
                # group: no build/download descendant may outlive cleanup.
                _stop_process_group(child.pid)
                _drain_stopped_process(child)
                raise
            finally:
                if tty_fd is not None:
                    try:
                        os.tcsetpgrp(tty_fd, foreground)
                    finally:
                        if previous_ttou is not None:
                            signal.signal(signal.SIGTTOU, previous_ttou)
            proc = subprocess.CompletedProcess(argv, child.returncode, stdout, stderr)
    except FileNotFoundError as e:
        raise SetupError(
            f"command not found: {argv[0]} — is it installed and on PATH? "
            "(a converge without --skip-brew installs the tooling)"
        ) from e
    except subprocess.TimeoutExpired as e:
        raise SetupError(f"command timed out after {timeout:.0f}s: {shown}") from e
    finally:
        if tty_fd is not None:
            os.close(tty_fd)
    if check and proc.returncode != 0:
        detail = ""
        if capture and proc.stderr:
            detail = f" — {runlog.redact(proc.stderr.strip())[:400]}"
        raise SetupError(f"command failed (exit {proc.returncode}): {shown}{detail}")
    return proc


def output(cmd: list, **kwargs) -> str:
    """Run a command and return its stripped stdout."""
    return run(cmd, capture=True, **kwargs).stdout.strip()


def require_interactive(what: str) -> None:
    if not INTERACTIVE:
        raise SetupError(
            f"{what} requires an interactive run — invoke ./setup manually on the machine"
        )


def prompt(message: str) -> str:
    require_interactive(f"prompting for input ({message.strip()})")
    return input(message).strip()


def confirm(message: str, default: bool = False) -> bool:
    require_interactive(f"confirmation ({message})")
    suffix = " [Y/n] " if default else " [y/N] "
    answer = input(message + suffix).strip().lower()
    if not answer:
        return default
    return answer in ("y", "yes")


def sudo_run(cmd: list, **kwargs) -> subprocess.CompletedProcess:
    """Run a command with sudo. Refuses in non-interactive mode: the automated
    (boot-time) path must work entirely without sudo."""
    require_interactive(f"`sudo {shlex.join(str(c) for c in cmd)}`")
    kwargs.setdefault("timeout", DEFAULT_SUDO_TIMEOUT)
    return run(["sudo", *cmd], **kwargs)


def download(url: str, dest: Path, *, timeout: float | _DefaultTimeout = _DEFAULT_TIMEOUT) -> None:
    """Download with socket and total deadlines, plus coarse TTY progress.

    Read one network chunk at a time so slow, continuous traffic cannot keep a
    large buffered read alive past the overall deadline. A pending socket read
    remains bounded by DOWNLOAD_READ_TIMEOUT.
    """
    if timeout is _DEFAULT_TIMEOUT:
        timeout = DOWNLOAD_TIMEOUT
    request = urllib.request.Request(url, headers={"User-Agent": "ci-runner-setup"})
    started = time.monotonic()
    deadline = started + timeout
    heartbeat = started + DOWNLOAD_HEARTBEAT_INTERVAL
    try:
        with urllib.request.urlopen(request, timeout=min(DOWNLOAD_READ_TIMEOUT, timeout)) as response, open(dest, "wb") as f:
            total = int(response.headers.get("Content-Length") or 0)
            done = 0
            last_pct = -1
            read_chunk = getattr(response, "read1", response.read)
            while True:
                if time.monotonic() >= deadline:
                    raise SetupError(f"download timed out after {timeout:g}s: {url}")
                chunk = read_chunk(256 * 1024)
                now = time.monotonic()
                if now >= deadline:
                    raise SetupError(f"download timed out after {timeout:g}s: {url}")
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                if now >= heartbeat:
                    amount = f"{done:,}/{total:,}" if total else f"{done:,}"
                    log(f"downloaded {amount} bytes after {now - started:.0f}s")
                    heartbeat = now + DOWNLOAD_HEARTBEAT_INTERVAL
                if total and sys.stdout.isatty():
                    pct = done * 100 // total
                    if pct != last_pct:
                        print(f"\r    downloading… {pct}%", end="", flush=True)
                        last_pct = pct
            if last_pct >= 0:
                print("\r" + " " * 40 + "\r", end="", flush=True)
    except OSError as e:
        raise SetupError(f"download failed: {url} ({e})") from e


def vtuple(version: str) -> tuple[int, ...]:
    """'2.328.0' -> (2, 328, 0). Raises ValueError on non-numeric parts."""
    return tuple(int(part) for part in version.strip().split("."))


def fmt_version(version: tuple[int, ...]) -> str:
    return ".".join(str(part) for part in version)


def runner_busy() -> bool:
    """True while the Actions runner is executing a job (Runner.Worker only
    exists during a job). Disruptive steps — runner updates, service
    restarts, Xcode/runtime deletion — defer themselves while this holds."""
    proc = run(["pgrep", "-x", "Runner.Worker"], check=False, capture=True)
    return proc.returncode == 0


STATE_DIR = Path.home() / "Library/Application Support/ci-runner-setup"

_lock_fd: int | None = None


def acquire_lock() -> str | None:
    """Take a machine-wide (per-user) lock so a manual run and the boot
    LaunchAgent never mutate state concurrently. Returns None when acquired,
    otherwise a description of the current holder."""
    global _lock_fd
    if _lock_fd is not None:
        return None
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    inherited = os.environ.pop("CI_SETUP_LOCK_FD", "")
    if inherited:
        try:
            fd = int(inherited)
            expected = (STATE_DIR / "setup.lock").stat()
            actual = os.fstat(fd)
            if (actual.st_dev, actual.st_ino) != (expected.st_dev, expected.st_ino):
                raise ValueError("wrong lock file")
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            os.set_inheritable(fd, False)
            _lock_fd = fd
            return None
        except (ValueError, OSError) as e:
            raise SetupError(f"could not retain setup lock across restart: {e}") from e
    # O_RDWR without truncation: the holder's record must survive our attempt.
    _lock_fd = os.open(STATE_DIR / "setup.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        try:
            holder = os.pread(_lock_fd, 256, 0).decode(errors="replace").strip()
        except OSError:
            holder = ""
        os.close(_lock_fd)
        _lock_fd = None
        return holder or "unknown holder"
    os.ftruncate(_lock_fd, 0)
    started = datetime.now().astimezone().isoformat(timespec="seconds")
    os.write(_lock_fd, f"pid {os.getpid()}, started {started}".encode())
    return None


def release_lock() -> None:
    global _lock_fd
    if _lock_fd is not None:
        os.close(_lock_fd)
        _lock_fd = None


def reexec_environment() -> dict[str, str]:
    """Keep the already-held lock across exec; never hand it to tool children."""
    if _lock_fd is None:
        raise SetupError("cannot restart setup without its lock")
    os.set_inheritable(_lock_fd, True)
    return dict(os.environ, CI_SETUP_LOCK_FD=str(_lock_fd))
