"""CLI: converge (default) / status / store-pat / uninstall."""

from __future__ import annotations

import argparse
import getpass
import re
import sys
from pathlib import Path

from . import boot, brew, config, power, runner, util, xcode
from .config import KEYCHAIN_SERVICE
from .util import SetupError, err, log, ok, run, warn


def cmd_converge(args, repo_root: Path) -> int:
    holder = util.acquire_lock()
    if holder is not None:
        log(f"another ci-setup run is already in progress ({holder}) — nothing to do")
        return 0

    cfg = config.load(repo_root)
    cfg.pat = config.resolve_pat(cfg)
    if not cfg.pat and not util.INTERACTIVE:
        # Not fatal: an already-registered runner converges fine without a
        # token — but registration and drift-triggered re-registration are
        # deferred until a PAT is available (see runner.ensure_registered).
        warn("no GitHub PAT available — (re-)registration will be skipped if needed")

    if args.skip_brew:
        brew_env = brew.probe()
    else:
        brew_env = brew.ensure(cfg)

    developer_dir = None
    if not args.skip_xcode:
        xcode.ensure_sudoless_select(cfg)
        developer_dir = xcode.ensure(cfg)

    runner.ensure_installed(cfg)
    runner.ensure_registered(cfg)
    env_changed = runner.ensure_job_env(cfg, brew_env, developer_dir)
    runner.ensure_service(cfg, restart=env_changed)
    boot.ensure(cfg)
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
        print(f"  runner:   not installed ({cfg.runner_dir} missing) — run ./setup.zsh")
        return 0
    version = runner.installed_version(cfg.runner_dir)
    print(f"  version:  {'v' + util.fmt_version(version) if version else 'unknown'}")

    if runner.is_registered(cfg):
        drift = runner.recorded_state(cfg) != runner.desired_state(cfg)
        print(f"  registered: yes{' (config drift — run ./setup.zsh)' if drift else ''}")
    else:
        print("  registered: no — run ./setup.zsh")

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


_PAT_REQUIREMENTS = f"""\
A GitHub personal access token (PAT) lets the setup register, re-register,
and deregister the runner via the GitHub API. Required rights:

  repository runner (github.scope = "repo"):
    classic PAT:       `repo` scope
    fine-grained PAT:  repository permission "Administration: write"

  organization runner (github.scope = "org"):
    classic PAT:       `admin:org` scope
    fine-grained PAT:  organization permission "Self-hosted runners: write"

Prefer a fine-grained PAT limited to exactly the target repo/org.
Create one at:
  https://github.com/settings/personal-access-tokens/new   (fine-grained)
  https://github.com/settings/tokens/new                   (classic)

The token is stored only in this machine's login Keychain (service
"{KEYCHAIN_SERVICE}") — never in a file or the repo.

Usage:
  ./setup.zsh store-pat            prompt for the token (input hidden)
  ./setup.zsh store-pat <token>    store the given token
"""


def cmd_store_pat(args, repo_root: Path) -> int:
    if args.token:
        if not re.fullmatch(r"[A-Za-z0-9_.=+/~-]+", args.token):
            raise SetupError("that does not look like a GitHub PAT")
        # `security -i` reads the command from stdin, keeping the token off
        # security's argv (it was on setup.zsh's argv already — the caller's
        # choice — but it should not leak any further).
        run(
            ["security", "-i"],
            input=(
                f'add-generic-password -U -a "{getpass.getuser()}" '
                f'-s "{KEYCHAIN_SERVICE}" -w "{args.token}"\n'
            ),
            capture=True,
        )
    else:
        print(_PAT_REQUIREMENTS)
        util.require_interactive("prompting for a PAT")
        # `-w` without a value makes `security` prompt for the secret itself
        # (hidden, with confirmation) — the PAT never appears in any argv.
        run(
            [
                "security", "add-generic-password",
                "-U",
                "-a", getpass.getuser(),
                "-s", KEYCHAIN_SERVICE,
                "-w",
            ]
        )
    ok(f"PAT stored in the login Keychain (service: {KEYCHAIN_SERVICE})")
    return 0


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


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="setup.zsh",
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
