# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
# SPDX-License-Identifier: MIT

"""Explicitly adopt a legacy runner without reading or replacing credentials."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from . import util
from .util import SetupError

if TYPE_CHECKING:
    from .config import Config


_STATE_NAME = "adopted-runner.json"
_IDENTITY_KEYS = {"agentId", "agentName", "poolId", "poolName", "gitHubUrl", "workFolder"}


def _state_path() -> Path:
    return util.STATE_DIR / _STATE_NAME


def _policy(cfg: Config) -> dict:
    return {
        "url": cfg.github_url,
        "name": cfg.runner_name,
        "labels": sorted(cfg.labels),
        "group": cfg.group or "",
        "runner_dir": str(cfg.runner_dir),
        "work_dir": cfg.work_dir,
    }


def _read_json(path: Path) -> dict:
    try:
        if path.stat().st_size > 64 * 1024:
            raise SetupError(f"runner adoption metadata is unexpectedly large: {path}")
        value = json.loads(path.read_text())
    except (OSError, ValueError) as error:
        raise SetupError(f"cannot read runner adoption metadata {path}: {error}") from error
    if not isinstance(value, dict):
        raise SetupError(f"runner adoption metadata must be an object: {path}")
    return value


def _text(value, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or any(c in value for c in "\r\n\0"):
        raise SetupError(f"invalid adopted runner {field}")
    return value


def _url(value: str) -> str:
    try:
        parsed = urlsplit(_text(value, "GitHub URL"))
    except ValueError as error:
        raise SetupError("invalid adopted runner GitHub URL") from error
    if (
        parsed.scheme != "https" or parsed.netloc.lower() != "github.com"
        or not parsed.path.strip("/") or parsed.query or parsed.fragment
    ):
        raise SetupError("adopted runner must have an unambiguous https://github.com registration URL")
    return f"https://github.com/{parsed.path.strip('/').lower()}"


def _identity(raw: dict, runner_dir: Path) -> dict:
    identity = {key: raw.get(key) for key in _IDENTITY_KEYS}
    for key in ("agentId", "poolId"):
        if type(identity[key]) is not int or identity[key] <= 0:
            raise SetupError(f"invalid adopted runner {key}")
    for key in ("agentName", "poolName", "gitHubUrl", "workFolder"):
        _text(identity[key], key)
    _url(identity["gitHubUrl"])
    # Cleanup hooks recursively remove the configured workspace. Never adopt
    # a root/home/runner directory or an external path through '..'/symlinks.
    work = Path(identity["workFolder"])
    if not work.is_absolute():
        work = runner_dir / work
    try:
        relative = work.resolve().relative_to(runner_dir.resolve())
    except (OSError, RuntimeError, ValueError) as error:
        raise SetupError("adopted runner work folder must be inside its runner directory") from error
    if relative == Path("."):
        raise SetupError("adopted runner work folder must not be the runner directory itself")
    return identity


def _check_credentials(runner_dir: Path) -> None:
    # Only inspect file existence. Their contents must never enter metadata,
    # logs or the setup process; the runner continues to own those secrets.
    for name in (".credentials", ".credentials_rsaparams"):
        if not (runner_dir / name).is_file():
            raise SetupError(f"adopted runner credential file is missing: {runner_dir / name}")


def _validate_state(state: dict) -> tuple[Path, dict]:
    if state.get("version") != 1 or type(state.get("version")) is not int:
        raise SetupError("unsupported runner adoption state version")
    runner_dir = Path(_text(state.get("runner_dir"), "directory"))
    if not runner_dir.is_absolute():
        raise SetupError("adopted runner directory must be absolute")
    registration = state.get("registration")
    if not isinstance(registration, dict) or set(registration) != _IDENTITY_KEYS:
        raise SetupError("invalid adopted runner registration metadata")
    identity = _identity(registration, runner_dir)
    policy = state.get("source_policy")
    if not isinstance(policy, dict):
        raise SetupError("invalid adopted runner source policy")
    if _url(identity["gitHubUrl"]) != _url(policy.get("url")):
        raise SetupError("adopted runner GitHub scope differs from the shared configuration")
    return runner_dir, identity


def _effective(cfg: Config, state: dict) -> Config:
    runner_dir, identity = _validate_state(state)
    return replace(
        cfg, runner_dir=runner_dir, runner_name=identity["agentName"],
        work_dir=identity["workFolder"], group=identity["poolName"],
        adopted_registration=state,
    )


def validate(cfg: Config) -> None:
    """Fail closed if adopted local identity or credentials no longer exist."""
    state = cfg.adopted_registration
    if not isinstance(state, dict):
        raise SetupError("runner has no adoption metadata")
    runner_dir, identity = _validate_state(state)
    if (
        cfg.runner_dir != runner_dir or cfg.runner_name != identity["agentName"]
        or cfg.work_dir != identity["workFolder"] or cfg.group != identity["poolName"]
        or _url(cfg.github_url) != _url(identity["gitHubUrl"])
        or sorted(cfg.labels) != state["source_policy"].get("labels")
    ):
        raise SetupError("adopted runner configuration changed; explicit migration is required")
    actual = _identity(_read_json(runner_dir / ".runner"), runner_dir)
    if actual != identity:
        raise SetupError("adopted runner registration changed; refusing implicit re-registration")
    _check_credentials(runner_dir)


def prepare(cfg: Config, path: Path | str) -> Config:
    """Read and validate a candidate; do not mutate files, services or GitHub."""
    try:
        runner_dir = Path(path).expanduser().resolve()
    except (OSError, RuntimeError, ValueError) as error:
        raise SetupError(f"invalid runner directory: {error}") from error
    if cfg.adopted_registration is not None:
        validate(cfg)
        if runner_dir != cfg.runner_dir:
            raise SetupError("a different runner is already adopted; explicit migration is required")
        return cfg
    if not runner_dir.is_dir():
        raise SetupError(f"runner directory does not exist: {runner_dir}")
    identity = _identity(_read_json(runner_dir / ".runner"), runner_dir)
    if _url(identity["gitHubUrl"]) != _url(cfg.github_url):
        raise SetupError(
            "cannot adopt runner: its GitHub scope differs from the shared configuration; "
            "moving between organizations requires re-registration"
        )
    state = {
        "version": 1,
        "runner_dir": str(runner_dir),
        "registration": identity,
        # This is policy at adoption time, not a claim that server-side labels
        # were queried or reconciled. Existing labels remain untouched.
        "source_policy": _policy(cfg),
    }
    candidate = _effective(cfg, state)
    validate(candidate)
    return candidate


def apply(cfg: Config) -> Config:
    """Apply explicit local adoption only while the shared policy is unchanged."""
    path = _state_path()
    if not path.exists():
        return cfg
    state = _read_json(path)
    _validate_state(state)
    if state["source_policy"] != _policy(cfg):
        raise SetupError(
            "shared runner registration policy changed since adoption; "
            "refusing implicit re-registration (review the migration explicitly)"
        )
    candidate = _effective(cfg, state)
    validate(candidate)
    return candidate


def save(cfg: Config) -> None:
    """Atomically persist metadata; caller holds the setup lock/intake pause."""
    validate(cfg)
    state = cfg.adopted_registration
    path = _state_path()
    content = json.dumps(state, indent=2, sort_keys=True) + "\n"
    if path.exists() and path.read_text() == content and path.stat().st_mode & 0o777 == 0o600:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=".adopted-runner-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
