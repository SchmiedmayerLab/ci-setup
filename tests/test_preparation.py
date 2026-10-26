# This source file is part of the macOS Self-Hosted GitHub Actions Runner open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
# SPDX-License-Identifier: MIT

"""Prepared-plan binding, cancellation, resume, isolation and honest completion."""

from contextlib import redirect_stdout, redirect_stderr
import io
import json
from pathlib import Path
import plistlib
import subprocess
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from cisetup import config, host, install, main, preparation as prep, util


class PreparationTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name).resolve()
        self.path = self.root / 'host.toml'
        self.identity = {'uid': 501, 'revision': 'a' * 40, 'root': str(self.root),
                         'python': '/example/python', 'config_path': str(self.path), 'settings_sha256': 'b' * 64}
        self.plan = {'host': {'cpu_overcommit': False},
                     'image': {'path': str(self.root / 'restore.ipsw'), 'sha256': 'c' * 64},
                     'guest': {'username': 'runner', 'automatic_login': True, 'remote_login': True},
                     'vm': [{'name': 'Runner 1', 'cpus': 4, 'memory_gib': 8, 'disk_gib': 256}]}
        self.cfg = config.Config(repo_root=self.root, owner='example', scope='org')
        self.output = io.StringIO()
        for context in (redirect_stdout(self.output), redirect_stderr(self.output)):
            context.__enter__(); self.addCleanup(context.__exit__, None, None, None)
        self.mock('host.STATE', new=self.root / 'state')
        self.mock('host.ROOT', new=self.root)
        self.mock('binding', return_value=self.identity)
        self.mock('config.load', return_value=self.cfg)
        self.mock('host.command', side_effect=AssertionError('unexpected external command'))
        self.mock('subprocess.run', side_effect=AssertionError('unexpected process'))
        self.mock('host.check_machine')
        self.mock('host_readiness.require_awake')
        self.mock('host.runner_credentials', return_value=self.cfg)
        self.mock('install.apple_access')
        self.mock('install.check_host_layout')
        self.worker = self.mock('install.install_host')

    def mock(self, name, **kwargs):
        obj = patch('cisetup.preparation.' + name, **kwargs)
        result = obj.start(); self.addCleanup(obj.stop)
        return result

    def receipt(self, **changes):
        data = dict(schema=prep.SCHEMA, binding=self.identity, plan=self.plan, owned={}, prepared_at=time.time())
        data.update(changes)
        prep.write_json(prep.paths(self.path)[0], data)
        prep.write_json(prep.paths(self.path)[1], dict(schema=1, state='prepared', attempt=0))
        return data

    def prepare_flow(self):
        self.mock('sys.stdin.isatty', return_value=True)
        self.mock('install.prerequisites')
        self.mock('install.background_check')
        self.mock('background.ensure')
        self.mock('background.request_network_access')
        self.mock('background.migrate_agents')
        self.mock('host.preflight')
        self.mock('host.inventory', return_value={})
        self.mock('host.discover_names', return_value=['Runner 1'])
        self.mock('host.plan_config', return_value=self.plan)
        return SimpleNamespace(config=self.path, dedicated_host=False, image=None, name=None,
                               reserve_memory_gib=None, cpu_ratio=1.5, no_start=True)

    def test_prepare_dispatch_never_enters_host_runner_maintenance(self):
        with patch.object(prep, 'main', return_value=0) as dispatch, patch.object(util, 'acquire_lock') as lock:
            self.assertEqual(main.main(['prepare', 'host', '--no-start']), 0)
        dispatch.assert_called_once_with(['host', '--no-start']); lock.assert_not_called()

    def test_prepare_saves_no_secrets_and_does_not_download_or_create(self):
        args = self.prepare_flow()
        with patch('builtins.input', return_value='y'), patch.object(host, 'materialize_config') as image, patch.object(host, 'create') as create:
            self.assertEqual(prep.prepare(args), 0)
        self.assertFalse(self.path.exists())
        image.assert_not_called(); create.assert_not_called()
        receipt = prep.read_receipt(self.path)
        self.assertEqual(receipt['plan'], self.plan)
        self.assertEqual(set(receipt), {'schema', 'binding', 'plan', 'owned', 'prepared_at'})
        self.assertEqual(prep.paths(self.path)[0].stat().st_mode & 0o777, 0o600)
        self.assertEqual(prep.snapshot(self.path)['state'], 'prepared')

    def test_no_start_preparation_leaves_existing_agents_loaded(self):
        args = self.prepare_flow()
        migrate = self.mock('background.migrate_agents')
        with patch('builtins.input', return_value='y'):
            prep.prepare(args)
        migrate.assert_not_called()

    def test_resume_reuses_materialized_image_without_new_download(self):
        pending = {key: value for key, value in self.plan.items() if key != 'image'}
        pending['restore'] = {'url': 'https://example.invalid/restore.ipsw'}
        self.receipt(plan=pending, materialized=self.plan)
        host.materialize_config(self.path, self.plan)
        with patch('cisetup.restore.download', side_effect=AssertionError('unnecessary image download')):
            self.assertEqual(prep.run(self.path), 0)
        self.worker.assert_called_once()

    def test_preparation_order_places_background_check_before_plan_and_launch(self):
        args = self.prepare_flow(); args.no_start = False
        events = []
        for name in ('install.prerequisites', 'host.runner_credentials', 'install.apple_access', 'install.background_check'):
            self.mock(name, side_effect=lambda *a, _n=name, **kw: events.append(_n))
        self.mock('install.confirm_host', side_effect=lambda *_: events.append('confirm'))
        self.mock('launch', side_effect=lambda *_: events.append('launch'))
        prep.prepare(args)
        self.assertEqual(events, ['install.prerequisites', 'host.runner_credentials', 'install.apple_access',
                                  'install.background_check', 'confirm', 'launch'])

    def test_unchanged_plan_is_not_reconfirmed(self):
        args = self.prepare_flow(); self.receipt()
        with patch('builtins.input', side_effect=AssertionError('unexpected repeated consent')):
            prep.prepare(args)

    def test_changed_revision_requires_plan_review(self):
        args = self.prepare_flow(); self.receipt(binding=dict(self.identity, revision='d' * 40))
        with patch('builtins.input', return_value='y') as prompt:
            prep.prepare(args)
        prompt.assert_called_once()

    def test_changed_host_configuration_refuses_old_receipt(self):
        self.receipt(materialized=self.plan)
        changed = dict(self.plan, vm=[dict(self.plan['vm'][0], cpus=6)])
        host.materialize_config(self.path, changed)
        with self.assertRaisesRegex(host.HostError, 'configuration changed'):
            prep.read_receipt(self.path)

    def test_changed_uncreated_layout_can_be_reviewed_in_preparation(self):
        args = self.prepare_flow()
        self.receipt(materialized=self.plan)
        changed = dict(self.plan, vm=[dict(self.plan['vm'][0], cpus=6)])
        host.materialize_config(self.path, changed)
        with patch('builtins.input', return_value='y') as prompt:
            prep.prepare(args)
        prompt.assert_called_once()
        self.assertEqual(prep.read_receipt(self.path)['materialized'], changed)

    def test_changed_owned_identity_refuses_resume(self):
        self.receipt(owned={'Runner 1': 'original'})
        host.materialize_config(self.path, self.plan)
        self.mock('host.read_state', return_value={'vms': {'Runner 1': {'id': 'replacement'}}})
        for verify in (True, False):
            with self.subTest(verify=verify), self.assertRaisesRegex(host.HostError, 'identity changed'):
                prep.read_receipt(self.path, verify=verify)

    def test_background_access_failure_never_creates_receipt_or_launches(self):
        args = self.prepare_flow()
        self.mock('install.background_check', side_effect=host.HostError('blocked'))
        launch = self.mock('launch')
        with self.assertRaises(host.HostError): prep.prepare(args)
        self.assertFalse(prep.paths(self.path)[0].exists()); launch.assert_not_called()

    def test_power_decline_stops_before_credentials(self):
        args = self.prepare_flow(); args.dedicated_host = True
        self.mock('host_readiness.configure_power', return_value=False)
        credentials = self.mock('host.runner_credentials')
        with self.assertRaises(install.SetupCancelled): prep.prepare(args)
        credentials.assert_not_called()

    def test_nonterminal_prepare_never_changes_machine(self):
        args = self.prepare_flow(); self.mock('sys.stdin.isatty', return_value=False)
        prerequisites = self.mock('install.prerequisites')
        with self.assertRaisesRegex(host.HostError, 'Terminal'): prep.prepare(args)
        prerequisites.assert_not_called()

    def test_worker_rejects_stale_binding_before_credentials_or_provisioning(self):
        self.receipt(binding=dict(self.identity, revision='d' * 40))
        credentials = self.mock('host.runner_credentials')
        self.assertEqual(prep.run(self.path), 1)
        credentials.assert_not_called(); self.worker.assert_not_called()
        self.assertFalse(self.path.exists())
        self.assertEqual(prep.snapshot(self.path)['state'], 'needs_attention')

    def test_worker_requires_all_phases_and_agent_verification_for_success(self):
        self.receipt()
        with patch('builtins.input', side_effect=AssertionError('worker prompted')):
            self.assertEqual(prep.run(self.path), 0)
        self.worker.assert_called_once()
        self.assertTrue(self.worker.call_args.kwargs['verify_agents'])
        self.assertEqual(prep.snapshot(self.path)['state'], 'configured')
        self.assertIn('not performed', prep.snapshot(self.path)['ci_validation'])
        self.assertEqual(host.load(self.path), self.plan)

    def test_failed_worker_retains_resources_and_never_persists_exception_secrets(self):
        self.receipt()
        def fail(*a, progress, **kw):
            progress.phase('configure')
            raise RuntimeError('private-test-password and private-test-session-url')
        self.worker.side_effect = fail
        self.assertEqual(prep.run(self.path), 1)
        data = prep.paths(self.path)[1].read_text()
        self.assertNotIn('private-test', data + self.output.getvalue())
        self.assertEqual(prep.snapshot(self.path)['phase'], 'configure')
        self.assertTrue(self.path.exists())

    def test_login_does_not_retry_failed_or_cancelled_or_completed_worker(self):
        self.receipt()
        for state in prep.TERMINAL:
            prep.write_json(prep.paths(self.path)[1], dict(schema=1, state=state))
            prep.run(self.path, agent=True)
        self.worker.assert_not_called()

    def test_login_preserves_no_start_preparation_until_explicit_resume(self):
        self.receipt()
        previous = prep.paths(self.path)[1].read_bytes()
        self.assertEqual(prep.run(self.path, agent=True), 0)
        self.worker.assert_not_called()
        self.assertEqual(prep.paths(self.path)[1].read_bytes(), previous)
        self.assertFalse(self.path.exists())

    def test_login_resumes_interrupted_worker_without_reconfirming(self):
        self.receipt()
        prep.write_json(prep.paths(self.path)[1], dict(schema=1, state='running', heartbeat_at=0))
        with patch('builtins.input', side_effect=AssertionError('repeat consent')):
            self.assertEqual(prep.run(self.path, agent=True), 0)
        self.worker.assert_called_once()

    def test_concurrent_worker_does_not_overwrite_active_progress(self):
        self.receipt()
        original = prep.paths(self.path)[1].read_bytes()
        with prep.exclusive(self.path):
            self.assertEqual(prep.run(self.path), 1)
        self.assertEqual(prep.paths(self.path)[1].read_bytes(), original)
        self.worker.assert_not_called()

    def test_cancel_persists_and_blocks_login_retry(self):
        self.receipt(); prep.cancel_pending(self.path)
        self.assertEqual(prep.snapshot(self.path)['state'], 'cancelled')
        self.assertEqual(prep.run(self.path, agent=True), 1)
        self.worker.assert_not_called()

    def test_progress_detects_stale_heartbeat_without_credentials_or_utm(self):
        prep.write_json(prep.paths(self.path)[1], dict(schema=1, state='running', heartbeat_at=0))
        credentials = self.mock('host.runner_credentials'); inventory = self.mock('host.inventory')
        self.assertEqual(prep.show(self.path, as_json=True), 1)
        self.assertEqual(json.loads(self.output.getvalue())['state'], 'interrupted_or_unresponsive')
        credentials.assert_not_called(); inventory.assert_not_called()

    def test_receipt_symlink_is_rejected(self):
        self.receipt()
        target = prep.paths(self.path)[0]; target.unlink(); target.symlink_to(self.root / 'elsewhere')
        with self.assertRaises(host.HostError): prep.read_receipt(self.path)

    def test_launch_has_no_stdin_or_secrets_and_no_keepalive_retry_loop(self):
        self.receipt()
        self.mock('subprocess.run', return_value=SimpleNamespace(returncode=1, stdout=''))
        command = self.mock('host.command', return_value='')
        with patch.object(Path, 'home', return_value=self.root):
            prep.launch(self.path)
            file = self.root / 'Library/LaunchAgents' / (prep.label(self.path) + '.plist')
            data = plistlib.loads(file.read_bytes())
        self.assertEqual(data['StandardInPath'], '/dev/null')
        self.assertNotIn('KeepAlive', data)
        self.assertIn('--prepared', data['ProgramArguments'])
        self.assertIn('--agent', data['ProgramArguments'])
        self.assertNotIn('PASSWORD', str(data))
        self.assertEqual(command.call_args.args[0][1], 'bootstrap')
        self.assertEqual(prep.snapshot(self.path)['state'], 'queued')

    def test_existing_agent_with_different_command_is_preserved(self):
        self.receipt()
        with patch.object(Path, 'home', return_value=self.root):
            file = self.root / 'Library/LaunchAgents' / (prep.label(self.path) + '.plist')
            file.parent.mkdir(parents=True); file.write_bytes(b'unrelated')
            with self.assertRaisesRegex(host.HostError, 'different provisioning agent'): prep.launch(self.path)
            self.assertEqual(file.read_bytes(), b'unrelated')

    def test_host_lock_contention_is_deferred_without_provisioning(self):
        self.receipt()
        self.mock('host.locked', side_effect=host.HostBusy('Another host operation is running'))
        self.assertEqual(prep.run(self.path), 2)
        self.assertEqual(prep.snapshot(self.path)['state'], 'deferred')
        self.worker.assert_not_called()

    def test_worker_restores_interactivity_flags_on_failure(self):
        self.receipt(); self.worker.side_effect = RuntimeError('failure')
        previous = host.UNATTENDED, util.INTERACTIVE
        prep.run(self.path)
        self.assertEqual((host.UNATTENDED, util.INTERACTIVE), previous)

    def test_unattended_host_command_has_no_terminal_or_stdin(self):
        self.mock('host.command', new=self.original_command)
        process = self.mock('subprocess.run', return_value=subprocess.CompletedProcess([], 0, '', ''))
        with prep.unattended():
            host.command(['/example/tool'])
            self.assertEqual(process.call_args.kwargs['stdin'], subprocess.DEVNULL)
            self.assertTrue(process.call_args.kwargs['start_new_session'])

    original_command = staticmethod(host.command)


class SourceBindingTests(unittest.TestCase):
    def test_detached_checkout_stops_preparation_before_prerequisites_or_access(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            path = root / 'host.toml'

            def git(args, **kwargs):
                if 'status' in args:
                    return ''
                if 'symbolic-ref' in args:
                    raise host.HostError('git: command failed')
                raise AssertionError('unexpected command')

            with patch.object(host, 'ROOT', root), patch.object(host, 'STATE', root / 'state'), \
                    patch.object(host, 'command', side_effect=git), \
                    patch.object(prep.sys.stdin, 'isatty', return_value=True), \
                    patch.object(install, 'prerequisites') as prerequisites, \
                    patch.object(host, 'runner_credentials') as credentials, \
                    patch.object(host, 'materialize_config') as image, \
                    patch.object(prep, 'launch') as launch:
                with self.assertRaisesRegex(host.HostError, 'named local Git branch'):
                    prep.prepare(SimpleNamespace(config=path))
                prerequisites.assert_not_called()
                credentials.assert_not_called()
                image.assert_not_called()
                launch.assert_not_called()
                self.assertFalse(prep.paths(path)[0].exists())

    def test_named_branch_keeps_exact_revision_in_binding(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / 'config.toml').write_text('synthetic test configuration\n')
            with patch.object(host, 'ROOT', root), patch.object(
                    host, 'command', side_effect=['', 'review-branch', 'a' * 40]):
                binding = prep.binding(root / 'host.toml')
            self.assertEqual(binding['revision'], 'a' * 40)


class AgentCompletionTests(unittest.TestCase):
    def test_failed_agent_exit_is_not_reported_as_installed(self):
        with patch.object(host, 'command', side_effect=['123', 'state = not running\nlast exit code = 1\n']):
            with self.assertRaisesRegex(host.HostError, 'agent failed'):
                install.verify_agent('example', timeout=10)

    def test_deferred_agent_exit_is_preserved_for_prepared_worker(self):
        with patch.object(host, 'command', return_value='state = not running\nlast exit code = 2\n'):
            with self.assertRaises(host.HostBusy):
                install.verify_agent('example', timeout=10)

    def test_running_agent_is_waited_for_before_success(self):
        with patch.object(host, 'command', side_effect=['123', 'state = running\nlast exit code = 0\n',
                                                      'state = not running\nlast exit code = 0\n']), \
                patch.object(install.time, 'sleep') as sleep:
            install.verify_agent('example', timeout=10)
        sleep.assert_called_once()

    def test_prepared_options_cannot_smuggle_a_new_plan(self):
        for options in (['--name', 'Different'], ['--dedicated-host'], ['--check'], ['--image', '/tmp/image']):
            with self.subTest(options=options), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                install.main(['host', '--prepared', *options])


if __name__ == '__main__':
    unittest.main()
