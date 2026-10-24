# This source file is part of the macOS Self-Hosted GitHub Actions Runner open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
# SPDX-License-Identifier: MIT

"""New layouts balance host memory and oversubscribed guest CPUs."""

import argparse
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from cisetup import host, install


class ResourcePolicyTests(unittest.TestCase):
    def test_default_memory_reserve_scales_and_is_bounded(self):
        for ram, per_guest in [(16, 4), (32, 12), (48, 18), (64, 24), (96, 40)]:
            with self.subTest(ram=ram):
                sizes = host.resource_sizes(ram, 18, 1862, 1700)
                self.assertEqual(sizes['memory_gib'], per_guest)
                self.assertEqual(sizes['cpus'], 12)
                self.assertLessEqual(2 * sizes['memory_gib'], ram - 8)

    def test_explicit_memory_and_cpu_overrides_remain_available(self):
        sizes = host.resource_sizes(64, 18, 1862, 1700, reserve_memory=8, cpu_ratio=1)
        self.assertEqual(sizes, {'cpus': 8, 'memory_gib': 28, 'disk_gib': 512})
        with self.assertRaises(host.HostError):
            host.resource_sizes(16, 18, 1862, 1700, reserve_memory=16)

    def test_generated_layout_records_overcommit_and_fixed_resources(self):
        for ratio, expected_cpus, overcommit in [(host.DEFAULT_CPU_RATIO, 12, True), (1, 8, False)]:
            with self.subTest(ratio=ratio), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                image = root / 'restore.ipsw'
                image.write_bytes(b'synthetic-image')
                target = root / 'host.toml'
                with patch.object(host.platform, 'system', return_value='Darwin'), \
                        patch.object(host.platform, 'machine', return_value='arm64'), \
                        patch.object(host, 'command', side_effect=[str(64 * 1024**3), '18']), \
                        patch.object(host.shutil, 'disk_usage', return_value=SimpleNamespace(total=1862 * 1024**3, free=1700 * 1024**3)):
                    host.init_config(target, image, ['Runner 1', 'Runner 2'], cpu_ratio=ratio)
                spec = host.load(target)
                self.assertEqual(spec['host']['cpu_overcommit'], overcommit)
                self.assertEqual([v['cpus'] for v in spec['vm']], [expected_cpus] * 2)
                self.assertEqual([v['memory_gib'] for v in spec['vm']], [24] * 2)

    def test_individual_init_passes_the_new_defaults(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(host, 'init_config') as init:
            self.assertEqual(host.main(['init', '--config', str(Path(directory) / 'new.toml'),
                                        '--name', 'Runner 1', '--name', 'Runner 2']), 0)
        self.assertIsNone(init.call_args.kwargs['reserve_memory'])
        self.assertEqual(init.call_args.kwargs['cpu_ratio'], 1.5)

    def test_saved_layout_is_not_resized_on_resume(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'host.toml'
            target.write_text('synthetic-existing-layout')
            spec = {'vm': [{'name': 'Runner 1', 'cpus': 4, 'memory_gib': 8}]}
            args = argparse.Namespace(config=target, image=None, name=None,
                                      reserve_memory_gib=None, cpu_ratio=1.5)
            with patch.object(host, 'load', return_value=spec), \
                    patch.object(install, 'check_host_layout'), patch.object(host, 'init_config') as init:
                _, result = install.host_plan(args)
            self.assertEqual(result, spec)
            self.assertEqual(target.read_text(), 'synthetic-existing-layout')
            init.assert_not_called()
