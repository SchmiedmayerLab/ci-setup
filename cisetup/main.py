#
# This source file is part of the SchmiedmayerLab ci-setup open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT
#

"""CLI for maintenance, updates, retained diagnostics and runner inventories."""

from __future__ import annotations

import argparse
import json
from collections import deque
import os
import sys
from pathlib import Path

from . import activity, adoption, boot, brew, config, inventory, legacy, maintenance, power, runner, runlog, update, util, xcode
from .report import RunReport
from .util import SetupError, err, log, ok, warn


class Restart(BaseException):
    """Leave all cleanup/logging contexts before replacing the interpreter."""

    def __init__(self, environment: dict[str, str]):
        self.environment = environment


def cmd_converge(args, cfg: config.Config, report: RunReport) -> int:
    python_changed = False
    with maintenance.paused(cfg) as pause:
        if args.command == "adopt":
            # Persist the identity before any interpreter/source restart. Both
            # operations must succeed before changing this legacy installation.
            if report.phase("runner adoption", lambda: adoption.save(cfg)).error:
                return 1
        if cfg.adopted_registration:
            if report.phase("legacy Homebrew schedule", legacy.retire_homebrew_autoupdate).error:
                return 1
        should_update = args.command == "update" or args.non_interactive
        if should_update and not os.environ.get("CI_SETUP_UPDATED"):
            result = report.phase("self-update", lambda: update.pull(cfg.repo_root))
            if result.error and args.command == "update":
                return 1
            if result.value:
                log("Setup changed — restarting with the updated source")
                pause.handoff()
                raise Restart(dict(os.environ, CI_SETUP_UPDATED="1"))

        cfg.pat = None if cfg.adopted_registration else config.resolve_pat(cfg)
        runlog.register_secret(cfg.pat)
        (Path.home() / "Library/LaunchAgents").mkdir(parents=True, exist_ok=True)
        if not cfg.pat and not util.INTERACTIVE and not cfg.adopted_registration:
            warn("no GitHub PAT available; existing registration can be retained")

        result = report.phase("homebrew", brew.probe if args.skip_brew else lambda: brew.ensure(cfg))
        brew_env = result.value or getattr(result.error, "env", None)
        if brew_env is None:
            brew_env = report.phase("existing tool paths", brew.probe).value
        if brew_env is not None:
            python_changed = brew_env.python_changed
        if python_changed and not os.environ.get("CI_SETUP_PY_REEXEC"):
            log("Homebrew updated Python — restarting before the remaining phases")
            pause.handoff()
            raise Restart(dict(os.environ, CI_SETUP_PY_REEXEC="1", CI_SETUP_UPDATED="1"))

        report.phase("Xcode sudo configuration", xcode.ensure_sudoless_select)
        report.phase("Apple signing certificate", xcode.ensure_wwdr_certificate)
        developer_dir = None
        if not args.skip_xcode:
            result = report.phase("Xcode", lambda: xcode.ensure(cfg))
            developer_dir = result.value or getattr(result.error, "developer_dir", None)
        else:
            report.note("Xcode", "skipped", "requested with --skip-xcode")

        report.phase("runner software", lambda: runner.ensure_installed(cfg))
        if (cfg.runner_dir / "config.sh").exists():
            report.phase("runner registration", lambda: runner.ensure_registered(cfg))
        else:
            report.note("runner registration", "failed", "runner config.sh is unavailable")
        if brew_env is not None and cfg.runner_dir.exists():
            report.phase("job environment", lambda: runner.ensure_job_env(cfg, brew_env, developer_dir))
        else:
            report.note("job environment", "failed", "required runner/tool paths are unavailable")
        report.phase("boot agent", lambda: boot.ensure(cfg, force_reload=args.command == "update"))
        report.phase("Spotlight", lambda: power.ensure_spotlight_exclusions(cfg))
        report.phase("power settings", lambda: power.ensure(cfg))

        def snapshot():
            value = inventory.collect(cfg)
            inventory.save(value, util.STATE_DIR / "inventory.json")
            if value["errors"]:
                raise SetupError("inventory incomplete: " + "; ".join(value["errors"]))
            return value
        report.phase("inventory", snapshot)

    # The context restores an existing service even when a phase failed. A
    # fresh, incomplete installation must not begin accepting jobs.
    if not report.failed and runner.is_registered(cfg):
        report.phase("runner service", lambda: runner.ensure_service(cfg))
    elif not runner.service_running(cfg):
        report.note("runner service", "skipped", "incomplete setup; no new service started")

    if report.failed:
        err("Setup remains incomplete; see phase results above and ./setup status")
        return 1
    ok(f"maintenance completed for runner '{cfg.runner_name}'")
    log(f"runner page: {cfg.runners_settings_page}")
    log(f"logs: {runlog.LOG_DIR}")
    return 0


def cmd_status(args, repo_root: Path) -> int:
    cfg = config.load(repo_root)
    if args.json:
        snapshot = inventory.collect(cfg)
        print(json.dumps(snapshot, indent=2))
        return 1 if snapshot["errors"] else 0
    return _print_status(cfg)


def _print_field(label: str, value: object, *, width: int = 18) -> None:
    print(f"  {label + ':':<{width}} {value}")


def _print_maintenance(cfg: config.Config) -> None:
    print("\nMaintenance:")
    previous = util.STATE_DIR / "last-run.json"
    if previous.exists():
        try:
            last = json.loads(previous.read_text())
            if not isinstance(last, dict):
                raise ValueError("expected a run-summary object")
            _print_field("last run", last.get("status", "unknown"))
            _print_field("started", last.get("started", "unknown"))
            _print_field("run ID", last.get("run_id", "unknown"))
            for phase in last.get("phases", []):
                if phase.get("status") not in ("succeeded", "skipped"):
                    detail = phase.get("detail") or "; ".join(phase.get("warnings", []))
                    _print_field(phase["name"], f"{phase['status']}{' — ' + detail if detail else ''}")
        except (ValueError, OSError) as error:
            warn(f"could not read previous run summary: {error}")
    else:
        _print_field("last run", "not recorded")
    _print_field("logs", f"{runlog.LOG_DIR} (./setup logs)")
    _print_field("log retention", f"up to {cfg.log_retention_days} days, {cfg.log_max_bytes / 1024**2:g} MiB total")


def _print_activity(value: dict) -> None:
    descriptions = {"busy": "busy", "idle": "idle (local listener running)",
                    "offline": "offline (no local runner process)", "paused": "paused", "unknown": "unknown"}
    _print_field("activity", descriptions.get(value["state"], "unknown"))
    if value.get("error"):
        _print_field("activity error", value["error"])
    for worker in value.get("workers", []):
        job = worker.get("job", {})
        _print_field("current job", job.get("name", job.get("key", "details unavailable")))
        _print_field("worker PID", worker["pid"])
        for key, label in (("repository", "repository"), ("workflow", "workflow"),
                           ("ref", "ref"), ("started_at", "job started")):
            if job.get(key):
                _print_field(label, job[key])
        if "elapsed_seconds" in job:
            minutes, seconds = divmod(job["elapsed_seconds"], 60)
            hours, minutes = divmod(minutes, 60)
            _print_field("elapsed", f"{hours}h {minutes:02}m {seconds:02}s" if hours else f"{minutes}m {seconds:02}s")
        if job.get("url"):
            _print_field("run", job["url"])


def _print_runner_status(cfg: config.Config, snapshot: dict | None = None) -> dict:
    print(f"{util.BOLD}Runner status:{util.OFF}")
    _print_field("name", cfg.runner_name)
    _print_field("target", cfg.github_url)
    current = (activity.collect(cfg) if snapshot is None else
               snapshot.get("runner", {}).get("activity", {"state": "unknown"}))
    _print_activity(current)
    agent = Path.home() / "Library/LaunchAgents" / f"{cfg.boot_label}.plist"
    _print_field("boot agent", "installed" if agent.exists() else "not installed")
    _print_field("runner directory", cfg.runner_dir)
    _print_field("restart pending", "yes" if (cfg.runner_dir / runner._RESTART_MARKER).exists() else "no")
    _print_field("recovery pending", "yes" if (util.STATE_DIR / "maintenance.json").exists() else "no")

    if not cfg.runner_dir.exists():
        _print_field("runner", f"not installed ({cfg.runner_dir} missing) — run ./setup")
        return current
    if snapshot is None:
        version = runner.installed_version(cfg.runner_dir)
        version_text = util.fmt_version(version) if version else None
    else:
        version_text = snapshot["comparable"].get("runner_version")
    _print_field("version", "v" + version_text if version_text else "unknown")

    if runner.is_registered(cfg):
        drift = not runner.registration_matches(cfg)
        _print_field("registered", "yes" + (" (config drift — run ./setup)" if drift else ""))
        if cfg.adopted_registration:
            _print_field("registration", "adopted; existing GitHub labels and identity preserved")
    else:
        _print_field("registered", "no — run ./setup")

    if runner.service_installed(cfg):
        running = runner.service_running(cfg)
        _print_field("service", "running" if running else "installed, NOT running")
    else:
        _print_field("service", "not installed")

    if cfg.xcode_manage and snapshot is None:
        try:
            installed = xcode.parse_installed(util.output(["xcodes", "installed"]))
            names = ", ".join(i.identifier for i in installed) or "none"
            _print_field("xcodes", names)
        except SetupError:
            _print_field("xcodes", "`xcodes` not available yet")
    return current


def _print_status(cfg: config.Config, snapshot: dict | None = None) -> int:
    current = _print_runner_status(cfg, snapshot)
    _print_maintenance(cfg)
    return 1 if current.get("error") else 0


def cmd_info(args, repo_root: Path) -> int:
    """Print current local state without updating tools, services or run records."""
    cfg = config.load(repo_root)
    snapshot = inventory.collect(cfg, include_homebrew_dependencies=args.json)
    if args.json:
        print(json.dumps(snapshot, indent=2))
        return 1 if snapshot["errors"] else 0

    values = snapshot["comparable"]
    print(f"Runner setup on {snapshot['host']} (as of {snapshot['captured_at']})")
    print(f"  checkout: {repo_root}")
    revision = values.get("setup", {})
    print(f"  revision: {revision.get('commit', 'unknown')}"
          f"{' (uncommitted changes)' if revision.get('dirty') else ''}")
    print(f"  config:   {repo_root / 'config.toml'}")
    system = values.get("os", {})
    print(f"  macOS:    {system.get('version', 'unknown')} "
          f"({system.get('build', 'unknown')}), {system.get('architecture', 'unknown')}")
    print()
    try:
        _print_runner_status(cfg, snapshot)
    except (SetupError, OSError) as error:
        snapshot["errors"].append(f"runner status: {error}")
    _print_maintenance(cfg)

    def xcode_text(value):
        if value is None:
            return "none"
        prerelease = " ".join(str(part) for part in value.get("prerelease", []))
        return f"{value['version']}{' ' + prerelease if prerelease else ''} ({value['build']})"

    print("\nXcode:")
    toolchains = values.get("xcodes")
    if toolchains is None:
        print("  unavailable (see incomplete checks below)")
    else:
        installed = toolchains["installed"]
        _print_field("installed", (", ".join(xcode_text(item) for item in installed) or "none"
                                   if installed is not None else "not inventoried (management disabled)"))
        _print_field("global selection", xcode_text(toolchains["selected"]))
        selected = toolchains["runner"]
        source = f" via {selected['source']}" if selected else ""
        _print_field("runner selection", f"{xcode_text(selected)}{source}")
    if snapshot["errors"]:
        print("\nIncomplete checks:")
        for error in snapshot["errors"]:
            print(f"  {error}")

    print("\nManaged tools (Homebrew):")
    packages = values.get("homebrew", {})
    labels = list(packages.get("formulae", {})) + [f"{name} (cask)" for name in packages.get("casks", {})]
    width = max((len(label) + 1 for label in labels), default=0)
    for name, package in sorted(packages.get("formulae", {}).items()):
        _print_field(name, f"{package['version']}{' [pinned]' if package['pinned'] else ''}", width=width)
    if "homebrew" not in values:
        print("  unavailable (see incomplete checks above)")
    for name, version in sorted(packages.get("casks", {}).items()):
        _print_field(f"{name} (cask)", version, width=width)
    return 1 if snapshot["errors"] else 0


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
    runlog.register_secret(cfg.pat)
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


def cmd_logs(args, repo_root: Path) -> int:
    if args.lines <= 0:
        raise SetupError("--lines must be positive")
    lines: deque[str] = deque(maxlen=args.lines)
    for path in sorted(runlog.LOG_DIR.glob("setup-????-??-??.log")):
        with path.open(errors="replace") as stream:
            lines.extend(stream)
    if not lines:
        print(f"No recorded runs in {runlog.LOG_DIR}")
    else:
        print("".join(lines), end="")
    return 0


def cmd_compare(args, repo_root: Path) -> int:
    if not args.token:
        raise SetupError("usage: ./setup compare /path/to/other-runner.json")
    other = json.loads(Path(args.token).expanduser().read_text())
    current = inventory.collect(config.load(repo_root))
    changes = inventory.differences(current, other)
    if changes:
        print("Runner environments differ:")
        for change in changes:
            print(f"  {change}")
        return 1
    ok("runner environments match (host names and capture times excluded)")
    return 0


_READ_ONLY = {"status": cmd_status, "info": cmd_info, "logs": cmd_logs, "compare": cmd_compare}
_RAW_ARGV: list[str] = []


def main(argv: list[str]) -> int:
    global _RAW_ARGV
    _RAW_ARGV = list(argv)
    parser = argparse.ArgumentParser(
        prog="setup", description="Maintain a dedicated self-hosted macOS CI runner.")
    parser.add_argument("command", nargs="?", default="converge",
                        choices=["converge", "update", "adopt", "status", "info", "logs", "compare", "store-pat", "uninstall"])
    parser.add_argument("token", nargs="?", help="PAT for store-pat; snapshot path for compare; existing runner directory for adopt")
    parser.add_argument("--non-interactive", action="store_true", help="never prompt")
    parser.add_argument("--skip-brew", action="store_true", help="skip Homebrew changes")
    parser.add_argument("--skip-xcode", action="store_true", help="skip the Xcode phase")
    parser.add_argument("--json", action="store_true", help="export a comparable inventory (status/info)")
    parser.add_argument("--lines", type=int, default=200, help="number of recent log lines (logs)")
    args = parser.parse_args(argv)
    if args.token and args.command not in ("store-pat", "compare", "adopt"):
        parser.error("unexpected extra argument")
    if args.command == "adopt" and not args.token:
        parser.error("adopt requires the existing runner directory (for example ~/runner)")
    util.INTERACTIVE = not args.non_interactive and sys.stdin.isatty()
    util.WARNINGS.clear()
    repo_root = Path(__file__).resolve().parent.parent
    if args.command == "store-pat":
        return cmd_store_pat(args, repo_root)
    if args.command in _READ_ONLY:
        try:
            return _READ_ONLY[args.command](args, repo_root)
        except (SetupError, OSError, ValueError) as error:
            err(str(error))
            return 1

    restart = None
    report = None
    cfg = None
    lock_acquired = False
    code = 1
    try:
        holder = util.acquire_lock()
        if holder is not None:
            with runlog.RunLog(args.command, console=not bool(os.environ.get(boot._MARKER_ENV))) as recording:
                warn(f"another ci-setup run is in progress ({holder}); deferred")
                recording.finish(2)
            return 2
        lock_acquired = True
        # Invalid configuration is still recorded with default retention.
        cfg = None
        config_error = None
        try:
            cfg = config.load(repo_root)
            if args.command == "adopt":
                cfg = adoption.prepare(cfg, Path(args.token).expanduser())
        except SetupError as error:
            config_error = error
        with runlog.RunLog(
            args.command,
            retention_days=cfg.log_retention_days if cfg else 30,
            max_bytes=cfg.log_max_bytes if cfg else 50 * 1024 * 1024,
            console=not bool(os.environ.get(boot._MARKER_ENV)),
        ) as recording:
            report = RunReport(recording.run_id, args.command)
            try:
                if config_error:
                    raise config_error
                if args.command == "uninstall":
                    if util.runner_busy():
                        raise maintenance.MaintenanceDeferred("uninstall deferred: a job is running")
                    code = cmd_uninstall(args, repo_root)
                else:
                    code = cmd_converge(args, cfg, report)
            except Restart as request:
                restart = request.environment
                code = 0
            except maintenance.MaintenanceDeferred as error:
                report.note("maintenance", "deferred", str(error))
                warn(str(error))
                code = 2
            except KeyboardInterrupt:
                err("interrupted; runner restoration attempted")
                code = 130
            except SystemExit as error:
                code = int(error.code or 0)
                err(f"interrupted (exit {code}); runner restoration attempted")
            except (SetupError, OSError) as error:
                err(str(error))
                report.note("maintenance", "failed", str(error))
                code = 1
            except Exception:
                # Preserve diagnostics for unexpected bugs while still allowing
                # maintenance/logging finally blocks to restore the machine.
                import traceback
                traceback.print_exc()
                code = 1
            report.finish(code, status="restarting" if restart is not None else None)
            recording.finish(code)
        if restart is not None:
            restart = util.reexec_environment() | restart
            script = str(repo_root / "setup")
            os.execve(script, [script, *_RAW_ARGV], restart)
            raise SetupError("setup restart unexpectedly returned")
    except (SetupError, OSError) as error:
        err(str(error))
        code = 1
        if report is not None:
            try:
                report.note("completion", "failed", str(error))
                report.finish(code)
            except OSError:
                pass  # The original error may be a full/unwritable disk.
    finally:
        try:
            # Successful exec never returns here. Restore any pending handoff
            # before releasing the lock, even if updated config cannot load.
            if lock_acquired:
                maintenance.recover()
        except (SetupError, OSError) as error:
            err(f"runner restoration failed: {error}; recovery marker retained")
            code = 1
            if report is not None:
                try:
                    report.note("runner recovery", "failed", str(error))
                    report.finish(code)
                except OSError:
                    pass
        finally:
            util.release_lock()
    return code
