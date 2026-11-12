# This source file is part of the macOS Self-Hosted GitHub Actions Runner open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
# SPDX-License-Identifier: MIT

"""Apple discovery, pinned downloads and rejection before VM installation."""

from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from cisetup import host, restore


class RestoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.info = {'url': 'https://updates.cdn-apple.com/example/restore.ipsw',
                     'version': '27.0.1', 'build': '26A434', 'minimum_cpus': 2,
                     'minimum_memory_bytes': 4 * 1024**3}
        for patcher in [patch.object(host, 'STATE', self.root),
                        patch.object(restore.shutil, 'disk_usage', return_value=SimpleNamespace(free=100 * 1024**3, total=1024 * 1024**3))]:
            patcher.start()
            self.addCleanup(patcher.stop)
        context = redirect_stdout(io.StringIO())
        context.__enter__()
        self.addCleanup(context.__exit__, None, None, None)

    def metadata_command(self, args, **kwargs):
        if args[0] in ('/usr/bin/xcrun', '/usr/bin/codesign'):
            return ''
        return json.dumps(self.info)

    def download_command(self, args, **kwargs):
        self.assertEqual(args[0], '/usr/bin/curl')
        self.assertIn('--max-time', args)
        output = Path(args[args.index('--output') + 1])
        self.assertTrue(output.name.endswith('.partial.ipsw'))
        output.write_bytes(b'test restore image')
        return self.info['url']

    def test_metadata_uses_only_temporary_entitled_helper(self):
        with patch.object(host, 'command', side_effect=self.metadata_command) as command:
            self.assertEqual(restore.metadata(), self.info)
        calls = command.call_args_list
        self.assertIn('swiftc', calls[0].args[0])
        self.assertIn('--entitlements', calls[1].args[0])
        helper = calls[2].args[0][0]
        self.assertFalse(helper.exists())

    def test_discovery_rejects_old_images_before_download(self):
        self.info['version'] = '26.1.0'
        with patch.object(host, 'command', side_effect=self.metadata_command):
            with self.assertRaisesRegex(host.HostError, 'macOS 27'):
                restore.metadata()

    def test_discovery_rejects_untrusted_url_and_incomplete_metadata(self):
        for field, value in [('url', 'https://example.com/restore.ipsw'),
                             ('build', '../outside'), ('minimum_cpus', True)]:
            with self.subTest(field=field):
                data = dict(self.info, **{field: value})
                with patch.object(host, 'command', side_effect=['', '', json.dumps(data)]):
                    with self.assertRaises(host.HostError):
                        restore.metadata()

    def test_download_is_pinned_and_reused_without_network_transfer(self):
        with patch.object(host, 'command', side_effect=self.download_command) as command, \
             patch.object(restore, 'metadata', return_value=self.info):
            path = restore.download(self.info)
            self.assertEqual(restore.download(self.info), path)
        self.assertEqual(command.call_count, 1)
        saved = json.loads(path.with_suffix('.json').read_text())
        self.assertEqual(saved['sha256'], host.digest(path))
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(saved['url'], self.info['url'])

    def test_corrupted_cache_is_not_repinned_or_redownloaded(self):
        with patch.object(host, 'command', side_effect=self.download_command), \
             patch.object(restore, 'metadata', return_value=self.info):
            path = restore.download(self.info)
        original_receipt = path.with_suffix('.json').read_bytes()
        path.write_bytes(b'changed')
        with patch.object(host, 'command') as command, self.assertRaisesRegex(host.HostError, 'verification'):
            restore.download(self.info)
        command.assert_not_called()
        self.assertEqual(path.with_suffix('.json').read_bytes(), original_receipt)

    def test_wrong_build_does_not_publish_download(self):
        with patch.object(host, 'command', side_effect=self.download_command), \
             patch.object(restore, 'metadata', return_value=dict(self.info, build='Different')):
            with self.assertRaisesRegex(host.HostError, 'differs'):
                restore.download(self.info)
        cache = self.root / 'restore-images'
        self.assertFalse((cache / '26A434.ipsw').exists())
        self.assertEqual(len(list(cache.glob('*.partial.ipsw'))), 1)

    def test_insufficient_disk_space_stops_before_transfer(self):
        with patch.object(restore.shutil, 'disk_usage', return_value=SimpleNamespace(free=32 * 1024**3)), \
             patch.object(host, 'command') as command:
            with self.assertRaisesRegex(host.HostError, '64 GiB'):
                restore.download(self.info)
        command.assert_not_called()

    def test_cancelled_plan_does_not_download_or_write_configuration(self):
        from cisetup.install import SetupCancelled
        output = self.root / 'host.toml'
        with patch.object(host.platform, 'system', return_value='Darwin'), \
             patch.object(host.platform, 'machine', return_value='arm64'), \
             patch.object(host, 'command', side_effect=[str(64 * 1024**3), '16']), \
             patch.object(host, 'resource_sizes', return_value={'cpus': 4, 'memory_gib': 8, 'disk_gib': 256}), \
             patch.object(restore, 'metadata', return_value=self.info), \
             patch.object(restore, 'download') as download:
            def cancel(vms):
                self.assertEqual(vms[0]['name'], 'Runner 1')
                raise SetupCancelled()
            with self.assertRaises(SetupCancelled):
                host.init_config(output, None, ['Runner 1'], confirm=cancel)
        download.assert_not_called()
        self.assertFalse(output.exists())

    def test_unrecorded_file_is_not_overwritten(self):
        cache = self.root / 'restore-images'
        cache.mkdir()
        path = cache / '26A434.ipsw'
        path.write_bytes(b'keep')
        with patch.object(host, 'command') as command, self.assertRaisesRegex(host.HostError, 'Unrecorded'):
            restore.download(self.info)
        command.assert_not_called()
        self.assertEqual(path.read_bytes(), b'keep')

    def test_redirect_to_non_apple_host_is_not_published(self):
        def redirect(args, **kwargs):
            self.download_command(args, **kwargs)
            return 'https://example.com/restore.ipsw'
        with patch.object(host, 'command', side_effect=redirect), patch.object(restore, 'metadata') as metadata:
            with self.assertRaisesRegex(host.HostError, 'download from'):
                restore.download(self.info)
        metadata.assert_not_called()
        self.assertFalse((self.root / 'restore-images/26A434.ipsw').exists())

    def test_hardware_minimums_apply_to_each_guest(self):
        vms = [{'name': 'Runner 1', 'cpus': 4, 'memory_gib': 8},
               {'name': 'Runner 2', 'cpus': 1, 'memory_gib': 8}]
        with self.assertRaisesRegex(host.HostError, 'Runner 2'):
            restore.requirements(self.info, vms)

    def test_local_override_does_not_discover_or_download(self):
        local = self.root / 'local.ipsw'
        local.write_bytes(b'local image')
        output = self.root / 'host.toml'
        with patch.object(host.platform, 'system', return_value='Darwin'), \
             patch.object(host.platform, 'machine', return_value='arm64'), \
             patch.object(host, 'command', side_effect=[str(64 * 1024**3), '16']), \
             patch.object(host, 'resource_sizes', return_value={'cpus': 4, 'memory_gib': 8, 'disk_gib': 256}), \
             patch.object(restore, 'metadata') as metadata, patch.object(restore, 'download') as download:
            host.init_config(output, local, ['Runner 1'])
        metadata.assert_not_called()
        download.assert_not_called()
        self.assertEqual(host.load(output)['image']['sha256'], host.digest(local))

    def test_automatic_init_persists_apple_url_and_checksum(self):
        local = self.root / 'downloaded.ipsw'
        local.write_bytes(b'downloaded image')
        output = self.root / 'host.toml'
        with patch.object(host.platform, 'system', return_value='Darwin'), \
             patch.object(host.platform, 'machine', return_value='arm64'), \
             patch.object(host, 'command', side_effect=[str(64 * 1024**3), '16']), \
             patch.object(host, 'resource_sizes', return_value={'cpus': 4, 'memory_gib': 8, 'disk_gib': 256}), \
             patch.object(restore, 'metadata', return_value=self.info), \
             patch.object(restore, 'download', return_value=local):
            host.init_config(output, None, ['Runner 1'])
        image = host.load(output)['image']
        self.assertEqual(image['url'], self.info['url'])
        self.assertEqual(image['sha256'], host.digest(local))


if __name__ == '__main__':
    unittest.main()
