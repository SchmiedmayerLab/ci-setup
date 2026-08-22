"""The GitHub Actions runner itself: download/update, registration, launchd
service, and the job environment (.env/.path) the runner hands to workflows."""

from __future__ import annotations

import hashlib
import json
import platform
import re
import shutil
import tempfile
from pathlib import Path

from . import github_api, util
from .brew import BrewEnv
from .config import Config
from .util import (
    SetupError,
    download,
    fmt_version,
    log,
    ok,
    prompt,
    run,
    vtuple,
    warn,
)

_MACHINE_TO_PLATFORM = {"arm64": "osx-arm64", "x86_64": "osx-x64"}

# Written by us next to the runner so re-runs know what they installed even if
# the runner binaries cannot be queried; the binary itself stays authoritative
# because the runner self-updates.
_VERSION_MARKER = ".setup-installed-version"
_STATE_FILE = ".setup-state.json"


def runner_platform() -> str:
    machine = platform.machine()
    try:
        return _MACHINE_TO_PLATFORM[machine]
    except KeyError:
        raise SetupError(f"unsupported architecture: {machine}") from None


# --- install / update --------------------------------------------------------


def installed_version(runner_dir: Path) -> tuple[int, ...] | None:
    listener = runner_dir / "bin/Runner.Listener"
    if listener.exists():
        result = run([listener, "--version"], check=False, capture=True)
        match = re.search(r"\d+\.\d+\.\d+", result.stdout or "")
        if result.returncode == 0 and match:
            return vtuple(match.group(0))
    marker = runner_dir / _VERSION_MARKER
    if marker.exists():
        try:
            return vtuple(marker.read_text())
        except ValueError:
            pass
    return None


def ensure_installed(cfg: Config) -> None:
    log("Actions runner")
    current = installed_version(cfg.runner_dir)
    try:
        release = github_api.latest_runner_release(cfg.pat)
    except SetupError as e:
        if current:
            # Offline/rate-limited must not stop the local healing phases;
            # the runner also self-updates on its own.
            warn(f"could not check for runner updates — keeping v{fmt_version(current)} ({e})")
            return
        raise

    latest_str = release.get("tag_name", "").lstrip("v")
    try:
        latest = vtuple(latest_str)
    except ValueError:
        raise SetupError(f"unexpected runner release tag: {release.get('tag_name')!r}") from None

    if current and current >= latest:
        ok(f"runner v{fmt_version(current)} is current (latest: v{latest_str})")
        return

    if current and util.runner_busy():
        warn(
            f"runner update v{fmt_version(current)} -> v{latest_str} deferred: "
            "a job is currently running"
        )
        return

    plat = runner_platform()
    asset_name = f"actions-runner-{plat}-{latest_str}.tar.gz"
    asset_url = next(
        (
            asset["browser_download_url"]
            for asset in release.get("assets", [])
            if asset.get("name") == asset_name
        ),
        None,
    )
    if not asset_url:
        raise SetupError(
            f"release v{latest_str} has no asset named {asset_name} — "
            "the runner's release layout changed; update this script"
        )

    # The release notes embed per-asset SHA-256 hashes as HTML comments.
    # Fail closed: no published checksum, no unattended install.
    sha_match = re.search(
        rf"<!-- BEGIN SHA {re.escape(plat)} -->([0-9a-fA-F]{{64}})",
        release.get("body") or "",
    )
    if not sha_match:
        if not (
            util.INTERACTIVE
            and util.confirm(
                f"No SHA-256 for {asset_name} in the v{latest_str} release notes. "
                "Install unverified anyway?",
                default=False,
            )
        ):
            raise SetupError(
                f"no SHA-256 for {asset_name} in the v{latest_str} release notes — "
                "refusing to install unverified"
            )

    if current:
        log(f"Updating runner v{fmt_version(current)} -> v{latest_str}")
    else:
        log(f"Installing runner v{latest_str} into {cfg.runner_dir}")

    with tempfile.TemporaryDirectory() as tmp:
        tarball = Path(tmp) / asset_name
        download(asset_url, tarball)
        if sha_match:
            digest = hashlib.sha256(tarball.read_bytes()).hexdigest()
            if digest.lower() != sha_match.group(1).lower():
                raise SetupError(
                    f"checksum mismatch for {asset_name}: got {digest}, "
                    f"expected {sha_match.group(1).lower()}"
                )
            ok("checksum verified")

        # Re-check right before stopping: a job may have started while the
        # tarball downloaded, and the stop would cancel it. The temp download
        # is simply discarded; the next converge retries.
        if current and util.runner_busy():
            warn(f"runner update to v{latest_str} deferred: a job started during download")
            return
        stop_service(cfg)
        cfg.runner_dir.mkdir(parents=True, exist_ok=True)
        # Extracting over an existing install is the supported manual-update
        # path; registration files (.runner/.credentials) are not in the tar.
        run(["/usr/bin/tar", "xzf", tarball, "-C", cfg.runner_dir])
        (cfg.runner_dir / _VERSION_MARKER).write_text(latest_str + "\n")
    ok(f"runner v{latest_str} installed")


# --- launchd service (svc.sh wraps launchctl; per-user, no sudo) -------------


def _svc(cfg: Config, *args: str, check: bool = True, capture: bool = False):
    return run(["./svc.sh", *args], cwd=cfg.runner_dir, check=check, capture=capture)


def service_installed(cfg: Config) -> bool:
    return (cfg.runner_dir / ".service").exists()


def service_running(cfg: Config) -> bool:
    if not service_installed(cfg):
        return False
    status = _svc(cfg, "status", check=False, capture=True)
    return "Started:" in (status.stdout or "")


def stop_service(cfg: Config) -> None:
    if service_installed(cfg):
        _svc(cfg, "stop", check=False, capture=True)


def uninstall_service(cfg: Config) -> None:
    if service_installed(cfg):
        _svc(cfg, "stop", check=False, capture=True)
        result = _svc(cfg, "uninstall", check=False, capture=True)
        if result.returncode != 0:
            warn(f"svc.sh uninstall failed: {(result.stderr or result.stdout).strip()[:200]}")


def ensure_service(cfg: Config, *, restart: bool = False) -> None:
    if not (cfg.runner_dir / "svc.sh").exists():
        raise SetupError("svc.sh missing — the runner is not installed/configured")
    if not service_installed(cfg):
        # svc.sh refuses to run when ~/Library/LaunchAgents is missing
        # (fresh macOS accounts don't have it until something creates it).
        (Path.home() / "Library/LaunchAgents").mkdir(parents=True, exist_ok=True)
        log("Installing the runner's launchd service")
        _svc(cfg, "install")
    if restart and service_running(cfg) and util.runner_busy():
        warn("service restart (changed job environment) deferred: a job is running")
        restart = False
    if restart and service_running(cfg):
        log("Restarting the runner service (job environment changed)")
        _svc(cfg, "stop", check=False, capture=True)
    if service_running(cfg):
        ok("runner service is running")
    else:
        _svc(cfg, "start")
        ok("runner service started")


# --- registration ------------------------------------------------------------


def is_registered(cfg: Config) -> bool:
    return (cfg.runner_dir / ".runner").exists()


def desired_state(cfg: Config) -> dict:
    return {
        "url": cfg.github_url,
        "name": cfg.runner_name,
        "labels": sorted(cfg.labels),
        "group": cfg.group or "",
        "work_dir": cfg.work_dir,
    }


def recorded_state(cfg: Config) -> dict | None:
    state_path = cfg.runner_dir / _STATE_FILE
    if not state_path.exists():
        return None
    try:
        return json.loads(state_path.read_text())
    except json.JSONDecodeError:
        return None


def _registration_token(cfg: Config) -> str:
    if cfg.pat:
        return github_api.registration_token(cfg)
    print()
    print("No GitHub PAT is available (see `./setup.zsh store-pat`).")
    print(f"Get a registration token manually from:\n  {cfg.new_runner_page}")
    print("(it is the value after --token in the shown ./config.sh command)")
    token = prompt("Registration token: ")
    if not token:
        raise SetupError("no registration token provided")
    return token


def _removal_token(cfg: Config) -> str:
    if cfg.pat:
        return github_api.removal_token(cfg)
    print()
    print("No GitHub PAT is available (see `./setup.zsh store-pat`).")
    print(f"Get a removal token from the runner's page under:\n  {cfg.runners_settings_page}")
    token = prompt("Removal token: ")
    if not token:
        raise SetupError("no removal token provided")
    return token


def _register(cfg: Config, token: str | None = None) -> None:
    if token is None:
        token = _registration_token(cfg)
    args = [
        "./config.sh",
        "--unattended",
        "--url", cfg.github_url,
        "--token", token,
        "--name", cfg.runner_name,
        "--work", cfg.work_dir,
        "--replace",
    ]
    if cfg.labels:
        args += ["--labels", ",".join(cfg.labels)]
    if cfg.group:
        args += ["--runnergroup", cfg.group]
    run(args, cwd=cfg.runner_dir)
    (cfg.runner_dir / _STATE_FILE).write_text(
        json.dumps(desired_state(cfg), indent=2) + "\n"
    )


def deregister(cfg: Config) -> None:
    # Get the removal token BEFORE any teardown: if no token is obtainable
    # (missing/expired PAT, unattended run), the existing service must keep
    # running instead of being left uninstalled. Tokens live for an hour —
    # far longer than the teardown takes.
    token = _removal_token(cfg)
    uninstall_service(cfg)
    result = run(
        ["./config.sh", "remove", "--token", token], cwd=cfg.runner_dir, check=False
    )
    if result.returncode != 0:
        warn(
            "config.sh remove failed (runner already deleted on GitHub?) — "
            "removing the local registration files"
        )
        for name in (".runner", ".credentials", ".credentials_rsaparams"):
            (cfg.runner_dir / name).unlink(missing_ok=True)
    (cfg.runner_dir / _STATE_FILE).unlink(missing_ok=True)


def ensure_registered(cfg: Config) -> None:
    if is_registered(cfg) and recorded_state(cfg) == desired_state(cfg):
        ok(f"runner '{cfg.runner_name}' registered with {cfg.github_url}")
        return
    if is_registered(cfg):
        if not cfg.pat and not util.INTERACTIVE:
            # Never tear down a working registration unattended when the
            # re-registration afterwards could not possibly succeed.
            warn(
                "runner configuration drifted, but no PAT is available for "
                "unattended re-registration — keeping the current registration"
            )
            return
        if util.runner_busy():
            warn("re-registration deferred: a job is currently running")
            return
        old_state = recorded_state(cfg)
        log("Runner configuration changed — re-registering")
        # Fetch the new registration token BEFORE tearing anything down, so a
        # token failure leaves the current registration and service running.
        new_token = _registration_token(cfg)
        deregister(cfg)
        log(f"Registering runner '{cfg.runner_name}' with {cfg.github_url}")
        _register(cfg, token=new_token)
        _cleanup_old_work_dir(cfg, old_state)
    else:
        log(f"Registering runner '{cfg.runner_name}' with {cfg.github_url}")
        _register(cfg)
    ok("runner registered")


def _cleanup_old_work_dir(cfg: Config, old_state: dict | None) -> None:
    """After a re-registration changed the work dir name, the previous one
    holds only dead job workspaces — reclaim the space."""
    old_name = (old_state or {}).get("work_dir")
    if not old_name or old_name == cfg.work_dir:
        return
    old_dir = Path(old_name)
    if not old_dir.is_absolute():
        old_dir = cfg.runner_dir / old_dir
    if old_dir.exists():
        shutil.rmtree(old_dir, ignore_errors=True)
        ok(f"removed the previous work dir ({old_dir})")


# --- job environment ---------------------------------------------------------


def ensure_job_env(cfg: Config, brew_env: BrewEnv, developer_dir: Path | None) -> bool:
    """Manage the runner's .path and .env files, which define the environment
    jobs run in. This is also how jobs get Homebrew tools, JAVA_HOME, and the
    default Xcode (DEVELOPER_DIR) — all without sudo or shell profiles.
    Returns True if anything changed (the service must then be restarted)."""
    path_entries = [
        f"{brew_env.prefix}/bin",
        f"{brew_env.prefix}/sbin",
        f"{brew_env.openjdk_prefix}/bin",
        *((brew_env.gem_bin,) if brew_env.gem_bin else ()),
        "/usr/bin",
        "/bin",
        "/usr/sbin",
        "/sbin",
    ]
    desired_path = ":".join(dict.fromkeys(path_entries))

    env_updates = {
        "LANG": "en_US.UTF-8",
        "LC_ALL": "en_US.UTF-8",  # fastlane wants both
        "JAVA_HOME": f"{brew_env.openjdk_prefix}/libexec/openjdk.jdk/Contents/Home",
    }
    if developer_dir is not None:
        env_updates["DEVELOPER_DIR"] = str(developer_dir)

    # Per-job cleanup hooks (reset simulators, wipe workspaces/caches): the
    # runner invokes these scripts around every job.
    hook_keys = (
        "ACTIONS_RUNNER_HOOK_JOB_STARTED",
        "ACTIONS_RUNNER_HOOK_JOB_COMPLETED",
        "CI_SETUP_WORK_DIR",
    )
    if cfg.cleanup_hooks:
        work_dir = Path(cfg.work_dir)
        if not work_dir.is_absolute():
            work_dir = cfg.runner_dir / work_dir
        env_updates.update(
            {
                "ACTIONS_RUNNER_HOOK_JOB_STARTED": str(cfg.repo_root / "hooks/job-started.sh"),
                "ACTIONS_RUNNER_HOOK_JOB_COMPLETED": str(cfg.repo_root / "hooks/job-completed.sh"),
                "CI_SETUP_WORK_DIR": str(work_dir),
            }
        )

    changed = False

    path_file = cfg.runner_dir / ".path"
    if not path_file.exists() or path_file.read_text().strip() != desired_path:
        path_file.write_text(desired_path + "\n")
        changed = True

    env_file = cfg.runner_dir / ".env"
    existing: dict[str, str] = {}
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                existing[key] = value
    merged = existing | env_updates
    if not cfg.cleanup_hooks:
        for key in hook_keys:
            merged.pop(key, None)
    if merged != existing or not env_file.exists():
        env_file.write_text("".join(f"{k}={v}\n" for k, v in merged.items()))
        changed = True

    if changed:
        ok("job environment (.env/.path) updated")
    else:
        ok("job environment is current")
    return changed


# --- teardown ----------------------------------------------------------------


def uninstall(cfg: Config) -> None:
    if not cfg.runner_dir.exists():
        ok(f"nothing to do — {cfg.runner_dir} does not exist")
        return
    if is_registered(cfg):
        deregister(cfg)
        ok("runner deregistered from GitHub")
    else:
        uninstall_service(cfg)
    if util.confirm(f"Delete {cfg.runner_dir} entirely?", default=False):
        shutil.rmtree(cfg.runner_dir)
        ok(f"{cfg.runner_dir} deleted")
