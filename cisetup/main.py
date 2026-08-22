"""CLI: converge (default) / status / store-pat / uninstall."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from . import boot, brew, config, power, runner, util, xcode
from .util import SetupError, err, log, ok, warn


def cmd_converge(args, repo_root: Path) -> int:
    holder = util.acquire_lock()
    if holder is not None:
        log(f"another ci-setup run is already in progress ({holder}) — nothing to do")
        return 0

    cfg = config.load(repo_root)
    cfg.pat = config.resolve_pat(cfg)

    # Fresh macOS accounts have no ~/Library/LaunchAgents yet, and the
    # runner's svc.sh (and brew autoupdate) error out instead of creating it.
    (Path.home() / "Library/LaunchAgents").mkdir(parents=True, exist_ok=True)
    if not cfg.pat and not util.INTERACTIVE:
        # Not fatal: an already-registered runner converges fine without a
        # token — but registration and drift-triggered re-registration are
        # deferred until a PAT is available (see runner.ensure_registered).
        warn("no GitHub PAT available — (re-)registration will be skipped if needed")

    if args.skip_brew:
        brew_env = brew.probe()
    else:
        brew_env = brew.ensure(cfg)
        if brew_env.python_changed and not os.environ.get("CI_SETUP_PY_REEXEC"):
            # We are running on the interpreter brew just replaced; its lazily
            # loaded stdlib pieces may no longer exist on disk. Restart onto
            # the new one before anything trips over that (the re-run's brew
            # phase is a fast no-op).
            log("Homebrew updated python3 — re-executing on the new interpreter")
            script = str(repo_root / "setup")
            env = dict(os.environ, CI_SETUP_PY_REEXEC="1")
            os.execve(script, [script, *_RAW_ARGV], env)

    xcode.ensure_sudoless_select()
    xcode.ensure_wwdr_certificate()
    developer_dir = None
    if not args.skip_xcode:
        developer_dir = xcode.ensure(cfg)

    runner.ensure_installed(cfg)
    runner.ensure_registered(cfg)
    env_changed = runner.ensure_job_env(cfg, brew_env, developer_dir)
    runner.ensure_service(cfg, restart=env_changed)
    boot.ensure(cfg)
    power.ensure_spotlight_exclusions(cfg)
    power.ensure(cfg)

    print()
    ok(
        f"{util.BOLD}converged:{util.OFF} runner '{cfg.runner_name}' → {cfg.github_url}"
        + (f" (labels: {', '.join(cfg.labels)})" if cfg.labels else "")
    )
    log(f"runner page: {cfg.runners_settings_page}")
    log(f"boot-time runs log to: {boot.LOG_PATH}")
    return 0


def cmd_status(args, repo_root: Path) -> int:
    cfg = config.load(repo_root)
    print(f"{util.BOLD}CI runner status{util.OFF}")
    print(f"  target:   {cfg.github_url}")
    print(f"  name:     {cfg.runner_name}")

    if not cfg.runner_dir.exists():
        print(f"  runner:   not installed ({cfg.runner_dir} missing) — run ./setup")
        return 0
    version = runner.installed_version(cfg.runner_dir)
    print(f"  version:  {'v' + util.fmt_version(version) if version else 'unknown'}")

    if runner.is_registered(cfg):
        drift = runner.recorded_state(cfg) != runner.desired_state(cfg)
        print(f"  registered: yes{' (config drift — run ./setup)' if drift else ''}")
    else:
        print("  registered: no — run ./setup")

    if runner.service_installed(cfg):
        running = runner.service_running(cfg)
        print(f"  service:  {'running' if running else 'installed, NOT running'}")
    else:
        print("  service:  not installed")

    agent = Path.home() / "Library/LaunchAgents" / f"{cfg.boot_label}.plist"
    print(f"  boot agent: {'installed' if agent.exists() else 'not installed'}")

    if cfg.xcode_manage:
        try:
            installed = xcode.parse_installed(util.output(["xcodes", "installed"]))
            names = ", ".join(i.identifier for i in installed) or "none"
            print(f"  xcodes:   {names}")
        except SetupError:
            print("  xcodes:   `xcodes` not available yet")
    return 0


def cmd_store_pat(args, repo_root: Path) -> int:
    # Implemented natively in ./setup (zsh) so a factory-fresh Mac can store
    # its PAT without bootstrapping Homebrew/python3 first; delegate so a
    # direct `bin/ci-setup store-pat` behaves identically. ./setup intercepts
    # store-pat before ever exec'ing python, so this cannot loop.
    script = str(repo_root / "setup")
    os.execv(script, [script, "store-pat", *([args.token] if args.token else [])])


def cmd_uninstall(args, repo_root: Path) -> int:
    holder = util.acquire_lock()
    if holder is not None:
        warn(f"another ci-setup run is in progress ({holder}) — wait for it to finish")
        return 1
    cfg = config.load(repo_root)
    cfg.pat = config.resolve_pat(cfg)
    if not util.confirm(
        f"Deregister runner '{cfg.runner_name}' from {cfg.github_url} "
        "and remove its services?",
        default=False,
    ):
        log("aborted")
        return 1
    boot.remove(cfg)
    runner.uninstall(cfg)
    ok("uninstall finished (Homebrew packages and Xcodes were left in place)")
    return 0


_COMMANDS = {
    "converge": cmd_converge,
    "status": cmd_status,
    "store-pat": cmd_store_pat,
    "uninstall": cmd_uninstall,
}


# The verbatim CLI args, kept for self re-exec (see cmd_converge).
_RAW_ARGV: list[str] = []


def main(argv: list[str]) -> int:
    global _RAW_ARGV
    _RAW_ARGV = list(argv)
    parser = argparse.ArgumentParser(
        prog="setup",
        description="Idempotent setup for a self-hosted macOS GitHub Actions runner.",
    )
    parser.add_argument(
        "command",
        nargs="?",
        default="converge",
        choices=sorted(_COMMANDS),
        help="what to do (default: converge)",
    )
    parser.add_argument(
        "token",
        nargs="?",
        default=None,
        help="the PAT to store (store-pat only); omit to see requirements and be prompted",
    )
    parser.add_argument(
        "--non-interactive",
        action="store_true",
        help="never prompt and never use sudo (what the boot agent uses)",
    )
    parser.add_argument("--skip-brew", action="store_true", help="skip the Homebrew phase")
    parser.add_argument("--skip-xcode", action="store_true", help="skip the Xcode phase")
    args = parser.parse_args(argv)
    if args.token and args.command != "store-pat":
        parser.error(f"unexpected argument {args.token!r} for command {args.command!r}")

    util.INTERACTIVE = not args.non_interactive and sys.stdin.isatty()

    repo_root = Path(__file__).resolve().parent.parent
    try:
        return _COMMANDS[args.command](args, repo_root)
    except KeyboardInterrupt:
        err("interrupted")
        return 130
    except SetupError as e:
        err(str(e))
        return 1
