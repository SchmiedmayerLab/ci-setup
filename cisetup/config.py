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

    cfg.scope = gh.get("scope", "repo")
    if cfg.scope not in ("repo", "org"):
        raise SetupError('config.toml: github.scope must be "repo" or "org"')
    cfg.owner = gh.get("owner", "").strip()
    if not cfg.owner or cfg.owner in ("my-org", "my-user"):
        raise SetupError("config.toml: set github.owner to your GitHub user/org")
    cfg.repo = gh.get("repo", "").strip() or None
    if cfg.scope == "repo" and (not cfg.repo or cfg.repo == "my-repo"):
        raise SetupError('config.toml: github.scope = "repo" requires github.repo')
    cfg.pat = gh.get("pat", "").strip() or None
    cfg.pat_command = gh.get("pat_command", "").strip() or None

    default_name = socket.gethostname().split(".")[0] or "mac-runner"
    cfg.runner_name = runner.get("name", "").strip() or default_name
    cfg.labels = _str_list(runner.get("labels", []), "runner.labels")
    cfg.runner_dir = Path(os.path.expanduser(runner.get("dir", "~/actions-runner")))
    cfg.work_dir = runner.get("work_dir", "_work")
    cfg.group = runner.get("group", "").strip() or None
    if cfg.group and cfg.scope != "org":
        raise SetupError('config.toml: runner.group only applies to github.scope = "org"')

    cfg.xcode_manage = bool(xcode.get("manage", True))
    cfg.xcode_install_beta = bool(xcode.get("install_beta", True))
    cfg.xcode_platforms = _str_list(xcode.get("platforms", ["all"]), "xcode.platforms")

    cfg.brew_extra_formulae = _str_list(brew.get("extra_formulae", []), "brew.extra_formulae")
    cfg.brew_extra_casks = _str_list(brew.get("extra_casks", []), "brew.extra_casks")

    cfg.boot_install = bool(boot.get("install_agent", True))
    cfg.boot_label = boot.get("label", "com.selfhosted-runner.setup")

    cfg.power_manage = bool(power.get("manage", False))

    return cfg


def resolve_pat(cfg: Config) -> str | None:
    """PAT resolution order: config value, pat_command, Keychain, none."""
    if cfg.pat:
        return cfg.pat
    if cfg.pat_command:
        try:
            token = output(["/bin/zsh", "-c", cfg.pat_command])
        except SetupError:
            token = ""
        if token:
            return token
        warn("github.pat_command produced no token; trying the Keychain instead")
    try:
        token = output(
            ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"]
        )
    except SetupError:
        token = ""
    return token or None
