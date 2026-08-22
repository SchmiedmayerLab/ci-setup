"""Loading and validating config.toml."""

from __future__ import annotations

import os
import socket
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .util import SetupError, output, warn

# `./setup.zsh store-pat` saves the PAT under this Keychain service name and
# converge picks it up automatically.
KEYCHAIN_SERVICE = "github-runner-pat"


@dataclass
class Config:
    repo_root: Path

    # [github]
    scope: str = "repo"  # "repo" | "org"
    owner: str = ""
    repo: str | None = None
    pat: str | None = None
    pat_command: str | None = None

    # [runner]
    runner_name: str = ""
    labels: list[str] = field(default_factory=list)
    runner_dir: Path = Path()
    work_dir: str = "_work"
    group: str | None = None

    # [xcode]
    xcode_manage: bool = True
    xcode_install_beta: bool = True
    xcode_platforms: list[str] = field(default_factory=lambda: ["all"])

    # [brew]
    brew_extra_formulae: list[str] = field(default_factory=list)
    brew_extra_casks: list[str] = field(default_factory=list)

    # [boot]
    boot_install: bool = True
    boot_label: str = "com.selfhosted-runner.setup"

    # [power]
    power_manage: bool = False

    @property
    def github_url(self) -> str:
        if self.scope == "repo":
            return f"https://github.com/{self.owner}/{self.repo}"
        return f"https://github.com/{self.owner}"

    @property
    def api_base(self) -> str:
        if self.scope == "repo":
            return f"repos/{self.owner}/{self.repo}"
        return f"orgs/{self.owner}"

    @property
    def new_runner_page(self) -> str:
        if self.scope == "repo":
            return f"https://github.com/{self.owner}/{self.repo}/settings/actions/runners/new"
        return f"https://github.com/organizations/{self.owner}/settings/actions/runners/new"

    @property
    def runners_settings_page(self) -> str:
        if self.scope == "repo":
            return f"https://github.com/{self.owner}/{self.repo}/settings/actions/runners"
        return f"https://github.com/organizations/{self.owner}/settings/actions/runners"


def _str_list(raw, where: str) -> list[str]:
    if not isinstance(raw, list) or not all(isinstance(x, str) for x in raw):
        raise SetupError(f"config.toml: {where} must be a list of strings")
    return raw


def _str(table: dict, key: str, where: str, default: str = "") -> str:
    raw = table.get(key, default)
    if not isinstance(raw, str):
        raise SetupError(f"config.toml: {where} must be a string")
    return raw.strip()


def load(repo_root: Path) -> Config:
    path = repo_root / "config.toml"
    if not path.exists():
        raise SetupError(
            "config.toml not found — run: cp config.example.toml config.toml "
            "and edit it first"
        )
    try:
        data = tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as e:
        raise SetupError(f"config.toml is not valid TOML: {e}") from e

    gh = data.get("github", {})
    runner = data.get("runner", {})
    xcode = data.get("xcode", {})
    brew = data.get("brew", {})
    boot = data.get("boot", {})
    power = data.get("power", {})

    cfg = Config(repo_root=repo_root)

    cfg.scope = _str(gh, "scope", "github.scope", "repo")
    if cfg.scope not in ("repo", "org"):
        raise SetupError('config.toml: github.scope must be "repo" or "org"')
    cfg.owner = _str(gh, "owner", "github.owner")
    if not cfg.owner or cfg.owner in ("my-org", "my-user"):
        raise SetupError("config.toml: set github.owner to your GitHub user/org")
    cfg.repo = _str(gh, "repo", "github.repo") or None
    if cfg.scope == "repo" and (not cfg.repo or cfg.repo == "my-repo"):
        raise SetupError('config.toml: github.scope = "repo" requires github.repo')
    cfg.pat = _str(gh, "pat", "github.pat") or None
    cfg.pat_command = _str(gh, "pat_command", "github.pat_command") or None

    default_name = socket.gethostname().split(".")[0] or "mac-runner"
    cfg.runner_name = _str(runner, "name", "runner.name") or default_name
    cfg.labels = _str_list(runner.get("labels", []), "runner.labels")
    cfg.runner_dir = Path(
        os.path.expanduser(_str(runner, "dir", "runner.dir", "~/actions-runner"))
    )
    if not cfg.runner_dir.is_absolute():
        # A relative dir would resolve against the cwd, which differs between
        # manual runs and the boot agent — two diverging runner installs.
        raise SetupError(
            "config.toml: runner.dir must be an absolute path (may start with ~)"
        )
    cfg.work_dir = _str(runner, "work_dir", "runner.work_dir", "_work") or "_work"
    cfg.group = _str(runner, "group", "runner.group") or None
    if cfg.group and cfg.scope != "org":
        raise SetupError('config.toml: runner.group only applies to github.scope = "org"')

    cfg.xcode_manage = bool(xcode.get("manage", True))
    cfg.xcode_install_beta = bool(xcode.get("install_beta", True))
    cfg.xcode_platforms = _str_list(xcode.get("platforms", ["all"]), "xcode.platforms")

    cfg.brew_extra_formulae = _str_list(brew.get("extra_formulae", []), "brew.extra_formulae")
    cfg.brew_extra_casks = _str_list(brew.get("extra_casks", []), "brew.extra_casks")

    cfg.boot_install = bool(boot.get("install_agent", True))
    cfg.boot_label = _str(boot, "label", "boot.label", "com.selfhosted-runner.setup")

    cfg.power_manage = bool(power.get("manage", False))

    return cfg


def resolve_pat(cfg: Config) -> str | None:
    """PAT resolution order: config value, pat_command, Keychain, none."""
    if cfg.pat:
        return cfg.pat
    if cfg.pat_command:
        try:
            # Bounded: an unattended run must not hang on a credential helper
            # that tries to prompt (1Password, locked keychains, ...).
            token = output(["/bin/zsh", "-c", cfg.pat_command], timeout=60)
        except SetupError as e:
            token = ""
            if "timed out" in str(e):
                warn("github.pat_command timed out (tried to prompt?)")
        if token:
            return token
        warn("github.pat_command produced no token; trying the Keychain instead")
    try:
        token = output(
            ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
            timeout=15,
        )
    except SetupError as e:
        token = ""
        if "timed out" in str(e):
            warn("Keychain lookup timed out (keychain locked?) — continuing without a PAT")
    return token or None
