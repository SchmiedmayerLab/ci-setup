#
# This source file is part of the SchmiedmayerLab ci-setup open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT
#

"""Legacy scheduler tests use temporary files and mocked launchctl only."""

import plistlib
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from cisetup import legacy
from cisetup.maintenance import MaintenanceDeferred
from cisetup.util import SetupError


class LegacyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        self.state = self.home / "state"
        self.patch("pathlib.Path.home", return_value=self.home)
        self.patch("cisetup.util.STATE_DIR", new=self.state)
        self.patch("cisetup.legacy.os.getuid", return_value=501)
        self.patch("subprocess.run", side_effect=AssertionError("host commands forbidden"))
        self.patch("subprocess.Popen", side_effect=AssertionError("host processes forbidden"))
        self.patch("cisetup.legacy.ok")
        self.run = self.patch("cisetup.legacy.run", side_effect=self.command)
        self.target = f"gui/501/{legacy.LABEL}"
        self.source = self.home / "Library/LaunchAgents" / f"{legacy.LABEL}.plist"
        self.archive = self.state / "legacy" / self.source.name
        self.commands = []
        self.loaded = True
        self.running = False
        self.bootout_succeeds = True
        self.program = str(legacy._program())
        self.on_disable = None
        self.print_error = None

    def patch(self, target, **kwargs):
        patcher = mock.patch(target, **kwargs)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def command(self, argv, **kwargs):
        self.commands.append(argv)
        self.assertEqual(argv[0], "launchctl")
        self.assertEqual(argv[2], self.target)
        self.assertTrue(kwargs["capture"])
        self.assertEqual(kwargs["timeout"], legacy.COMMAND_TIMEOUT)
        if argv[1] == "print":
            if self.print_error:
                return subprocess.CompletedProcess(argv, 1, "", self.print_error)
            if not self.loaded:
                return subprocess.CompletedProcess(
                    argv, 113, "", f'Could not find service "{legacy.LABEL}" in domain for user gui: 501',
                )
            state = "running" if self.running else "not running"
            output = f"{self.target} = {{\n\tprogram = {self.program}\n\tstate = {state}\n"
            if self.running:
                output += "\tpid = 1234\n"
            return subprocess.CompletedProcess(argv, 0, output + "}\n", "")
        if argv[1] == "disable":
            if self.on_disable:
                self.on_disable()
            return subprocess.CompletedProcess(argv, 0, "", "")
        self.assertEqual(argv[1], "bootout")
        if self.bootout_succeeds:
            self.loaded = False
            return subprocess.CompletedProcess(argv, 0, "", "")
        return subprocess.CompletedProcess(argv, 5, "", "Input/output error")

    def install_plist(self, **changes):
        definition = {
            "Label": legacy.LABEL,
            "Program": str(legacy._program()),
            "StartInterval": 86400,
        }
        definition.update(changes)
        self.source.parent.mkdir(parents=True, exist_ok=True)
        self.source.write_bytes(plistlib.dumps(definition))
        return self.source.read_bytes()

    def verbs(self):
        return [command[1] for command in self.commands]

    def test_idle_agent_is_disabled_unloaded_verified_and_archived(self):
        original = self.install_plist()
        legacy.retire_homebrew_autoupdate()
        self.assertEqual(self.verbs(), ["print", "disable", "print", "bootout", "print"])
        self.assertEqual(self.archive.read_bytes(), original)
        self.assertFalse(self.source.exists())
        legacy.retire_homebrew_autoupdate()
        self.assertEqual(self.verbs()[-1], "print")
        self.assertEqual(self.verbs().count("bootout"), 1)

    def test_absent_unloaded_agent_does_nothing(self):
        self.loaded = False
        legacy.retire_homebrew_autoupdate()
        self.assertEqual(self.verbs(), ["print"])
        self.assertFalse(self.state.exists())

    def test_unloaded_existing_plist_is_disabled_and_archived(self):
        original = self.install_plist()
        self.loaded = False
        legacy.retire_homebrew_autoupdate()
        self.assertNotIn("bootout", self.verbs())
        self.assertIn("disable", self.verbs())
        self.assertEqual(self.archive.read_bytes(), original)

    def test_already_running_agent_is_left_untouched(self):
        original = self.install_plist()
        self.running = True
        with self.assertRaises(MaintenanceDeferred):
            legacy.retire_homebrew_autoupdate()
        self.assertEqual(self.verbs(), ["print"])
        self.assertEqual(self.source.read_bytes(), original)
        self.assertFalse(self.archive.exists())

    def test_agent_starting_during_disable_is_not_killed(self):
        original = self.install_plist()
        self.on_disable = lambda: setattr(self, "running", True)
        with self.assertRaises(MaintenanceDeferred):
            legacy.retire_homebrew_autoupdate()
        self.assertEqual(self.verbs(), ["print", "disable", "print"])
        self.assertEqual(self.source.read_bytes(), original)

    def test_failed_bootout_keeps_original_plist(self):
        original = self.install_plist()
        self.bootout_succeeds = False
        with self.assertRaisesRegex(SetupError, "still loaded"):
            legacy.retire_homebrew_autoupdate()
        self.assertEqual(self.source.read_bytes(), original)
        self.assertFalse(self.archive.exists())

    def test_missing_plist_for_loaded_agent_refuses_without_changes(self):
        with self.assertRaisesRegex(SetupError, "plist is missing"):
            legacy.retire_homebrew_autoupdate()
        self.assertEqual(self.verbs(), ["print"])

    def test_unexpected_plist_label_or_program_refuses_without_commands(self):
        for change in ({"Label": "other.service"}, {"Program": "/other/program"}):
            with self.subTest(change=change):
                original = self.install_plist(**change)
                with self.assertRaisesRegex(SetupError, "unexpected label or program"):
                    legacy.retire_homebrew_autoupdate()
                self.assertEqual(self.source.read_bytes(), original)
        self.run.assert_not_called()

    def test_unexpected_loaded_program_refuses_without_changes(self):
        self.install_plist()
        self.program = "/other/program"
        with self.assertRaisesRegex(SetupError, "unexpected loaded program"):
            legacy.retire_homebrew_autoupdate()
        self.assertEqual(self.verbs(), ["print"])

    def test_malformed_plist_refuses_without_commands(self):
        self.install_plist()
        self.source.write_bytes(b'<?xml version="1.0"?><plist><bad')
        with self.assertRaisesRegex(SetupError, "cannot read"):
            legacy.retire_homebrew_autoupdate()
        self.run.assert_not_called()

    def test_unknown_launchctl_failure_is_not_treated_as_absent(self):
        self.install_plist()
        self.print_error = "Operation not permitted"
        with self.assertRaisesRegex(SetupError, "cannot verify"):
            legacy.retire_homebrew_autoupdate()
        self.assertEqual(self.verbs(), ["print"])
        self.assertTrue(self.source.exists())

    def test_different_existing_archive_is_never_overwritten(self):
        original = self.install_plist()
        self.archive.parent.mkdir(parents=True)
        self.archive.write_bytes(b"older rollback copy")
        with self.assertRaisesRegex(SetupError, "different legacy updater backup"):
            legacy.retire_homebrew_autoupdate()
        self.run.assert_not_called()
        self.assertEqual(self.source.read_bytes(), original)
        self.assertEqual(self.archive.read_bytes(), b"older rollback copy")

    def test_identical_existing_archive_allows_idempotent_retirement(self):
        original = self.install_plist()
        self.archive.parent.mkdir(parents=True)
        self.archive.write_bytes(original)
        legacy.retire_homebrew_autoupdate()
        self.assertEqual(self.archive.read_bytes(), original)
        self.assertFalse(self.source.exists())

    def test_symlink_plist_is_not_followed(self):
        original = self.install_plist()
        elsewhere = self.home / "unrelated.plist"
        self.source.rename(elsewhere)
        self.source.symlink_to(elsewhere)
        with self.assertRaisesRegex(SetupError, "symlink"):
            legacy.retire_homebrew_autoupdate()
        self.run.assert_not_called()
        self.assertEqual(elsewhere.read_bytes(), original)

    def test_definition_changed_during_unload_is_not_archived(self):
        self.install_plist()
        self.on_disable = lambda: self.install_plist(Program="/replacement")
        with self.assertRaisesRegex(SetupError, "changed during adoption"):
            legacy.retire_homebrew_autoupdate()
        self.assertFalse(self.archive.exists())
        self.assertEqual(plistlib.loads(self.source.read_bytes())["Program"], "/replacement")


if __name__ == "__main__":
    unittest.main()
