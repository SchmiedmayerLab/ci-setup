#
# This source file is part of the SchmiedmayerLab ci-setup open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT
#

"""Package failures and pins must not starve independent maintenance."""

import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from cisetup import brew, util
from cisetup.config import Config


class BrewTests(unittest.TestCase):
    def setUp(self):
        self.cfg = Config(repo_root=Path("/fake/repo"))
        self.commands = []
        self.pinned = ""
        self.outdated = "node\nfirebase-cli\npython@3.14"
        self.installed = "node\nfirebase-cli\npython@3.14"
        self.failures = set()
        self.env = brew.BrewEnv("/fake/brew", "/fake/brew/opt/openjdk", None)
        mocks = [
            patch("subprocess.Popen", side_effect=AssertionError("real command forbidden")),
            patch.object(brew, "FORMULAE", ["node", "firebase-cli", "python3"]),
            patch.object(brew, "probe", return_value=self.env),
            patch.object(brew, "_resolve_names", return_value=(
                {"node": "node", "firebase-cli": "firebase-cli", "python3": "python@3.14"}, {})),
            patch.object(brew, "output", side_effect=self.output),
            patch.object(brew, "run", side_effect=self.run_command),
            patch.object(brew, "_remove_autoupdate"),
            patch.object(brew, "_ensure_xcpretty", return_value="/fake/gem/bin"),
            patch.object(util, "runner_busy", return_value=False),
            patch.object(util, "WARNINGS", []),
        ]
        for mock in mocks:
            mock.start()
            self.addCleanup(mock.stop)

    def output(self, command, **kwargs):
        if command == ["brew", "list", "--formula", "-1"]:
            return self.installed
        if command == ["brew", "outdated", "--formula", "--quiet"]:
            return self.outdated
        if command == ["brew", "list", "--pinned"]:
            return self.pinned
        self.fail(f"unexpected command: {command}")

    def run_command(self, command, **kwargs):
        self.commands.append(command)
        self.assertEqual(kwargs["env"]["HOMEBREW_NO_AUTO_UPDATE"], "1")
        self.assertEqual(kwargs["env"]["HOMEBREW_NO_INSTALL_CLEANUP"], "1")
        if tuple(command) in self.failures:
            raise util.SetupError("simulated package failure")
        return subprocess.CompletedProcess(command, 0)

    def test_outdated_pin_reported_other_packages_upgrade(self):
        self.pinned = "node"
        with self.assertRaisesRegex(brew.BrewError, "pins respected") as failure:
            brew.ensure(self.cfg)
        self.assertNotIn(["brew", "upgrade", "--yes", "--formula", "node"], self.commands)
        self.assertIn(["brew", "upgrade", "--yes", "--formula", "firebase-cli"], self.commands)
        self.assertTrue(failure.exception.env.python_changed)
        self.assertIn(["git", "lfs", "install"], self.commands)

    def test_failed_upgrade_continues_and_returns_usable_environment(self):
        self.failures.add(("brew", "upgrade", "--yes", "--formula", "firebase-cli"))
        with self.assertRaises(brew.BrewError) as failure:
            brew.ensure(self.cfg)
        self.assertIn(["brew", "upgrade", "--yes", "--formula", "node"], self.commands)
        self.assertEqual(failure.exception.env.prefix, "/fake/brew")
        self.assertTrue(failure.exception.env.python_changed)

    def test_update_failure_is_not_reported_as_success(self):
        self.failures.add(("brew", "update", "--quiet"))
        with self.assertRaises(brew.BrewError):
            brew.ensure(self.cfg)
        self.assertIn(["brew", "upgrade", "--yes", "--formula", "node"], self.commands)

    def test_missing_formula_failure_does_not_skip_other_packages(self):
        self.installed = "node\npython@3.14"
        self.outdated = "node"
        self.failures.add(("brew", "install", "--yes", "--formula", "firebase-cli"))
        with self.assertRaises(brew.BrewError):
            brew.ensure(self.cfg)
        self.assertIn(["brew", "upgrade", "--yes", "--formula", "node"], self.commands)

    def test_busy_guard_prevents_all_package_operations(self):
        with patch.object(util, "runner_busy", return_value=True):
            with self.assertRaisesRegex(util.SetupError, "job is running"):
                brew.ensure(self.cfg)
        self.assertEqual(self.commands, [])

    def test_current_packages_dont_upgrade(self):
        self.outdated = ""
        result = brew.ensure(self.cfg)
        self.assertFalse(result.python_changed)
        self.assertFalse(any(command[1] in ("install", "upgrade") for command in self.commands))


if __name__ == "__main__":
    unittest.main()
