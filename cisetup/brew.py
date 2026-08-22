"""Homebrew-managed CI tooling (plus the xcpretty gem)."""

from __future__ import annotations

import json
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


def ensure(cfg: Config) -> BrewEnv:
    log("Homebrew packages")
    if not shutil.which("brew"):
        raise SetupError("Homebrew not found on PATH — run via ./setup")

    formulae = FORMULAE + [f for f in cfg.brew_extra_formulae if f not in FORMULAE]
    casks = CASKS + [c for c in cfg.brew_extra_casks if c not in CASKS]

    update = run(["brew", "update", "--quiet"], check=False)
    if update.returncode != 0:
        warn("`brew update` failed (offline?) — continuing with the local package index")

    formula_map, cask_map = _resolve_names(formulae, casks)

    installed_formulae = set(output(["brew", "list", "--formula", "-1"]).split())
    missing_formulae = [f for f in formulae if formula_map[f] not in installed_formulae]
    if missing_formulae:
        log(f"Installing formulae: {', '.join(missing_formulae)}")
        # --yes: since Homebrew 6, install/upgrade ask for confirmation by
        # default; answer yes so runs (especially unattended ones) never stall.
        run(["brew", "install", "--yes", "--formula", *missing_formulae])

    if casks:
        installed_casks = set(output(["brew", "list", "--cask", "-1"], check=False).split())
        missing_casks = [c for c in casks if cask_map[c] not in installed_casks]
        if missing_casks and not util.INTERACTIVE:
            # Cask installers frequently sudo; keep the unattended run alive
            # and leave them for the next manual converge.
            warn(
                f"skipping cask install in unattended mode (may need sudo): "
                f"{', '.join(missing_casks)}"
            )
        elif missing_casks:
            log(f"Installing casks: {', '.join(missing_casks)} (may require sudo)")
            run(["brew", "install", "--yes", "--cask", *missing_casks])

    wanted_canonical = {formula_map[f] for f in formulae}
    outdated = set(output(["brew", "outdated", "--formula", "--quiet"], check=False).split())
    upgrades = sorted(wanted_canonical & outdated)
    if upgrades and util.runner_busy():
        # Swapping tool binaries under a running job is as disruptive as a
        # runner update; the next converge retries.
        warn(f"brew upgrades deferred (a job is running): {', '.join(upgrades)}")
        upgrades = []
    elif upgrades:
        log(f"Upgrading: {', '.join(upgrades)}")
        run(["brew", "upgrade", "--yes", "--formula", *upgrades])
    if casks:
        outdated_casks = set(
            output(["brew", "outdated", "--cask", "--quiet"], check=False).split()
        )
        cask_upgrades = sorted({cask_map[c] for c in casks} & outdated_casks)
        if cask_upgrades and (not util.INTERACTIVE or util.runner_busy()):
            warn(f"cask upgrades deferred (sudo/busy): {', '.join(cask_upgrades)}")
        elif cask_upgrades:
            log(f"Upgrading casks: {', '.join(cask_upgrades)}")
            run(["brew", "upgrade", "--yes", "--cask", *cask_upgrades])

    if not missing_formulae and not upgrades:
        ok(f"all {len(formulae)} formulae installed and current")

    _remove_autoupdate()

    # git-lfs needs a one-time (idempotent) hook into the user's gitconfig.
    run(["git", "lfs", "install"], capture=True)

    gem_bin = _ensure_xcpretty()

    python_canonical = formula_map.get("python3", "python3")
    python_changed = "python3" in missing_formulae or python_canonical in upgrades

    prefix = output(["brew", "--prefix"])
    return BrewEnv(
        prefix=prefix,
        openjdk_prefix=f"{prefix}/opt/openjdk",
        gem_bin=gem_bin,
        python_changed=python_changed,
    )
