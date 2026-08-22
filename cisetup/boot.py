"""A per-user LaunchAgent that re-runs `setup.zsh converge --non-interactive`
at every login (with auto-login: every boot), so the runner re-converges and
re-registers itself without anyone touching the machine. No sudo involved."""

from __future__ import annotations

import os
import plistlib
from pathlib import Path

from .config import Config
from .util import log, ok, run, warn

LOG_PATH = Path.home() / "Library/Logs/ci-runner-setup.log"


def _agent_path(cfg: Config) -> Path:
    return Path.home() / "Library/LaunchAgents" / f"{cfg.boot_label}.plist"


def _plist_bytes(cfg: Config) -> bytes:
    return plistlib.dumps(
        {
            "Label": cfg.boot_label,
            "ProgramArguments": [
                str(cfg.repo_root / "setup.zsh"),
                "converge",
                "--non-interactive",
            ],
            "RunAtLoad": True,
            "WorkingDirectory": str(cfg.repo_root),
            "StandardOutPath": str(LOG_PATH),
            "StandardErrorPath": str(LOG_PATH),
        },
        sort_keys=True,
    )


def _domain() -> str:
    return f"gui/{os.getuid()}"


def _loaded(cfg: Config) -> bool:
    result = run(
        ["launchctl", "print", f"{_domain()}/{cfg.boot_label}"],
        check=False,
        capture=True,
    )
    return result.returncode == 0


def ensure(cfg: Config) -> None:
    if not cfg.boot_install:
        return
    target = _agent_path(cfg)
    desired = _plist_bytes(cfg)
    changed = not target.exists() or target.read_bytes() != desired

    if changed:
        # Unload any old definition before replacing it (ignore "not loaded").
        run(
            ["launchctl", "bootout", f"{_domain()}/{cfg.boot_label}"],
            check=False,
            capture=True,
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(desired)

    if changed or not _loaded(cfg):
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
    target = _agent_path(cfg)
    run(
        ["launchctl", "bootout", f"{_domain()}/{cfg.boot_label}"],
        check=False,
        capture=True,
    )
    if target.exists():
        target.unlink()
        ok("boot agent removed")
