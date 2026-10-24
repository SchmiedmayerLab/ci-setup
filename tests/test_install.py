# This source file is part of the macOS Self-Hosted GitHub Actions Runner open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
# SPDX-License-Identifier: MIT

"""Guided setup ordering, credential lifetime and safe failure boundaries."""

from contextlib import nullcontext, redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import plistlib
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from cisetup import config, host, install, main


class InstallTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.cfg = config.Config(repo_root=self.root, owner='example', scope='org', runner_name='Runner 1')
        self.spec = {'guest': {'automatic_login': True, 'remote_login': True},
                     'vm': [{'name': 'Runner 1', 'disk_gib': 128}]}
        self.out = io.StringIO()
        for stream in (redirect_stdout(self.out), redirect_stderr(self.out)):
            stream.__enter__()
            self.addCleanup(stream.__exit__, None, None, None)
        self.mock('subprocess.run', side_effect=AssertionError('unexpected external command'))
        self.mock('host.command', return_value='')
        self.mock('host.STATE', new=self.root)
        self.mock('config.load', return_value=self.cfg)
        self.mock('host.check_machine')
        self.mock('host_readiness.require_awake')
        self.mock('host_readiness.check', return_value=0)
        self.mock('sys.stdin.isatty', return_value=True)

    def mock(self, name, **kwargs):
        patcher = patch('cisetup.install.' + name, **kwargs)
        result = patcher.start()
        self.addCleanup(patcher.stop)
        return result

    def flow(self):
        events = []
        for name in ('prerequisites', 'host.runner_credentials', 'apple_access', 'background_check'):
            self.mock(name, side_effect=lambda *a, _name=name, **k: events.append(_name))
        def plan(*args, **kwargs):
            if kwargs.get('confirm'):
                install.confirm_host(self.spec['vm'])
            return self.root / 'host.toml', self.spec
        self.mock('host_plan', side_effect=plan)
        self.mock('install_host', side_effect=lambda *a: events.append('install_host'))
        self.mock('install_runner', side_effect=AssertionError('host invoked runner installation'))
        return events

    def test_dispatch_does_not_enter_regular_converge(self):
        with patch('cisetup.install.main', return_value=0) as dispatch, patch('cisetup.util.acquire_lock') as lock:
            self.assertEqual(main.main(['install', 'host', '--check']), 0)
            dispatch.assert_called_once_with(['host', '--check'])
            lock.assert_not_called()

    def test_access_and_plan_precede_host_install(self):
        events = self.flow()
        with patch('builtins.input', return_value='y'):
            self.assertEqual(install.main(['host']), 0)
        self.assertEqual(events, ['prerequisites', 'host.runner_credentials', 'apple_access',
                                  'background_check', 'install_host'])

    def test_permission_failure_never_provisions(self):
        events = self.flow()
        self.mock('background_check', side_effect=host.HostError('permission denied'))
        with patch('builtins.input') as prompt:
            self.assertEqual(install.main(['host']), 1)
        self.assertNotIn('install_host', events)
        prompt.assert_not_called()

    def test_cancel_preserves_preparation_without_provisioning(self):
        events = self.flow()
        with patch('builtins.input', return_value='n'):
            self.assertEqual(install.main(['host']), 0)
        self.assertNotIn('install_host', events)

    def test_check_never_bootstraps_plans_or_prompts(self):
        events = self.flow()
        self.mock('host.load', return_value=self.spec)
        preflight = self.mock('check_host_layout')
        with patch('builtins.input') as prompt:
            self.assertEqual(install.main(['host', '--check']), 0)
        self.assertEqual(events, ['host.runner_credentials', 'apple_access', 'background_check'])
        preflight.assert_called_once()
        self.assertEqual(preflight.call_args.args[1], self.spec)
        prompt.assert_not_called()

    def test_dedicated_host_power_precedes_credentials_and_provisioning(self):
        events = self.flow()
        power = self.mock('host_readiness.configure_power', side_effect=lambda **k: events.append('power') or True)
        awake = self.mock('host_readiness.require_awake', side_effect=lambda: events.append('awake'))
        with patch('builtins.input', return_value='y'):
            self.assertEqual(install.main(['host', '--dedicated-host']), 0)
        self.assertEqual(events[:4], ['prerequisites', 'power', 'awake', 'host.runner_credentials'])
        power.assert_called_once_with(apply=True)
        awake.assert_called_once()

    def test_unsafe_sleep_stops_before_credentials_or_guest_creation(self):
        events = self.flow()
        self.mock('host_readiness.require_awake', side_effect=host.HostError('sleep prevention missing'))
        self.assertEqual(install.main(['host']), 1)
        self.assertEqual(events, ['prerequisites'])

    def test_cancel_power_plan_stops_installation(self):
        events = self.flow()
        self.mock('host_readiness.configure_power', return_value=False)
        self.assertEqual(install.main(['host', '--dedicated-host']), 0)
        self.assertEqual(events, ['prerequisites'])

    def test_power_flag_rejected_for_check_and_physical_runner(self):
        for args in (['host', '--check', '--dedicated-host'], ['runner', '--dedicated-host']):
            with self.subTest(args=args), self.assertRaises(SystemExit) as error:
                install.main(args)
            self.assertEqual(error.exception.code, 2)

    def test_post_install_readiness_failure_is_reported(self):
        self.flow()
        self.mock('host_readiness.check', return_value=1)
        with patch('builtins.input', return_value='y'):
            self.assertEqual(install.main(['host']), 1)

    def test_nonterminal_install_fails_before_prerequisites(self):
        events = self.flow()
        self.mock('sys.stdin.isatty', return_value=False)
        self.assertEqual(install.main(['runner']), 1)
        self.assertEqual(events, [])

    def test_existing_config_cannot_be_overwritten_with_new_names(self):
        path = self.root / 'host.toml'
        path.write_text('existing')
        args = SimpleNamespace(config=path, image=None, name=['Different'])
        with self.assertRaises(host.HostError):
            install.host_plan(args)
        self.assertEqual(path.read_text(), 'existing')

    def test_no_image_argument_uses_automatic_discovery_without_a_prompt(self):
        args = SimpleNamespace(config=self.root / 'new.toml', image=None, name=['Runner 1'],
                               reserve_memory_gib=8, cpu_ratio=1.0)
        self.mock('host.locked', return_value=nullcontext())
        init = self.mock('host.init_config')
        self.mock('host.load', return_value=self.spec)
        self.mock('check_host_layout')
        with patch('builtins.input', side_effect=AssertionError('unexpected image prompt')):
            install.host_plan(args)
        self.assertIsNone(init.call_args.args[1])

    def test_unsupported_host_stops_before_credentials_or_installation(self):
        events = self.flow()
        self.mock('host.check_machine', side_effect=host.HostError('host too old'))
        self.assertEqual(install.main(['host']), 1)
        self.assertEqual(events, [])

    def test_dirty_source_stops_before_prerequisite_installation(self):
        events = self.flow()
        self.mock('host.command', return_value=' M setup')
        self.assertEqual(install.main(['host']), 1)
        self.assertEqual(events, [])

    def test_physical_runner_rejects_vm_options_before_preparation(self):
        events = self.flow()
        self.assertEqual(install.main(['runner', '--image', '/tmp/restore.ipsw']), 1)
        self.assertEqual(events, [])

    def layout_checks(self, *, existing=False):
        self.spec['image'] = {'path': str(self.root / 'restore.ipsw')}
        self.spec['vm'][0].update(cpus=4, memory_gib=8)
        self.mock('host.preflight')
        self.mock('host.read_state', return_value={'vms': {'Runner 1': {}} if existing else {}})
        self.mock('host.check_owned')
        self.mock('host.check_other_running')
        self.mock('host.inventory', return_value={})
        self.mock('shutil.disk_usage', return_value=SimpleNamespace(free=1024 * 1024**3))
        return self.mock('host.image_ready', return_value=27)

    def test_layout_check_rejects_unowned_name_before_image_work(self):
        image = self.layout_checks()
        self.mock('host.inventory', return_value={'other-id': {'name': 'runner 1'}})
        with self.assertRaisesRegex(host.HostError, 'outside this setup'):
            install.check_host_layout(self.root / 'host.toml', self.spec)
        image.assert_not_called()

    def test_layout_check_rejects_disk_shortage_before_image_work(self):
        image = self.layout_checks()
        self.mock('shutil.disk_usage', return_value=SimpleNamespace(free=100 * 1024**3))
        with self.assertRaisesRegex(host.HostError, 'free space'):
            install.check_host_layout(self.root / 'host.toml', self.spec)
        image.assert_not_called()

    def test_check_does_not_download_missing_pinned_image(self):
        image = self.layout_checks()
        with self.assertRaisesRegex(host.HostError, 'Pinned restore image is missing'):
            install.check_host_layout(self.root / 'host.toml', self.spec)
        image.assert_not_called()

    def test_layout_check_enforces_native_image_requirements(self):
        self.layout_checks()
        self.mock('restore.metadata', return_value={'minimum_cpus': 8, 'minimum_memory_bytes': 4 * 1024**3})
        with self.assertRaisesRegex(host.HostError, 'minimum CPU or memory'):
            install.check_host_layout(self.root / 'host.toml', self.spec, allow_download=True)

    def test_existing_guests_do_not_require_restore_media(self):
        image = self.layout_checks(existing=True)
        metadata = self.mock('restore.metadata')
        install.check_host_layout(self.root / 'host.toml', self.spec)
        image.assert_not_called()
        metadata.assert_not_called()

    def test_custom_cask_limitation_is_reported_before_credentials(self):
        events = self.flow()
        self.cfg.brew_extra_casks = ['custom-installer']
        self.assertEqual(install.main(['host']), 1)
        self.assertEqual(events, [])

    def test_existing_registration_is_not_renamed(self):
        events = self.flow()
        self.mock('config.saved_runner_name', return_value=None)
        self.mock('runner.is_registered', return_value=True)
        self.mock('runner.registration_matches', return_value=False)
        save = self.mock('config.save_runner_name')
        with patch('builtins.input') as prompt:
            self.assertEqual(install.main(['runner', '--name', 'Different']), 1)
        prompt.assert_not_called()
        save.assert_not_called()
        self.assertNotIn('install_host', events)

    def test_ssh_timeout_does_not_reset_or_recreate_guests(self):
        self.mock('host.read_state', return_value={})
        self.mock('guests.connections', side_effect=host.HostError('not ready'))
        self.mock('time.monotonic', side_effect=[0, 601])
        create = self.mock('host.create')
        with self.assertRaisesRegex(host.HostError, 'rerun the same command'):
            install.wait_for_guests(self.spec, self.root / 'state')
        create.assert_not_called()

    def test_ssh_wait_retries_then_succeeds(self):
        connection = Mock()
        self.mock('host.read_state', return_value={})
        self.mock('guests.connections', side_effect=[host.HostError('not ready'), [(None, connection)]])
        self.mock('time.sleep')
        install.wait_for_guests(self.spec, self.root / 'state')
        connection.run.assert_called_once_with(['/usr/bin/true'], timeout=20)

    def test_host_loads_agents_only_after_success_and_unlock(self):
        events = []
        class Lock:
            def __enter__(self):
                events.append('lock')

            def __exit__(self, *args):
                events.append('unlock')
        self.mock('host.locked', return_value=Lock())
        for name in ('host.create', 'host.start', 'wait_for_guests', 'guests.configure', 'host.login_agent', 'xcode_cache.agent', 'load_agent'):
            self.mock(name, side_effect=lambda *a, _name=name, **k: events.append(_name))
        install.install_host(self.root / 'host.toml', self.spec)
        self.assertEqual(events, ['lock', 'host.create', 'host.start', 'wait_for_guests',
                                  'guests.configure', 'host.login_agent', 'xcode_cache.agent', 'unlock',
                                  'load_agent', 'load_agent'])

    def test_configuration_failure_does_not_enable_agents(self):
        self.mock('host.locked', return_value=nullcontext())
        for name in ('host.create', 'host.start', 'wait_for_guests'):
            self.mock(name)
        self.mock('guests.configure', side_effect=host.HostError('incomplete'))
        agent = self.mock('host.login_agent')
        load = self.mock('load_agent')
        with self.assertRaises(host.HostError):
            install.install_host(self.root / 'host.toml', self.spec)
        agent.assert_not_called()
        load.assert_not_called()

    def test_probe_never_records_secret_error_details(self):
        self.mock('host.runner_credentials', side_effect=host.HostError('private-test-token'))
        result = self.root / 'result.json'
        install.probe('host', result)
        self.assertEqual(json.loads(result.read_text()), {'ok': False, 'stage': 'GitHub token and login Keychain'})
        self.assertEqual(result.stat().st_mode & 0o777, 0o600)
        self.assertNotIn('private-test-token', self.out.getvalue())

    def test_probe_covers_both_utm_interfaces(self):
        self.mock('host.runner_credentials', return_value=self.cfg)
        self.mock('apple_access')
        inventory = self.mock('host.inventory')
        command = self.mock('host.command')
        result = self.root / 'result.json'
        install.probe('host', result)
        inventory.assert_called_once()
        self.assertIn('osascript', str(command.call_args))
        self.assertEqual(json.loads(result.read_text()), {'ok': True})

    def test_background_agent_cleanup_after_failure(self):
        calls = []
        def command(args, **kwargs):
            calls.append(args)
            if 'bootstrap' in args:
                definition = plistlib.loads(Path(args[-1]).read_bytes())
                Path(definition['ProgramArguments'][-1]).write_text(json.dumps({'ok': False, 'stage': 'Apple session'}))
            return ''
        self.mock('host.command', side_effect=command)
        run = self.mock('subprocess.run', return_value=SimpleNamespace(returncode=0))
        with self.assertRaisesRegex(host.HostError, 'Apple session'):
            install.background_check('host')
        self.assertIn('bootout', run.call_args.args[0])
        self.assertEqual(list(self.root.iterdir()), [])

    def test_admin_secret_only_in_memory_and_cleared_on_failure(self):
        password = 'test-private-administrator-password'
        self.mock('getpass.getpass', return_value=password)
        self.mock('runlog.register_secret')
        calls = []
        def run(args, **kwargs):
            self.assertNotIn(password, str(args))
            calls.append((args, kwargs))
            return SimpleNamespace(returncode=0)
        self.mock('subprocess.run', side_effect=run)
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, 'test failure'):
                with install.administrator():
                    path = Path(os.environ['SUDO_ASKPASS'])
                    self.assertNotIn(password, path.read_text())
                    self.assertEqual(path.stat().st_mode & 0o777, 0o700)
                    self.assertEqual(os.environ['RUNNER_BOOTSTRAP_PASSWORD'], password)
                    raise RuntimeError('test failure')
            self.assertNotIn('RUNNER_BOOTSTRAP_PASSWORD', os.environ)
            self.assertNotIn('SUDO_ASKPASS', os.environ)
            self.assertFalse(path.exists())
        self.assertEqual(calls[0][1]['input'], password + '\n')
        self.assertEqual(calls[-1][0], ['/usr/bin/sudo', '-k'])
        self.assertNotIn(password, self.out.getvalue())

    def test_physical_preparation_releases_lock_before_child(self):
        events = []
        self.mock('administrator', return_value=nullcontext())
        self.mock('util.acquire_lock', side_effect=lambda: events.append('lock'))
        self.mock('maintenance.paused', return_value=nullcontext())
        for name in ('config.save_runner_name', 'xcode.ensure_sudoless_select', 'xcode.ensure_wwdr_certificate', 'power.ensure', 'util.release_lock'):
            self.mock(name, side_effect=lambda *a, _name=name, **k: events.append(_name))
        def run(args, **kwargs):
            self.assertEqual(events[-1], 'util.release_lock')
            self.assertEqual(args[1:], ['converge', '--non-interactive'])
            self.assertEqual(kwargs['stdin'], subprocess.DEVNULL)
            self.assertTrue(kwargs['start_new_session'])
            self.assertEqual(kwargs['env']['CI_SETUP_UPDATED'], '1')
            return SimpleNamespace(returncode=7)
        self.mock('subprocess.run', side_effect=run)
        self.assertEqual(install.install_runner(self.cfg, 'Runner 1'), 7)


if __name__ == '__main__':
    unittest.main()
