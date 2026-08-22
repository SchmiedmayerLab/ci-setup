"""Shared helpers: logging, subprocess wrappers, prompts, download, locking."""

from __future__ import annotations

import fcntl
import shlex
import subprocess
import sys
import urllib.request
from pathlib import Path


class SetupError(Exception):
    """Fatal, user-facing error. Raised instead of printing a traceback."""


# main() sets this; when False, nothing may prompt and nothing may sudo.
INTERACTIVE = True

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
    print(f"{YELLOW}  !{OFF} {msg}", flush=True)


def err(msg: str) -> None:
    print(f"{RED}  ✗{OFF} {msg}", file=sys.stderr, flush=True)


def run(
    cmd: list,
    *,
    check: bool = True,
    capture: bool = False,
    cwd=None,
    env: dict | None = None,
    stdin_devnull: bool = False,
) -> subprocess.CompletedProcess:
    """Run a command. With capture=True stdout/stderr are collected; otherwise
    the child inherits our stdio so long-running tools stay visible."""
    argv = [str(c) for c in cmd]
    kwargs: dict = {"cwd": str(cwd) if cwd else None, "env": env, "text": True}
    if capture:
        kwargs["stdout"] = subprocess.PIPE
        kwargs["stderr"] = subprocess.PIPE
    if stdin_devnull or not INTERACTIVE:
        # Never let a child block on stdin during unattended runs.
        if stdin_devnull or not sys.stdin.isatty():
            kwargs["stdin"] = subprocess.DEVNULL
    proc = subprocess.run(argv, **kwargs)
    if check and proc.returncode != 0:
        detail = ""
        if capture and proc.stderr:
            detail = f" — {proc.stderr.strip()[:400]}"
        raise SetupError(
            f"command failed (exit {proc.returncode}): {shlex.join(argv)}{detail}"
        )
    return proc


def output(cmd: list, **kwargs) -> str:
    """Run a command and return its stripped stdout."""
    return run(cmd, capture=True, **kwargs).stdout.strip()


def require_interactive(what: str) -> None:
    if not INTERACTIVE:
        raise SetupError(
            f"{what} requires an interactive run — invoke ./setup.zsh manually on the machine"
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
    return run(["sudo", *cmd], **kwargs)


def download(url: str, dest: Path) -> None:
    """Download a file with a coarse progress display when on a TTY."""
    request = urllib.request.Request(url, headers={"User-Agent": "ci-runner-setup"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response, open(dest, "wb") as f:
            total = int(response.headers.get("Content-Length") or 0)
            done = 0
            last_pct = -1
            while chunk := response.read(256 * 1024):
                f.write(chunk)
                done += len(chunk)
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


_lock_file = None


def acquire_lock() -> bool:
    """Take a machine-wide (per-user) lock so a manual run and the boot
    LaunchAgent never converge concurrently. Returns False if already held."""
    global _lock_file
    lock_dir = Path.home() / "Library/Application Support/ci-runner-setup"
    lock_dir.mkdir(parents=True, exist_ok=True)
    _lock_file = open(lock_dir / "setup.lock", "w")
    try:
        fcntl.flock(_lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False
