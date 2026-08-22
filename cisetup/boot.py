"""A per-user LaunchAgent that re-runs `setup.zsh converge --non-interactive`
at every login (with auto-login: every boot), so the runner re-converges and
re-registers itself without anyone touching the machine. No sudo involved."""

from __future__ import annotations

import os
import plistlib
from pathlib import Path

from .config import Config
from .util import STATE_DIR, ok, run, warn

LOG_PATH = Path.home() / "Library/Logs/ci-runner-setup.log"

# Set inside the agent's plist; lets a converge detect that it IS the boot
# agent's process (setup.zsh execs into python, so launchd tracks our PID).
_MARKER_ENV = "CI_SETUP_BOOT_AGENT"

# Records which label we last installed, so renaming boot.label (or disabling
# the agent) cleans up the old LaunchAgent instead of orphaning it.
_LABEL_STATE = STATE_DIR / "boot-agent-label"


def _agents_dir() -> Path:
    return Path.home() / "Library/LaunchAgents"


def _plist_path(label: str) -> Path:
    return _agents_dir() / f"{label}.plist"


def _plist_bytes(cfg: Config) -> bytes:
    return plistlib.dumps(
        {
            "Label": cfg.boot_label,
            "ProgramArguments": [
                str(cfg.repo_root / "setup.zsh"),
                "converge",
                "--non-interactive",
            ],
            "EnvironmentVariables": {_MARKER_ENV: cfg.boot_label},
            "RunAtLoad": True,
            # Also converge daily (03:30, or on the next wake), so long-lived
            # login sessions still pick up new Xcode releases, runner updates,
            # and config changes without a reboot. Disruptive steps defer
            # themselves while a job is running (see util.runner_busy).
            "StartCalendarInterval": {"Hour": 3, "Minute": 30},
            "WorkingDirectory": str(cfg.repo_root),
            "StandardOutPath": str(LOG_PATH),
            "StandardErrorPath": str(LOG_PATH),
        },
        sort_keys=True,
    )


def _domain() -> str:
    return f"gui/{os.getuid()}"


def _running_as_agent(label: str) -> bool:
    return os.environ.get(_MARKER_ENV) == label


def _loaded(label: str) -> bool:
    result = run(
        ["launchctl", "print", f"{_domain()}/{label}"], check=False, capture=True
    )
    return result.returncode == 0


def _bootout(label: str) -> None:
    run(["launchctl", "bootout", f"{_domain()}/{label}"], check=False, capture=True)


def _recorded_label() -> str | None:
    try:
        return _LABEL_STATE.read_text().strip() or None
    except OSError:
        return None


def _record_label(label: str) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    _LABEL_STATE.write_text(label + "\n")


def ensure(cfg: Config) -> None:
    if not cfg.boot_install:
        # install_agent was turned off — actively remove a previous agent.
        if _recorded_label() or _plist_path(cfg.boot_label).exists():
            remove(cfg)
        return

    # A previously installed agent under a different label is stale.
    previous = _recorded_label()
    if previous and previous != cfg.boot_label:
        if _running_as_agent(previous):
            warn(
                f"boot.label changed while running under the old agent "
                f"({previous}) — finish this run, then converge once manually"
            )
        else:
            _bootout(previous)
            _plist_path(previous).unlink(missing_ok=True)
            ok(f"removed old boot agent ({previous})")

    target = _plist_path(cfg.boot_label)
    desired = _plist_bytes(cfg)
    changed = not target.exists() or target.read_bytes() != desired

    if changed:
        # Write first: even if anything below goes sideways, the correct
        # definition is on disk and the next login uses it.
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(desired)
    _record_label(cfg.boot_label)

    if _running_as_agent(cfg.boot_label):
        # We ARE the launchd job — bootout would kill this very process.
        # launchd reads the updated plist at the next login on its own.
        if changed:
            ok("boot agent definition updated (takes effect at the next login)")
        else:
            ok("boot agent is current")
        return

    if changed or not _loaded(cfg.boot_label):
        if changed:
            _bootout(cfg.boot_label)
        run(
            ["launchctl", "enable", f"{_domain()}/{cfg.boot_label}"],
            check=False,
            capture=True,
        )
        result = run(
            ["launchctl", "bootstrap", _domain(), str(target)],
            check=False,
            capture=True,
        )
        if result.returncode != 0:
            # Typical over SSH, where no gui/ domain is reachable. The plist is
            # in place, so it still loads at the next login.
            warn(
                "boot agent installed but could not be loaded now "
                f"({(result.stderr or '').strip()[:120] or 'no gui session — SSH?'}); "
                "it will load at the next login"
            )
        else:
            ok(f"boot agent loaded ({cfg.boot_label})")
    else:
        ok("boot agent is current")


def remove(cfg: Config) -> None:
    labels = {cfg.boot_label}
    recorded = _recorded_label()
    if recorded:
        labels.add(recorded)
    removed = False
    for label in labels:
        if _running_as_agent(label):
            warn(f"not removing boot agent {label} from within its own run")
            continue
        _bootout(label)
        plist = _plist_path(label)
        if plist.exists():
            plist.unlink()
            removed = True
    _LABEL_STATE.unlink(missing_ok=True)
    if removed:
        ok("boot agent removed")
