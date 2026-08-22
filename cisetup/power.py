"""Machine settings: power management (keep the CI box awake; sudo, so
interactive runs only), screen saver, and Spotlight exclusions."""

from __future__ import annotations

import shutil
from pathlib import Path

from . import util
from .config import Config
from .util import log, ok, output, run, warn

_DERIVED_DATA_NOINDEX = Path.home() / "Library/Developer/Xcode/DerivedData.noindex"


def ensure_spotlight_exclusions() -> None:
    """Keep Spotlight's mdworker away from build artifacts: macOS reliably
    skips directories whose name ends in `.noindex` (the old
    .metadata_never_index marker is no longer honored). The runner work dir
    carries the suffix via its configured name; DerivedData is relocated
    through Xcode's own preference, which xcodebuild honors as well.
    User-level defaults — no sudo, unattended-safe."""
    domain, key = "com.apple.dt.Xcode", "IDECustomDerivedDataLocation"
    current = run(["defaults", "read", domain, key], check=False, capture=True)
    if current.returncode == 0 and current.stdout.strip() == str(_DERIVED_DATA_NOINDEX):
        ok("DerivedData lives outside Spotlight indexing")
    else:
        run(["defaults", "write", domain, key, "-string", str(_DERIVED_DATA_NOINDEX)])
        ok(f"DerivedData relocated to {_DERIVED_DATA_NOINDEX}")
    # The previous default location is a pure cache; reclaim it when idle.
    legacy = Path.home() / "Library/Developer/Xcode/DerivedData"
    if legacy.exists():
        if util.runner_busy():
            warn("removal of the old DerivedData directory deferred: a job is running")
        else:
            shutil.rmtree(legacy, ignore_errors=True)
            ok("removed the old DerivedData directory")

# sleep 0:        never sleep the system
# displaysleep 0: never sleep the display (simulators/UI tests keep running)
# disksleep 0:    never spin down disks
# autorestart 1:  come back automatically after a power failure
DESIRED = {"sleep": "0", "displaysleep": "0", "disksleep": "0", "autorestart": "1"}


def _current() -> dict[str, str]:
    """AC-power settings from `pmset -g custom`. On laptops the output has a
    'Battery Power:' section first — only the AC section matters for CI."""
    settings: dict[str, str] = {}
    try:
        text = output(["pmset", "-g", "custom"])
    except util.SetupError:
        return settings
    in_ac_section = True  # desktops print no section headers at all
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.endswith("Power:"):
            in_ac_section = stripped.startswith("AC")
            continue
        parts = stripped.split()
        if in_ac_section and len(parts) >= 2 and parts[0] in DESIRED:
            settings[parts[0]] = parts[1]
    return settings


def _ensure_screensaver_off() -> None:
    """User-level, no sudo — safe in unattended runs. A kicking-in screen
    saver costs CPU/GPU while UI tests run."""
    current = run(
        ["defaults", "-currentHost", "read", "com.apple.screensaver", "idleTime"],
        check=False,
        capture=True,
    )
    if current.returncode == 0 and current.stdout.strip() == "0":
        ok("screen saver disabled")
        return
    run(["defaults", "-currentHost", "write", "com.apple.screensaver", "idleTime", "-int", "0"])
    ok("screen saver disabled")


def ensure(cfg: Config) -> None:
    if not cfg.power_manage:
        return
    _ensure_screensaver_off()
    current = _current()
    # Only converge keys pmset actually reports; retrying a setting the
    # hardware never echoes back would re-run sudo on every converge.
    diffs = {k: v for k, v in DESIRED.items() if k in current and current[k] != v}
    for key in DESIRED:
        if current and key not in current:
            warn(f"pmset does not report '{key}' on this machine — leaving it alone")
    if not diffs:
        ok("power settings already configured")
        return
    if not util.INTERACTIVE:
        warn(
            "power settings need sudo and were skipped in this unattended run — "
            "run ./setup.zsh interactively once"
        )
        return
    args = [item for pair in diffs.items() for item in pair]
    log(f"Configuring power settings: sudo pmset -a {' '.join(args)}")
    util.sudo_run(["pmset", "-a", *args])
    ok("power settings configured")
