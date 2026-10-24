# This source file is part of the macOS Self-Hosted GitHub Actions Runner open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
# SPDX-License-Identifier: MIT

"""Host credential isolation, cache integrity and guest archive validation."""

import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch, MagicMock

from cisetup import host, xcode, xcode_cache
from cisetup.config import Config
from cisetup.util import SetupError


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.addCleanup(patch.stopall)
        patch.object(host, 'STATE', self.root).start()
        self.command = patch.object(xcode_cache, 'command', side_effect=self.fake_command).start()
        patch.object(xcode_cache.shutil, 'disk_usage', return_value=MagicMock(free=100 * 1024 ** 3)).start()
        self.cfg = Config(repo_root=self.root)

    def fake_command(self, args, **kwargs):
        if args[0] == 'list':
            return '27.1 (A)\n27.2 Release Candidate (B)\n27.3 Beta (C)\n'
        if args[0] == 'download':
            (Path(args[-1]) / 'archive.xip').write_bytes(b'archive-' + args[1].encode())
        return ''

    def test_downloads_each_build_once_and_rechecks_digest(self):
        first = xcode_cache.prepare(self.cfg)
        second = xcode_cache.prepare(self.cfg)
        self.assertEqual(first, second)
        downloads = [call for call in self.command.call_args_list if call.args[0][0] == 'download']
        self.assertEqual(len(downloads), 1)
        Path(first['archives'][0]['path']).write_bytes(b'corrupt')
        with self.assertRaisesRegex(host.HostError, 'verification'):
            xcode_cache.prepare(self.cfg)
        self.assertEqual(Path(first['archives'][0]['path']).read_bytes(), b'corrupt')

    def test_promoted_rc_reuses_the_same_verified_build(self):
        initial = xcode_cache.prepare(self.cfg)
        def promoted(args, **kwargs):
            if args[0] == 'list':
                return '27.1 (A)\n27.2 (B)\n'
            return self.fake_command(args, **kwargs)
        self.command.side_effect = promoted
        final = xcode_cache.prepare(self.cfg)
        self.assertEqual(final['archives'][0]['identifier'], '27.2')
        self.assertEqual(final['archives'][0]['sha256'], initial['archives'][0]['sha256'])
        downloads = [call for call in self.command.call_args_list if call.args[0][0] == 'download']
        self.assertEqual(len(downloads), 1)

    def test_unrecorded_download_is_not_adopted_or_removed(self):
        directory = self.root / 'xcode-cache/B'
        directory.mkdir(parents=True)
        archive = directory / 'archive.xip'
        archive.write_bytes(b'interrupted')
        with self.assertRaisesRegex(host.HostError, 'Unrecorded'):
            xcode_cache.prepare(self.cfg)
        self.assertEqual(archive.read_bytes(), b'interrupted')

    def test_stage_contains_only_archives_and_metadata(self):
        manifest = xcode_cache.prepare(self.cfg)
        connection = MagicMock()
        xcode_cache.stage(connection, manifest, '/guest/staging')
        self.assertEqual(connection.upload_file.call_count, 1)
        transferred = json.loads(connection.upload.call_args.args[0])
        self.assertEqual(set(transferred), {'schema', 'archives'})
        self.assertEqual(set(transferred['archives'][0]), {'identifier', 'build', 'path', 'sha256'})
        self.assertTrue(all(e['path'].startswith('/guest/staging/') for e in transferred['archives']))

    def test_login_uses_host_terminal_and_never_captures_credentials(self):
        with patch.object(xcode_cache.sys.stdin, 'isatty', return_value=True), \
                patch.object(xcode_cache, 'cli', return_value='/host/xcodes'), \
                patch.object(xcode_cache.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0)) as run:
            xcode_cache.login()
        self.assertEqual(run.call_args.args[0], ['/host/xcodes', 'update', '--data-source', 'apple'])
        self.assertFalse(run.call_args.kwargs)

    def test_login_rejects_noninteractive_session(self):
        with patch.object(xcode_cache.sys.stdin, 'isatty', return_value=False), \
                self.assertRaisesRegex(host.HostError, 'your terminal'):
            xcode_cache.login()

    def test_manifest_restricts_paths_and_checks_content(self):
        directory = self.root / 'Library/Application Support/runner-bootstrap'
        directory.mkdir(parents=True)
        archive = directory / 'Xcode-A.xip'
        archive.write_bytes(b'archive')
        item = {'identifier': '27.1', 'build': 'A', 'path': str(archive), 'sha256': xcode_cache.digest(archive)}
        manifest = directory / 'xcode-archives.json'
        manifest.write_text(json.dumps({'schema': 1, 'archives': [item]}))
        with patch.object(Path, 'home', return_value=self.root):
            releases, archives = xcode.host_archives()
            self.assertEqual(releases[0].identifier, '27.1')
            self.assertEqual(xcode.verified_archive(archives['27.1']), archive)
            archive.write_bytes(b'changed')
            with self.assertRaisesRegex(SetupError, 'checksum'):
                xcode.verified_archive(archives['27.1'])
            item['path'] = str(self.root / 'outside.xip')
            manifest.write_text(json.dumps({'schema': 1, 'archives': [item]}))
            with self.assertRaisesRegex(SetupError, 'Invalid host'):
                xcode.host_archives()

    def test_refresh_tries_second_guest_after_first_failure(self):
        first, second = MagicMock(username='runner'), MagicMock(username='runner')
        first.password.return_value = second.password.return_value = 'test-password'
        first.logged.side_effect = host.HostError('failure')
        with patch('cisetup.config.load', return_value=self.cfg), \
                patch.object(host, 'read_state', return_value={'vms': {'one': {'runner_revision': 'test'}}}), \
                patch('cisetup.guests.connections', return_value=[({'name': 'one'}, first), ({'name': 'two'}, second)]), \
                patch.object(xcode_cache, 'stage'):
            with self.assertRaisesRegex(host.HostError, 'one'):
                xcode_cache.refresh({}, self.root / 'state')
        second.logged.assert_called_once()

    def test_staging_failure_is_not_reported_as_guest_converge_failure(self):
        first = MagicMock(username='runner')
        with patch('cisetup.config.load', return_value=self.cfg), \
                patch.object(host, 'read_state', return_value={'vms': {'one': {'runner_revision': 'test'}}}), \
                patch('cisetup.guests.connections', return_value=[({'name': 'one'}, first)]), \
                patch.object(xcode_cache, 'stage', side_effect=host.HostError('private-test-secret')), \
                patch('builtins.print') as output:
            with self.assertRaisesRegex(host.HostError, 'archive staging') as caught:
                xcode_cache.refresh({}, self.root / 'state')
        first.logged.assert_not_called()
        self.assertNotIn('private-test-secret', str(caught.exception))
        self.assertNotIn('private-test-secret', str(output.call_args_list))

    def test_busy_guest_is_deferred_and_other_guest_still_refreshes(self):
        first, second = MagicMock(username='runner'), MagicMock(username='runner')
        first.logged.side_effect = host.HostBusy('synthetic private detail')
        with patch('cisetup.config.load', return_value=self.cfg), \
                patch.object(host, 'read_state', return_value={'vms': {'one': {'runner_revision': 'test'}}}), \
                patch('cisetup.guests.connections', return_value=[({'name': 'one'}, first), ({'name': 'two'}, second)]), \
                patch.object(xcode_cache, 'stage'):
            with self.assertRaisesRegex(host.HostBusy, 'deferred in: one') as caught:
                xcode_cache.refresh({}, self.root / 'state')
        second.logged.assert_called_once()
        self.assertTrue(first.logged.call_args.kwargs['defer_on_busy'])
        self.assertNotIn('synthetic private', str(caught.exception))

    def test_real_failure_is_not_hidden_by_another_guests_deferral(self):
        first, second = MagicMock(username='runner'), MagicMock(username='runner')
        first.logged.side_effect = host.HostBusy('busy')
        second.logged.side_effect = host.HostError('failed')
        with patch('cisetup.config.load', return_value=self.cfg), \
                patch.object(host, 'read_state', return_value={'vms': {'one': {'runner_revision': 'test'}}}), \
                patch('cisetup.guests.connections', return_value=[({'name': 'one'}, first), ({'name': 'two'}, second)]), \
                patch.object(xcode_cache, 'stage'):
            with self.assertRaisesRegex(host.HostError, 'two') as caught:
                xcode_cache.refresh({}, self.root / 'state')
        self.assertNotIsInstance(caught.exception, host.HostBusy)

    def test_guest_installs_from_archive_without_apple_discovery(self):
        release = xcode.Release((27, 2), ('rc', 1), '27.2 Release Candidate', 'B')
        stable = xcode.Release((27, 1), (), '27.1', 'A')
        local = self.root / 'archive.xip'
        local.write_bytes(b'archive')
        installed = [xcode.InstalledXcode(stable.identifier, 'A', '/Applications/Stable.app'),
                     xcode.InstalledXcode(release.identifier, 'B', '/Applications/RC.app')]
        catalog = ([stable, release], {release.identifier: (local, xcode_cache.digest(local))})
        with patch.object(xcode, 'host_archives', return_value=catalog), \
                patch.object(xcode.util, 'runner_busy', return_value=False), \
                patch.object(xcode.shutil, 'which', return_value='/xcodes'), \
                patch.object(xcode, 'output', return_value='') as output, \
                patch.object(xcode, 'parse_installed', side_effect=[installed[:1], installed]), \
                patch.object(xcode, 'run') as run, \
                patch.object(xcode, '_post_install'), \
                patch.object(xcode, '_ensure_global_selection'), \
                patch.object(xcode, '_remove_unwanted_xcodes', return_value=installed), \
                patch.object(xcode, '_cleanup_runtimes'), \
                patch.object(xcode, 'runner_developer_dir', return_value=None):
            selected = xcode.ensure(self.cfg)
        self.assertEqual(selected, Path('/Applications/RC.app/Contents/Developer'))
        self.assertTrue(all(call.args[0] == ['xcodes', 'installed'] for call in output.call_args_list))
        self.assertEqual(run.call_count, 1)
        self.assertIn('--path', run.call_args.args[0])
        self.assertIn(str(local), run.call_args.args[0])
