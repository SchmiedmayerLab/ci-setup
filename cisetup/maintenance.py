#
# This source file is part of the SchmiedmayerLab ci-setup open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT
#

"""Pause job intake while the caller holds the per-user setup lock.

GitHub's listener has no drain signal: SIGINT/SIGTERM cancel active workers.
Freeze only the listener, verify it is stopped and has no children/workers,
then unload its service and wait for it to exit before yielding. A job
assignment already in flight at GitHub can still be retried; a running
Worker is never intentionally stopped. We never escalate to SIGKILL.

SIGINT/SIGTERM/SIGHUP and ordinary failures restore the previous service.
An atomic recovery marker preserves that obligation across re-exec, power
loss or SIGKILL. The next locked converge recovers it; SIGKILL cannot run
Python cleanup immediately.
"""

from __future__ import annotations

import json
import os
import signal
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path

from . import runner, util
from .config import Config
from .util import SetupError, log, run


class MaintenanceDeferred(SetupError):
    """Maintenance cannot start safely; retry after the active work finishes."""


@dataclass(frozen=True)
class Process:
    pid: int
    ppid: int
    uid: int
    state: str
    started: str
    executable: str


def _processes() -> list[Process]:
    result = run(
        ["/bin/ps", "-ww", "-axo", "pid=,ppid=,uid=,stat=,lstart=,comm="],
        capture=True, timeout=10,
    )
    processes = []
    for line in result.stdout.splitlines():
        fields = line.split(maxsplit=9)
        if not fields:
            continue
        if len(fields) != 10:
            raise SetupError("cannot verify runner processes: unexpected ps output")
        try:
            processes.append(Process(
                int(fields[0]), int(fields[1]), int(fields[2]), fields[3],
                " ".join(fields[4:9]), fields[9],
            ))
        except ValueError as e:
            raise SetupError("cannot verify runner processes: invalid ps output") from e
    return processes


def _listeners(processes: list[Process]) -> list[Process]:
    return [
        process for process in processes
        if process.uid == os.getuid()
        and Path(process.executable).name == "Runner.Listener"
    ]


def _busy(processes: list[Process], listeners: list[Process] = ()) -> bool:
    # Any user may have a Worker using machine-wide Homebrew/Xcode tools.
    # Direct children also catch a just-forked Worker before its exec.
    listener_pids = {process.pid for process in listeners}
    return any(
        Path(process.executable).name == "Runner.Worker"
        or process.ppid in listener_pids
        for process in processes
    )


def _same_process(left: Process, right: Process) -> bool:
    # Avoid signalling a recycled PID from an interrupted earlier run.
    return (left.pid, left.uid, left.started, left.executable) == (
        right.pid, right.uid, right.started, right.executable,
    )


def _wait_for(predicate, description: str, timeout: float) -> list[Process]:
    deadline = time.monotonic() + timeout
    while True:
        processes = _processes()
        if predicate(processes):
            return processes
        if time.monotonic() >= deadline:
            raise SetupError(f"timed out waiting for {description}; maintenance aborted")
        time.sleep(0.1)


def _write_marker(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    with temporary.open("w") as stream:
        json.dump(state, stream)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def _resume(listeners: list[Process]) -> None:
    if not listeners:
        return
    current = _processes()
    for listener in listeners:
        if any(_same_process(listener, process) for process in current):
            try:
                os.kill(listener.pid, signal.SIGCONT)
            except ProcessLookupError:
                pass


def _restore(cfg: Config, marker: Path, state: dict) -> None:
    _resume([Process(**process) for process in state.get("listeners", [])])
    if state["was_running"]:
        if not runner.is_registered(cfg):
            raise SetupError(
                "cannot restore the runner service: registration is missing; "
                "maintenance recovery remains pending for the next converge"
            )
        # Do not restart a busy service after merely deferring maintenance.
        # Pending environment changes remain on disk until a later idle run.
        if not runner.service_running(cfg):
            _wait_for(
                lambda ps: not _listeners(ps), "the previous listener to exit", 5,
            )
            runner.ensure_service(cfg)
    marker.unlink(missing_ok=True)


def _interrupted(signum, frame):
    raise SystemExit(128 + signum)


def _read_marker(cfg: Config | None, marker: Path) -> dict | None:
    if not marker.exists():
        return None
    try:
        state = json.loads(marker.read_text())
        runner_dir = state["runner_dir"]
        if not isinstance(runner_dir, str) or not Path(runner_dir).is_absolute():
            raise ValueError("runner_dir must be an absolute path")
        if cfg is not None and runner_dir != str(cfg.runner_dir):
            raise SetupError(
                f"pending maintenance belongs to {state['runner_dir']}; "
                "restore that runner before changing runner.dir"
            )
        if not isinstance(state["was_running"], bool):
            raise ValueError("invalid was_running value")
        for process in state["listeners"]:
            Process(**process)
        return state
    except (ValueError, TypeError, KeyError) as e:
        raise SetupError(f"invalid maintenance recovery marker: {marker}") from e


def recover(cfg: Config | None = None) -> None:
    """Restore pending service recovery under setup.lock.

    Without cfg, use only the stored runner directory. This restores the
    existing registration even if updated configuration can no longer load
    or now points elsewhere; it never registers or configures a runner.
    """
    marker = util.STATE_DIR / "maintenance.json"
    state = _read_marker(cfg, marker)
    if state is not None:
        if cfg is None:
            cfg = Config(
                repo_root=Path(__file__).resolve().parent.parent,
                runner_dir=Path(state["runner_dir"]),
            )
        log("Restoring runner after interrupted maintenance handoff")
        _restore(cfg, marker, state)


@dataclass
class Pause:
    marker: Path
    state: dict
    handed_off: bool = False

    def handoff(self) -> None:
        """Keep intake stopped across an imminent exec while retaining setup.lock.

        The caller must immediately unwind with its restart BaseException,
        and call recover(cfg) if replacing the process fails. Ordinary errors,
        SIGINT, SIGTERM and SIGHUP still trigger the context's restoration.
        """
        # These listeners exited before the maintenance body began. Recovery
        # must not retain obsolete PIDs when transferring to new code.
        self.state["listeners"] = []
        _write_marker(self.marker, self.state)
        self.handed_off = True


@contextmanager
def paused(cfg: Config):
    """Yield with job intake stopped; caller MUST already hold setup.lock.

    Start a newly installed service only after leaving this context. All
    mutations, including environment/registration changes, belong inside it.
    """
    marker = util.STATE_DIR / "maintenance.json"
    previous = _read_marker(cfg, marker)
    if previous is not None:
        log("Recovering interrupted runner maintenance")
        _resume([Process(**process) for process in previous["listeners"]])

    handlers = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGHUP)}
    for sig in handlers:
        signal.signal(sig, _interrupted)
    state = None
    pause = None
    exception = None
    try:
        processes = _processes()
        listeners = _listeners(processes)
        if _busy(processes, listeners):
            raise MaintenanceDeferred("maintenance deferred: a runner job is active")
        expected = str(cfg.runner_dir / "bin/Runner.Listener")
        if any(process.executable != expected for process in listeners):
            raise MaintenanceDeferred(
                "maintenance deferred: another runner listener is using this account"
            )
        running = runner.service_running(cfg)
        if listeners and not running:
            raise MaintenanceDeferred(
                "maintenance deferred: the listener is running outside its managed service"
            )
        if running and not listeners:
            raise MaintenanceDeferred(
                "maintenance deferred: service is loaded but its listener cannot be "
                "verified; inspect the runner service log and retry"
            )
        state = {
            "runner_dir": str(cfg.runner_dir),
            "was_running": running or bool(previous and previous["was_running"]),
            "listeners": [asdict(process) for process in listeners],
        }
        # Persist before stopping anything, including the listener itself.
        _write_marker(marker, state)
        if running:
            log("Pausing runner job intake for maintenance")
            for listener in listeners:
                try:
                    os.kill(listener.pid, signal.SIGSTOP)
                except ProcessLookupError as e:
                    raise MaintenanceDeferred("listener changed while pausing; retry") from e
            frozen = _wait_for(
                lambda ps: all(
                    any(_same_process(old, new) and "T" in new.state for new in ps)
                    for old in listeners
                ),
                "the runner listener to pause", 5,
            )
            if _busy(frozen, listeners):
                raise MaintenanceDeferred("maintenance deferred: a job started while pausing")
            if len(_listeners(frozen)) != len(listeners):
                raise MaintenanceDeferred("maintenance deferred: the listener processes changed")
            runner.stop_service(cfg)
            stopped = _wait_for(
                lambda ps: not _listeners(ps), "the runner listener to stop", 40,
            )
            if _busy(stopped):
                raise MaintenanceDeferred("maintenance deferred: a runner worker is still active")
        pause = Pause(marker, state)
        yield pause
    except BaseException as error:
        exception = error
        raise
    finally:
        try:
            handoff = (
                pause is not None and pause.handed_off and exception is not None
                and not isinstance(exception, (Exception, KeyboardInterrupt, SystemExit))
            )
            if handoff:
                log("Runner intake remains paused across setup restart")
            elif state is not None:
                _restore(cfg, marker, state)
            elif previous is not None:
                _restore(cfg, marker, previous)
        finally:
            for sig, handler in handlers.items():
                signal.signal(sig, handler)
