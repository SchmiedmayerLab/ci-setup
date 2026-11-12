# This source file is part of the macOS Self-Hosted GitHub Actions Runner open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
# SPDX-License-Identifier: MIT

"""Real-job probes must validate the requested toolchain and its runtime family."""

import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from cisetup import validation, xcode
from cisetup.util import SetupError


class ToolchainSelectionTests(unittest.TestCase):
    def setUp(self):
        self.installed = xcode.parse_installed(
            '26.6 (PIN) /fake/Pinned.app\n27.2 Release Candidate (RC) /fake/Current.app\n')
        self.default = '/fake/Current.app/Contents/Developer'

    def test_exact_pin_and_default_resolve_without_global_mutation(self):
        self.assertEqual(validation.installed_toolchain(self.installed, '26.6.0', self.default).build, 'PIN')
        self.assertEqual(validation.installed_toolchain(self.installed, 'default', self.default).build, 'RC')

    def test_unrelated_custom_toolchain_does_not_block_an_exact_pin(self):
        installed = self.installed + [xcode.InstalledXcode('Custom', 'CUSTOM', '/fake/Custom.app')]
        self.assertEqual(validation.installed_toolchain(installed, '26.6', self.default).build, 'PIN')

    def test_missing_ambiguous_or_invalid_selection_fails(self):
        for selector in ('26.5', 'latest', '26.*'):
            with self.subTest(selector=selector), self.assertRaises(SetupError):
                validation.installed_toolchain(self.installed, selector, self.default)
        with self.assertRaisesRegex(SetupError, 'exactly one'):
            validation.installed_toolchain(self.installed + self.installed, '26.6', self.default)
        with self.assertRaisesRegex(SetupError, 'exactly one'):
            validation.installed_toolchain(self.installed, 'default', '/missing')

    def test_control_characters_in_path_are_rejected(self):
        installed = [xcode.InstalledXcode('26.6', 'PIN', '/fake/Bad\nVARIABLE=value.app')]
        with self.assertRaisesRegex(SetupError, 'path'):
            validation.installed_toolchain(installed, '26.6', self.default)

    def test_version_and_build_are_verified_before_exporting_job_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env_file = root / 'env'
            summary = root / 'summary'
            for actual, valid in [('Xcode 26.6\nBuild version PIN', True),
                                  ('Xcode 26.6\nBuild version OTHER', False),
                                  ('Xcode 27.2\nBuild version PIN', False)]:
                with self.subTest(actual=actual), patch.dict(os.environ, {
                    'DEVELOPER_DIR': self.default, 'GITHUB_ENV': str(env_file),
                    'GITHUB_STEP_SUMMARY': str(summary),
                }), patch.object(validation, 'output', side_effect=[
                    self.default, '26.6 (PIN) /fake/Pinned.app\n', actual,
                ]) as output, patch.object(Path, 'is_dir', return_value=True):
                    env_file.write_text('')
                    if valid:
                        validation.select_xcode('26.6')
                        self.assertEqual(env_file.read_text(), 'DEVELOPER_DIR=/fake/Pinned.app/Contents/Developer\n')
                        self.assertIn('26.6 (PIN)', summary.read_text())
                    else:
                        with self.assertRaisesRegex(SetupError, 'differs'):
                            validation.select_xcode('26.6')
                        self.assertEqual(env_file.read_text(), '')
                    self.assertEqual(output.call_args.kwargs['env']['DEVELOPER_DIR'],
                                     '/fake/Pinned.app/Contents/Developer')
                    self.assertFalse(any('-s' in c.args[0] for c in output.call_args_list))

    def test_runner_and_global_default_must_agree(self):
        with patch.dict(os.environ, {'DEVELOPER_DIR': self.default}), \
                patch.object(validation, 'output', return_value='/wrong/Developer') as output, \
                self.assertRaisesRegex(SetupError, 'must agree'):
            validation.select_xcode('26.6')
        self.assertEqual(output.call_count, 1)

    def test_runner_override_cannot_mask_a_different_global_selection(self):
        def selected(command, **kwargs):
            if command == ['/usr/bin/env', '-u', 'DEVELOPER_DIR', '/usr/bin/xcode-select', '-p']:
                return '/fake/Other.app/Contents/Developer'
            if command == ['/usr/bin/xcode-select', '-p']:
                return os.environ['DEVELOPER_DIR']
            self.fail('Selection mismatch must stop before discovery or environment export')

        with patch.dict(os.environ, {'DEVELOPER_DIR': self.default}), \
                patch.object(validation, 'output', side_effect=selected), \
                self.assertRaisesRegex(SetupError, 'must agree'):
            validation.select_xcode('26.6')


class SimulatorValidationTests(unittest.TestCase):
    def setUp(self):
        self.inventory = {'runtimes': [], 'devicetypes': []}
        self.matches = {}
        for platform, family, platform_id in validation.PLATFORMS:
            device = {'name': family + ' Test', 'identifier': platform + '-device'}
            self.inventory['devicetypes'].append(device)
            self.matches[platform] = {
                'platform': 'com.apple.platform.' + platform_id,
                'sdkVersion': '26.5.1', 'chosenRuntimeBuild': platform + 'SDK',
            }
            for version, build in [('26.5', '23F77'), ('27.2', '24B114')]:
                # simctl list -j can omit platformIdentifier; the SDK and
                # runtime build/patch versions also need not be identical.
                self.inventory['runtimes'].append({
                    'name': platform + ' ' + version, 'version': version,
                    'buildversion': build, 'identifier': platform + '-' + version,
                    'isAvailable': True, 'supportedDeviceTypes': [device],
                })

    def test_pinned_sdk_selects_older_runtime_family_instead_of_newest_visible(self):
        plan = validation.simulator_plan(self.inventory, self.matches)
        self.assertEqual(len(plan), 4)
        self.assertTrue(all(runtime['version'] == '26.5' for _, runtime, _ in plan))

    def test_latest_sdk_selects_its_own_runtime_family(self):
        for match in self.matches.values():
            match['sdkVersion'] = '27.2'
        plan = validation.simulator_plan(self.inventory, self.matches)
        self.assertTrue(all(runtime['version'] == '27.2' for _, runtime, _ in plan))

    def test_missing_or_unavailable_pinned_runtime_does_not_fall_back(self):
        for available in (False, None):
            inventory = copy.deepcopy(self.inventory)
            inventory['runtimes'][0]['isAvailable'] = available
            with self.subTest(available=available), self.assertRaisesRegex(SetupError, 'compatible'):
                validation.simulator_plan(inventory, self.matches)

    def test_newest_released_compatible_runtime_is_preferred(self):
        for version, build in [('26.5.2', '23F100'), ('26.5.3', '23F101a')]:
            runtime = dict(self.inventory['runtimes'][0], version=version, buildversion=build)
            self.inventory['runtimes'].append(runtime)
        self.assertEqual(validation.simulator_plan(self.inventory, self.matches)[0][1]['buildversion'], '23F100')

    def test_explicit_sdk_runtime_match_can_cross_minor_train(self):
        self.matches['iOS']['sdkVersion'] = '26.4'
        self.matches['iOS']['chosenRuntimeBuild'] = '23F77'
        self.assertEqual(validation.simulator_plan(self.inventory, self.matches)[0][1]['version'], '26.5')

    def test_missing_sdk_matches_or_device_types_fail(self):
        for matches in ({}, [], {'bad': None}):
            with self.subTest(matches=matches), self.assertRaises(SetupError):
                validation.simulator_plan(self.inventory, matches)
        self.inventory['devicetypes'] = []
        with self.assertRaisesRegex(SetupError, 'device type'):
            validation.simulator_plan(self.inventory, self.matches)

    def test_missing_platform_fails_before_any_device_is_created(self):
        self.inventory['runtimes'].pop()
        self.inventory['runtimes'].pop()
        with patch.object(validation, 'simctl', side_effect=[
            json.dumps(self.inventory), json.dumps(self.matches),
        ]) as simctl, self.assertRaises(SetupError):
            validation.boot_simulators()
        self.assertEqual(simctl.call_count, 2)

    def test_failed_boot_cleans_up_only_the_created_device(self):
        with patch.object(validation, 'simctl', side_effect=[
            json.dumps(self.inventory), json.dumps(self.matches), 'created-uuid', SetupError('boot failed'), '',
        ]) as simctl, patch.object(validation, 'run') as run, self.assertRaisesRegex(SetupError, 'boot failed'):
            validation.boot_simulators()
        self.assertEqual(simctl.call_args.args, ('delete', 'created-uuid'))
        self.assertEqual(run.call_args.args[0], ['xcrun', 'simctl', 'shutdown', 'created-uuid'])
