# This source file is part of the macOS Self-Hosted GitHub Actions Runner open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
# SPDX-License-Identifier: MIT

"""Retention/default policy resolution without touching installed tools."""

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from cisetup import config, xcode
from cisetup.util import SetupError


CATALOG = "26.6 (17F113)\n27.1 (A)\n27.2 Release Candidate (B)\n27.3 Beta (C)\n"


class PolicyTests(unittest.TestCase):
    def select(self, **kwargs):
        desired, default = xcode.select_desired(xcode.parse_list(CATALOG), False, **kwargs)
        return [release.identifier for release in desired], default.identifier

    def test_pins_are_retained_alongside_latest_without_changing_default(self):
        self.assertEqual(self.select(pinned_versions=['26.6']),
                         (['27.2 Release Candidate', '26.6'], '27.2 Release Candidate'))

    def test_new_latest_does_not_displace_pin(self):
        desired, default = xcode.select_desired(xcode.parse_list(CATALOG + '27.3 (D)\n'), False,
                                               pinned_versions=['26.6'])
        self.assertEqual([release.identifier for release in desired], ['27.3', '26.6'])
        self.assertEqual(default.identifier, '27.3')

    def test_fixed_default_does_not_disable_latest_tracking(self):
        self.assertEqual(self.select(pinned_versions=['26.6'], default_version='26.6'),
                         (['27.2 Release Candidate', '26.6'], '26.6'))

    def test_pin_only_ignores_new_releases(self):
        self.assertEqual(self.select(track_latest=False, pinned_versions=['26.6', '27.1']),
                         (['26.6', '27.1'], '27.1'))

    def test_equivalent_duplicate_identifiers_install_once(self):
        self.assertEqual(self.select(pinned_versions=['26.6', '26.6.0', '26.6']),
                         (['27.2 Release Candidate', '26.6'], '27.2 Release Candidate'))

    def test_requested_missing_ambiguous_or_unretained_versions_fail(self):
        for options in ({'pinned_versions': ['26.7']}, {'default_version': '26.6'},
                        {'track_latest': False}, {'pinned_versions': ['26.*']}):
            with self.subTest(options=options), self.assertRaises(SetupError):
                self.select(**options)
        with self.assertRaisesRegex(SetupError, 'ambiguous'):
            xcode.select_desired(xcode.parse_list(CATALOG + '26.6 (OTHER)\n'), False,
                                 pinned_versions=['26.6'])

    def test_beta_opt_in_does_not_become_implicit_default(self):
        desired, default = xcode.select_desired(xcode.parse_list(CATALOG), True,
                                               pinned_versions=['26.6'])
        self.assertEqual([release.identifier for release in desired],
                         ['27.2 Release Candidate', '26.6', '27.3 Beta'])
        self.assertEqual(default.identifier, '27.2 Release Candidate')

    def test_pinned_beta_needs_an_explicit_beta_default_if_no_stable_is_retained(self):
        with self.assertRaisesRegex(SetupError, 'requires a retained release'):
            self.select(track_latest=False, pinned_versions=['27.3 Beta'])
        self.assertEqual(self.select(track_latest=False, pinned_versions=['27.3 Beta'],
                                     default_version='27.3 Beta'),
                         (['27.3 Beta'], '27.3 Beta'))

    def test_cleanup_keeps_pin_and_latest_but_reclaims_unwanted_older_release(self):
        desired, _ = xcode.select_desired(xcode.parse_list(CATALOG), False, pinned_versions=['26.6'])
        installed = [xcode.InstalledXcode(release.identifier, release.build,
                                         '/fake/' + release.build + '.app')
                     for release in xcode.parse_list(CATALOG) if not release.pre or release.pre[0] == 'rc']
        with patch.object(xcode, 'run') as run:
            retained = xcode._remove_unwanted_xcodes(installed, desired)
        self.assertEqual({release.identifier for release in retained}, {'26.6', '27.2 Release Candidate'})
        run.assert_called_once_with(['xcodes', 'uninstall', '27.1'], timeout=30 * 60)


class PolicyConfigTests(unittest.TestCase):
    def load(self, policy):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'config.toml').write_text('[github]\nscope="org"\nowner="example"\n[xcode]\n' + policy)
            with patch.object(config, 'saved_runner_name', return_value=None):
                return config.load(root)

    def test_defaults_preserve_existing_policy(self):
        cfg = self.load('')
        self.assertTrue(cfg.xcode_track_latest)
        self.assertEqual(cfg.xcode_pinned_versions, [])
        self.assertEqual(cfg.xcode_default_version, 'latest')

    def test_pin_and_default_roundtrip(self):
        cfg = self.load('track_latest=false\npinned_versions=["26.6", "27.1"]\ndefault_version="26.6"')
        self.assertEqual(xcode.selected_releases(xcode.parse_list(CATALOG), cfg)[1].identifier, '26.6')

    def test_malformed_configuration_is_rejected(self):
        for policy in ('track_latest="false"', 'manage="false"', 'install_beta="false"',
                       'pinned_versions="26.6"', 'pinned_versions=[26.6]',
                       'pinned_versions=[""]', 'pinned_versions=["26.*"]', 'pinned_versions=[" 26.6"]',
                       'default_version=""', 'default_version="26.*"', 'default_version=26.6',
                       'track_latest=false', 'track_latest=false\npinned_versions=["26.6"]\ninstall_beta=true'):
            with self.subTest(policy=policy), self.assertRaises(SetupError):
                self.load(policy)

    def test_alternative_and_mixed_policy_keys_are_rejected(self):
        for policy in ('versions=["26.6"]', 'track="none"\npinned_versions=["26.6"]',
                       'versions=["26.6"]\ntrack_latest=true',
                       'track="stable"\ntrack_latest=false\npinned_versions=["26.6"]',
                       'pinned_verisons=["26.6"]', 'manage=false\nversions=["26.6"]'):
            with self.subTest(policy=policy), self.assertRaisesRegex(SetupError, 'unsupported xcode keys'):
                self.load(policy)
