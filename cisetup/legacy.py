#
# This source file is part of the SchmiedmayerLab ci-setup open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT
#

"""Retire the specific Homebrew updater installed by the legacy CI setup.

Runs for explicitly adopted runners, while holding the setup lock and pausing
runner job intake. Other LaunchAgents and the old updater's files are untouched.
The archived plist can be restored; rollback also requires launchctl enable.
"""

from __future__ import annotations

import os
import plistlib
import re
from pathlib import Path
from xml.parsers.expat import ExpatError

from . import util
from .maintenance import MaintenanceDeferred
from .util import SetupError, ok, run

LABEL = "com.github.domt4.homebrew-autoupdate"
COMMAND_TIMEOUT = 15.0


def _program() -> Path:
    return Path.home() / "Library/Application Support" / LABEL / "brew_autoupdate"


def _field(output: str, name: str) -> str | None:
    match = re.search(r"(?m)^\s*" + re.escape(name) + r" = (.+)$", output)
    return match.group(1).strip() if match else None


def _service(target: str) -> str | None:
    result = run(
        ["launchctl", "print", target], check=False, capture=True,
        timeout=COMMAND_TIMEOUT,
    )
    if result.returncode == 0:
        return result.stdout
    message = (result.stderr or "") + (result.stdout or "")
    if "Could not find service" in message and LABEL in message:
        return None
    raise SetupError("cannot verify the legacy Homebrew updater's launchd state")


def _require_idle(output: str) -> None:
    if _field(output, "program") != str(_program()):
        raise SetupError("legacy Homebrew updater has an unexpected loaded program; leaving it untouched")
    pid = _field(output, "pid")
    state = _field(output, "state")
    if pid is not None or state not in {"not running", "waiting"}:
        raise MaintenanceDeferred(
            "legacy Homebrew updater is running or its idle state cannot be verified; "
            "retry adoption after it finishes"
        )


def retire_homebrew_autoupdate() -> None:
    """Disable/unload the known idle agent and archive its plist for rollback.

    Fail closed for an unexpected definition, ambiguous launchctl result, or
    active updater. launchctl offers no atomic 'unload only if idle' operation:
    recheck immediately before bootout, but an external start can still race it.
    Disabling prevents later loads; it is not treated as an atomic timer pause.
    """
    source = Path.home() / "Library/LaunchAgents" / f"{LABEL}.plist"
    archive = util.STATE_DIR / "legacy" / source.name
    target = f"gui/{os.getuid()}/{LABEL}"
    original = None
    if source.is_symlink():
        raise SetupError("legacy Homebrew updater plist is a symlink; leaving it untouched")
    if source.exists():
        original = source.read_bytes()
        try:
            definition = plistlib.loads(original)
        except (ValueError, plistlib.InvalidFileException, ExpatError) as error:
            raise SetupError("cannot read the legacy Homebrew updater plist") from error
        if not isinstance(definition, dict) or (
            definition.get("Label") != LABEL
            or definition.get("Program") != str(_program())
        ):
            raise SetupError("legacy Homebrew updater plist has an unexpected label or program")
        if archive.is_symlink() or (archive.exists() and archive.read_bytes() != original):
            raise SetupError(f"a different legacy updater backup already exists at {archive}")

    loaded = _service(target)
    if loaded is None and original is None:
        ok("legacy Homebrew updater is already retired or absent")
        return
    if original is None:
        raise SetupError("legacy Homebrew updater is loaded but its plist is missing; cannot preserve rollback")
    if loaded is not None:
        _require_idle(loaded)

    # Persistently prevent a future login/bootstrap from loading this updater.
    # If a new active process is found afterwards, leave it alone and retain
    # the original plist so adoption can be retried after the process exits.
    run(["launchctl", "disable", target], capture=True, timeout=COMMAND_TIMEOUT)
    loaded = _service(target)
    if loaded is not None:
        _require_idle(loaded)
        run(
            ["launchctl", "bootout", target], check=False, capture=True,
            timeout=COMMAND_TIMEOUT,
        )
    if _service(target) is not None:
        raise SetupError("legacy Homebrew updater is still loaded; original plist retained")

    if source.is_symlink() or source.read_bytes() != original:
        raise SetupError("legacy Homebrew updater plist changed during adoption; leaving it in place")
    archive.parent.mkdir(parents=True, exist_ok=True)
    if archive.is_symlink() or archive.exists():
        # An interrupted prior adoption may have already archived identical
        # contents. Never overwrite a different rollback copy.
        if archive.is_symlink() or archive.read_bytes() != original:
            raise SetupError(f"legacy updater backup changed during adoption: {archive}")
        source.unlink()
    else:
        # Exclusive creation also protects a rollback copy created after the
        # earlier check. Keep the source until the backup is fully written.
        try:
            with archive.open("xb") as backup:
                backup.write(original)
                backup.flush()
                os.fsync(backup.fileno())
        except FileExistsError as error:
            raise SetupError(f"legacy updater backup appeared during adoption: {archive}") from error
        source.unlink()
    ok(f"retired legacy Homebrew updater; rollback plist: {archive}")
