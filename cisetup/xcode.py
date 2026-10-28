#
# This source file is part of the macOS Self-Hosted GitHub Actions Runner open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT
#

"""Maintain pinned and rolling Xcodes with their platforms and Metal tools."""

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

# A runner/job override must not mask the machine-wide selection.
GLOBAL_SELECTION_COMMAND = ['/usr/bin/env', '-u', 'DEVELOPER_DIR', '/usr/bin/xcode-select', '-p']

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
        return (self.version + (0,) * (3 - len(self.version)), kind, number)


class XcodeSetupError(SetupError):
    """An incomplete phase, optionally with a verified replacement toolchain.

    Cleanup can fail after old Xcodes have been removed. In that case the
    caller must still wire the runner to this ready developer directory.
    """

    def __init__(self, message: str, developer_dir: Path | None = None):
        super().__init__(message)
        self.developer_dir = developer_dir


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
    releases: list[Release], install_beta: bool, *, track_latest: bool = True,
    pinned_versions: list[str] | None = None, default_version: str = "latest",
) -> tuple[list[Release], Release]:
    """Resolve retained releases and default before any install or cleanup."""
    if install_beta and not track_latest:
        raise SetupError("install_beta requires track_latest")
    def key(identifier):
        parsed = parse_identifier(identifier)
        if parsed is None:
            raise SetupError(f"Invalid exact Xcode version: {identifier}")
        version, pre = parsed
        return version + (0,) * (3 - len(version)), pre

    def resolve(identifier, catalog):
        matches = [release for release in catalog if key(release.identifier) == key(identifier)]
        if not matches:
            raise SetupError(f"Requested Xcode {identifier} is unavailable in the release catalog")
        if len({release.build for release in matches}) != 1:
            raise SetupError(f"Requested Xcode {identifier} has ambiguous builds in the release catalog")
        return matches[0]

    eligible = [r for r in releases if not r.pre or r.pre[0] == 'rc']
    if track_latest and not eligible:
        raise SetupError("could not parse any Xcode releases or release candidates from `xcodes list`")
    desired = [max(eligible, key=Release.sort_key)] if track_latest else []
    desired += [resolve(identifier, releases) for identifier in pinned_versions or []]
    if install_beta:
        newer_prereleases = [
            r for r in releases if r.pre and r.pre[0] == 'beta' and r.sort_key() > desired[0].sort_key()
        ]
        if newer_prereleases:
            desired.append(max(newer_prereleases, key=Release.sort_key))
    desired = list({key(release.identifier): release for release in desired}.values())
    if not desired:
        raise SetupError("Xcode policy must retain at least one release")
    if default_version == "latest":
        defaults = [release for release in desired if not release.pre or release.pre[0] == 'rc']
        if not defaults:
            raise SetupError("default_version=latest requires a retained release or RC")
        preferred = max(defaults, key=Release.sort_key)
    else:
        preferred = resolve(default_version, desired)
    return desired, preferred


def selected_releases(releases: list[Release], cfg: Config) -> tuple[list[Release], Release]:
    return select_desired(releases, cfg.xcode_install_beta,
                          track_latest=cfg.xcode_track_latest,
                          pinned_versions=cfg.xcode_pinned_versions,
                          default_version=cfg.xcode_default_version)


def _xcodebuild(args: list[str], developer_dir: Path, *, check: bool = True):
    env = dict(os.environ, DEVELOPER_DIR=str(developer_dir))
    timeout = 30 * 60 if "-runFirstLaunch" in args else 4 * 60 * 60
    return run(["/usr/bin/xcodebuild", *args], env=env, check=check, timeout=timeout)


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
            "./setup run to install)"
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
    """Run first launch with passwordless sudo, restoring the prior selection.

    The caller holds the maintenance pause. Never leave a partially prepared
    Xcode selected, even when xcodebuild itself fails to start.
    """
    previous_result = run(
        GLOBAL_SELECTION_COMMAND, check=False, capture=True
    )
    previous = previous_result.stdout.strip()
    if previous_result.returncode != 0 or not previous:
        return False
    select = run(
        ["sudo", "-n", "/usr/bin/xcode-select", "-s", str(developer_dir)],
        check=False,
        capture=True,
    )
    if select.returncode != 0:
        return False
    try:
        result = run(["sudo", "-n", "/usr/bin/xcodebuild", "-runFirstLaunch"],
                     check=False, timeout=30 * 60)
    finally:
        if previous != str(developer_dir):
            restore = run(
                ["sudo", "-n", "/usr/bin/xcode-select", "-s", previous],
                check=False,
                capture=True,
            )
            if restore.returncode != 0:
                raise SetupError(f"could not restore the previous Xcode selection: {previous}")
    return result.returncode == 0


def _post_install(cfg: Config, release: Release, app_path: Path) -> None:
    """Prepare one Xcode, reporting failures instead of treating it as ready."""
    developer_dir = app_path / "Contents/Developer"
    if not developer_dir.exists():
        raise SetupError(f"Xcode {release.identifier}: {developer_dir} missing")

    status = run(
        ["/usr/bin/xcodebuild", "-checkFirstLaunchStatus"],
        env=dict(os.environ, DEVELOPER_DIR=str(developer_dir)),
        check=False,
        capture=True,
    )
    if status.returncode != 0:
        log(f"Xcode {release.identifier}: running first-launch setup")
        bootstrap = bool(os.environ.get('SUDO_ASKPASS') and os.environ.get('RUNNER_BOOTSTRAP_PASSWORD'))
        executable = developer_dir / 'usr/bin/xcodebuild'
        if bootstrap:
            # The host supplies the existing guest credential transiently.
            # Keep scheduled guest runs and the sudo policy unchanged.
            run(['sudo', '-A', executable, '-license', 'accept'], capture=True)
            run(['sudo', '-A', executable, '-runFirstLaunch'], timeout=30 * 60)
        elif util.INTERACTIVE:
            util.sudo_run([executable, '-license', 'accept'])
            # sudo strips DEVELOPER_DIR; use this Xcode's executable.
            util.sudo_run([executable, '-runFirstLaunch'], timeout=30 * 60)
        elif not _unattended_first_launch(developer_dir):
            # An unprivileged attempt can wait on a GUI authorization dialog
            # until its timeout, never reaching a return-code-based fallback.
            raise SetupError(
                f"Xcode {release.identifier}: first-launch setup failed; "
                "complete authorization interactively on the runner during maintenance"
            )
        recheck = run(
            ["/usr/bin/xcodebuild", "-checkFirstLaunchStatus"],
            env=dict(os.environ, DEVELOPER_DIR=str(developer_dir)),
            check=False,
            capture=True,
        )
        if recheck.returncode != 0:
            raise SetupError(f"Xcode {release.identifier}: first-launch setup still incomplete")

    # Platforms are independent of one another. Try every requested download
    # (and Metal), then report all failures together to the phase coordinator.
    failures = []

    def download_component(args: list[str], description: str) -> None:
        log(f"Xcode {release.identifier}: downloading/updating {description}")
        try:
            _xcodebuild(args, developer_dir)
        except SetupError as e:
            failures.append(f"{description}: {e}")
            warn(f"Xcode {release.identifier}: {description} failed ({e})")

    if "all" in cfg.xcode_platforms:
        download_component(["-downloadAllPlatforms"], "all platforms")
    else:
        for platform_name in cfg.xcode_platforms:
            download_component(["-downloadPlatform", platform_name], f"{platform_name} platform")
    if release.version[0] >= 26:
        download_component(["-downloadComponent", "MetalToolchain"], "Metal toolchain")
    if failures:
        raise SetupError(f"Xcode {release.identifier} incomplete: " + "; ".join(failures))



def runner_developer_dir(cfg: Config) -> Path | None:
    """Read only the runner's DEVELOPER_DIR override, never evaluate its .env.

    Preserve the last occurrence, matching how the runner environment is
    merged. Other keys may contain secrets and must never enter diagnostics.
    """
    value = ""
    try:
        with (cfg.runner_dir / ".env").open() as stream:
            for line in stream:
                key, separator, candidate = line.strip().partition("=")
                if separator and key == "DEVELOPER_DIR":
                    value = candidate
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError) as error:
        raise SetupError("cannot inspect the runner's DEVELOPER_DIR override") from error
    if not value:
        return None
    path = Path(value)
    if not path.is_absolute():
        raise SetupError("runner DEVELOPER_DIR must be absolute")
    return path.resolve()

def host_archives():
    """Read the host's pinned catalog without authenticating inside a guest."""
    directory = Path.home() / 'Library/Application Support/runner-bootstrap'
    manifest = directory / 'xcode-archives.json'
    if not manifest.exists():
        return None
    try:
        if manifest.stat().st_size > 64 * 1024:
            raise ValueError('oversized manifest')
        data = json.loads(manifest.read_text())
        if data['schema'] != 1 or not data['archives']:
            raise ValueError('unsupported or empty manifest')
        releases, archives = [], {}
        for item in data['archives']:
            parsed = parse_identifier(item['identifier'])
            build, checksum = item['build'], item['sha256']
            path = Path(item['path'])
            if (parsed is None or not re.fullmatch(r'[0-9A-Za-z]+', build)
                    or not re.fullmatch(r'[0-9a-f]{64}', checksum)
                    or path != directory / ('Xcode-' + build + '.xip')
                    or item['identifier'] in archives):
                raise ValueError('invalid archive entry')
            releases.append(Release(*parsed, item['identifier'], build))
            archives[item['identifier']] = (path, checksum)
        return releases, archives
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise SetupError('Invalid host Xcode manifest; rerun host configure') from error


def verified_archive(entry):
    path, expected = entry
    if not path.is_file() or path.is_symlink():
        raise SetupError('Host Xcode archive is missing; rerun host configure')
    with path.open('rb') as stream:
        if hashlib.file_digest(stream, 'sha256').hexdigest() != expected:
            raise SetupError('Host Xcode archive checksum mismatch; rerun host configure')
    return path


def ensure(cfg: Config) -> Path | None:
    """Return the ready policy-selected developer directory, or report failure.

    Failed discovery, installs or readiness checks preserve all installed
    Xcodes, runtimes and the current selection. The caller owns the runner
    maintenance pause for this entire phase.
    """
    if not cfg.xcode_manage:
        return None
    log("Xcode releases")
    if util.runner_busy():
        raise SetupError("Xcode maintenance deferred: a runner job is currently running")
    if shutil.which("xcodes") is None:
        raise SetupError(
            "`xcodes` is not installed; converge without --skip-brew to install it"
        )

    failures = []
    supplied = host_archives()
    if supplied is not None:
        releases, archives = supplied
    else:
        refresh = run(["xcodes", "update"], check=False, capture=True, timeout=10 * 60)
        if refresh.returncode != 0:
            message = "`xcodes update` failed; cached releases cannot confirm the latest Xcode"
            failures.append(message)
            warn(message)
        releases = parse_list(output(["xcodes", "list"], timeout=5 * 60))
    desired, latest = selected_releases(releases, cfg)
    installed = parse_installed(output(["xcodes", "installed"]))
    installed_builds = {(i.identifier, i.build) for i in installed}
    for release in desired:
        if (release.identifier, release.build) in installed_builds:
            ok(f"Xcode {release.identifier} already installed")
            continue
        install_cmd = [
            "xcodes", "install", "--experimental-unxip", "--empty-trash",
            *([] if util.INTERACTIVE else ["--no-superuser"]),
            release.identifier,
        ]
        log(f"Installing Xcode {release.identifier}")
        try:
            if supplied is not None:
                install_cmd += ['--path', str(verified_archive(archives[release.identifier]))]
            # Downloading and extracting a full Xcode can be quiet for a long
            # time. Use an absolute ceiling, not an output-idle timeout.
            run(install_cmd, stdin_devnull=not util.INTERACTIVE, timeout=4 * 60 * 60)
        except SetupError as e:
            message = (
                f"Xcode {release.identifier} install failed: {e}; "
                + ("rerun host configure to refresh the archive" if supplied is not None else
                   "if Apple authentication expired, rerun ./setup interactively to authenticate and retry")
            )
            failures.append(message)
            warn(message)

    installed = parse_installed(output(["xcodes", "installed"]))
    installed_by_id = {i.identifier: i for i in installed}
    for release in desired:
        info = installed_by_id.get(release.identifier)
        if info is None or info.build != release.build:
            failures.append(f"Xcode {release.identifier} ({release.build}) is not installed")
            continue
        try:
            _post_install(cfg, release, Path(info.path))
        except SetupError as e:
            failures.append(str(e))
            warn(str(e))

    if failures:
        raise XcodeSetupError(
            "Xcode maintenance incomplete; skipping Xcode/runtime cleanup and "
            "final selection: " + "; ".join(failures)
        )
    # Defense in depth for callers that did not establish the maintenance pause.
    if util.runner_busy():
        raise XcodeSetupError("Xcode selection/cleanup deferred: a runner job is currently running")

    developer_dir = Path(installed_by_id[latest.identifier].path) / "Contents/Developer"
    previous_runner_dir = runner_developer_dir(cfg)
    _ensure_global_selection(developer_dir)
    try:
        kept = _remove_unwanted_xcodes(
            installed, desired,
            protected_developer_dirs={previous_runner_dir} if previous_runner_dir else set(),
        )
        # Unrecognized/newer installed Xcodes are retained too. Their SDKs must
        # participate in runtime matching rather than losing their runtimes.
        _cleanup_runtimes([Path(info.path) / "Contents/Developer" for info in kept])
    except SetupError as e:
        raise XcodeSetupError(
            f"Xcode cleanup incomplete: {e}", developer_dir=developer_dir
        ) from e
    return developer_dir


def _remove_unwanted_xcodes(
    installed: list[InstalledXcode], desired: list[Release],
    *, protected_developer_dirs: set[Path] | None = None,
) -> list[InstalledXcode]:
    """Retain the runner's current Xcode until a later pass sees its new .env.

    Environment writes or process interruption can fail after this phase.
    Keeping the prior toolchain (and its runtimes) lets the restored service
    continue working; the next convergence can remove it after the switch.
    """
    protected = protected_developer_dirs or set()
    desired_ids = {r.identifier for r in desired}
    max_desired = max(r.version for r in desired)
    kept = []
    failures = []
    for entry in installed:
        if entry.identifier in desired_ids:
            kept.append(entry)
            continue
        if (Path(entry.path) / "Contents/Developer").resolve() in protected:
            ok(f"keeping Xcode {entry.identifier}: still used by the runner environment")
            kept.append(entry)
            continue
        parsed = parse_identifier(entry.identifier)
        if parsed is None or parsed[0] > max_desired:
            warn(f"keeping unrecognized/newer Xcode {entry.identifier} ({entry.path})")
            kept.append(entry)
            continue
        log(f"Removing unwanted Xcode {entry.identifier} ({entry.path})")
        try:
            run(["xcodes", "uninstall", entry.identifier], timeout=30 * 60)
        except SetupError as e:
            failures.append(f"could not uninstall Xcode {entry.identifier}: {e}")
    if failures:
        # Skip runtime cleanup: failed removals may still need their runtimes.
        raise SetupError("; ".join(failures))
    return kept


_RUNTIME_PLATFORMS = {
    "com.apple.platform.iphoneos": "iOS",
    "com.apple.platform.iphonesimulator": "iOS",
    "com.apple.platform.appletvos": "tvOS",
    "com.apple.platform.appletvsimulator": "tvOS",
    "com.apple.platform.watchos": "watchOS",
    "com.apple.platform.watchsimulator": "watchOS",
    "com.apple.platform.xros": "visionOS",
    "com.apple.platform.xrsimulator": "visionOS",
}


def _runtime_train(platform, version) -> tuple[str, int, int] | None:
    """Match device SDKs to simulator runtimes without equating their builds."""
    if not isinstance(platform, str) or not isinstance(version, str):
        return None
    family = _RUNTIME_PLATFORMS.get(platform)
    if family is None or not re.fullmatch(r"\d+\.\d+(?:\.\d+)?", version):
        return None
    major, minor = (int(part) for part in version.split(".")[:2])
    return family, major, minor


def _cleanup_runtimes(developer_dirs: list[Path]) -> None:
    """Delete only runtimes proven unused by every retained Xcode.

    Validate all discovery output before the first deletion. Broad simctl
    --outdated/--unusable deletion could discard another kept Xcode's runtime.
    """
    if not developer_dirs:
        return
    env = dict(os.environ, DEVELOPER_DIR=str(developer_dirs[0]))
    wanted_builds: set[str] = set()
    wanted_trains: set[tuple[str, int, int]] = set()
    for dev in developer_dirs:
        match = run(
            ["xcrun", "simctl", "runtime", "match", "list", "-j"],
            env=dict(os.environ, DEVELOPER_DIR=str(dev)),
            capture=True,
        )
        try:
            entries = json.loads(match.stdout)
            if not isinstance(entries, dict):
                raise ValueError("invalid runtime match list")
            if not entries:
                # A valid empty result is possible when no simulator platforms
                # were installed (for example, platforms=[]). There is no safe
                # deletion plan, but that is not an installation failure.
                warn(f"runtime cleanup skipped: no SDK runtime matches for {dev}")
                return
            for entry in entries.values():
                if not isinstance(entry, dict):
                    raise ValueError("invalid runtime match entry")
                builds = []
                for field in ("chosenRuntimeBuild", "defaultBuild"):
                    build = entry.get(field)
                    if build is None or build == "":
                        continue
                    if not isinstance(build, str):
                        raise ValueError("runtime match has an invalid build")
                    builds.append(build)
                if not builds:
                    raise ValueError("runtime match has no build")
                train = _runtime_train(entry.get("platform"), entry.get("sdkVersion"))
                if train is None:
                    raise ValueError("runtime match has an unknown platform or SDK version")
                wanted_builds.update(builds)
                wanted_trains.add(train)
        except (ValueError, TypeError) as e:
            raise SetupError(f"cannot safely match runtimes for {dev}: {e}") from e

    listing = run(["xcrun", "simctl", "runtime", "list", "-j"], env=env, capture=True)
    try:
        runtimes = json.loads(listing.stdout)
        if not isinstance(runtimes, dict) or any(
            not isinstance(runtime, dict) for runtime in runtimes.values()
        ):
            raise ValueError("invalid runtime list")
    except (ValueError, TypeError) as e:
        raise SetupError(f"cannot safely list simulator runtimes: {e}") from e

    failures = []
    for uuid, runtime in runtimes.items():
        build = runtime.get("build")
        if not isinstance(build, str) or not build or runtime.get("deletable") is not True:
            continue
        train = _runtime_train(runtime.get("platformIdentifier"), runtime.get("version"))
        # Apple's downloadable runtime may have a different build and patch
        # version from the SDK's match (e.g. iOS 26.5/23F77 for SDK 26.5.1/23F81a).
        # Preserve every runtime in a kept SDK's platform/minor train, plus
        # explicit matches. Unknown metadata cannot prove a runtime is unused.
        if train is None or train in wanted_trains or build in wanted_builds:
            continue
        name = f"{runtime.get('runtimeIdentifier', uuid)} ({build})"
        log(f"Removing simulator runtime no kept Xcode uses: {name}")
        try:
            run(["xcrun", "simctl", "runtime", "delete", uuid],
                env=env, capture=True, timeout=30 * 60)
        except SetupError as e:
            failures.append(f"could not delete runtime {name}: {e}")
    if failures:
        raise SetupError("; ".join(failures))


def _ensure_global_selection(developer_dir: Path) -> None:
    """Point the system-wide xcode-select at the policy default. CI jobs
    don't depend on this (they pin DEVELOPER_DIR via the runner's .env), but
    it keeps SSH sessions and anything else on the machine consistent.
    Passwordless via the sudoers rule; `sudo -n` never prompts."""
    current = run(GLOBAL_SELECTION_COMMAND, check=False, capture=True).stdout.strip()
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
        raise SetupError(
            "could not update the global xcode-select (sudoers rule missing? "
            "run ./setup interactively once)"
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
            util.download(_WWDR_CERT_URL, cert, timeout=120)
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
                "./setup run to install)"
            )
            return
        log("Installing the Apple WWDR intermediate certificate (sudo)")
        # An intermediate inherits trust from Apple's system root. Verify the
        # chain, then import it without creating a custom trust-root override.
        run(["security", "verify-cert", "-c", cert, "-p", "basic"], capture=True)
        util.sudo_run(
            [
                "security", "add-certificates",
                "-k", "/Library/Keychains/System.keychain",
                cert,
            ]
        )
        ok("WWDR intermediate certificate installed")
