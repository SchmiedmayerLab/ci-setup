"""Xcode releases via `xcodes`: keep the latest stable release, the previous
minor release, and (optionally) the newest beta/RC installed — including
simulator runtimes, SDKs and the Metal toolchain for each of them."""

from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from . import util
from .config import Config
from .util import SetupError, log, ok, output, run, warn

# Matches `xcodes list` lines. xcodes 2.0.1+ appends a bracketed architecture
# label after the build; 1.x has none. Annotations like "(Installed, Selected)"
# may follow. Examples:
#   16.4 (16F6)
#   16.4 (16F6) [Universal] (Installed)
#   26.0 Beta 5 (17A5295f) [Apple Silicon]
#   26.1 Release Candidate (17B35)
# The architecture label must NOT become part of the identifier — identifiers
# are passed to `xcodes install` and compared against `xcodes installed`.
_LIST_RE = re.compile(
    r"^(?P<version>\d+(?:\.\d+){0,2})"
    r"(?P<pre> Beta(?: \d+)?| Release Candidate(?: \d+)?)?"
    r" \((?P<build>[0-9A-Za-z]+)\)"
    r"(?: \[[^\]]*\])?"
    r"(?P<annotations>(?: \([^)]*\))*)\s*$"
)

# Matches `xcodes installed` lines (same optional arch label / annotations):
#   16.4 (16F6)          /Applications/Xcode-16.4.0.app
#   26.0 (17A324) [Apple Silicon] (Selected)  /Applications/Xcode.app
_INSTALLED_RE = re.compile(
    r"^(?P<identifier>.+?) \((?P<build>[0-9A-Za-z]+)\)"
    r"(?: \[[^\]]*\])?"
    r"(?: \([^)]*\))*"
    r"\s+(?P<path>/.+?)\s*$"
)


@dataclass(frozen=True)
class Release:
    version: tuple[int, ...]
    # () for stable, ("beta", n) or ("rc", n) for prereleases
    pre: tuple
    identifier: str  # what `xcodes install` expects, e.g. "26.0 Beta 5"
    build: str

    @property
    def prerelease(self) -> bool:
        return bool(self.pre)

    def sort_key(self):
        if not self.pre:
            kind, number = 3, 0
        elif self.pre[0] == "rc":
            kind, number = 2, self.pre[1]
        else:
            kind, number = 1, self.pre[1]
        return (self.version, kind, number)


@dataclass(frozen=True)
class InstalledXcode:
    identifier: str
    build: str
    path: str


def _parse_pre(raw: str | None) -> tuple:
    if not raw:
        return ()
    raw = raw.strip()
    number_match = re.search(r"(\d+)$", raw)
    number = int(number_match.group(1)) if number_match else 1
    if raw.startswith("Release Candidate"):
        return ("rc", number)
    return ("beta", number)


def parse_list(text: str) -> list[Release]:
    releases = []
    for line in text.splitlines():
        match = _LIST_RE.match(line.strip())
        if not match:
            continue
        pre = _parse_pre(match.group("pre"))
        version = tuple(int(p) for p in match.group("version").split("."))
        identifier = match.group("version") + (match.group("pre") or "")
        releases.append(
            Release(
                version=version,
                pre=pre,
                identifier=identifier,
                build=match.group("build"),
            )
        )
    return releases


def parse_installed(text: str) -> list[InstalledXcode]:
    installed = []
    for line in text.splitlines():
        match = _INSTALLED_RE.match(line.strip())
        if not match:
            continue
        installed.append(
            InstalledXcode(
                identifier=match.group("identifier").strip(),
                build=match.group("build"),
                path=match.group("path"),
            )
        )
    return installed


def select_desired(
    releases: list[Release], install_beta: bool
) -> tuple[list[Release], Release]:
    """Pick (desired releases, latest stable): the newest stable release, the
    newest release of the previous minor train, and — when it is newer than
    the newest stable — the newest beta/RC."""
    stables = [r for r in releases if not r.prerelease]
    if not stables:
        raise SetupError("could not parse any stable Xcode releases from `xcodes list`")
    latest = max(stables, key=Release.sort_key)
    latest_train = latest.version[:2]
    previous_pool = [r for r in stables if r.version[:2] != latest_train]
    previous = max(previous_pool, key=Release.sort_key) if previous_pool else None
    beta = None
    if install_beta:
        newer_prereleases = [
            r for r in releases if r.prerelease and r.version > latest.version
        ]
        if newer_prereleases:
            beta = max(newer_prereleases, key=Release.sort_key)
    desired = [r for r in (latest, previous, beta) if r is not None]
    return desired, latest


def _xcodebuild(args: list[str], developer_dir: Path, *, check: bool = True):
    env = dict(os.environ, DEVELOPER_DIR=str(developer_dir))
    return run(["/usr/bin/xcodebuild", *args], env=env, check=check)


def _post_install(cfg: Config, release: Release, app_path: Path) -> None:
    """First-launch setup, simulator/SDK platforms and Metal toolchain for one
    installed Xcode. Everything here is idempotent; xcodebuild itself skips
    components that are already present and current."""
    developer_dir = app_path / "Contents/Developer"
    if not developer_dir.exists():
        warn(f"Xcode {release.identifier}: {developer_dir} missing — skipping")
        return

    status = run(
        ["/usr/bin/xcodebuild", "-checkFirstLaunchStatus"],
        env=dict(os.environ, DEVELOPER_DIR=str(developer_dir)),
        check=False,
        capture=True,
    )
    if status.returncode != 0:
        log(f"Xcode {release.identifier}: running first-launch setup")
        result = _xcodebuild(["-runFirstLaunch"], developer_dir, check=False)
        if result.returncode != 0:
            # Typically means the license/packages need admin rights.
            if util.INTERACTIVE:
                log(f"Xcode {release.identifier}: retrying first-launch setup with sudo")
                # Invoke this Xcode's own xcodebuild: sudo's env_reset would
                # strip a DEVELOPER_DIR passed via the environment, silently
                # running first-launch against the wrong Xcode.
                util.sudo_run(
                    [developer_dir / "usr/bin/xcodebuild", "-runFirstLaunch"]
                )
            else:
                warn(
                    f"Xcode {release.identifier}: first-launch setup failed without sudo "
                    "— run ./setup.zsh interactively once"
                )
                return
        recheck = run(
            ["/usr/bin/xcodebuild", "-checkFirstLaunchStatus"],
            env=dict(os.environ, DEVELOPER_DIR=str(developer_dir)),
            check=False,
            capture=True,
        )
        if recheck.returncode != 0:
            warn(f"Xcode {release.identifier}: first-launch setup still incomplete")

    platforms = cfg.xcode_platforms
    if not platforms:
        pass
    elif "all" in platforms:
        log(f"Xcode {release.identifier}: downloading/updating all platforms")
        result = _xcodebuild(["-downloadAllPlatforms"], developer_dir, check=False)
        if result.returncode != 0:
            warn(f"Xcode {release.identifier}: -downloadAllPlatforms failed")
    else:
        for platform_name in platforms:
            log(f"Xcode {release.identifier}: downloading/updating {platform_name} platform")
            result = _xcodebuild(
                ["-downloadPlatform", platform_name], developer_dir, check=False
            )
            if result.returncode != 0:
                warn(f"Xcode {release.identifier}: -downloadPlatform {platform_name} failed")

    # Xcode 26+ ships the Metal toolchain as a separate download.
    if release.version[0] >= 26:
        log(f"Xcode {release.identifier}: downloading/updating Metal toolchain")
        result = _xcodebuild(
            ["-downloadComponent", "MetalToolchain"], developer_dir, check=False
        )
        if result.returncode != 0:
            warn(f"Xcode {release.identifier}: Metal toolchain download failed")


def ensure(cfg: Config) -> Path | None:
    """Converge the set of installed Xcodes. Returns the developer dir of the
    latest stable Xcode (for the runner's DEVELOPER_DIR), or None."""
    if not cfg.xcode_manage:
        return None
    log("Xcode releases")

    if shutil.which("xcodes") is None:
        warn("`xcodes` is not installed yet — skipping the Xcode phase "
             "(converge without --skip-brew installs it)")
        return None

    # Refresh the release list; fall back to the cached one when offline.
    refresh = run(["xcodes", "update"], check=False, capture=True)
    if refresh.returncode != 0:
        warn("`xcodes update` failed (offline?) — using the cached release list")

    releases = parse_list(output(["xcodes", "list"]))
    desired, latest = select_desired(releases, cfg.xcode_install_beta)
    installed = parse_installed(output(["xcodes", "installed"]))
    installed_ids = {i.identifier for i in installed}

    for release in desired:
        if release.identifier in installed_ids:
            ok(f"Xcode {release.identifier} already installed")
            continue
        if util.INTERACTIVE:
            log(
                f"Installing Xcode {release.identifier} "
                "(first time: xcodes will prompt for your Apple ID)"
            )
            run(["xcodes", "install", release.identifier])
        else:
            # A stored xcodes session may allow this unattended; if it needs
            # interactive Apple ID auth it fails fast thanks to /dev/null stdin.
            result = run(
                ["xcodes", "install", release.identifier], check=False, stdin_devnull=True
            )
            if result.returncode != 0:
                warn(
                    f"could not install Xcode {release.identifier} unattended "
                    "(Apple ID session expired?) — run ./setup.zsh interactively"
                )

    installed = parse_installed(output(["xcodes", "installed"]))
    installed_by_id = {i.identifier: i for i in installed}

    for release in desired:
        info = installed_by_id.get(release.identifier)
        if info:
            _post_install(cfg, release, Path(info.path))

    desired_ids = {r.identifier for r in desired}
    for entry in installed:
        if entry.identifier in desired_ids:
            continue
        if "Beta" in entry.identifier or "Release Candidate" in entry.identifier:
            warn(
                f"Xcode {entry.identifier} looks superseded — "
                f"free ~15 GB with: xcodes uninstall '{entry.identifier}'"
            )

    latest_info = installed_by_id.get(latest.identifier)
    if latest_info:
        return Path(latest_info.path) / "Contents/Developer"
    warn(f"latest stable Xcode {latest.identifier} is not installed yet")
    return None
