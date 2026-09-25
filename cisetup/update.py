#
# This source file is part of the SchmiedmayerLab ci-setup open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT
#

"""Bounded, non-interactive updates of a clean setup checkout."""

from __future__ import annotations

import os
import time
from pathlib import Path

from . import util
from .util import SetupError, log, ok


TIMEOUT_SECONDS = 120


def pull(repo_root: Path) -> bool:
    """Fast-forward the current branch from its configured upstream.

    The caller must hold the setup lock and pause runner intake before calling.
    No branch switching, resetting, stashing, or registration changes happen
    here. Return whether HEAD changed so the caller can restart on new code.
    """
    deadline = time.monotonic() + TIMEOUT_SECONDS
    env = dict(
        os.environ,
        GIT_TERMINAL_PROMPT="0",
        GIT_ASKPASS="/usr/bin/false",
        SSH_ASKPASS="/usr/bin/false",
        GIT_SSH_COMMAND="ssh -o BatchMode=yes",
    )

    def git(*args: str, check: bool = True):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise SetupError(f"setup update timed out after {TIMEOUT_SECONDS}s")
        return util.run(
            ["git", "-c", "credential.interactive=false", *args],
            cwd=repo_root,
            env=env,
            capture=True,
            check=check,
            stdin_devnull=True,
            start_new_session=True,
            timeout=remaining,
        )

    if git("status", "--porcelain", "--untracked-files=normal").stdout.strip():
        raise SetupError(
            "setup update refused: the checkout has local changes or untracked files; "
            "commit, stash, or remove them before updating"
        )
    branch = git("symbolic-ref", "--quiet", "--short", "HEAD", check=False)
    if branch.returncode != 0:
        raise SetupError("setup update refused: detached HEAD; check out a branch with an upstream")
    upstream = git("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}", check=False)
    if upstream.returncode != 0:
        raise SetupError(
            f"setup update refused: branch '{branch.stdout.strip()}' has no usable upstream; "
            "configure its tracking branch before updating"
        )
    before = git("rev-parse", "HEAD").stdout.strip()
    log(f"updating setup from {upstream.stdout.strip()} (current commit {before})")
    # Explicit --no-rebase wins over pull.rebase; --ff-only refuses both a
    # merge commit and rewriting local commits if the branches diverged.
    git("pull", "--ff-only", "--no-rebase", "--no-edit")
    after = git("rev-parse", "HEAD").stdout.strip()
    if after != before:
        ok(f"setup updated: {before} → {after}")
    else:
        ok(f"setup revision unchanged: {after}")
    return after != before
