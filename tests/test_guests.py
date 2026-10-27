# This source file is part of the macOS Self-Hosted GitHub Actions Runner open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
# SPDX-License-Identifier: MIT

"""Guest connections are restricted to owned VMs and isolated SSH settings."""

from pathlib import Path
import subprocess
import json
import tempfile
import unittest
from unittest.mock import MagicMock, patch
import uuid

from cisetup import guests, host


class GuestConnectionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.identifier = str(uuid.uuid4()).upper()
        self.spec = {'guest': {'username': 'runner', 'remote_login': True},
                     'vm': [{'name': 'GitHub Runner 3'}]}
        self.entry = {'id': self.identifier, 'phase': 'ready',
                      'password_service': host.password_service(self.identifier)}
        self.addCleanup(patch.stopall)
        patch.object(host, 'STATE', self.root).start()
        self.command = patch.object(host, 'command', return_value='192.168.64.5\nfe80::1').start()
        self.run = patch.object(guests.subprocess, 'run', side_effect=AssertionError('real SSH forbidden')).start()

    def test_ssh_has_pinned_identity_and_no_host_agent_or_forwarding(self):
        connection = guests.Connection(self.spec, self.entry)
        self.run.side_effect = None
        self.run.return_value = subprocess.CompletedProcess([], 0, 'ready\n', '')
        self.assertEqual(connection.run(['printf', '%s', 'space; literal']), 'ready')
        args = self.run.call_args.args[0]
        self.assertIn('StrictHostKeyChecking=accept-new', args)
        self.assertIn(f'HostKeyAlias={self.identifier}', args)
        self.assertIn('IdentityAgent=none', args)
        self.assertIn('ForwardAgent=no', args)
        self.assertIn('ClearAllForwardings=yes', args)
        self.assertEqual(args[-1], "printf %s 'space; literal'")
        self.assertEqual(connection.environment['RUNNER_VM_KEYCHAIN_SERVICE'], self.entry['password_service'])

    def test_unmanaged_password_refuses_connection(self):
        self.entry.pop('password_service')
        with self.assertRaisesRegex(host.HostError, 'not managed'):
            guests.Connection(self.spec, self.entry)
        self.command.assert_not_called()

    def test_unexpected_address_never_becomes_an_ssh_destination(self):
        for address in ('127.0.0.1', '8.8.8.8', '-oProxyCommand=anything', '169.254.1.2'):
            self.command.return_value = address
            with self.subTest(address=address), self.assertRaisesRegex(host.HostError, 'no private IPv4'):
                guests.Connection(self.spec, self.entry)
        self.run.assert_not_called()

    def test_remote_failure_does_not_echo_stdin_or_remote_output(self):
        connection = guests.Connection(self.spec, self.entry)
        self.run.side_effect = None
        self.run.return_value = subprocess.CompletedProcess([], 1, 'private-test-secret', 'private-test-secret')
        with self.assertRaises(host.HostError) as caught:
            connection.run(['command'], input='private-test-secret\n')
        self.assertNotIn('private-test-secret', str(caught.exception))
        self.assertNotIn('private-test-secret', str(self.run.call_args.args))

    def test_exit_two_is_deferred_only_for_explicit_maintenance(self):
        connection = guests.Connection(self.spec, self.entry)
        self.run.side_effect = None
        self.run.return_value = subprocess.CompletedProcess([], 2, 'private-test-secret', 'private-test-secret')
        with self.assertRaises(guests.GuestCommandError) as ordinary:
            connection.logged(['command'], '/example/log')
        self.assertEqual(ordinary.exception.returncode, 2)
        with self.assertRaises(host.HostBusy) as busy:
            connection.logged(['maintenance'], '/example/log', defer_on_busy=True)
        self.assertNotIn('private-test-secret', str(busy.exception))

    def test_no_route_has_safe_background_permission_hint(self):
        connection = guests.Connection(self.spec, self.entry)
        self.run.side_effect = None
        self.run.return_value = subprocess.CompletedProcess([], 255, 'private-test-secret',
                                                            'No route to host private-test-secret')
        with self.assertRaisesRegex(guests.GuestConnectionError, 'Local Network') as caught:
            connection.run(['command'], input='private-test-secret\n')
        self.assertNotIn('private-test-secret', str(caught.exception))

    def test_remote_output_does_not_become_transport_diagnostic(self):
        result = subprocess.CompletedProcess([], 1, '', 'No route to host')
        self.assertIsNone(guests.check_transport(result))
        with self.assertRaises(guests.GuestConnectionError):
            guests.check_transport(subprocess.CompletedProcess([], 255, b'', b'No route to host'))

    def test_archive_transfer_streams_stdin_and_publishes_only_verified_bytes(self):
        import hashlib
        import shlex
        import sys
        source = self.root / 'source.xip'
        target = self.root / 'guest.xip'
        source.write_bytes(b'archive' * 1024)
        target.write_bytes(b'previous')
        connection = guests.Connection(self.spec, self.entry)
        with patch.object(connection, 'run', return_value='upload'):
            def execute(args, **kwargs):
                remote = shlex.split(args[-1])
                # Use Popen directly because run is mocked for SSH interception.
                process = subprocess.Popen([sys.executable, *remote[1:]], stdin=kwargs['stdin'],
                                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                out, err = process.communicate(timeout=10)
                return subprocess.CompletedProcess([], process.returncode, out, err)
            self.run.side_effect = execute
            with self.assertRaises(host.HostError):
                connection.upload_file(source, str(target), '0' * 64)
            self.assertEqual(target.read_bytes(), b'previous')
            connection.upload_file(source, str(target), hashlib.sha256(source.read_bytes()).hexdigest())
            self.assertEqual(target.read_bytes(), source.read_bytes())
            self.assertEqual(list(self.root.glob('*.part')), [])


class GuestConfigurationTests(unittest.TestCase):
    def test_listener_startup_delay_is_retried(self):
        connection = MagicMock()
        connection.run.side_effect = [json.dumps({'errors': [], 'runner': {
            'name': 'Runner 1', 'activity': {'state': state}}}) for state in ('offline', 'idle')]
        with patch.object(guests.time, 'sleep') as sleep:
            guests.wait_for_runner(connection, '/checkout', 'Runner 1')
        self.assertEqual(connection.run.call_count, 2)
        sleep.assert_called_once_with(2)

    def test_listener_startup_timeout_never_reports_ready(self):
        connection = MagicMock()
        connection.run.return_value = json.dumps({'errors': [], 'runner': {
            'name': 'Runner 1', 'activity': {'state': 'offline'}}})
        with patch.object(guests.time, 'monotonic', side_effect=[0, 61]), \
                patch.object(guests.time, 'sleep') as sleep:
            with self.assertRaisesRegex(host.HostError, 'listener did not start'):
                guests.wait_for_runner(connection, '/checkout', 'Runner 1')
        sleep.assert_not_called()

    def test_invalid_inventory_is_not_retried(self):
        connection = MagicMock()
        for snapshot in ({'errors': ['missing tool']}, {'runner': {'name': 'Another runner'}}):
            connection.run.return_value = json.dumps(snapshot)
            with patch.object(guests.time, 'sleep') as sleep:
                with self.assertRaisesRegex(host.HostError, 'inventory is incomplete'):
                    guests.wait_for_runner(connection, '/checkout', 'Runner 1')
            sleep.assert_not_called()

    def test_dirty_source_stops_before_credentials_or_guest_access(self):
        spec = {'guest': {'automatic_login': True}}
        with patch.object(host, 'command', return_value=' M file'), \
                patch.object(host, 'runner_credentials') as credentials, \
                patch.object(guests, 'connections') as connections:
            with self.assertRaisesRegex(host.HostError, 'Commit the reviewed'):
                guests.configure(spec, Path('state'))
            credentials.assert_not_called()
            connections.assert_not_called()

    def configure(self, *, fail=False):
        name = 'GitHub Runner 3'
        spec = {'guest': {'automatic_login': True}}
        state = {'vms': {name: {}}}
        connection = MagicMock(username='runner')
        connection.run.return_value = json.dumps({'errors': [], 'runner': {
            'name': name, 'activity': {'state': 'idle'}}})
        connection.password.return_value = 'test-guest-password'
        if fail:
            connection.logged.side_effect = host.HostError('guest phase failed')

        def command(args, **kwargs):
            if 'status' in args:
                return ''
            if 'symbolic-ref' in args:
                return 'test-branch'
            if 'bundle' in args:
                Path(args[-2]).write_bytes(b'committed-source')
                return ''
            return 'a' * 40

        with patch.object(host, 'command', side_effect=command), \
                patch.object(host, 'read_state', return_value=state), \
                patch.object(guests, 'connections', return_value=[({'name': name}, connection)]), \
                patch.object(host, 'runner_credentials') as credentials, \
                patch('cisetup.github_api.registration_token', return_value='temporary-registration-token'), \
                patch.object(host, 'save_state') as save, \
                patch('cisetup.xcode_cache.prepare', return_value=None), \
                patch('cisetup.config.load'):
            credentials.return_value.pat = 'long-lived-host-secret'
            if fail:
                with self.assertRaisesRegex(host.HostError, 'guest phase failed'):
                    guests.configure(spec, Path('state'))
                save.assert_not_called()
                self.assertNotIn('runner_revision', state['vms'][name])
            else:
                guests.configure(spec, Path('state'))
                save.assert_called_once()
                self.assertEqual(state['vms'][name]['runner_revision'], 'a' * 40)
                calls = str(connection.mock_calls)
                self.assertNotIn('long-lived-host-secret', calls)
                registration = next(c for c in connection.logged.call_args_list if 'register' in c.args[0])
                self.assertEqual(registration.kwargs['input'], 'temporary-registration-token\n')
                self.assertNotIn('temporary-registration-token', str(registration.args))
                converge = next(c for c in connection.logged.call_args_list if '--converge' in c.args[0])
                self.assertEqual(converge.kwargs['input'], 'test-guest-password\n')
                self.assertNotIn('test-guest-password', str(converge.args))

    def test_only_short_lived_registration_token_reaches_guest_stdin(self):
        self.configure()

    def test_failed_guest_never_receives_ready_revision(self):
        self.configure(fail=True)
