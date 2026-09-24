#
# This source file is part of the SchmiedmayerLab ci-setup open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT
#

"""Durable phase checkpoints for an individual maintenance run."""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from . import runlog, util


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Result:
    value: object = None
    error: Exception | None = None


class RunReport:
    def __init__(self, run_id: str, command: str):
        self.path = util.STATE_DIR / "last-run.json"
        self.data = {"run_id": run_id, "command": command, "started": now(),
                     "status": "running", "phases": []}
        self.save()

    @property
    def failed(self) -> bool:
        return any(p["status"] == "failed" for p in self.data["phases"])

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump(self.data, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(self.path)

    def note(self, name: str, status: str, detail: str) -> None:
        self.data["phases"].append({"name": name, "status": status,
                                    "finished": now(), "detail": runlog.redact(detail)})
        self.save()
        util.log(f"phase {name}: {status} — {detail}")

    def phase(self, name: str, action) -> Result:
        record = {"name": name, "status": "running", "started": now()}
        self.data["phases"].append(record)
        self.save()
        util.log(f"phase {name}: started")
        start = time.monotonic()
        warning_start = len(util.WARNINGS)
        result = Result()
        try:
            result.value = action()
            record["status"] = "succeeded"
        except (util.SetupError, OSError) as error:
            result.error = error
            record.update(status="failed", detail=runlog.redact(str(error)))
            util.err(f"{name}: {error}")
        except BaseException:
            record["status"] = "interrupted"
            raise
        finally:
            warnings = util.WARNINGS[warning_start:]
            if warnings:
                record["warnings"] = [runlog.redact(x) for x in warnings]
                if record["status"] == "succeeded":
                    record["status"] = "warning"
            record.update(finished=now(), duration_seconds=round(time.monotonic() - start, 2))
            self.save()
            util.log(f"phase {name}: {record['status']} ({record['duration_seconds']}s)")
        return result

    def finish(self, code: int, *, status: str | None = None) -> None:
        warnings = any(p["status"] == "warning" for p in self.data["phases"])
        self.data.update(finished=now(), exit_code=code,
                         status=status or ("succeeded" if code == 0 else
                                           "deferred" if code == 2 else "failed"))
        if code == 0 and warnings and status is None:
            self.data["status"] = "warning"
        self.save()
        util.log(f"run {self.data['run_id']}: {self.data['status']} (exit {code})")
