"""Machine settings: power management (keep the CI box awake; sudo, so
interactive runs only), screen saver, and Spotlight exclusions."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from . import util
from .config import Config
from .util import log, ok, output, run, warn

_DERIVED_DATA_NOINDEX = Path.home() / "Library/Developer/Xcode/DerivedData.noindex"

# Umbrella for redirected cache dirs whose locations are hardcoded into build
# tools (SwiftPM & co.). *.noindex trees are skipped by Spotlight.
_NOINDEX_ROOT = Path.home() / "Library/Caches/ci-setup.noindex"


def _ensure_noindex_redirect(path: Path) -> None:
    """Exclude a build-tool-hardcoded path from Spotlight by turning it into
    a symlink into a .noindex tree. Two Spotlight behaviors make this
    reliable: *.noindex directories are skipped, and the indexer never
    follows symlinks — so tools keep their usual path while mdworker never
    sees the content. (The GUI's Privacy list has no supported CLI; its
    root-owned plist is rewritten by mds at will, so we do not touch it.)"""
    target = _NOINDEX_ROOT / path.name
    if path.is_symlink():
        if os.path.realpath(path) == os.path.realpath(target) and target.is_dir():
            return  # converged; stay quiet on re-runs
        path.unlink()
    target.mkdir(parents=True, exist_ok=True)
    if path.exists() and not path.is_symlink():
        if util.runner_busy():
            warn(f"Spotlight redirect of {path} deferred: a job is running")
            return
        # Preserve the warm cache: move the contents into the target.
        for child in path.iterdir():
            shutil.move(str(child), str(target / child.name))
        path.rmdir()
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.is_symlink() and not path.exists():
        path.symlink_to(target)
    ok(f"excluded from Spotlight (noindex redirect): {path}")


def ensure_spotlight_exclusions(cfg: Config) -> None:
    """Keep Spotlight's mdworker away from build artifacts: macOS reliably
    skips directories whose name ends in `.noindex` (the old
    .metadata_never_index marker is no longer honored). The runner work dir
    carries the suffix via its configured name; DerivedData is relocated
    through Xcode's own preference, which xcodebuild honors as well; paths
    hardcoded into build tools get noindex symlink redirects.
    User-level only — no sudo, unattended-safe."""
    for raw in cfg.spotlight_noindex_paths:
        _ensure_noindex_redirect(Path(os.path.expanduser(raw)))
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
            "run ./setup interactively once"
        )
        return
    args = [item for pair in diffs.items() for item in pair]
    log(f"Configuring power settings: sudo pmset -a {' '.join(args)}")
    util.sudo_run(["pmset", "-a", *args])
    ok("power settings configured")
