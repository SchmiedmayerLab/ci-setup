# This source file is part of the macOS Self-Hosted GitHub Actions Runner open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
# SPDX-License-Identifier: MIT

"""Host isolation, VM ownership, and interrupted installation safety."""

import copy
import io
import json
import plistlib
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import uuid
from contextlib import redirect_stdout, redirect_stderr

from cisetup import host, main


class HostTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / 'state.json'
        self.spec = {'image': {'path': str(self.root / 'restore.ipsw'), 'sha256': 'a' * 64},
                     'guest': {'username': 'runner', 'automatic_login': False, 'remote_login': False},
                     'vm': [{'name': 'runner-01', 'cpus': 4, 'memory_gib': 8, 'disk_gib': 128}]}
        self.identifier = str(uuid.uuid4()).upper()
        self.calls = []
        self.patch('preflight')
        self.live = self.patch('inventory', return_value={})
        self.image = self.patch('image_ready', return_value=27)
        self.patch('shutil.disk_usage', return_value=type('Disk', (), {'free': 10**13})())
        self.patch('platform.mac_ver', return_value=('27.0', '', ''))
        self.script = self.patch('applescript', side_effect=self.apple)
        self.command = self.patch('command', return_value='')
        self.patch('time.sleep')
        self.output = io.StringIO()
        # Keep test output free of VM status noise.
        self.stdout = redirect_stdout(self.output)
        self.stdout.__enter__()
        self.addCleanup(self.stdout.__exit__, None, None, None)

    def patch(self, name, **kwargs):
        p = patch('cisetup.host.' + name, **kwargs)
        value = p.start()
        self.addCleanup(p.stop)
        return value

    def apple(self, action, *args):
        self.calls.append((action, *args))
        return self.identifier if action == 'create' else 'installed'

    def seed(self, phase='ready'):
        data = {'schema': 1, 'spec': self.spec, 'vms': {'runner-01': {'id': self.identifier, 'phase': phase}}}
        host.save_state(self.path, data)
        self.live.return_value = {self.identifier: {'name': 'runner-01', 'status': 'stopped'}}
        return data

    def test_dispatch_never_loads_runner_configuration_or_lock(self):
        with patch('cisetup.host.main', return_value=0) as dispatch, \
             patch('cisetup.config.load') as config, patch('cisetup.util.acquire_lock') as lock:
            self.assertEqual(main.main(['host', 'plan']), 0)
            dispatch.assert_called_once_with(['plan'])
            config.assert_not_called()
            lock.assert_not_called()

    def test_create_installs_only_new_vm_and_rerun_does_not_install(self):
        host.create(self.spec, self.path)
        self.assertEqual([c[0] for c in self.calls], ['create', 'install', 'progress'])
        self.assertEqual(json.loads(self.path.read_text())['vms']['runner-01']['phase'], 'ready')
        self.live.return_value = {self.identifier: {'name': 'runner-01', 'status': 'started'}}
        self.calls.clear()
        self.command.reset_mock()
        host.create(self.spec, self.path)
        self.assertEqual(self.calls, [])
        self.command.assert_not_called()

    def test_foreign_same_name_is_never_adopted(self):
        self.live.return_value = {self.identifier: {'name': 'runner-01', 'status': 'stopped'}}
        with self.assertRaisesRegex(host.HostError, 'outside this setup'):
            host.create(self.spec, self.path)
        self.script.assert_not_called()
        self.image.assert_not_called()

    def test_unrelated_active_vm_blocks_new_allocation_without_stopping_it(self):
        self.live.return_value = {str(uuid.uuid4()).upper(): {'name': 'personal-vm', 'status': 'started'}}
        with self.assertRaisesRegex(host.HostError, 'unrelated VM is active'):
            host.create(self.spec, self.path)
        self.script.assert_not_called()
        self.command.assert_not_called()

    def test_unrelated_active_vm_blocks_start_without_stopping_it(self):
        self.seed()
        self.live.return_value[str(uuid.uuid4()).upper()] = {'name': 'personal-vm', 'status': 'started'}
        with self.assertRaisesRegex(host.HostError, 'unrelated VM is active'):
            host.start(self.spec, self.path)
        self.command.assert_not_called()

    def test_configuration_drift_is_rejected(self):
        self.seed()
        changed = copy.deepcopy(self.spec)
        changed['vm'][0]['memory_gib'] = 16
        with self.assertRaisesRegex(host.HostError, 'differs'):
            host.create(changed, self.path)
        self.script.assert_not_called()

    def test_missing_or_renamed_vm_is_not_replaced(self):
        self.seed()
        self.live.return_value = {}
        with self.assertRaisesRegex(host.HostError, 'missing/renamed'):
            host.create(self.spec, self.path)
        self.script.assert_not_called()

    def test_interrupted_create_fails_closed(self):
        self.script.side_effect = host.HostError('timeout')
        with self.assertRaises(host.HostError):
            host.create(self.spec, self.path)
        self.assertEqual(json.loads(self.path.read_text())['vms']['runner-01']['phase'], 'creating')
        self.script.reset_mock()
        with self.assertRaisesRegex(host.HostError, 'incomplete creation'):
            host.create(self.spec, self.path)
        self.script.assert_not_called()

    def test_interrupted_install_queries_progress_without_reinstalling(self):
        self.seed('installing')
        host.create(self.spec, self.path)
        self.assertEqual([c[0] for c in self.calls], ['progress'])

    def test_lost_install_progress_does_not_restart_installation(self):
        self.seed('installing')
        self.script.side_effect = host.HostError('No installation started')
        with self.assertRaises(host.HostError):
            host.create(self.spec, self.path)
        self.script.assert_called_once_with('progress', self.identifier)
        self.command.assert_not_called()
        self.assertEqual(json.loads(self.path.read_text())['vms']['runner-01']['phase'], 'installing')

    def test_resumed_installation_still_requests_first_boot_password(self):
        self.seed('installing')
        with patch.object(host.sys.stdin, 'isatty', return_value=True), \
             patch.object(host.getpass, 'getpass', return_value='long-test-password') as prompt:
            host.create(self.spec, self.path, provision=True)
        self.assertEqual(prompt.call_count, 2)
        self.assertEqual(self.command.call_args.kwargs['input'], 'long-test-password\n')
        self.assertEqual([c[0] for c in self.calls], ['progress'])

    def test_failed_start_remains_ambiguous_and_is_not_retried(self):
        self.seed('installed')
        self.command.side_effect = host.HostError('timed out')
        with self.assertRaises(host.HostError):
            host.create(self.spec, self.path)
        self.assertEqual(json.loads(self.path.read_text())['vms']['runner-01']['phase'], 'starting')
        self.command.reset_mock()
        with self.assertRaisesRegex(host.HostError, 'interrupted first boot'):
            host.create(self.spec, self.path)
        self.command.assert_not_called()

    def test_keychain_passwords_are_distinct_per_guest_and_not_saved_in_state(self):
        self.spec['vm'].append({**self.spec['vm'][0], 'name': 'runner-02'})
        identifiers = [self.identifier, str(uuid.uuid4()).upper()]
        def apple(action, *args):
            return identifiers.pop(0) if action == 'create' else 'installed'
        self.script.side_effect = apple
        passwords = ['unique-secret-one', 'unique-secret-two']
        with patch.object(host, 'keychain_password', side_effect=passwords) as keychain:
            host.create(self.spec, self.path, provision=True, keychain_passwords=True)
        self.assertEqual(keychain.call_count, 2)
        self.assertNotEqual(keychain.call_args_list[0].args[0], keychain.call_args_list[1].args[0])
        self.assertEqual([c.kwargs['input'] for c in self.command.call_args_list], [p + '\n' for p in passwords])
        saved = self.path.read_text()
        self.assertNotIn(passwords[0], saved)
        self.assertNotIn(passwords[1], saved)
        self.assertIn('password_service', saved)

    def test_keychain_failure_does_not_mark_first_boot_as_started(self):
        self.seed('installed')
        with patch.object(host, 'keychain_password', side_effect=host.HostError('locked')):
            with self.assertRaisesRegex(host.HostError, 'locked'):
                host.create(self.spec, self.path, provision=True, keychain_passwords=True)
        self.command.assert_not_called()
        self.assertEqual(json.loads(self.path.read_text())['vms']['runner-01']['phase'], 'installed')

    def test_password_stays_off_command_line_and_out_of_state(self):
        secret = 'test-password-not-real'
        self.spec['guest']['remote_login'] = True
        host.create(self.spec, self.path, provision=True, password=secret)
        args = self.command.call_args.args[0]
        self.assertNotIn(secret, ' '.join(map(str, args)))
        self.assertIn('--provision-password-stdin', args)
        self.assertIn('--provision-ssh', args)
        self.assertNotIn('--provision-auto-login', args)
        self.assertEqual(self.command.call_args.kwargs['input'], secret + '\n')
        self.assertNotIn(secret, self.path.read_text())

    def test_invalid_password_is_rejected_before_creation(self):
        for password in ['short', 'long-password\nextra-line']:
            with self.subTest(password=password), self.assertRaises(host.HostError):
                host.create(self.spec, self.path, provision=True, password=password)
        self.script.assert_not_called()

    def test_old_guest_rejects_automatic_account_before_creation(self):
        self.image.return_value = 26
        with self.assertRaisesRegex(host.HostError, '27'):
            host.create(self.spec, self.path, provision=True, password='long-test-password')
        self.script.assert_not_called()

    def test_start_only_owned_ready_vms(self):
        self.seed()
        unrelated = str(uuid.uuid4()).upper()
        self.live.return_value[unrelated] = {'name': 'personal-vm', 'status': 'stopped'}
        host.start(self.spec, self.path)
        self.command.assert_called_once_with([host.CTL, 'start', self.identifier], timeout=300)

    def test_start_leaves_running_vm_alone(self):
        self.seed()
        self.live.return_value[self.identifier]['status'] = 'started'
        host.start(self.spec, self.path)
        self.command.assert_not_called()

    def test_start_refuses_incomplete_install(self):
        self.seed('installing')
        with self.assertRaisesRegex(host.HostError, 'incomplete'):
            host.start(self.spec, self.path)
        self.command.assert_not_called()

    def test_login_agent_does_not_replace_an_existing_definition(self):
        self.seed()
        target = self.root / 'Library/LaunchAgents' / (host.LABEL + '.plist')
        target.parent.mkdir(parents=True)
        target.write_bytes(b'existing user configuration')
        with patch.object(host.Path, 'home', return_value=self.root):
            with self.assertRaisesRegex(host.HostError, 'different login agent'):
                host.login_agent(self.root / 'host.toml', self.spec, self.path)
        self.assertEqual(target.read_bytes(), b'existing user configuration')

    def test_login_agent_is_idempotent_and_has_no_restart_loop(self):
        self.seed()
        with patch.object(host.Path, 'home', return_value=self.root):
            host.login_agent(self.root / 'host.toml', self.spec, self.path)
            host.login_agent(self.root / 'host.toml', self.spec, self.path)
        target = self.root / 'Library/LaunchAgents' / (host.LABEL + '.plist')
        definition = plistlib.loads(target.read_bytes())
        self.assertTrue(definition['RunAtLoad'])
        self.assertNotIn('KeepAlive', definition)
        self.assertEqual(definition['ProgramArguments'][:4],
                         [host.sys.executable, str(host.ROOT / 'bin/ci-setup'), 'host', 'start'])
        result = subprocess.run(definition['ProgramArguments'][:3] + ['--help'],
                                env={'PATH': '/usr/bin:/bin'}, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('login-agent', result.stdout)
        self.command.assert_not_called()

    def test_invalid_saved_phase_stops_before_utm(self):
        self.seed('unexpected')
        with self.assertRaisesRegex(host.HostError, 'Invalid saved'):
            host.create(self.spec, self.path)
        self.script.assert_not_called()

    def test_state_is_private(self):
        self.seed()
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)


class HostBoundaryTests(unittest.TestCase):
    def test_runner_names_fill_the_first_available_numbers(self):
        for existing, expected in [([1, 2], [3, 4]), ([3, 4], [1, 2]), ([1, 3], [2, 4])]:
            with self.subTest(existing=existing):
                self.assertEqual(host.available_names([f'GitHub Runner {n}' for n in existing]),
                                 [f'GitHub Runner {n}' for n in expected])
        self.assertEqual(host.hostname('GitHub Runner 12'), 'github-runner-12')

    def test_name_discovery_reads_every_page_and_includes_local_vms(self):
        from cisetup import config, github_api
        cfg = config.Config(repo_root=host.ROOT, owner='example', scope='org', pat='test-token')
        pages = [{'runners': [{'name': f'GitHub Runner {n}'} for n in range(1, 101)]},
                 {'runners': [{'name': 'GitHub Runner 101'}]}]
        with patch.object(host, 'runner_credentials', return_value=cfg) as credentials, \
             patch.object(github_api, 'get', side_effect=pages) as get, \
             patch.object(host, 'inventory', return_value={'id': {'name': 'GitHub Runner 102'}}):
            self.assertEqual(host.discover_names(), ['GitHub Runner 103', 'GitHub Runner 104'])
            credentials.assert_called_once_with(interactive=False)
            self.assertIn('page=2', get.call_args.args[0])

    def test_name_discovery_does_not_guess_without_credentials(self):
        from cisetup import config, github_api
        missing = subprocess.CompletedProcess([], 44, '', 'not found')
        with patch.object(config, 'load'), patch.object(host.subprocess, 'run', return_value=missing), \
             patch.object(github_api, 'get') as get:
            with self.assertRaisesRegex(host.HostError, 'store-pat'):
                host.discover_names()
            get.assert_not_called()

    def test_token_prompt_precedes_registration_permission_check(self):
        from cisetup import config, github_api
        cfg = config.Config(repo_root=host.ROOT, owner='example', scope='org')
        missing = subprocess.CompletedProcess([], 44, '', 'not found')
        entered = subprocess.CompletedProcess([], 0)
        with patch.object(config, 'load', return_value=cfg), \
             patch.object(host.subprocess, 'run', side_effect=[missing, entered]) as run, \
             patch.object(host, 'command', return_value='private-test-token') as lookup, \
             patch.object(github_api, 'registration_token', return_value='short-lived') as registration:
            result = host.runner_credentials(interactive=True)
        self.assertEqual(result.pat, 'private-test-token')
        self.assertEqual(run.call_args_list[1].args[0], [str(host.ROOT / 'setup'), 'store-pat'])
        self.assertNotIn('private-test-token', str(run.call_args_list) + str(lookup.call_args_list))
        registration.assert_called_once_with(cfg)

    def test_locked_token_does_not_prompt_or_overwrite(self):
        from cisetup import config, github_api
        locked = subprocess.CompletedProcess([], 36, '', 'interaction required')
        with patch.object(config, 'load'), patch.object(host.subprocess, 'run', return_value=locked) as run, \
             patch.object(github_api, 'registration_token') as registration:
            with self.assertRaisesRegex(host.HostError, 'unlock'):
                host.runner_credentials(interactive=True)
        self.assertEqual(run.call_count, 1)
        registration.assert_not_called()

    def test_read_only_token_stops_before_name_allocation(self):
        from cisetup import config, github_api
        from cisetup.util import SetupError
        cfg = config.Config(repo_root=host.ROOT, owner='example', scope='org')
        stored = subprocess.CompletedProcess([], 0, 'private-test-token\n', '')
        with patch.object(config, 'load', return_value=cfg), \
             patch.object(host.subprocess, 'run', return_value=stored) as run, \
             patch.object(github_api, 'registration_token', side_effect=SetupError('denied')), \
             patch.object(host, 'inventory') as inventory:
            with self.assertRaisesRegex(host.HostError, 'Self-hosted runners: write'):
                host.discover_names(interactive=True)
        self.assertEqual(run.call_count, 1)
        inventory.assert_not_called()

    def test_invalid_runner_response_does_not_allocate_names(self):
        from cisetup import config, github_api
        cfg = config.Config(repo_root=host.ROOT, owner='example', scope='org')
        with patch.object(host, 'runner_credentials', return_value=cfg), \
             patch.object(github_api, 'get', return_value=[]), patch.object(host, 'inventory') as inventory:
            with self.assertRaisesRegex(host.HostError, 'invalid runner list'):
                host.discover_names()
        inventory.assert_not_called()

    def test_rejected_token_reports_authentication_not_additional_permissions(self):
        from cisetup import config, github_api
        cfg = config.Config(repo_root=host.ROOT, owner='example', scope='org')
        stored = subprocess.CompletedProcess([], 0, 'private-test-token\n', '')
        with patch.object(config, 'load', return_value=cfg), \
             patch.object(host.subprocess, 'run', return_value=stored), \
             patch.object(github_api, 'registration_token', side_effect=github_api.GitHubAPIError('denied', 401)):
            with self.assertRaisesRegex(host.HostError, 'complete active token'):
                host.runner_credentials()

    def test_sizes_reserve_host_ram_and_scale_disk_capacity(self):
        one_tb = host.resource_sizes(64, 18, 931, 870)
        self.assertEqual(one_tb, {'cpus': 8, 'memory_gib': 28, 'disk_gib': 384})
        two_tb = host.resource_sizes(64, 18, 1862, 1700, reserve_memory=16, cpu_ratio=1.5)
        self.assertEqual(two_tb, {'cpus': 12, 'memory_gib': 24, 'disk_gib': 512})
        with self.assertRaises(host.HostError):
            host.resource_sizes(64, 18, 1862, 500)
        with self.assertRaises(host.HostError):
            host.resource_sizes(8, 8, 1862, 1700)

    def test_existing_keychain_password_is_reused_without_writing(self):
        result = subprocess.CompletedProcess([], 0, 'existing-private-password\n', '')
        with patch.object(host.subprocess, 'run', return_value=result), patch.object(host, 'command') as command:
            self.assertEqual(host.keychain_password(str(uuid.uuid4()), 'runner'), 'existing-private-password')
            command.assert_not_called()

    def test_locked_keychain_does_not_create_replacement_password(self):
        result = subprocess.CompletedProcess([], 36, '', 'interaction required')
        with patch.object(host.subprocess, 'run', return_value=result), patch.object(host, 'command') as command:
            with self.assertRaisesRegex(host.HostError, 'unlock'):
                host.keychain_password(str(uuid.uuid4()), 'runner')
            command.assert_not_called()

    def test_keychain_creation_keeps_secret_off_argv(self):
        missing = subprocess.CompletedProcess([], 44, '', 'not found')
        secret = 'a-unique-private-secret'
        with patch.object(host.subprocess, 'run', return_value=missing), \
             patch.object(host.secrets, 'token_urlsafe', return_value=secret), \
             patch.object(host, 'command', side_effect=['', secret]) as command:
            self.assertEqual(host.keychain_password(str(uuid.uuid4()), 'runner'), secret)
        self.assertIn(secret, command.call_args_list[0].kwargs['input'])
        for call in command.call_args_list:
            self.assertNotIn(secret, str(call.args))
        self.assertNotIn('"-A"', command.call_args_list[0].kwargs['input'])
        self.assertNotIn('"-U"', command.call_args_list[0].kwargs['input'])

    def test_example_validates_after_image_pin_is_supplied(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'host.toml'
            example = (host.ROOT / 'host.example.toml').read_text()
            path.write_text(example.replace('REPLACE_WITH_SHA256', 'a' * 64))
            spec = host.load(path)
            self.assertEqual(len(spec['vm']), 2)
            self.assertFalse(spec['guest']['remote_login'])

    def test_configuration_rejects_malformed_and_unsafe_values(self):
        example = (host.ROOT / 'host.example.toml').read_text().replace('REPLACE_WITH_SHA256', 'a' * 64)
        cases = [example.replace('cpus = 4', 'cpus = true'),
                 example.replace('GitHub Runner 2', 'GitHub Runner 1'),
                 example.replace('GitHub Runner 2', 'GITHUB RUNNER 1'),
                 example.replace('GitHub Runner 1', 'bad/name'),
                 example.replace('remote_login = false', 'remote_login = "false"'),
                 example.replace('a' * 64, 'bad-hash'),
                 example.replace('[image]', '[image]\nurl = "https://example.com/image.ipsw"'),
                 example.replace('memory_gib = 12', 'memory_gib = 2')]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'host.toml'
            for content in cases:
                with self.subTest(content=content):
                    self.assertNotEqual(content, example, "invalid fixture must change the example")
                    path.write_text(content)
                    with self.assertRaises(host.HostError):
                        host.load(path)

    def test_inventory_fails_closed_on_empty_or_invalid_output(self):
        for output in ['', 'Not authorized', 'UUID Status Name\ninvalid stopped example']:
            with self.subTest(output=output), patch.object(host, 'command', return_value=output):
                with self.assertRaises((host.HostError, ValueError)):
                    host.inventory()

    def test_preflight_reserves_host_resources(self):
        spec = {'vm': [{'cpus': 4, 'memory_gib': 12}]}
        with tempfile.TemporaryDirectory() as directory:
            app = Path(directory)
            (app / 'Contents').mkdir()
            (app / 'Contents/Info.plist').write_bytes(plistlib.dumps({'CFBundleShortVersionString': '5.0.8'}))
            ctl = app / 'utmctl'
            ctl.touch()
            with patch.object(host, 'UTM', app), patch.object(host, 'CTL', ctl), \
                 patch.object(host.platform, 'system', return_value='Darwin'), \
                 patch.object(host.platform, 'machine', return_value='arm64'):
                for ram, cores, message in [(16, 8, '8 GiB'), (32, 4, 'two cores')]:
                    with self.subTest(ram=ram, cores=cores), \
                         patch.object(host, 'command', side_effect=[str(ram * 1024**3), str(cores)]):
                        with self.assertRaisesRegex(host.HostError, message):
                            host.preflight(spec)
                with patch.object(host, 'command', side_effect=[str(32 * 1024**3), '8']):
                    host.preflight(spec)
                spec['host'] = {'cpu_overcommit': True}
                with patch.object(host, 'command', side_effect=[str(32 * 1024**3), '4']):
                    host.preflight(spec)
                with patch.object(host, 'command', side_effect=[str(16 * 1024**3), '4']):
                    with self.assertRaisesRegex(host.HostError, '8 GiB'):
                        host.preflight(spec)

    def test_command_hides_errors_when_stdin_contains_a_secret(self):
        result = subprocess.CompletedProcess([], 1, '', 'sensitive-password')
        with patch.object(host.subprocess, 'run', return_value=result):
            with self.assertRaisesRegex(host.HostError, 'guest provisioning failed') as error:
                host.command(['utmctl'], input='sensitive-password')
            self.assertNotIn('sensitive-password', str(error.exception))

    def test_restore_version_uses_small_restore_metadata(self):
        import zipfile
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / 'image.ipsw'
            with zipfile.ZipFile(image, 'w') as archive:
                archive.writestr('Restore.plist', plistlib.dumps({'ProductVersion': '27.0.1'}))
                # A large build manifest must not be required for version detection.
                archive.writestr('BuildManifest.plist', b'unused')
            spec = {'image': {'path': str(image), 'sha256': host.digest(image)}}
            with redirect_stdout(io.StringIO()):
                self.assertEqual(host.image_ready(spec), 27)

    def test_invalid_restore_archive_fails_before_vm_creation(self):
        import zipfile
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / 'invalid.ipsw'
            image.write_bytes(b'not a restore archive')
            with redirect_stdout(io.StringIO()), self.assertRaisesRegex(host.HostError, 'invalid macOS metadata'):
                host.image_ready({'image': {'path': str(image), 'sha256': host.digest(image)}})
            for manifest in ({}, [], {'ProductVersion': 27}):
                with self.subTest(manifest=manifest):
                    with zipfile.ZipFile(image, 'w') as archive:
                        archive.writestr('Restore.plist', plistlib.dumps(manifest))
                    with redirect_stdout(io.StringIO()), self.assertRaisesRegex(host.HostError, 'invalid macOS metadata'):
                        host.image_ready({'image': {'path': str(image), 'sha256': host.digest(image)}})

    def test_utm_event_failure_is_not_success_even_with_zero_exit_status(self):
        result = subprocess.CompletedProcess([], 0, '', 'Error from event: Not authorized')
        with patch.object(host.subprocess, 'run', return_value=result):
            with self.assertRaises(host.HostError):
                host.command([host.CTL, 'start', 'example'])

    def test_command_timeout_has_no_sensitive_payload(self):
        with patch.object(host.subprocess, 'run', side_effect=subprocess.TimeoutExpired('utmctl', 60)):
            with self.assertRaisesRegex(host.HostError, 'timed out'):
                host.command(['utmctl'], input='sensitive-password')

    def test_checksum_failure_does_not_accept_image(self):
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / 'image.ipsw'
            image.write_bytes(b'incorrect image')
            with redirect_stdout(io.StringIO()), self.assertRaisesRegex(host.HostError, 'checksum mismatch'):
                host.image_ready({'image': {'path': str(image), 'sha256': 'a' * 64}})

    def test_host_help_does_not_read_configuration(self):
        with patch.object(host, 'load') as load, redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as result:
            host.main(['--help'])
        self.assertEqual(result.exception.code, 0)
        load.assert_not_called()

    def test_status_does_not_create_state_directory(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(host, 'STATE', Path(directory) / 'absent'), \
             patch.object(host, 'load', return_value={}), patch.object(host, 'inventory', return_value={}), \
             redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()):
            self.assertEqual(host.main(['status', '--config', str(Path(directory) / 'host.toml')]), 0)
            self.assertFalse(host.STATE.exists())
            self.assertIn('unverified', json.loads(output.getvalue())['runner_setup'])


if __name__ == '__main__':
    unittest.main()
