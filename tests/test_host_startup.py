# This source file is part of the macOS Self-Hosted GitHub Actions Runner open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
# SPDX-License-Identifier: MIT

"""Login refresh must leave the VM startup lock available while guests boot."""

from contextlib import contextmanager, redirect_stdout, redirect_stderr
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from cisetup import guests, host, xcode_cache


class HostStartupTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.spec = {'vm': [{'name': 'one'}]}
        self.state = {'vms': {'one': {'id': 'owned', 'runner_revision': 'test'}}}
        for context in (patch.object(host, 'STATE', self.root),
                        patch.object(host, 'read_state', return_value=self.state),
                        patch.object(host, 'command', side_effect=AssertionError('unexpected command')),
                        redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO())):
            context.__enter__(); self.addCleanup(context.__exit__, None, None, None)

    def live(self, status):
        return {'owned': {'name': 'one', 'status': status}}

    def test_refresh_waits_for_boot_and_ssh_without_taking_lock(self):
        connection = MagicMock()
        connection.run.side_effect = [host.HostError('synthetic private error'), '']
        with patch.object(host, 'inventory', side_effect=[self.live('stopped'), self.live('resuming'), self.live('started'), self.live('started')]), \
                patch.object(guests, 'connections', return_value=[(self.spec['vm'][0], connection)]), \
                patch.object(host, 'locked', side_effect=AssertionError('startup lock taken')), \
                patch.object(xcode_cache.time, 'sleep') as sleep:
            xcode_cache.wait_for_guests(self.spec, self.root / 'state')
        self.assertEqual(sleep.call_count, 3)
        self.assertEqual(connection.run.call_count, 2)

    def test_stopped_guest_deadline_defers_without_starting_or_refreshing(self):
        with patch.object(host, 'inventory', return_value=self.live('stopped')), \
                patch.object(guests, 'connections') as connections:
            with self.assertRaisesRegex(host.HostBusy, 'deferred'):
                xcode_cache.wait_for_guests(self.spec, self.root / 'state', timeout=0)
        connections.assert_not_called()

    def test_renamed_owned_guest_fails_without_waiting(self):
        with patch.object(host, 'inventory', return_value={'owned': {'name': 'unrelated', 'status': 'started'}}), \
                patch.object(xcode_cache.time, 'sleep') as sleep:
            with self.assertRaisesRegex(host.HostError, 'missing/renamed'):
                xcode_cache.wait_for_guests(self.spec, self.root / 'state')
        sleep.assert_not_called()

    def test_refresh_readiness_precedes_cli_host_lock(self):
        events = []
        @contextmanager
        def lock(**kwargs):
            events.append('lock')
            yield
        with patch.object(host, 'load', return_value=self.spec), patch.object(host, 'locked', side_effect=lock), \
                patch.object(xcode_cache, 'wait_for_guests', side_effect=lambda *a: events.append('ready')), \
                patch.object(xcode_cache, 'refresh', side_effect=lambda *a: events.append('refresh')):
            self.assertEqual(host.main(['refresh-xcode', '--config', str(self.root / 'host.toml')]), 0)
        self.assertEqual(events, ['ready', 'lock', 'refresh'])

    def test_start_retries_busy_lock_without_interrupting_owner(self):
        with patch.object(host.fcntl, 'flock', side_effect=[BlockingIOError(), None]) as flock, \
                patch.object(host.time, 'sleep') as sleep:
            with host.locked(wait_timeout=10):
                pass
        self.assertEqual(flock.call_count, 2)
        sleep.assert_called_once_with(1)

    def test_default_lock_and_cli_deferral_remain_bounded(self):
        with patch.object(host.fcntl, 'flock', side_effect=BlockingIOError()):
            with self.assertRaises(host.HostBusy):
                with host.locked():
                    self.fail('busy lock acquired')
        with patch.object(host, 'load', return_value=self.spec), \
                patch.object(host, 'locked', side_effect=host.HostBusy('busy')) as lock:
            self.assertEqual(host.main(['start', '--config', str(self.root / 'host.toml')]), 2)
        lock.assert_called_once_with(wait_timeout=600)
