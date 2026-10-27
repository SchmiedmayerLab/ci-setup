# This source file is part of the macOS Self-Hosted GitHub Actions Runner open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
# SPDX-License-Identifier: MIT

"""Stable app identity, sealed bundle metadata and non-destructive migration."""

from contextlib import redirect_stdout
import io
from pathlib import Path
import plistlib
import subprocess
import sys
import tempfile
from unittest import TestCase
from unittest.mock import patch

from cisetup import background, host


class BackgroundTests(TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.source = self.root / 'source'
        (self.source / 'host').mkdir(parents=True)
        (self.source / 'host/runner-host.swift').write_text('synthetic test source')
        self.state = self.root / 'state'
        for target, value in [('ROOT', self.source), ('STATE', self.state)]:
            p = patch.object(host, target, value); p.start(); self.addCleanup(p.stop)
        p = patch.object(host, 'command', side_effect=self.command); self.commands = p.start(); self.addCleanup(p.stop)
        out = redirect_stdout(io.StringIO()); out.__enter__(); self.addCleanup(out.__exit__, None, None, None)

    def command(self, args, **kwargs):
        if str(args[0]) == '/usr/bin/xcrun':
            Path(args[-1]).write_bytes(b'synthetic executable')
        return ''

    def test_bundle_has_explicit_usage_descriptions_and_no_unsealed_root_receipt(self):
        background.ensure()
        app = background.app_path()
        self.assertEqual({p.name for p in app.iterdir()}, {'Contents'})
        info = plistlib.loads((app / 'Contents/Info.plist').read_bytes())
        self.assertIn('NSLocalNetworkUsageDescription', info)
        self.assertIn('NSAppleEventsUsageDescription', info)
        self.assertEqual(info['RunnerRoot'], str(self.source))
        self.assertEqual(background.verify(), background.executable())
        self.assertEqual((self.state / 'background-app.json').stat().st_mode & 0o777, 0o600)
        self.assertTrue(any('codesign' in str(c.args) and '--verify' in str(c.args) for c in self.commands.call_args_list))

    def test_app_from_another_checkout_is_not_retargeted(self):
        background.ensure()
        other = self.root / 'other'
        (other / 'host').mkdir(parents=True)
        (other / 'host/runner-host.swift').write_text('another synthetic source')
        with patch.object(host, 'ROOT', other), self.assertRaisesRegex(host.HostError, 'another checkout'):
            background.ensure()

    def test_same_identity_is_reused_without_recompiling(self):
        background.ensure(); self.commands.reset_mock(); background.ensure()
        self.assertFalse(any('swiftc' in str(c.args) for c in self.commands.call_args_list))

    def test_modified_binary_is_not_silently_adopted_or_rebuilt(self):
        background.ensure(); background.executable().write_bytes(b'changed')
        self.commands.reset_mock()
        with self.assertRaisesRegex(host.HostError, 'changed'): background.ensure()
        self.commands.assert_not_called()

    def test_reviewed_source_update_retains_previous_generated_bundle(self):
        background.ensure()
        (self.source / 'host/runner-host.swift').write_text('new synthetic source')
        background.ensure()
        self.assertEqual(len(list((self.state / 'background-app-versions').iterdir())), 1)
        background.verify()

    def test_no_app_uses_existing_python_path(self):
        self.assertEqual(background.arguments(['host', 'start']),
                         [sys.executable, str(self.source / 'bin/ci-setup'), 'host', 'start'])
        self.assertEqual(background.properties(), {})

    def test_prepared_app_is_the_shared_responsible_identity(self):
        background.ensure()
        self.assertEqual(background.arguments(['host', 'start']), [str(background.executable()), 'host', 'start'])
        self.assertEqual(background.properties(), {'AssociatedBundleIdentifiers': [background.BUNDLE_ID]})

    def test_network_gate_requires_actual_agent_success_and_cleans_up(self):
        background.ensure()
        def command(args, **kw):
            if 'print' in args: return 'state = not running\nlast exit code = 1\n'
            return ''
        with patch.object(host, 'command', side_effect=command), \
                patch.object(subprocess, 'run', return_value=subprocess.CompletedProcess([], 0)) as process:
            with self.assertRaisesRegex(host.HostError, 'consent probe failed'):
                background.request_network_access()
        self.assertIn('bootout', process.call_args.args[0])

    def legacy(self):
        path = self.root / 'host.toml'
        label = host.LABEL
        target = self.root / 'Library/LaunchAgents' / (label + '.plist')
        target.parent.mkdir(parents=True)
        data = {'Label': label, 'RunAtLoad': True,
                'ProgramArguments': [sys.executable, str(self.source / 'bin/ci-setup'), 'host', 'start', '--config', str(path)],
                'StandardOutPath': str(self.state / 'login.log'), 'StandardErrorPath': str(self.state / 'login.log')}
        target.write_bytes(plistlib.dumps(data))
        return path, target, data

    def test_migration_preserves_unknown_agent(self):
        background.ensure(); path, target, data = self.legacy()
        data['ProgramArguments'][0] = '/another/python'
        original = plistlib.dumps(data); target.write_bytes(original)
        with patch.object(Path, 'home', return_value=self.root), self.assertRaisesRegex(host.HostError, 'exact managed'):
            background.migrate_agents(path)
        self.assertEqual(target.read_bytes(), original)

    def test_migration_preserves_running_maintenance(self):
        background.ensure(); path, target, data = self.legacy()
        output = 'arguments = {\n' + '\n'.join(data['ProgramArguments']) + '\n}\nstate = running\n'
        original = target.read_bytes()
        with patch.object(Path, 'home', return_value=self.root), \
                patch.object(subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, output, '')):
            with self.assertRaisesRegex(host.HostError, 'maintenance is running'): background.migrate_agents(path)
        self.assertEqual(target.read_bytes(), original)

    def test_idle_exact_legacy_agent_migrates_with_backup(self):
        background.ensure(); path, target, data = self.legacy()
        original = target.read_bytes()
        with patch.object(Path, 'home', return_value=self.root), \
                patch.object(subprocess, 'run', return_value=subprocess.CompletedProcess([], 1, '', '')):
            background.migrate_agents(path)
        new = plistlib.loads(target.read_bytes())
        self.assertEqual(new['ProgramArguments'][0], str(background.executable()))
        self.assertEqual(list((self.state / 'agent-backups').iterdir())[0].read_bytes(), original)
