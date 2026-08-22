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
    settings: dict[str, str] = {}
    try:
        text = output(["pmset", "-g", "custom"])
    except util.SetupError:
        return settings
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] in DESIRED and parts[0] not in settings:
            settings[parts[0]] = parts[1]
    return settings


def ensure(cfg: Config) -> None:
    if not cfg.power_manage:
        return
    current = _current()
    diffs = {k: v for k, v in DESIRED.items() if current.get(k) != v}
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
