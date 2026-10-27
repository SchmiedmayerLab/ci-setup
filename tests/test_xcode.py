#
# This source file is part of the macOS Self-Hosted GitHub Actions Runner open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT
#

"""Hermetic tests for Xcode selection and failure handling (all commands mocked).

Run with:  python3 -m unittest discover -s tests
"""

import json
import pathlib
import subprocess
import tempfile
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from cisetup import xcode  # noqa: E402
from cisetup.config import Config  # noqa: E402
from cisetup.util import SetupError  # noqa: E402
from cisetup.xcode import (  # noqa: E402
    Release,
    parse_identifier,
    parse_installed,
    parse_list,
    select_desired,
)

LISTING_BETA_AHEAD = """\
12.5.1 (12E507)
16.2 (16C5032a)
16.3 (16E140)
16.4 (16F6) (Installed)
26.0 Beta 3 (17A5276g)
26.0 Beta 5 (17A5295f)
"""

LISTING_AFTER_STABLE = LISTING_BETA_AHEAD + "26.0 (17A324)\n"


class ParseListTests(unittest.TestCase):
    def test_missing_intermediate_is_verified_and_imported_without_trust_override(self):
        def download(url, target, **kwargs):
            target.write_bytes(b'test-certificate')

        with mock.patch.object(xcode.util, 'download', side_effect=download), \
                mock.patch.object(xcode.util, 'INTERACTIVE', True), \
                mock.patch.object(xcode, 'run', return_value=subprocess.CompletedProcess([], 0, '', '')) as run, \
                mock.patch.object(xcode.util, 'sudo_run') as sudo:
            xcode.ensure_wwdr_certificate()
            self.assertEqual(run.call_args.args[0][:2], ['security', 'verify-cert'])
            args = sudo.call_args.args[0]
            self.assertEqual(args[:4], ['security', 'add-certificates', '-k', '/Library/Keychains/System.keychain'])
            self.assertNotIn('trustRoot', args)

    def test_parses_stable_beta_and_annotations(self):
        releases = parse_list(LISTING_BETA_AHEAD)
        identifiers = [r.identifier for r in releases]
        self.assertEqual(
            identifiers,
            ["12.5.1", "16.2", "16.3", "16.4", "26.0 Beta 3", "26.0 Beta 5"],
        )
        installed_stable = next(r for r in releases if r.identifier == "16.4")
        self.assertEqual(installed_stable.version, (16, 4))
        self.assertEqual(installed_stable.pre, ())
        self.assertEqual(installed_stable.build, "16F6")
        beta = next(r for r in releases if r.identifier == "26.0 Beta 5")
        self.assertEqual(beta.pre, ("beta", 5))
        self.assertTrue(beta.prerelease)

    def test_release_candidate_parsing(self):
        releases = parse_list("26.1 Release Candidate (17B35)\n26.1 Release Candidate 2 (17B40)\n")
        self.assertEqual(releases[0].pre, ("rc", 1))
        self.assertEqual(releases[1].pre, ("rc", 2))
        self.assertEqual(releases[0].identifier, "26.1 Release Candidate")
        self.assertEqual(releases[1].identifier, "26.1 Release Candidate 2")

    def test_ignores_garbage_lines(self):
        releases = parse_list("something else\n\nUpdated Xcode list\n16.4 (16F6)\n")
        self.assertEqual([r.identifier for r in releases], ["16.4"])

    def test_xcodes_2x_architecture_labels(self):
        # xcodes 2.0.1+ appends [Universal]/[Apple Silicon]/[Intel] after the
        # build, before any (Installed...) annotation, plus a trailing hint.
        listing = (
            "16.4 (16F6) [Universal]\n"
            "26.0 Beta 5 (17A5295f) [Apple Silicon]\n"
            "26.1 (17B35) [Universal] (Installed, Selected)\n"
            "Showing Xcodes for this Mac by default. "
            "Switch with `--architecture arm64`.\n"
        )
        releases = parse_list(listing)
        self.assertEqual(
            [r.identifier for r in releases],
            ["16.4", "26.0 Beta 5", "26.1"],
        )
        self.assertEqual(releases[2].build, "17B35")
        self.assertEqual(releases[2].pre, ())


class SelectDesiredTests(unittest.TestCase):
    def _ids(self, listing, install_beta=True):
        desired, latest = select_desired(parse_list(listing), install_beta)
        return [r.identifier for r in desired], latest.identifier

    def test_beta_ahead_of_stable(self):
        desired, latest = self._ids(LISTING_BETA_AHEAD)
        self.assertEqual(desired, ["16.4", "26.0 Beta 5"])
        self.assertEqual(latest, "16.4")

    def test_beta_not_newer_than_stable_is_dropped(self):
        desired, latest = self._ids(LISTING_AFTER_STABLE)
        self.assertEqual(desired, ["26.0"])
        self.assertEqual(latest, "26.0")

    def test_install_beta_false(self):
        desired, _ = self._ids(LISTING_BETA_AHEAD, install_beta=False)
        self.assertEqual(desired, ["16.4"])

    def test_patch_releases_group_into_minor_trains(self):
        listing = "16.3 (16E140)\n16.4 (16F6)\n16.4.1 (16F8)\n"
        desired, latest = self._ids(listing, install_beta=False)
        self.assertEqual(desired, ["16.4.1"])
        self.assertEqual(latest, "16.4.1")

    def test_rc_preferred_over_beta_of_same_version(self):
        listing = (
            "16.4 (16F6)\n"
            "26.0 Beta 7 (17A5305f)\n"
            "26.0 Release Candidate (17A321)\n"
        )
        desired, _ = self._ids(listing)
        self.assertIn("26.0 Release Candidate", desired)
        self.assertNotIn("26.0 Beta 7", desired)

    def test_newer_rc_train(self):
        listing = "26.0 (17A324)\n16.4 (16F6)\n26.1 Release Candidate (17B35)\n"
        desired, latest = self._ids(listing)
        self.assertEqual(desired, ["26.1 Release Candidate"])
        self.assertEqual(latest, "26.1 Release Candidate")

    def test_rc_is_default_even_with_betas_disabled(self):
        listing = '27.0 (a)\n27.1 (b)\n27.2 Release Candidate (c)\n27.3 Beta (d)\n'
        desired, latest = self._ids(listing, install_beta=False)
        self.assertEqual(latest, '27.2 Release Candidate')
        self.assertEqual(desired, ['27.2 Release Candidate'])

    def test_final_release_wins_over_same_version_rc(self):
        listing = '27.1 (a)\n27.2.0 Release Candidate 2 (b)\n27.2 (c)\n'
        desired, latest = self._ids(listing, install_beta=False)
        self.assertEqual(latest, '27.2')
        self.assertNotIn('27.2.0 Release Candidate 2', desired)

    def test_single_stable_release_has_no_previous(self):
        desired, latest = self._ids("16.4 (16F6)\n", install_beta=False)
        self.assertEqual(desired, ["16.4"])
        self.assertEqual(latest, "16.4")


class ParseInstalledTests(unittest.TestCase):
    def test_parses_paths_and_identifiers(self):
        text = (
            "16.4 (16F6)\t/Applications/Xcode-16.4.0.app\n"
            "26.0 Beta 5 (17A5295f)\t/Applications/Xcode-26.0.0-Beta.5.app\n"
        )
        installed = parse_installed(text)
        self.assertEqual(installed[0].identifier, "16.4")
        self.assertEqual(installed[0].path, "/Applications/Xcode-16.4.0.app")
        self.assertEqual(installed[1].identifier, "26.0 Beta 5")
        self.assertEqual(installed[1].path, "/Applications/Xcode-26.0.0-Beta.5.app")

    def test_tolerates_annotations_and_spaces_in_path(self):
        text = "16.4 (16F6) (Selected)  /Applications/Xcode 16.4.app\n"
        installed = parse_installed(text)
        self.assertEqual(installed[0].identifier, "16.4")
        self.assertEqual(installed[0].path, "/Applications/Xcode 16.4.app")

    def test_xcodes_2x_architecture_labels(self):
        text = (
            "16.4 (16F6) [Universal]\t/Applications/Xcode-16.4.0.app\n"
            "26.0 (17A324) [Apple Silicon] (Selected)  /Applications/Xcode.app\n"
            "26.0 Beta 5 (17A5295f) [Apple Silicon]\t/Applications/Xcode-26.0.0-Beta.5.app\n"
        )
        installed = parse_installed(text)
        self.assertEqual(
            [(i.identifier, i.path) for i in installed],
            [
                ("16.4", "/Applications/Xcode-16.4.0.app"),
                ("26.0", "/Applications/Xcode.app"),
                ("26.0 Beta 5", "/Applications/Xcode-26.0.0-Beta.5.app"),
            ],
        )
        self.assertEqual(installed[1].build, "17A324")


class ParseIdentifierTests(unittest.TestCase):
    def test_stable(self):
        self.assertEqual(parse_identifier("16.4"), ((16, 4), ()))
        self.assertEqual(parse_identifier("16.4.1"), ((16, 4, 1), ()))

    def test_prerelease(self):
        self.assertEqual(parse_identifier("26.0 Beta 5"), ((26, 0), ("beta", 5)))
        self.assertEqual(parse_identifier("26.1 Release Candidate"), ((26, 1), ("rc", 1)))

    def test_unrecognizable_is_none(self):
        self.assertIsNone(parse_identifier("garbage"))
        self.assertIsNone(parse_identifier("16.4 [Universal]"))


class SortKeyTests(unittest.TestCase):
    def test_ordering(self):
        beta = Release(version=(26, 0), pre=("beta", 7), identifier="26.0 Beta 7", build="a")
        rc = Release(version=(26, 0), pre=("rc", 1), identifier="26.0 Release Candidate", build="b")
        stable = Release(version=(26, 0), pre=(), identifier="26.0", build="c")
        self.assertLess(beta.sort_key(), rc.sort_key())
        self.assertLess(rc.sort_key(), stable.sort_key())



class XcodeMaintenanceTests(unittest.TestCase):
    """No test may invoke xcodes, xcodebuild, sudo or a real runner."""

    def setUp(self):
        self.cfg = Config(repo_root=pathlib.Path("/fake/repo"))
        self.cfg.xcode_install_beta = False
        self.run = self._patch("run", return_value=subprocess.CompletedProcess([], 0, "", ""))
        self.output = self._patch("output")
        self._patch("shutil.which", return_value="/fake/xcodes")
        self._patch("util.runner_busy", return_value=False)
        self._patch("util.sudo_run", side_effect=AssertionError("unexpected sudo"))
        self._patch("util.INTERACTIVE", new=False)
        self.post_install = self._patch("_post_install")
        self.runner_developer_dir = self._patch("runner_developer_dir", return_value=None)
        self.select = self._patch("_ensure_global_selection")
        self.remove = self._patch("_remove_unwanted_xcodes", return_value=[])
        self.runtimes = self._patch("_cleanup_runtimes")
        self.listing = "16.3 (16E140)\n16.4 (16F6)\n"
        self.installed = (
            "16.3 (16E140) /fake/Xcode-16.3.app\n"
            "16.4 (16F6) /fake/Xcode-16.4.app\n"
        )
        self.output.side_effect = lambda cmd, **kwargs: (
            self.listing if cmd == ["xcodes", "list"] else self.installed
        )

    def _patch(self, name, **kwargs):
        patcher = mock.patch("cisetup.xcode." + name, **kwargs)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def assert_preserved(self):
        self.select.assert_not_called()
        self.remove.assert_not_called()
        self.runtimes.assert_not_called()

    def test_pinned_and_latest_toolchains_are_both_prepared_and_retained(self):
        self.cfg.xcode_pinned_versions = ['16.3']
        xcode.ensure(self.cfg)
        self.assertEqual([call.args[1].identifier for call in self.post_install.call_args_list], ['16.4', '16.3'])
        self.assertEqual([release.identifier for release in self.remove.call_args.args[1]], ['16.4', '16.3'])
        self.select.assert_called_once_with(pathlib.Path('/fake/Xcode-16.4.app/Contents/Developer'))

    def test_missing_pin_prevents_install_selection_and_cleanup(self):
        self.cfg.xcode_pinned_versions = ['26.6']
        with self.assertRaisesRegex(SetupError, '26.6.*unavailable'):
            xcode.ensure(self.cfg)
        self.post_install.assert_not_called()
        self.assert_preserved()
        self.assertFalse(any(call.args[0][:2] == ['xcodes', 'install'] for call in self.run.call_args_list))

    def test_explicit_older_default_keeps_both_and_returns_its_developer_directory(self):
        self.cfg.xcode_pinned_versions = ['16.3']
        self.cfg.xcode_default_version = '16.3'
        selected = xcode.ensure(self.cfg)
        self.assertEqual(selected, pathlib.Path('/fake/Xcode-16.3.app/Contents/Developer'))
        self.select.assert_called_once_with(selected)
        self.assertEqual([release.identifier for release in self.remove.call_args.args[1]], ['16.4', '16.3'])

    def test_pinned_preparation_failure_prevents_selection_and_cleanup(self):
        self.cfg.xcode_pinned_versions = ['16.3']
        self.post_install.side_effect = [None, SetupError('pinned first launch or platforms incomplete')]
        with self.assertRaisesRegex(xcode.XcodeSetupError, 'pinned first launch or platforms incomplete'):
            xcode.ensure(self.cfg)
        self.assertEqual(self.post_install.call_count, 2)
        self.assert_preserved()

    def test_both_retained_toolchains_reach_runtime_cleanup(self):
        self.cfg.xcode_pinned_versions = ['16.3']
        self.remove.return_value = parse_installed(self.installed)
        xcode.ensure(self.cfg)
        self.runtimes.assert_called_once_with([
            pathlib.Path('/fake/Xcode-16.3.app/Contents/Developer'),
            pathlib.Path('/fake/Xcode-16.4.app/Contents/Developer'),
        ])

    def test_failed_install_preserves_working_xcodes_and_runtimes(self):
        self.installed = "16.3 (16E140) /fake/Xcode-16.3.app\n"
        def command(cmd, **kwargs):
            if cmd[:2] == ["xcodes", "install"]:
                raise SetupError("Apple ID authentication required")
            return subprocess.CompletedProcess(cmd, 0, "", "")
        self.run.side_effect = command
        with self.assertRaisesRegex(xcode.XcodeSetupError, "Apple ID authentication") as ctx:
            xcode.ensure(self.cfg)
        self.assertIsNone(ctx.exception.developer_dir)
        self.assert_preserved()
        self.post_install.assert_not_called()
        install = next(call.args[0] for call in self.run.call_args_list if call.args[0][:2] == ['xcodes', 'install'])
        self.assertIn('--no-superuser', install)

    def test_independent_installs_continue_and_errors_are_aggregated(self):
        self.cfg.xcode_install_beta = True
        self.listing += "26.0 Beta 5 (17A5295f)\n"
        self.installed = "16.3 (16E140) /fake/Xcode-16.3.app\n"
        def command(cmd, **kwargs):
            if cmd[:2] == ["xcodes", "install"]:
                raise SetupError("failed " + cmd[-1])
            return subprocess.CompletedProcess(cmd, 0, "", "")
        self.run.side_effect = command
        with self.assertRaises(xcode.XcodeSetupError) as ctx:
            xcode.ensure(self.cfg)
        self.assertIn("failed 16.4", str(ctx.exception))
        self.assertIn("failed 26.0 Beta 5", str(ctx.exception))
        self.assertEqual(
            [call.args[0][-1] for call in self.run.call_args_list if call.args[0][1] == "install"],
            ["16.4", "26.0 Beta 5"],
        )
        self.assert_preserved()

    def test_readiness_failure_keeps_selection_and_prepares_other_xcodes(self):
        self.post_install.side_effect = [SetupError("first-launch incomplete"), None]
        with self.assertRaisesRegex(xcode.XcodeSetupError, "first-launch incomplete"):
            xcode.ensure(self.cfg)
        self.assertEqual(self.post_install.call_count, 1)
        self.assert_preserved()

    def test_cached_releases_do_not_justify_cleanup_or_success(self):
        self.run.return_value = subprocess.CompletedProcess([], 1, "", "offline")
        with self.assertRaisesRegex(xcode.XcodeSetupError, "cached releases"):
            xcode.ensure(self.cfg)
        self.assertEqual(self.post_install.call_count, 1)
        self.assert_preserved()

    def test_wrong_installed_build_is_not_considered_ready(self):
        self.installed = self.installed.replace("16F6", "16F5")
        with self.assertRaisesRegex(xcode.XcodeSetupError, r"16.4 \(16F6\) is not installed"):
            xcode.ensure(self.cfg)
        self.assert_preserved()

    def test_busy_runner_blocks_all_xcode_operations(self):
        with mock.patch("cisetup.xcode.util.runner_busy", return_value=True):
            with self.assertRaisesRegex(SetupError, "job is currently running"):
                xcode.ensure(self.cfg)
        self.run.assert_not_called()
        self.output.assert_not_called()
        self.assert_preserved()

    def test_missing_xcodes_is_a_phase_failure(self):
        with mock.patch("cisetup.xcode.shutil.which", return_value=None):
            with self.assertRaisesRegex(SetupError, "not installed"):
                xcode.ensure(self.cfg)
        self.assert_preserved()

    def test_selection_failure_prevents_cleanup(self):
        self.select.side_effect = SetupError("selection failed")
        with self.assertRaisesRegex(SetupError, "selection failed"):
            xcode.ensure(self.cfg)
        self.remove.assert_not_called()
        self.runtimes.assert_not_called()

    def test_cleanup_error_carries_ready_replacement_for_runner_environment(self):
        self.remove.side_effect = SetupError("old Xcode removal failed")
        with self.assertRaises(xcode.XcodeSetupError) as ctx:
            xcode.ensure(self.cfg)
        ready = pathlib.Path("/fake/Xcode-16.4.app/Contents/Developer")
        self.assertEqual(ctx.exception.developer_dir, ready)
        self.select.assert_called_once_with(ready)
        self.runtimes.assert_not_called()

    def test_success_selects_ready_latest_and_preserves_retained_runtime_needs(self):
        kept = [xcode.InstalledXcode("99.0", "99A1", "/fake/Future.app")]
        self.remove.return_value = kept
        ready = xcode.ensure(self.cfg)
        self.assertEqual(ready, pathlib.Path("/fake/Xcode-16.4.app/Contents/Developer"))
        self.select.assert_called_once_with(ready)
        self.runtimes.assert_called_once_with([pathlib.Path("/fake/Future.app/Contents/Developer")])

    def test_current_runner_toolchain_is_protected_and_kept_for_runtime_matching(self):
        old = xcode.InstalledXcode("16.2", "16C50", "/fake/Previous.app")
        current = pathlib.Path(old.path) / "Contents/Developer"
        self.runner_developer_dir.return_value = current
        self.remove.return_value = [old]
        xcode.ensure(self.cfg)
        self.assertEqual(self.remove.call_args.kwargs["protected_developer_dirs"], {current})
        self.runtimes.assert_called_once_with([current])


class XcodeReadinessTests(unittest.TestCase):
    def setUp(self):
        self.cfg = Config(repo_root=pathlib.Path("/fake/repo"))
        self.release = Release((26, 0), (), "26.0", "17A324")
        self.app = pathlib.Path("/fake/Xcode.app")
        self.run = self._patch("run", return_value=subprocess.CompletedProcess([], 0, "", ""))
        self._patch("util.sudo_run", side_effect=AssertionError("unexpected sudo"))
        self._patch("util.INTERACTIVE", new=False)
        self._patch("Path.exists", return_value=True)

    def _patch(self, name, **kwargs):
        patcher = mock.patch("cisetup.xcode." + name, **kwargs)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def test_failed_platforms_and_metal_are_all_attempted_and_reported(self):
        self.cfg.xcode_platforms = ["iOS", "watchOS"]
        def command(cmd, **kwargs):
            if cmd[1].startswith("-download"):
                raise SetupError("download unavailable: " + " ".join(cmd[1:]))
            return subprocess.CompletedProcess(cmd, 0, "", "")
        self.run.side_effect = command
        with self.assertRaises(SetupError) as ctx:
            xcode._post_install(self.cfg, self.release, self.app)
        for component in ("iOS platform", "watchOS platform", "Metal toolchain"):
            self.assertIn(component, str(ctx.exception))
        self.assertEqual(len([c for c in self.run.call_args_list if c.args[0][1].startswith("-download")]), 3)

    def test_first_launch_recheck_must_succeed_before_downloads(self):
        self._patch('_unattended_first_launch', return_value=True)
        self.run.side_effect = [
            subprocess.CompletedProcess([], 1, "", ""),
            subprocess.CompletedProcess([], 1, "", ""),
        ]
        with self.assertRaisesRegex(SetupError, "still incomplete"):
            xcode._post_install(self.cfg, self.release, self.app)
        self.assertFalse(any(c.args[0][1].startswith("-download") for c in self.run.call_args_list))

    def test_unattended_first_launch_failure_is_not_ready(self):
        self.run.return_value = subprocess.CompletedProcess([], 1, "", "")
        with mock.patch("cisetup.xcode._unattended_first_launch", return_value=False):
            with self.assertRaisesRegex(SetupError, "first-launch setup failed"):
                xcode._post_install(self.cfg, self.release, self.app)

    def test_host_bootstrap_accepts_license_before_first_launch_without_prompt(self):
        self.run.side_effect = [
            subprocess.CompletedProcess([], 1, '', ''),
            subprocess.CompletedProcess([], 0, '', ''),
            subprocess.CompletedProcess([], 0, '', ''),
            subprocess.CompletedProcess([], 0, '', ''),
            subprocess.CompletedProcess([], 0, '', ''),
            subprocess.CompletedProcess([], 0, '', ''),
        ]
        with mock.patch.dict(xcode.os.environ, {'SUDO_ASKPASS': '/private/askpass',
                                               'RUNNER_BOOTSTRAP_PASSWORD': 'private-test-password'}):
            xcode._post_install(self.cfg, self.release, self.app)
        commands = [call.args[0] for call in self.run.call_args_list]
        executable = self.app / 'Contents/Developer/usr/bin/xcodebuild'
        self.assertEqual(commands[1], ['sudo', '-A', executable, '-license', 'accept'])
        self.assertEqual(commands[2], ['sudo', '-A', executable, '-runFirstLaunch'])
        self.assertNotIn(['/usr/bin/xcodebuild', '-runFirstLaunch'], commands)
        self.assertNotIn('private-test-password', str(commands))
        self.assertTrue(any('-downloadAllPlatforms' in command for command in commands))
        self.assertTrue(any('MetalToolchain' in command for command in commands))

    def test_license_failure_prevents_first_launch_and_platform_downloads(self):
        self.run.side_effect = [subprocess.CompletedProcess([], 1, '', ''), SetupError('license acceptance failed')]
        with mock.patch.dict(xcode.os.environ, {'SUDO_ASKPASS': '/private/askpass',
                                               'RUNNER_BOOTSTRAP_PASSWORD': 'private-test-password'}):
            with self.assertRaisesRegex(SetupError, 'license acceptance failed'):
                xcode._post_install(self.cfg, self.release, self.app)
        self.assertEqual(self.run.call_count, 2)

    def test_temporary_selection_is_restored_even_when_command_raises(self):
        self.run.side_effect = [
            subprocess.CompletedProcess([], 0, "/fake/Previous/Developer\n", ""),
            subprocess.CompletedProcess([], 0, "", ""),
            SetupError("could not start xcodebuild"),
            subprocess.CompletedProcess([], 0, "", ""),
        ]
        with self.assertRaisesRegex(SetupError, "could not start xcodebuild"):
            xcode._unattended_first_launch(self.app / "Contents/Developer")
        self.assertEqual(self.run.call_args.args[0][-1], "/fake/Previous/Developer")

    def test_unattended_preparation_never_attempts_gui_first_launch(self):
        self.run.side_effect = lambda cmd, **kwargs: subprocess.CompletedProcess(
            cmd, 1 if self.run.call_count == 1 else 0, '', '')
        helper = self._patch('_unattended_first_launch', return_value=True)
        xcode._post_install(self.cfg, self.release, self.app)
        helper.assert_called_once_with(self.app / 'Contents/Developer')
        self.assertFalse(any('-runFirstLaunch' in c.args[0] for c in self.run.call_args_list))
        self.assertEqual(sum('-checkFirstLaunchStatus' in c.args[0] for c in self.run.call_args_list), 2)

    def test_unattended_failure_or_timeout_prevents_component_downloads(self):
        for failure in (False, SetupError('first launch timed out')):
            with self.subTest(failure=failure):
                self.run.reset_mock()
                self.run.return_value = subprocess.CompletedProcess([], 1, '', '')
                options = {'side_effect': failure} if isinstance(failure, Exception) else {'return_value': failure}
                with mock.patch.object(xcode, '_unattended_first_launch', **options), self.assertRaises(SetupError):
                    xcode._post_install(self.cfg, self.release, self.app)
                self.assertEqual(self.run.call_count, 1)

    def test_interactive_first_launch_uses_exact_xcode_and_rechecks(self):
        self.run.side_effect = lambda cmd, **kwargs: subprocess.CompletedProcess(
            cmd, 1 if self.run.call_count == 1 else 0, '', '')
        with mock.patch.object(xcode.util, 'INTERACTIVE', True), \
                mock.patch.object(xcode.util, 'sudo_run') as sudo:
            xcode._post_install(self.cfg, self.release, self.app)
        executable = self.app / 'Contents/Developer/usr/bin/xcodebuild'
        self.assertEqual(sudo.call_args_list, [
            mock.call([executable, '-license', 'accept']),
            mock.call([executable, '-runFirstLaunch'], timeout=30 * 60),
        ])
        self.assertFalse(any('-runFirstLaunch' in c.args[0] for c in self.run.call_args_list))

    def test_unattended_helper_restores_selection_after_success_failure_and_timeout(self):
        for result in (subprocess.CompletedProcess([], 0), subprocess.CompletedProcess([], 1),
                       SetupError('command timed out')):
            with self.subTest(result=result):
                self.run.reset_mock()
                self.run.side_effect = [
                    subprocess.CompletedProcess([], 0, '/fake/Previous/Developer\n', ''),
                    subprocess.CompletedProcess([], 0), result,
                    subprocess.CompletedProcess([], 0),
                ]
                if isinstance(result, Exception):
                    with self.assertRaisesRegex(SetupError, 'timed out'):
                        xcode._unattended_first_launch(self.app / 'Contents/Developer')
                else:
                    self.assertEqual(xcode._unattended_first_launch(self.app / 'Contents/Developer'),
                                     result.returncode == 0)
                self.assertEqual(self.run.call_args.args[0],
                                 ['sudo', '-n', '/usr/bin/xcode-select', '-s', '/fake/Previous/Developer'])
                self.assertEqual(self.run.call_args_list[2],
                                 mock.call(['sudo', '-n', '/usr/bin/xcodebuild', '-runFirstLaunch'],
                                           check=False, timeout=30 * 60))

    def test_unattended_helper_denied_selection_never_runs_first_launch(self):
        self.run.side_effect = [
            subprocess.CompletedProcess([], 0, '/fake/Previous/Developer\n', ''),
            subprocess.CompletedProcess([], 1),
        ]
        self.assertFalse(xcode._unattended_first_launch(self.app / 'Contents/Developer'))
        self.assertEqual(self.run.call_count, 2)

    def test_unattended_helper_reports_failed_selection_restoration(self):
        self.run.side_effect = [
            subprocess.CompletedProcess([], 0, '/fake/Previous/Developer\n', ''),
            subprocess.CompletedProcess([], 0), subprocess.CompletedProcess([], 0),
            subprocess.CompletedProcess([], 1),
        ]
        with self.assertRaisesRegex(SetupError, 'could not restore'):
            xcode._unattended_first_launch(self.app / 'Contents/Developer')


class RuntimeCleanupTests(unittest.TestCase):
    def test_pinned_runtime_survives_repeated_cleanup_and_rolling_upgrade(self):
        catalog = '26.6 (PIN)\n27.2 Release Candidate (RC)\n'
        installed = [xcode.InstalledXcode(r.identifier, r.build, '/fake/' + r.build + '.app')
                     for r in parse_list(catalog)]
        runtimes = {
            'pin': self.runtime('PIN-RUNTIME', version='26.5'),
            'rolling': self.runtime('RC-RUNTIME', version='27.2'),
            'obsolete': self.runtime('OLD', version='27.1'),
        }
        for upgraded in (False, False, True, True):
            if upgraded and 'NEW' not in {i.build for i in installed}:
                catalog += '27.3 (NEW)\n'
                installed.append(xcode.InstalledXcode('27.3', 'NEW', '/fake/NEW.app'))
                runtimes['new'] = self.runtime('NEW-RUNTIME', version='27.3')
            desired, _ = xcode.select_desired(parse_list(catalog), False, pinned_versions=['26.6'])
            def command(cmd, **kwargs):
                if cmd[:2] == ['xcodes', 'uninstall']:
                    return subprocess.CompletedProcess(cmd, 0)
                if cmd[2:5] == ['runtime', 'match', 'list']:
                    dev = kwargs['env']['DEVELOPER_DIR']
                    version = '26.5.1' if 'PIN.app' in dev else '27.3' if 'NEW.app' in dev else '27.2'
                    return self.result({'iOS': self.match('SDK-BUILD', version=version)})
                if cmd[2:4] == ['runtime', 'list']:
                    return self.result(runtimes)
                if cmd[2:4] == ['runtime', 'delete']:
                    del runtimes[cmd[-1]]
                    return subprocess.CompletedProcess(cmd, 0)
                raise AssertionError(cmd)
            with mock.patch.object(xcode, 'run', side_effect=command):
                installed = xcode._remove_unwanted_xcodes(installed, desired)
                xcode._cleanup_runtimes([pathlib.Path(i.path) / 'Contents/Developer' for i in installed])
            self.assertIn('26.6', {i.identifier for i in installed})
            self.assertEqual(set(runtimes), {'pin', 'new' if upgraded else 'rolling'})

    @staticmethod
    def result(value):
        return subprocess.CompletedProcess([], 0, json.dumps(value), "")

    @staticmethod
    def match(build="A", *, version="26.5", platform="iphoneos", **extra):
        return {"chosenRuntimeBuild": build, "platform": f"com.apple.platform.{platform}",
                "sdkVersion": version, **extra}

    @staticmethod
    def runtime(build, *, version="26.5", platform="iphonesimulator", **extra):
        return {"build": build, "version": version,
                "platformIdentifier": f"com.apple.platform.{platform}",
                "deletable": True, **extra}

    def test_invalid_discovery_never_deletes_any_runtime(self):
        for discovery in (
            "invalid", '{"iOS": {}}', "[]",
            json.dumps({"iOS": self.match(platform="unknown")}),
            json.dumps({"iOS": self.match(version="unknown")}),
            json.dumps({"iOS": self.match(defaultBuild=["B"])}),
            '{"iOS":{"chosenRuntimeBuild":"A"}}',
        ):
            with self.subTest(discovery=discovery), mock.patch(
                "cisetup.xcode.run",
                return_value=subprocess.CompletedProcess([], 0, discovery, ""),
            ) as run:
                with self.assertRaises(SetupError):
                    xcode._cleanup_runtimes([pathlib.Path("/fake/Developer")])
                self.assertFalse(any("delete" in c.args[0] for c in run.call_args_list))

    def test_empty_match_for_any_kept_xcode_skips_all_runtime_cleanup(self):
        with mock.patch("cisetup.xcode.run") as run, mock.patch("cisetup.xcode.warn") as warn:
            run.side_effect = [
                self.result({"iOS": self.match()}),
                self.result({}),
            ]
            xcode._cleanup_runtimes([pathlib.Path("/fake/One"), pathlib.Path("/fake/NoPlatforms")])
            self.assertEqual(run.call_count, 2)
            self.assertFalse(any("delete" in c.args[0] for c in run.call_args_list))
            warn.assert_called_once()
            self.assertIn("no SDK runtime matches", warn.call_args.args[0])

    def test_discovery_failure_for_second_xcode_prevents_all_deletion(self):
        with mock.patch("cisetup.xcode.run") as run:
            run.side_effect = [
                self.result({"iOS": self.match()}),
                SetupError("cannot query other Xcode"),
            ]
            with self.assertRaises(SetupError):
                xcode._cleanup_runtimes([pathlib.Path("/fake/One"), pathlib.Path("/fake/Two")])
            self.assertFalse(any("delete" in c.args[0] for c in run.call_args_list))

    def test_invalid_runtime_listing_never_deletes(self):
        with mock.patch("cisetup.xcode.run") as run:
            run.side_effect = [
                self.result({"iOS": self.match()}),
                self.result({"runtime": None}),
            ]
            with self.assertRaisesRegex(SetupError, "cannot safely list"):
                xcode._cleanup_runtimes([pathlib.Path("/fake/One")])
            self.assertFalse(any("delete" in c.args[0] for c in run.call_args_list))

    def test_runtime_deletion_uses_all_kept_xcode_builds(self):
        with mock.patch("cisetup.xcode.run") as run:
            run.side_effect = [
                self.result({"iOS": self.match("A", defaultBuild="A-default")}),
                self.result({"iOS": self.match(None, version="27.0", defaultBuild="B")}),
                self.result({
                    "one": self.runtime("A", version="26.4"),
                    "default": self.runtime("A-default", version="26.3"),
                    "two": self.runtime("B", version="26.2"),
                    "old": self.runtime("C", version="26.1"),
                    "system": self.runtime("D", version="26.1", deletable=False),
                }),
                subprocess.CompletedProcess([], 0, "", ""),
            ]
            xcode._cleanup_runtimes([pathlib.Path("/fake/One"), pathlib.Path("/fake/Two")])
            deletions = [c.args[0] for c in run.call_args_list if "delete" in c.args[0]]
            self.assertEqual(deletions, [["xcrun", "simctl", "runtime", "delete", "old"]])

    def test_downloaded_runtime_is_retained_despite_sdk_patch_and_build_difference(self):
        with mock.patch("cisetup.xcode.run") as run:
            run.side_effect = [
                self.result({"iphoneos26.5": self.match(
                    "23F81a", version="26.5.1", defaultBuild="23F81a", sdkBuild="23F81a"
                )}),
                self.result({"iphoneos27.0": self.match("24A1", version="27.0")}),
                self.result({
                    "downloaded-ios-26.5": self.runtime("23F77"),
                    "sdk-ios-26.5.1": self.runtime("23F81a", version="26.5.1"),
                    "downloaded-ios-27.0": self.runtime("24A2", version="27.0"),
                    "obsolete-ios": self.runtime("23E1", version="26.4"),
                    "unrelated-tvos": self.runtime("23L1", platform="appletvsimulator"),
                }),
                subprocess.CompletedProcess([], 0, "", ""),
                subprocess.CompletedProcess([], 0, "", ""),
                subprocess.CompletedProcess([], 0, "", ""),
                subprocess.CompletedProcess([], 0, "", ""),
            ]
            xcode._cleanup_runtimes([pathlib.Path("/fake/Xcode26.6"), pathlib.Path("/fake/Xcode27.0")])
            deletions = [c.args[0][-1] for c in run.call_args_list if "delete" in c.args[0]]
            self.assertEqual(deletions, ["obsolete-ios", "unrelated-tvos"])

    def test_unknown_runtime_metadata_is_preserved(self):
        with mock.patch("cisetup.xcode.run") as run:
            run.side_effect = [
                self.result({"iOS": self.match()}),
                self.result({
                    "missing": {},
                    "platform": self.runtime("B", platform="futureplatform"),
                    "version": self.runtime("C", version=None),
                    "build": self.runtime(["D"]),
                    "deletable": self.runtime("E", version="26.4", deletable=None),
                }),
            ]
            xcode._cleanup_runtimes([pathlib.Path("/fake/Developer")])
            self.assertEqual(run.call_count, 2)


class XcodeRemovalTests(unittest.TestCase):
    def test_removal_failures_are_aggregated_and_newer_or_unknown_are_kept(self):
        desired = [Release((26, 0), (), "26.0", "17A324")]
        installed = [
            xcode.InstalledXcode("16.2", "old1", "/fake/Old1.app"),
            xcode.InstalledXcode("16.3", "old2", "/fake/Old2.app"),
            xcode.InstalledXcode("26.0", "17A324", "/fake/Desired.app"),
            xcode.InstalledXcode("99.0", "newer", "/fake/Newer.app"),
            xcode.InstalledXcode("unknown", "custom", "/fake/Custom.app"),
        ]
        with mock.patch("cisetup.xcode.run", side_effect=SetupError("uninstall failed")) as run:
            with self.assertRaises(SetupError) as ctx:
                xcode._remove_unwanted_xcodes(installed, desired)
            self.assertIn("16.2", str(ctx.exception))
            self.assertIn("16.3", str(ctx.exception))
            self.assertEqual([c.args[0][-1] for c in run.call_args_list], ["16.2", "16.3"])

    def test_runner_toolchain_survives_until_environment_switch_completed(self):
        current = pathlib.Path("/fake/Previous.app/Contents/Developer")
        replacement = pathlib.Path("/fake/Ready.app/Contents/Developer")
        old = xcode.InstalledXcode("16.2", "16C50", str(current.parent.parent))
        ready = xcode.InstalledXcode("26.0", "17A324", str(replacement.parent.parent))
        desired = [Release((26, 0), (), "26.0", "17A324")]
        with mock.patch("cisetup.xcode.run") as run:
            retained = xcode._remove_unwanted_xcodes(
                [old, ready], desired, protected_developer_dirs={current}
            )
            self.assertEqual(retained, [old, ready])
            run.assert_not_called()
            # If writing .env fails or setup is interrupted, repeated cleanup
            # still retains old. Only the next pass after a switch removes it.
            xcode._remove_unwanted_xcodes([old, ready], desired, protected_developer_dirs={current})
            run.assert_not_called()
            retained = xcode._remove_unwanted_xcodes(
                [old, ready], desired, protected_developer_dirs={replacement}
            )
            self.assertEqual(retained, [ready])
            run.assert_called_once_with(["xcodes", "uninstall", "16.2"], timeout=30 * 60)


class RunnerDeveloperDirectoryTests(unittest.TestCase):
    def test_reads_only_last_override_without_evaluating_other_environment_keys(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            cfg = Config(repo_root=root, runner_dir=root)
            self.assertIsNone(xcode.runner_developer_dir(cfg))
            (root / ".env").write_text(
                f"SECRET=do-not-read-or-execute-this\nDEVELOPER_DIR={root}/Old\n"
                f"DEVELOPER_DIR={root}/Current\nOTHER=$(false)\n"
            )
            self.assertEqual(xcode.runner_developer_dir(cfg), (root / "Current").resolve())

if __name__ == "__main__":
    unittest.main()
