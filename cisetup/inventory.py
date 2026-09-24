#
# This source file is part of the SchmiedmayerLab ci-setup open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT
#

"""Read-only runner inventories and comparisons; no refresh or installation."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import plistlib
import socket
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from . import brew, runner, xcode
from .config import Config
from .util import SetupError, fmt_version, output

SCHEMA_VERSION = 1


def _version(value: str) -> str:
    parts = value.split(".")
    while len(parts) > 2 and parts[-1] == "0":
        parts.pop()
    return ".".join(parts)


def _homebrew(cfg: Config, *, include_dependencies: bool = True) -> dict:
    env = dict(os.environ, HOMEBREW_NO_AUTO_UPDATE="1")
    prefix = Path(output(["brew", "--prefix"], env=env))
    info = json.loads(output(["brew", "info", "--json=v2", "--installed"], env=env))
    by_name = {entry["name"]: entry for entry in info["formulae"]}
    aliases = {}
    for name, entry in by_name.items():
        for alias in [name, entry.get("full_name", name), *entry.get("aliases", [])]:
            aliases[alias] = name
    requested = sorted(set(brew.FORMULAE + cfg.brew_extra_formulae))
    pending = [aliases.get(name, aliases.get(name.rsplit("/", 1)[-1], name)) for name in requested]
    active = {}
    while pending:
        name = pending.pop()
        if name in active:
            continue
        if name not in by_name:
            raise SetupError(f"managed Homebrew formula/dependency is missing: {name}")
        entry = by_name[name]
        # The opt symlink identifies the active keg, including keg-only tools
        # such as openjdk. Old installed kegs must not produce false drift.
        version = (prefix / "opt" / name).resolve(strict=True).name
        receipt = next((item for item in entry["installed"] if item["version"] == version), None)
        if receipt is None:
            raise SetupError(f"cannot identify active Homebrew keg for {name}")
        active[name] = {"version": version, "pinned": bool(entry["pinned"])}
        if not include_dependencies:
            continue
        dependencies = receipt.get("runtime_dependencies")
        if not isinstance(dependencies, list):
            raise SetupError(f"installed dependency metadata is missing for {name}")
        for dependency in dependencies:
            full_name = dependency["full_name"]
            pending.append(aliases.get(full_name, full_name.rsplit("/", 1)[-1]))
    casks = {entry["token"]: entry for entry in info.get("casks", [])}
    managed_casks = {}
    for requested_name in sorted(set(brew.CASKS + cfg.brew_extra_casks)):
        name = requested_name.rsplit("/", 1)[-1]
        entry = casks.get(name)
        if not entry or not entry.get("installed"):
            raise SetupError(f"managed Homebrew cask is missing: {name}")
        managed_casks[name] = entry["installed"]
    return {"formulae": dict(sorted(active.items())), "casks": managed_casks}


def _xcode_metadata(developer_dir: Path) -> dict:
    # Read metadata rather than executing xcodebuild, which may initiate
    # first-launch setup on an unprepared installation.
    with (developer_dir.parent / "Info.plist").open("rb") as file:
        info = plistlib.load(file)
    with (developer_dir.parent / "version.plist").open("rb") as file:
        version = plistlib.load(file)
    short_version, build = info["CFBundleShortVersionString"], version["ProductBuildVersion"]
    if not all(isinstance(value, str) and value for value in (short_version, build)):
        raise ValueError("invalid Xcode version metadata")
    return {"version": _version(short_version), "build": build}


def _xcodes(cfg: Config) -> dict:
    releases = None
    if cfg.xcode_manage:
        text = output(["xcodes", "installed"])
        installed = xcode.parse_installed(text)
        if text.strip() and not installed:
            raise SetupError("could not parse installed Xcodes")
        releases = []
        for entry in installed:
            parsed = xcode.parse_identifier(entry.identifier)
            if parsed is None:
                raise SetupError(f"could not identify installed Xcode: {entry.identifier}")
            releases.append({"version": _version(fmt_version(parsed[0])),
                             "prerelease": list(parsed[1]), "build": entry.build})
        releases.sort(key=lambda item: json.dumps(item, sort_keys=True))

    selected_path = output(["/usr/bin/xcode-select", "-p"], check=cfg.xcode_manage)
    selected = None
    if selected_path:
        try:
            selected = _xcode_metadata(Path(selected_path))
        except FileNotFoundError:
            if cfg.xcode_manage:
                raise
            # Xcode management may be intentionally disabled on a runner that
            # uses only Command Line Tools (no Xcode app metadata).
    runner_dir = xcode.runner_developer_dir(cfg)
    runner_selected = _xcode_metadata(runner_dir) if runner_dir else selected
    if runner_selected is not None:
        runner_selected = dict(runner_selected, source="DEVELOPER_DIR" if runner_dir else "global")
    return {"managed": cfg.xcode_manage, "installed": releases,
            "selected": selected, "runner": runner_selected}


def collect(cfg: Config, *, include_homebrew_dependencies: bool = True) -> dict:
    """Capture independent local probes, retaining errors as incomplete data.

    Only the human info display omits dependencies. Exported and persisted
    inventories must retain the default full dependency closure.
    """
    snapshot = {"schema_version": SCHEMA_VERSION, "host": socket.gethostname(),
                "captured_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "comparable": {}, "errors": []}

    def probe(name, read):
        try:
            snapshot["comparable"][name] = read()
        except (SetupError, OSError, ValueError, TypeError, KeyError) as error:
            snapshot["errors"].append(f"{name}: {error}")

    def setup():
        return {"commit": output(["git", "-C", str(cfg.repo_root), "rev-parse", "HEAD"]),
                "dirty": bool(output(["git", "-C", str(cfg.repo_root), "status", "--porcelain"]))}

    def runner_version():
        value = runner.installed_version(cfg.runner_dir)
        if value is None:
            raise SetupError("runner version is unavailable")
        return fmt_version(value)

    probe("setup", setup)
    probe("config_sha256", lambda: hashlib.sha256((cfg.repo_root / "config.toml").read_bytes()).hexdigest())
    probe("os", lambda: {"version": output(["/usr/bin/sw_vers", "-productVersion"]),
                         "build": output(["/usr/bin/sw_vers", "-buildVersion"]),
                         "architecture": platform.machine()})
    probe("runner_version", runner_version)
    probe("homebrew", lambda: _homebrew(cfg, include_dependencies=include_homebrew_dependencies))
    probe("xcodes", lambda: _xcodes(cfg))
    return snapshot


def _valid_xcodes(value) -> bool:
    def metadata(item):
        return isinstance(item, dict) and all(
            isinstance(item.get(key), str) and item[key] for key in ("version", "build")
        )
    if not isinstance(value, dict) or not {"managed", "installed", "selected", "runner"} <= value.keys() or not isinstance(value.get("managed"), bool):
        return False
    if value["managed"]:
        if not isinstance(value.get("installed"), list) or not metadata(value.get("selected")):
            return False
        if any(not metadata(item) or not isinstance(item.get("prerelease"), list) for item in value["installed"]):
            return False
    elif value.get("installed") is not None or (value.get("selected") is not None and not metadata(value["selected"])):
        return False
    runner_selection = value.get("runner")
    return (
        runner_selection is None and not value["managed"] and value["selected"] is None
        or metadata(runner_selection) and runner_selection.get("source") in ("global", "DEVELOPER_DIR")
    )


def differences(left: dict, right: dict) -> list[str]:
    """Compare host-independent state; unknown data must never mean in sync."""
    required = {"setup", "config_sha256", "os", "runner_version", "homebrew", "xcodes"}
    for label, snapshot in (("left", left), ("right", right)):
        if not isinstance(snapshot, dict) or snapshot.get("schema_version") != SCHEMA_VERSION:
            raise SetupError(f"{label} inventory uses an unsupported schema")
        values = snapshot.get("comparable")
        if snapshot.get("errors") != [] or not isinstance(values, dict) or not required <= values.keys():
            raise SetupError(f"{label} inventory is incomplete: {snapshot.get('errors') or 'missing fields'}")
        setup = values.get("setup")
        homebrew = values.get("homebrew")
        xcodes = values.get("xcodes")
        os_info = values.get("os")
        if not (
            isinstance(setup, dict) and isinstance(setup.get("commit"), str) and setup["commit"]
            and isinstance(setup.get("dirty"), bool)
            and isinstance(values["config_sha256"], str) and len(values["config_sha256"]) == 64
            and isinstance(os_info, dict)
            and all(isinstance(os_info.get(key), str) and os_info[key] for key in ("version", "build", "architecture"))
            and isinstance(values["runner_version"], str) and values["runner_version"]
            and isinstance(homebrew, dict) and isinstance(homebrew.get("formulae"), dict)
            and isinstance(homebrew.get("casks"), dict)
            and all(isinstance(item, dict) and isinstance(item.get("version"), str) and item["version"]
                    and isinstance(item.get("pinned"), bool) for item in homebrew["formulae"].values())
            and _valid_xcodes(xcodes)
        ):
            raise SetupError(f"{label} inventory is incomplete: invalid state fields")
        if setup["dirty"]:
            raise SetupError(f"{label} setup checkout has uncommitted changes; clean or commit it before comparing")
    changes = []

    def compare(path, before, after):
        if isinstance(before, dict) and isinstance(after, dict):
            for key in sorted(before.keys() | after.keys()):
                child = f"{path}.{key}" if path else key
                if key not in before:
                    changes.append(f"{child}: only in right inventory ({json.dumps(after[key], sort_keys=True)})")
                elif key not in after:
                    changes.append(f"{child}: only in left inventory ({json.dumps(before[key], sort_keys=True)})")
                else:
                    compare(child, before[key], after[key])
        elif before != after:
            changes.append(f"{path}: {json.dumps(before, sort_keys=True)} -> {json.dumps(after, sort_keys=True)}")

    compare("", left["comparable"], right["comparable"])
    return changes


def save(snapshot: dict, path: Path) -> None:
    """Atomically replace an inventory, readable only by its owner."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w") as file:
            json.dump(snapshot, file, indent=2, sort_keys=True)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
