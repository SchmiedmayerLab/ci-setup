# This source file is part of the macOS Self-Hosted GitHub Actions Runner open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
# SPDX-License-Identifier: MIT

"""Host inspection must stay read-only; power changes need review and readback."""

from contextlib import redirect_stdout
import io
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from cisetup import host, host_readiness as readiness


def result(text='', code=0):
    return subprocess.CompletedProcess([], code, stdout=text, stderr='')


class HostReadinessTests(unittest.TestCase):
    def setUp(self):
        self.out = io.StringIO()
        redirect = redirect_stdout(self.out)
        redirect.__enter__()
        self.addCleanup(redirect.__exit__, None, None, None)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.config = self.root / 'host.toml'
        for target, kwargs in (
            ('host.STATE', {'new': self.root / 'state'}),
            ('subprocess.run', {'side_effect': AssertionError('unexpected command')}),
            ('Path.home', {'return_value': self.root}),
        ):
            patcher = patch('cisetup.host_readiness.' + target, **kwargs)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_ac_values_do_not_come_from_battery_or_ups(self):
        text = 'Battery Power:\n sleep 5\n autorestart 0\nAC Power:\n sleep 0\n autorestart 1\nUPS Power:\n sleep 3\n'
        with patch.object(readiness, 'probe', return_value=result(text)):
            self.assertEqual(readiness.ac_settings(), {'sleep': '0', 'autorestart': '1'})

    def test_unreadable_missing_or_ambiguous_power_never_passes(self):
        for response in (result('', 1), result(''), result('Battery Power:\n sleep 0\n'),
                         result('AC Power:\n sleep invalid\n'), result('AC Power:\n sleep 0\n autorestart invalid\n'),
                         result('AC Power:\n sleep 0\n autorestart 2\n'), result('AC Power:\n sleep 0\n sleep 1\n')):
            with self.subTest(response=response), patch.object(readiness, 'probe', return_value=response):
                with self.assertRaises(host.HostError):
                    readiness.ac_settings()

    def test_unknown_power_recovery_is_not_invented(self):
        with patch.object(readiness, 'probe', return_value=result('AC Power:\n sleep 0\n')):
            self.assertEqual(readiness.ac_settings(), {'sleep': '0'})

    def test_utm_preference_is_only_needed_when_system_sleep_enabled(self):
        with patch.object(readiness, 'probe') as probe:
            self.assertEqual(readiness.sleep_check({'sleep': '0'})[0], 'OK')
            probe.assert_not_called()
        for response, expected in ((result('1'), 'OK'), (result('0'), 'ATTENTION'),
                                   (result('', 1), 'ATTENTION'), (result('garbage'), 'ATTENTION')):
            with self.subTest(response=response), patch.object(readiness, 'probe', return_value=response):
                self.assertEqual(readiness.sleep_check({'sleep': '10'})[0], expected)

    def test_default_is_preview_and_decline_never_writes(self):
        with patch.object(readiness, 'ac_settings', return_value={'sleep': '10', 'autorestart': '0'}), \
             patch.object(sys.stdin, 'isatty', return_value=True), patch('builtins.input', return_value='n') as prompt:
            self.assertTrue(readiness.configure_power())
            prompt.assert_not_called()
            self.assertFalse(readiness.configure_power(apply=True))
        self.assertIn('Restore previous values: /usr/bin/sudo /usr/bin/pmset -c sleep 10 autorestart 0', self.out.getvalue())

    def test_apply_requires_terminal(self):
        with patch.object(readiness, 'ac_settings', return_value={'sleep': '10'}), \
             patch.object(sys.stdin, 'isatty', return_value=False), self.assertRaises(host.HostError):
            readiness.configure_power(apply=True)

    def test_apply_changes_only_supported_ac_settings_then_verifies(self):
        before = {'sleep': '10', 'autorestart': '0'}
        with patch.object(readiness, 'ac_settings', side_effect=[before, before, {'sleep': '0', 'autorestart': '1'}]), \
             patch.object(sys.stdin, 'isatty', return_value=True), patch('builtins.input', return_value='y'), \
             patch.object(subprocess, 'run', return_value=result()) as run:
            self.assertTrue(readiness.configure_power(apply=True))
        run.assert_called_once_with(['/usr/bin/sudo', '/usr/bin/pmset', '-c', 'sleep', '0', 'autorestart', '1'],
                                    check=True, timeout=120)

    def test_unsupported_restart_is_not_applied_and_rerun_is_noop(self):
        with patch.object(readiness, 'ac_settings', side_effect=[{'sleep': '10'}, {'sleep': '10'}, {'sleep': '0'}]), \
             patch.object(sys.stdin, 'isatty', return_value=True), patch('builtins.input', return_value='y'), \
             patch.object(subprocess, 'run', return_value=result()) as run:
            self.assertTrue(readiness.configure_power(apply=True))
        self.assertEqual(run.call_args.args[0], ['/usr/bin/sudo', '/usr/bin/pmset', '-c', 'sleep', '0'])
        with patch.object(readiness, 'ac_settings', return_value={'sleep': '0'}), patch('builtins.input') as prompt:
            self.assertTrue(readiness.configure_power(apply=True))
            prompt.assert_not_called()

    def test_changed_settings_after_confirmation_abort(self):
        with patch.object(readiness, 'ac_settings', side_effect=[{'sleep': '10'}, {'sleep': '20'}]), \
             patch.object(sys.stdin, 'isatty', return_value=True), patch('builtins.input', return_value='y'), \
             self.assertRaisesRegex(host.HostError, 'changed during review'):
            readiness.configure_power(apply=True)

    def test_apply_failure_and_readback_mismatch_are_not_success(self):
        for error in (None, subprocess.CalledProcessError(1, 'sudo'), subprocess.TimeoutExpired('sudo', 120)):
            with self.subTest(error=error), \
                 patch.object(readiness, 'ac_settings', return_value={'sleep': '10'}), \
                 patch.object(sys.stdin, 'isatty', return_value=True), patch('builtins.input', return_value='y'), \
                 patch.object(subprocess, 'run', side_effect=error, return_value=result()), self.assertRaises(host.HostError):
                readiness.configure_power(apply=True)

    def agent(self, action='start'):
        label = host.LABEL if action == 'start' else readiness.REFRESH
        path = self.root / 'Library/LaunchAgents' / (label + '.plist')
        path.parent.mkdir(parents=True, exist_ok=True)
        args = [sys.executable, str(host.ROOT / 'bin/ci-setup'), 'host', action, '--config', str(self.config)]
        definition = {'Label': label, 'RunAtLoad': True, 'ProgramArguments': args}
        if action == 'refresh-xcode':
            definition['StartInterval'] = 21600
        path.write_bytes(plistlib.dumps(definition))
        return label, path, definition

    def loaded(self, definition, suffix='last exit code = 0'):
        schedule = 'properties = runatload | inferred program\n'
        if 'StartInterval' in definition:
            schedule += f'run interval = {definition["StartInterval"]} seconds\n'
        return result('arguments = {\n' + '\n'.join(definition['ProgramArguments']) + '\n}\n' + schedule + suffix + '\n')

    def test_both_agents_are_checked_against_saved_and_loaded_definitions(self):
        for action in ('start', 'refresh-xcode'):
            label, path, definition = self.agent(action)
            with patch.object(readiness, 'probe', return_value=self.loaded(definition)) as probe:
                self.assertEqual(readiness.agent_check(label, action, self.config, '')[0], 'OK')
            self.assertEqual(probe.call_args.args[0], ['/bin/launchctl', 'print', f'gui/{os.getuid()}/{label}'])

    def test_missing_malformed_disabled_or_misdirected_agent_is_rejected(self):
        self.assertEqual(readiness.agent_check(host.LABEL, 'start', self.config, '')[0], 'ATTENTION')
        for mutation in (lambda d: d.update(RunAtLoad=False), lambda d: d.update(Disabled=True),
                         lambda d: d.update(Label='other'), lambda d: d['ProgramArguments'].__setitem__(0, '/missing/python'),
                         lambda d: d['ProgramArguments'].__setitem__(-1, '/other/config')):
            label, path, definition = self.agent()
            mutation(definition)
            path.write_bytes(plistlib.dumps(definition))
            self.assertEqual(readiness.agent_check(label, 'start', self.config, '')[0], 'ATTENTION')
        path.write_text('invalid plist')
        self.assertEqual(readiness.agent_check(label, 'start', self.config, '')[0], 'ATTENTION')

    def test_refresh_interval_and_launchd_disabled_override_are_checked(self):
        label, path, definition = self.agent('refresh-xcode')
        for disabled in (None, f'"{label}" => true'):
            self.assertEqual(readiness.agent_check(label, 'refresh-xcode', self.config, disabled)[0], 'ATTENTION')
        definition['StartInterval'] = 30
        path.write_bytes(plistlib.dumps(definition))
        self.assertEqual(readiness.agent_check(label, 'refresh-xcode', self.config, '')[0], 'ATTENTION')

    def test_failed_stale_unloaded_or_never_run_agent_is_reported(self):
        label, path, definition = self.agent()
        responses = [result('', 1), result(''), self.loaded(definition, 'last exit code = 1'),
                     self.loaded(definition, 'last exit code = (never exited)')]
        stale = dict(definition, ProgramArguments=[*definition['ProgramArguments'][:-1], '/other/config'])
        responses.append(self.loaded(stale))
        for response in responses:
            with self.subTest(response=response), patch.object(readiness, 'probe', return_value=response):
                self.assertEqual(readiness.agent_check(label, 'start', self.config, '')[0], 'ATTENTION')
        with patch.object(readiness, 'probe', return_value=self.loaded(definition, 'state = running')):
            self.assertEqual(readiness.agent_check(label, 'start', self.config, '')[0], 'OK')

    def test_loaded_schedule_must_match_saved_agent(self):
        label, path, definition = self.agent('refresh-xcode')
        good = self.loaded(definition).stdout
        for bad in (good.replace('21600 seconds', '30 seconds'), good.replace('runatload', 'other')):
            with patch.object(readiness, 'probe', return_value=result(bad)):
                self.assertEqual(readiness.agent_check(label, 'refresh-xcode', self.config, '')[0], 'ATTENTION')

    def test_failed_probes_are_reported_without_raw_output(self):
        for error in (OSError('private'), subprocess.TimeoutExpired('command', 15)):
            with patch.object(subprocess, 'run', side_effect=error), self.assertRaises(host.HostError) as caught:
                readiness.probe(['/bin/launchctl', 'print'])
            self.assertNotIn('private', str(caught.exception))

    def test_check_dispatch_has_no_bootstrap_utm_or_credential_access(self):
        with patch.object(readiness, 'ac_settings', return_value={'sleep': '0', 'autorestart': '1'}), \
             patch.object(host, 'load', return_value={}), patch.object(readiness, 'probe', return_value=result()), \
             patch.object(readiness, 'agent_check', return_value=('OK', 'agent', 'loaded')), \
             patch.object(host, 'bootstrap', side_effect=AssertionError('bootstrap')), \
             patch.object(host, 'inventory', side_effect=AssertionError('UTM')), \
             patch.object(host, 'runner_credentials', side_effect=AssertionError('credentials')):
            self.assertEqual(host.main(['check', '--config', str(self.config)]), 0)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_check_collects_failures_and_returns_nonzero(self):
        with patch.object(readiness, 'ac_settings', side_effect=host.HostError('unreadable')), \
             patch.object(host, 'load', side_effect=host.HostError('invalid config')), \
             patch.object(readiness, 'probe', side_effect=host.HostError('unavailable')):
            self.assertEqual(readiness.check(self.config), 1)
        self.assertIn('Configuration: missing or invalid', self.out.getvalue())
        self.assertIn('Power settings: unreadable', self.out.getvalue())
        self.assertIn('missing or unreadable agent', self.out.getvalue())

    def test_power_dispatch_needs_no_host_configuration(self):
        with patch.object(readiness, 'configure_power', return_value=True) as power, \
             patch.object(host, 'load', side_effect=AssertionError('config read')):
            self.assertEqual(host.main(['power']), 0)
            power.assert_called_once_with(apply=False)


if __name__ == '__main__':
    unittest.main()
