#
# This source file is part of the SchmiedmayerLab ci-setup open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT
#

"""Self-update checks using temporary, local Git repositories only."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cisetup import update, util  # noqa: E402
from cisetup.util import SetupError  # noqa: E402


class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.remote = self.root / "remote.git"
        self.author = self.root / "author"
        self.checkout = self.root / "checkout"
        self.env = {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_AUTHOR_NAME": "Test Author",
            "GIT_AUTHOR_EMAIL": "author@example.invalid",
            "GIT_COMMITTER_NAME": "Test Author",
            "GIT_COMMITTER_EMAIL": "author@example.invalid",
            "GIT_TERMINAL_PROMPT": "0",
        }
        self.environment = patch.dict(os.environ, self.env)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        # Never let an inherited Git override redirect these fixture mutations
        # into the real checkout or a user's alternate index/object database.
        for variable in (
            "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
            "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_CONFIG_COUNT",
        ):
            os.environ.pop(variable, None)
        self.git(self.root, "init", "--bare", str(self.remote))
        self.git(self.root, "init", "--initial-branch=maintenance", str(self.author))
        (self.author / "setup-content").write_text("initial version\n")
        (self.author / ".gitignore").write_text("ignored-output\n")
        self.git(self.author, "add", ".")
        self.git(self.author, "commit", "-m", "Initial commit")
        self.git(self.author, "remote", "add", "upstream", str(self.remote))
        self.git(self.author, "push", "--set-upstream", "upstream", "maintenance")
        self.git(self.root, "clone", "--origin", "team", "--branch", "maintenance", str(self.remote), str(self.checkout))
        self.before = self.git(self.checkout, "rev-parse", "HEAD")
        self.addCleanup(util.WARNINGS.clear)

    def git(self, cwd, *args):
        return subprocess.run(
            ["git", "-c", "core.hooksPath=" + os.devnull, "-c", "commit.gpgSign=false", *args],
            cwd=cwd,
            env=dict(os.environ, **self.env),
            text=True,
            capture_output=True,
            check=True,
            timeout=10,
            stdin=subprocess.DEVNULL,
        ).stdout.strip()

    def remote_change(self):
        (self.author / "setup-content").write_text("updated version\n")
        self.git(self.author, "commit", "-am", "Update setup")
        self.git(self.author, "push")
        return self.git(self.author, "rev-parse", "HEAD")

    def test_follows_configured_upstream_and_reports_changed_then_unchanged(self):
        after = self.remote_change()
        self.assertTrue(update.pull(self.checkout))
        self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), after)
        self.assertEqual(self.git(self.checkout, "branch", "--show-current"), "maintenance")
        self.assertEqual((self.checkout / "setup-content").read_text(), "updated version\n")
        self.assertFalse(update.pull(self.checkout))

    def test_refuses_tracked_changes_and_untracked_files_without_touching_head(self):
        self.remote_change()
        changed = self.checkout / "setup-content"
        changed.write_text("local edit\n")
        with self.assertRaisesRegex(SetupError, "local changes or untracked files"):
            update.pull(self.checkout)
        self.assertEqual(changed.read_text(), "local edit\n")
        self.git(self.checkout, "restore", "setup-content")
        untracked = self.checkout / "untracked-output"
        untracked.write_text("preserve this\n")
        with self.assertRaisesRegex(SetupError, "local changes or untracked files"):
            update.pull(self.checkout)
        self.assertEqual(untracked.read_text(), "preserve this\n")
        self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), self.before)

    def test_ignored_files_do_not_block_update(self):
        self.remote_change()
        ignored = self.checkout / "ignored-output"
        ignored.write_text("local cache\n")
        self.assertTrue(update.pull(self.checkout))
        self.assertEqual(ignored.read_text(), "local cache\n")

    def test_refuses_detached_checkout(self):
        self.git(self.checkout, "checkout", "--detach", "HEAD")
        with self.assertRaisesRegex(SetupError, "detached HEAD"):
            update.pull(self.checkout)
        self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), self.before)

    def test_refuses_branch_without_upstream(self):
        self.git(self.checkout, "branch", "--unset-upstream")
        with self.assertRaisesRegex(SetupError, "no usable upstream"):
            update.pull(self.checkout)

    def test_divergence_is_not_merged_or_rebased_even_when_pull_rebase_is_configured(self):
        self.remote_change()
        (self.checkout / "local-only").write_text("local commit\n")
        self.git(self.checkout, "add", "local-only")
        self.git(self.checkout, "commit", "-m", "Local change")
        local_head = self.git(self.checkout, "rev-parse", "HEAD")
        self.git(self.checkout, "config", "pull.rebase", "true")
        with self.assertRaises(SetupError):
            update.pull(self.checkout)
        self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), local_head)
        self.assertEqual(self.git(self.checkout, "status", "--porcelain"), "")
        self.assertEqual((self.checkout / "local-only").read_text(), "local commit\n")

    def test_network_failure_leaves_current_commit_intact(self):
        self.git(self.checkout, "remote", "set-url", "team", str(self.root / "missing.git"))
        with self.assertRaises(SetupError):
            update.pull(self.checkout)
        self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), self.before)

    def test_all_commands_disable_prompts_and_share_a_bounded_timeout(self):
        with patch.object(util, "run", wraps=util.run) as run:
            self.assertFalse(update.pull(self.checkout))
        timeouts = []
        for call in run.call_args_list:
            self.assertTrue(call.kwargs["stdin_devnull"])
            self.assertTrue(call.kwargs["start_new_session"])
            self.assertEqual(call.kwargs["env"]["GIT_TERMINAL_PROMPT"], "0")
            self.assertEqual(call.kwargs["env"]["GIT_SSH_COMMAND"], "ssh -o BatchMode=yes")
            self.assertEqual(call.kwargs["env"]["GIT_ASKPASS"], "/usr/bin/false")
            self.assertIn("credential.interactive=false", call.args[0])
            timeouts.append(call.kwargs["timeout"])
        self.assertTrue(all(0 < timeout <= update.TIMEOUT_SECONDS for timeout in timeouts))
        self.assertEqual(timeouts, sorted(timeouts, reverse=True))

    def test_exhausted_deadline_never_starts_another_git_command(self):
        with patch.object(update.time, "monotonic", side_effect=[0, 121]), patch.object(util, "run") as run:
            with self.assertRaisesRegex(SetupError, "timed out after 120s"):
                update.pull(self.checkout)
            run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
