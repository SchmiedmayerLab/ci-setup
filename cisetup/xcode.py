"""Xcode releases via `xcodes`: keep the latest stable release, the previous
minor release, and (optionally) the newest beta/RC installed — including
simulator runtimes, SDKs and the Metal toolchain for each of them."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
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


_IDENTIFIER_RE = re.compile(
    r"^(?P<version>\d+(?:\.\d+){0,2})"
    r"(?P<pre> Beta(?: \d+)?| Release Candidate(?: \d+)?)?$"
)


def parse_identifier(identifier: str) -> tuple[tuple[int, ...], tuple] | None:
    """'26.0 Beta 5' -> ((26, 0), ('beta', 5)); None if unrecognizable."""
    match = _IDENTIFIER_RE.match(identifier.strip())
    if not match:
        return None
    version = tuple(int(p) for p in match.group("version").split("."))
    return version, _parse_pre(match.group("pre"))


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


# --- passwordless xcode-select (sudoers rule, ported from StanfordBDHG) ------

_SUDOERS_PATH = Path("/etc/sudoers.d/xcode")
_SUDOERS_CONTENT = (
    "# Installed by ci-setup: lets CI jobs and unattended converges switch the\n"
    "# selected Xcode and run its first-launch setup without a sudo password.\n"
    "%admin ALL=NOPASSWD: /usr/bin/xcode-select, /usr/bin/xcodebuild -runFirstLaunch\n"
)


def ensure_sudoless_select() -> None:
    # The file is root-readable only, but stat works: it is managed solely by
    # this setup, so existence is enough to consider it converged.
    if _SUDOERS_PATH.exists():
        ok("passwordless xcode-select rule present")
        return
    if not util.INTERACTIVE:
        warn(
            "passwordless xcode-select rule missing (needs one interactive "
            "./setup.zsh run to install)"
        )
        return
    log("Installing passwordless xcode-select sudoers rule (sudo)")
    with tempfile.NamedTemporaryFile("w", suffix=".sudoers", delete=False) as tmp:
        tmp.write(_SUDOERS_CONTENT)
        tmp_path = tmp.name
    try:
        # Validate before installing — a broken sudoers file locks out sudo.
        run(["/usr/sbin/visudo", "-cf", tmp_path], capture=True)
        util.sudo_run(
            ["install", "-m", "0440", "-o", "root", "-g", "wheel", tmp_path, _SUDOERS_PATH]
        )
    finally:
        os.unlink(tmp_path)
    ok("passwordless xcode-select rule installed")


def _unattended_first_launch(developer_dir: Path) -> bool:
    """First-launch setup without a password, via the sudoers rule: briefly
    point the global selection at this Xcode (jobs are unaffected — they pin
    theirs via DEVELOPER_DIR in the runner's .env), run -runFirstLaunch
    (which only matches the passwordless rule in its bare /usr/bin form),
    then restore the previous selection. `sudo -n` never prompts; it simply
    fails when the rule is absent."""
    probe = run(["sudo", "-n", "/usr/bin/xcode-select", "-p"], check=False, capture=True)
    if probe.returncode != 0:
        return False
    previous = run(["/usr/bin/xcode-select", "-p"], check=False, capture=True).stdout.strip()
    select = run(
        ["sudo", "-n", "/usr/bin/xcode-select", "-s", str(developer_dir)],
        check=False,
        capture=True,
    )
    if select.returncode != 0:
        return False
    result = run(["sudo", "-n", "/usr/bin/xcodebuild", "-runFirstLaunch"], check=False)
    if previous and previous != str(developer_dir):
        run(["sudo", "-n", "/usr/bin/xcode-select", "-s", previous], check=False, capture=True)
    return result.returncode == 0


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
            elif not _unattended_first_launch(developer_dir):
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
        # --experimental-unxip: much faster unarchiving; --empty-trash:
        # reclaim the tens of GB the trashed .xip would otherwise occupy.
        install_cmd = [
            "xcodes", "install", "--experimental-unxip", "--empty-trash",
            release.identifier,
        ]
        if util.INTERACTIVE:
            log(
                f"Installing Xcode {release.identifier} "
                "(first time: xcodes will prompt for your Apple ID)"
            )
            run(install_cmd)
        else:
            # A stored xcodes session may allow this unattended; if it needs
            # interactive Apple ID auth it fails fast thanks to /dev/null stdin.
            result = run(install_cmd, check=False, stdin_devnull=True)
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

    if util.runner_busy():
        warn("Xcode/runtime cleanup deferred: a job is currently running")
    else:
        _remove_unwanted_xcodes(installed, desired)
        kept_dirs = [
            Path(installed_by_id[r.identifier].path) / "Contents/Developer"
            for r in desired
            if r.identifier in installed_by_id
        ]
        _cleanup_runtimes(kept_dirs)

    latest_info = installed_by_id.get(latest.identifier)
    if latest_info:
        developer_dir = Path(latest_info.path) / "Contents/Developer"
        _ensure_global_selection(developer_dir)
        return developer_dir
    warn(f"latest stable Xcode {latest.identifier} is not installed yet")
    return None


def _remove_unwanted_xcodes(
    installed: list[InstalledXcode], desired: list[Release]
) -> None:
    """Delete installed Xcodes outside the desired set (latest stable,
    previous minor, newest beta). Never touches a version newer than
    everything in the desired set — if the release-list parser ever misses
    the newest Xcode, this must fail safe rather than delete it."""
    desired_ids = {r.identifier for r in desired}
    max_desired = max(r.version for r in desired)
    for entry in installed:
        if entry.identifier in desired_ids:
            continue
        parsed = parse_identifier(entry.identifier)
        if parsed is None or parsed[0] > max_desired:
            warn(f"keeping unrecognized/newer Xcode {entry.identifier} ({entry.path})")
            continue
        log(f"Removing unwanted Xcode {entry.identifier} ({entry.path})")
        result = run(["xcodes", "uninstall", entry.identifier], check=False)
        if result.returncode != 0:
            warn(f"could not uninstall Xcode {entry.identifier}")


def _cleanup_runtimes(developer_dirs: list[Path]) -> None:
    """Delete simulator runtimes no kept Xcode needs: superseded builds of
    the same runtime (a stable release replacing its beta), unusable images,
    and builds that none of the kept Xcodes' SDKs match. Conservative: any
    parse/tool failure deletes nothing further."""
    if not developer_dirs:
        return
    env = dict(os.environ, DEVELOPER_DIR=str(developer_dirs[0]))
    run(["xcrun", "simctl", "runtime", "delete", "--outdated"], env=env, check=False, capture=True)
    run(["xcrun", "simctl", "runtime", "delete", "--unusable"], env=env, check=False, capture=True)

    wanted_builds: set[str] = set()
    for dev in developer_dirs:
        match = run(
            ["xcrun", "simctl", "runtime", "match", "list", "-j"],
            env=dict(os.environ, DEVELOPER_DIR=str(dev)),
            check=False,
            capture=True,
        )
        if match.returncode != 0:
            return
        try:
            entries = json.loads(match.stdout or "{}")
        except json.JSONDecodeError:
            return
        for entry in entries.values():
            build = entry.get("chosenRuntimeBuild") or entry.get("defaultBuild")
            if build:
                wanted_builds.add(build)
    if not wanted_builds:
        return

    listing = run(
        ["xcrun", "simctl", "runtime", "list", "-j"], env=env, check=False, capture=True
    )
    if listing.returncode != 0:
        return
    try:
        runtimes = json.loads(listing.stdout or "{}")
    except json.JSONDecodeError:
        return
    for uuid, runtime in runtimes.items():
        build = runtime.get("build")
        if not build or build in wanted_builds or runtime.get("deletable") is False:
            continue
        name = f"{runtime.get('runtimeIdentifier', uuid)} ({build})"
        log(f"Removing simulator runtime no kept Xcode uses: {name}")
        result = run(
            ["xcrun", "simctl", "runtime", "delete", uuid], env=env, check=False, capture=True
        )
        if result.returncode != 0:
            warn(f"could not delete runtime {name}")


def _ensure_global_selection(developer_dir: Path) -> None:
    """Point the system-wide xcode-select at the latest stable Xcode. CI jobs
    don't depend on this (they pin DEVELOPER_DIR via the runner's .env), but
    it keeps SSH sessions and anything else on the machine consistent.
    Passwordless via the sudoers rule; `sudo -n` never prompts."""
    current = run(["/usr/bin/xcode-select", "-p"], check=False, capture=True).stdout.strip()
    if current == str(developer_dir):
        ok(f"xcode-select points at {developer_dir}")
        return
    result = run(
        ["sudo", "-n", "/usr/bin/xcode-select", "-s", str(developer_dir)],
        check=False,
        capture=True,
    )
    if result.returncode == 0:
        ok(f"globally selected {developer_dir}")
    else:
        warn(
            "could not update the global xcode-select (sudoers rule missing? "
            "run ./setup.zsh interactively once)"
        )


# --- Apple WWDR intermediate certificate (code-signing chain) ----------------

_WWDR_CERT_URL = "https://www.apple.com/certificateauthority/AppleWWDRCAG3.cer"
_WWDR_CERT_NAME = "Apple Worldwide Developer Relations Certification Authority"


def ensure_wwdr_certificate() -> None:
    """Install Apple's WWDR G3 intermediate certificate into the System
    keychain — without it, code-signing in CI can fail with 'unable to build
    certificate chain' on an otherwise fresh macOS."""
    with tempfile.TemporaryDirectory() as tmp:
        cert = Path(tmp) / "AppleWWDRCAG3.cer"
        try:
            util.download(_WWDR_CERT_URL, cert)
        except SetupError as e:
            warn(f"could not download the WWDR certificate — skipping ({e})")
            return
        digest = hashlib.sha256(cert.read_bytes()).hexdigest().upper()
        existing = run(
            [
                "security", "find-certificate", "-a", "-Z",
                "-c", _WWDR_CERT_NAME,
                "/Library/Keychains/System.keychain",
            ],
            check=False,
            capture=True,
        )
        if digest in (existing.stdout or "").upper():
            ok("WWDR intermediate certificate present")
            return
        if not util.INTERACTIVE:
            warn(
                "WWDR intermediate certificate missing (needs one interactive "
                "./setup.zsh run to install)"
            )
            return
        log("Installing the Apple WWDR intermediate certificate (sudo)")
        util.sudo_run(
            [
                "security", "add-trusted-cert", "-d", "-r", "trustRoot",
                "-k", "/Library/Keychains/System.keychain",
                cert,
            ]
        )
        ok("WWDR intermediate certificate installed")
