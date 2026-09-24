#
# This source file is part of the SchmiedmayerLab ci-setup open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT
#

"""Homebrew-managed CI tooling (plus the xcpretty gem)."""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from . import util
from .config import Config
from .util import SetupError, log, ok, output, run, warn

# Notes:
#  - java: `openjdk` (formula) instead of a JDK cask, because casks run
#    `sudo installer`. openjdk is keg-only and sudo-free; jobs get it via
#    JAVA_HOME/PATH wired into the runner's .env/.path (see runner.py).
#  - python3 resolves to the current python@3.x formula via a brew alias.
#  - xcpretty is not in Homebrew; it is installed as a Ruby user gem below.
FORMULAE = [
    "aria2",
    "fastlane",
    "firebase-cli",
    "git-lfs",
    "jq",
    "node",
    "openjdk",
    "periphery",
    "python3",
    "swiftlint",
    "xcbeautify",
    "xcodes",
]
CASKS: list[str] = []


@dataclass
class BrewEnv:
    """Paths the rest of the setup needs for wiring the runner's job env."""

    prefix: str
    openjdk_prefix: str
    gem_bin: str | None
    # True when this run installed/upgraded the python3 the setup itself runs
    # on — the caller must re-exec before anything lazily imports stdlib bits.
    python_changed: bool = False


def _resolve_names(formulae: list[str], casks: list[str]) -> tuple[dict, dict]:
    """Map requested names to canonical names/tokens (resolving brew aliases
    like python3 -> python@3.x), so `brew list` comparisons are accurate."""
    names = formulae + casks
    formula_map = {name: name for name in formulae}
    cask_map = {name: name for name in casks}
    if not names:
        return formula_map, cask_map
    try:
        info = json.loads(output(["brew", "info", "--json=v2", *names]))
    except (SetupError, json.JSONDecodeError) as e:
        raise SetupError(
            f"`brew info` failed — is one of the configured packages misspelled? ({e})"
        ) from e
    alias_to_name: dict[str, str] = {}
    for formula in info.get("formulae", []):
        alias_to_name[formula["name"]] = formula["name"]
        for alias in formula.get("aliases") or []:
            alias_to_name[alias] = formula["name"]
        for old in formula.get("oldnames") or []:
            alias_to_name[old] = formula["name"]
    token_map = {cask["token"]: cask["token"] for cask in info.get("casks", [])}
    for name in formulae:
        # Tap-qualified names ("user/tap/tool") appear as short names in
        # `brew list` output; compare on the short name.
        fallback = name.rsplit("/", 1)[-1]
        formula_map[name] = alias_to_name.get(name) or alias_to_name.get(fallback) or fallback
    for name in casks:
        fallback = name.rsplit("/", 1)[-1]
        cask_map[name] = token_map.get(name) or token_map.get(fallback) or fallback
    return formula_map, cask_map


def _ensure_xcpretty() -> str | None:
    """xcpretty has no brew formula; install it as a user gem with the system
    Ruby (no sudo). Returns the gem bin dir that jobs need on PATH."""
    gem = "/usr/bin/gem"
    ruby = "/usr/bin/ruby"
    if not (Path(gem).exists() and Path(ruby).exists()):
        warn(
            "system Ruby not found — skipping xcpretty "
            "(install it manually, e.g. via a Homebrew ruby + `gem install xcpretty`)"
        )
        return None
    gem_bin = output([ruby, "-e", "print Gem.user_dir"]) + "/bin"
    installed = run(
        [gem, "list", "--installed", "^xcpretty$"], check=False, capture=True
    )
    if installed.returncode != 0:
        log("Installing xcpretty (ruby user gem)")
        run([gem, "install", "--user-install", "--no-document", "xcpretty"])
        ok("xcpretty installed")
    else:
        ok("xcpretty already installed")
    return gem_bin


def _remove_autoupdate() -> None:
    """Earlier revisions enabled homebrew/autoupdate; it is redundant next to
    the 6-hourly converge and dangerous besides — it upgrades on its own
    schedule with no busy-guard, so it could swap binaries (even python3)
    under a running job or converge. Actively dismantle it where present."""
    status = run(["brew", "autoupdate", "status"], check=False, capture=True)
    if status.returncode != 0 or "not configured" in (status.stdout or ""):
        return
    result = run(["brew", "autoupdate", "delete"], check=False, capture=True)
    if result.returncode == 0:
        ok("removed the obsolete brew autoupdate agent")
    else:
        warn("could not remove the brew autoupdate agent — run: brew autoupdate delete")


def probe() -> BrewEnv:
    """Just resolve paths, without installing anything (used by --skip-brew)."""
    if not shutil.which("brew"):
        raise SetupError("Homebrew not found on PATH — run via ./setup")
    prefix = output(["brew", "--prefix"])
    openjdk_prefix = f"{prefix}/opt/openjdk"
    gem_bin = None
    if Path("/usr/bin/ruby").exists():
        gem_bin = output(["/usr/bin/ruby", "-e", "print Gem.user_dir"]) + "/bin"
    return BrewEnv(prefix=prefix, openjdk_prefix=openjdk_prefix, gem_bin=gem_bin)


class BrewError(SetupError):
    """A partial package failure with usable paths for independent phases."""

    def __init__(self, message: str, env: BrewEnv):
        super().__init__(message)
        self.env = env


def ensure(cfg: Config) -> BrewEnv:
    log("Homebrew packages")
    if util.runner_busy():
        raise SetupError("Homebrew changes deferred: a job is running")
    brew_env = probe()
    # Retain the running interpreter's keg until the whole maintenance pass
    # ends. No tool may auto-refresh the index between individual upgrades.
    command_env = dict(os.environ, HOMEBREW_NO_AUTO_UPDATE="1",
                       HOMEBREW_NO_INSTALL_CLEANUP="1")
    errors: list[str] = []

    def attempt(cmd: list[str], *, capture: bool = False) -> bool:
        try:
            run(cmd, env=command_env, capture=capture)
            return True
        except SetupError as e:
            errors.append(str(e))
            warn(str(e))
            return False

    attempt(["brew", "update", "--quiet"])
    formulae = list(dict.fromkeys(FORMULAE + cfg.brew_extra_formulae))
    casks = list(dict.fromkeys(CASKS + cfg.brew_extra_casks))
    formula_map, cask_map = _resolve_names(formulae, casks)
    installed = set(output(["brew", "list", "--formula", "-1"], env=command_env).split())
    missing = [f for f in formulae if formula_map[f] not in installed]
    outdated = set(output(["brew", "outdated", "--formula", "--quiet"], env=command_env).split())
    pinned = set(output(["brew", "list", "--pinned"], env=command_env).split())
    wanted = set(formula_map.values())
    held = sorted(wanted & outdated & pinned)
    if held:
        message = "pinned formulae remain outdated (pins respected): " + ", ".join(held)
        errors.append(message)
        warn(message)
    upgrades = sorted((wanted & outdated) - pinned)
    python_name = formula_map.get("python3", "python3")
    for name in missing:
        log(f"Installing formula: {name}")
        if attempt(["brew", "install", "--yes", "--formula", name]):
            brew_env.python_changed |= formula_map[name] == python_name
    for name in upgrades:
        log(f"Upgrading formula: {name}")
        if attempt(["brew", "upgrade", "--yes", "--formula", name]):
            brew_env.python_changed |= name == python_name

    if casks:
        installed_casks = set(output(["brew", "list", "--cask", "-1"], env=command_env).split())
        outdated_casks = set(output(["brew", "outdated", "--cask", "--quiet"], env=command_env).split())
        for name in casks:
            action = "install" if cask_map[name] not in installed_casks else "upgrade"
            if action == "upgrade" and cask_map[name] not in outdated_casks:
                continue
            if not util.INTERACTIVE:
                message = f"cask {action} deferred (may require sudo): {name}"
                warn(message)
                errors.append(message)
            else:
                attempt(["brew", action, "--yes", "--cask", name])

    # Each independent operation gets a chance even when another package
    # failed. Never retry destructive commands blindly.
    try:
        _remove_autoupdate()
    except SetupError as e:
        errors.append(str(e))
    attempt(["git", "lfs", "install"], capture=True)
    try:
        brew_env.gem_bin = _ensure_xcpretty()
    except SetupError as e:
        errors.append(str(e))
        warn(str(e))
    if errors:
        raise BrewError("Homebrew incomplete: " + "; ".join(errors), brew_env)
    ok(f"all {len(formulae)} managed formulae installed and current")
    return brew_env
