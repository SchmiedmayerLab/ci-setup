# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
# SPDX-License-Identifier: MIT

"""Read-only local job activity; never infer an active job from old logs."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from .config import Config
from .maintenance import _processes, _same_process
from .util import SetupError, run

_LOG_MAX_BYTES = 8 * 1024 * 1024
_JOB_MESSAGE = re.compile(r"^\[([^\]\n]+) INFO Worker\] Job message:\r?\n", re.MULTILINE)
_JOB_BOUNDARY = re.compile(
    r"^\[[^\]\n]+ INFO (?:Worker\] (?:Version:|Job completed\.)"
    r"|JobRunner\] Job result after all job steps finish:)", re.MULTILINE,
)


def _text(value) -> str | None:
    if not isinstance(value, str):
        return None
    # Job names and contexts are workflow-controlled, including in JSON logs.
    return "".join(char for char in value if char.isprintable()).strip()[:500] or None


def _job(path: Path, cfg: Config) -> dict | None:
    # Worker.cs writes this message just before JobRunner.RunAsync. Read only
    # a bounded file, never export the full payload (which includes secrets).
    # Logs created within the same second can be appended to by another job.
    with path.open("rb") as stream:
        raw = stream.read(_LOG_MAX_BYTES + 1)
    if len(raw) > _LOG_MAX_BYTES:
        return None
    text = raw.decode("utf-8", errors="replace")
    matches = list(_JOB_MESSAGE.finditer(text))
    if not matches:
        return None
    match = matches[-1]
    if _JOB_BOUNDARY.search(text, match.end()):
        return None
    try:
        message, _ = json.JSONDecoder().raw_decode(text[match.end():].lstrip())
    except (ValueError, RecursionError):
        return None
    if not isinstance(message, dict):
        return None
    job = {}
    for source, key in (("jobDisplayName", "name"), ("jobName", "key"), ("jobId", "id")):
        if value := _text(message.get(source)):
            job[key] = value
    context = message.get("contextData")
    github = context.get("github") if isinstance(context, dict) else None
    # PipelineContextDataJsonConverter encodes dictionaries as k/v entries;
    # string values are plain JSON strings. Unknown formats stay unavailable.
    if isinstance(github, dict) and isinstance(github.get("d"), list):
        for entry in github["d"]:
            if not isinstance(entry, dict):
                continue
            key = entry.get("k")
            if key in ("repository", "workflow", "run_id", "run_attempt", "ref"):
                if value := _text(entry.get("v")):
                    job[key] = value
    if not job:
        return None
    repository, run_id = job.get("repository", ""), job.get("run_id", "")
    if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository) and re.fullmatch(r"[0-9]+", run_id):
        # Use the configured GitHub origin, never a URL from the job payload.
        origin = urlsplit(cfg.github_url)
        job["url"] = f"{origin.scheme}://{origin.netloc}/{repository}/actions/runs/{run_id}"
        if re.fullmatch(r"[0-9]+", job.get("run_attempt", "")):
            job["url"] += f"/attempts/{job['run_attempt']}"
    try:
        started = datetime.fromisoformat(match[1].replace("Z", "+00:00"))
        if started.tzinfo is not None:
            job["started_at"] = started.isoformat(timespec="seconds")
            job["elapsed_seconds"] = max(0, int((datetime.now(timezone.utc) - started).total_seconds()))
    except ValueError:
        pass
    return job


def _worker(process, cfg: Config) -> dict:
    worker = {"pid": process.pid}
    try:
        result = run(["/usr/sbin/lsof", "-nP", "-a", "-p", str(process.pid), "-Fn"],
                     capture=True, check=False, timeout=5)
        diagnostic_dir = (cfg.runner_dir / "_diag").resolve()
        paths = set()
        for line in (result.stdout or "").splitlines():
            if not line.startswith("n/"):
                continue
            path = Path(line[1:])
            if (path.name.startswith("Worker_") and path.suffix == ".log"
                    and path.resolve().parent == diagnostic_dir and path.is_file()):
                paths.add(path)
        if result.returncode == 0 and len(paths) == 1:
            if job := _job(paths.pop(), cfg):
                worker["job"] = job
                return worker
    except (SetupError, OSError):
        pass
    # Rotation, permissions, a just-started worker or an unfamiliar log format
    # must not turn busy into idle or resurrect a historical job.
    worker["details_unavailable"] = "live worker found; current job metadata is unavailable"
    return worker


def collect(cfg: Config) -> dict:
    """Snapshot processes and allowlisted job metadata without API credentials.

    Idle means a local listener exists without a worker, not that GitHub has
    confirmed connectivity. Recheck process identity after reading log files.
    """
    activity = {"state": "unknown", "source": "local processes and runner diagnostic logs", "workers": []}

    def matching(processes, name):
        executable = (cfg.runner_dir / "bin" / name).resolve()
        return [process for process in processes
                if "Z" not in process.state and Path(process.executable).name == name
                and Path(process.executable).resolve() == executable]

    try:
        processes = _processes()
        initial_workers = matching(processes, "Runner.Worker")
        details = [(process, _worker(process, cfg)) for process in initial_workers]
        if initial_workers:
            processes = _processes()
        workers = matching(processes, "Runner.Worker")
        listeners = matching(processes, "Runner.Listener")
        activity["listener_pids"] = [process.pid for process in listeners]
        for process in workers:
            detail = next((detail for original, detail in details if _same_process(original, process)), None)
            activity["workers"].append(detail or {
                "pid": process.pid, "details_unavailable": "worker started during inspection; retry for job details",
            })
        activity["state"] = ("busy" if workers else "paused" if listeners and all(
            "T" in process.state for process in listeners
        ) else "idle" if listeners else "offline")
    except (SetupError, OSError) as error:
        activity["error"] = str(error)
    return activity
