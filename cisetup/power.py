"""Optional power management: keep the machine awake as a CI box.
Needs sudo, therefore interactive runs only; boot-time runs skip it."""

from __future__ import annotations

from . import util
from .config import Config
from .util import log, ok, output, warn

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


def ensure(cfg: Config) -> None:
    if not cfg.power_manage:
        return
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
