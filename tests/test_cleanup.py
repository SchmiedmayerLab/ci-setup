# This source file is part of the macOS Self-Hosted GitHub Actions Runner open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
# SPDX-License-Identifier: MIT

"""Exercise deletion only in temporary fixtures; never call host simulator tools."""

from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from cisetup import cleanup
from cisetup.config import Config
from cisetup.util import SetupError


class CleanupTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.home = self.root / 'home'
        self.runner = self.home / 'runner'
        self.work = self.runner / '_work'
        self.pipeline = self.work / 'project'
        self.workspace = self.pipeline / 'project'
        self.workspace.mkdir(parents=True)
        self.cfg = Config(repo_root=self.home / 'setup', runner_dir=self.runner, work_dir='_work')
        self.metadata = self.runner / '.runner'
        self.metadata.write_text(json.dumps({'workFolder': '_work'}))
        self.env_file = self.work / '_temp/env'
        self.env_file.parent.mkdir()
        self.env_file.write_text('existing=true\n')
        self.outside = self.root / 'outside'
        self.outside.mkdir()
        (self.outside / 'keep').write_text('keep')
        self.cache = self.home / 'Library/Caches/com.github.peripheryapp'
        self.cache.mkdir(parents=True)
        self.output = io.StringIO()
        for context in (redirect_stdout(self.output), redirect_stderr(self.output),
                        patch.object(cleanup, 'home_directory', return_value=self.home),
                        patch.dict(os.environ, {'GITHUB_ACTIONS': 'true', 'RUNNER_ENVIRONMENT': 'self-hosted',
                                                'CI_SETUP_WORK_DIR': str(self.work), 'RUNNER_WORKSPACE': str(self.pipeline),
                                                'GITHUB_WORKSPACE': str(self.workspace), 'GITHUB_ENV': str(self.env_file)})):
            context.__enter__()
            self.addCleanup(context.__exit__, None, None, None)

    def test_removes_hidden_and_stale_job_data_but_keeps_current_workspace_and_control_files(self):
        (self.work / '.hidden').write_text('old')
        (self.workspace / '.git').mkdir()
        (self.workspace / '.git/config').write_text('old')
        (self.work / 'previous-job').mkdir()
        (self.work / 'previous-job/build').write_text('old')
        (self.cache / '.hidden-cache').write_text('old')
        for name in cleanup.CONTROL_DIRS:
            (self.work / name).mkdir(exist_ok=True)
            (self.work / name / 'keep').write_text('control')
        cleanup.clean_files(self.cfg)
        self.assertTrue(self.workspace.is_dir())
        self.assertEqual(list(self.workspace.iterdir()), [])
        self.assertFalse((self.work / '.hidden').exists())
        self.assertFalse((self.work / 'previous-job').exists())
        self.assertEqual(list(self.cache.iterdir()), [])
        for name in cleanup.CONTROL_DIRS:
            self.assertEqual((self.work / name / 'keep').read_text(), 'control')
        self.assertEqual(self.env_file.read_text(), 'existing=true\n')
        cleanup.clean_files(self.cfg)  # Retrying after success is harmless.

    def test_symlinks_in_job_contents_are_unlinked_without_visiting_targets(self):
        (self.workspace / 'outside').symlink_to(self.outside, target_is_directory=True)
        (self.workspace / '.file-link').symlink_to(self.outside / 'keep')
        (self.workspace / 'broken').symlink_to(self.outside / 'missing')
        cleanup.clean_files(self.cfg)
        self.assertEqual(list(self.workspace.iterdir()), [])
        self.assertEqual((self.outside / 'keep').read_text(), 'keep')

    def test_dangerous_configured_paths_are_rejected(self):
        for folder in ('.', '/', str(self.home), str(self.outside), '../outside', 'bin', 'externals',
                       '_diag', 'BIN', 'Externals.2.0', '_DIAG', '.credentials', 'bin.2.0',
                       '_work/../../outside', '_work\nOTHER=value'):
            with self.subTest(folder=folder), self.assertRaises(SetupError):
                cleanup.work_path(replace(self.cfg, work_dir=folder))
        for root in (Path('/'), self.home, self.root):
            with self.subTest(root=root), self.assertRaises(SetupError):
                cleanup.work_path(replace(self.cfg, runner_dir=root))
        with self.assertRaises(SetupError):
            cleanup.work_path(replace(self.cfg, repo_root=self.workspace / 'setup'))
        self.assertEqual((self.outside / 'keep').read_text(), 'keep')

    def test_symlinked_work_path_or_parent_is_rejected(self):
        (self.runner / 'linked').symlink_to(self.outside, target_is_directory=True)
        for folder in ('linked', 'linked/nested'):
            with self.subTest(folder=folder), self.assertRaises(SetupError):
                cleanup.work_path(replace(self.cfg, work_dir=folder))

    def test_registration_and_job_environment_must_agree(self):
        for key, value in [('CI_SETUP_WORK_DIR', str(self.outside)), ('RUNNER_WORKSPACE', str(self.outside)),
                           ('GITHUB_WORKSPACE', str(self.outside)), ('RUNNER_ENVIRONMENT', 'github-hosted'),
                           ('GITHUB_ACTIONS', 'false')]:
            with self.subTest(key=key), patch.dict(os.environ, {key: value}), self.assertRaises(SetupError):
                cleanup.clean_files(self.cfg)
        self.metadata.write_text(json.dumps({'workFolder': 'other'}))
        with self.assertRaisesRegex(SetupError, 'registered workspace'):
            cleanup.clean_files(self.cfg)
        self.assertTrue(self.workspace.exists())

    def test_registration_symlink_and_nonregular_metadata_are_rejected(self):
        self.metadata.unlink()
        self.metadata.symlink_to(self.outside / 'keep')
        with self.assertRaises(OSError):
            cleanup.context(self.cfg)
        self.metadata.unlink()
        os.mkfifo(self.metadata)
        with self.assertRaisesRegex(SetupError, 'regular file'):
            cleanup.context(self.cfg)

    def test_current_workspace_symlink_is_rejected_before_deletion(self):
        self.workspace.rmdir()
        self.workspace.symlink_to(self.outside, target_is_directory=True)
        (self.work / 'old').write_text('retain')
        with self.assertRaises(OSError):
            cleanup.clean_files(self.cfg)
        self.assertEqual((self.work / 'old').read_text(), 'retain')
        self.assertEqual((self.outside / 'keep').read_text(), 'keep')

    def test_control_directories_are_protected_on_case_insensitive_volumes(self):
        actions = self.work / '_Actions'
        actions.mkdir()
        (actions / 'keep').write_text('action')
        cleanup.clean_files(self.cfg)
        self.assertEqual((actions / 'keep').read_text(), 'action')
        with patch.dict(os.environ, RUNNER_WORKSPACE=str(self.work / '_TEMP'),
                        GITHUB_WORKSPACE=str(self.work / '_TEMP/repo')), self.assertRaises(SetupError):
            cleanup.context(self.cfg)
        self.assertTrue(self.env_file.exists())

    def test_current_directory_is_kept_when_filesystem_preserves_an_older_capitalization(self):
        actual = self.workspace / 'CaseSensitiveName'
        actual.mkdir()
        (actual / 'old').write_text('old')
        with cleanup.directory(self.workspace) as fd:
            cleanup.remove_children(fd, keep={('casesensitivename',)})
        self.assertTrue(actual.is_dir())
        self.assertEqual(list(actual.iterdir()), [])

    def test_cache_path_symlink_is_never_followed(self):
        self.cache.rmdir()
        self.cache.symlink_to(self.outside, target_is_directory=True)
        with self.assertRaises(OSError):
            cleanup.clean_files(self.cfg)
        self.assertEqual((self.outside / 'keep').read_text(), 'keep')

    def test_directory_replaced_by_symlink_during_cleanup_is_not_followed(self):
        victim = self.workspace / 'victim'
        victim.mkdir()
        (victim / 'old').write_text('old')
        original = os.open
        def swap(path, flags, *args, **kwargs):
            if path == 'victim':
                victim.rename(self.workspace / 'moved')
                victim.symlink_to(self.outside, target_is_directory=True)
            return original(path, flags, *args, **kwargs)
        with cleanup.directory(self.workspace) as fd, patch.object(cleanup.os, 'open', side_effect=swap):
            with self.assertRaises(OSError):
                cleanup.remove_children(fd)
        self.assertEqual((self.outside / 'keep').read_text(), 'keep')

    def test_filesystem_boundary_stops_before_child_deletion(self):
        child = self.workspace / 'mounted'
        child.mkdir()
        (child / 'keep').write_text('keep')
        original = os.fstat
        with cleanup.directory(self.workspace) as fd:
            def mounted(handle):
                value = original(handle)
                if handle != fd:
                    return SimpleNamespace(st_dev=value.st_dev + 1, st_ino=value.st_ino)
                return value
            with patch.object(cleanup.os, 'fstat', side_effect=mounted), self.assertRaises(SetupError):
                cleanup.remove_children(fd)
        self.assertEqual((child / 'keep').read_text(), 'keep')

    def test_hook_cwd_inside_workspace_is_retained(self):
        cwd = self.workspace / 'subdirectory'
        cwd.mkdir()
        (cwd / 'old').write_text('old')
        with patch.object(cleanup.Path, 'cwd', return_value=cwd):
            cleanup.clean_files(self.cfg)
        self.assertTrue(cwd.is_dir())
        self.assertEqual(list(cwd.iterdir()), [])

    def test_environment_marker_rejects_outside_symlink_and_hardlink_targets(self):
        with patch.dict(os.environ, GITHUB_ENV=str(self.outside / 'keep')), self.assertRaises(SetupError):
            cleanup.mark_started(self.runner, self.work)
        self.env_file.unlink()
        self.env_file.symlink_to(self.outside / 'keep')
        with self.assertRaises(OSError):
            cleanup.mark_started(self.runner, self.work)
        self.env_file.unlink()
        os.link(self.outside / 'keep', self.env_file)
        with self.assertRaises(SetupError):
            cleanup.mark_started(self.runner, self.work)
        self.assertEqual((self.outside / 'keep').read_text(), 'keep')

    def run_hook(self, phase, run):
        with patch.object(cleanup.config, 'load', return_value=self.cfg), \
             patch.object(cleanup.util, 'acquire_lock', return_value=None), \
             patch.object(cleanup.util, 'release_lock') as release, \
             patch.object(cleanup.util, 'run', side_effect=run):
            result = cleanup.main([phase])
        release.assert_called_once()
        return result

    def test_start_cleans_previous_job_and_preserves_environment_files(self):
        (self.workspace / '.leftover').write_text('old')
        calls = []
        def run(args, **kwargs):
            calls.append((args, kwargs))
            self.assertTrue(kwargs['stdin_devnull'])
            if args[-1] == '_files':
                cleanup.clean_files(self.cfg)
            return subprocess.CompletedProcess(args, 0)
        self.assertEqual(self.run_hook('started', run), 0)
        self.assertFalse((self.workspace / '.leftover').exists())
        self.assertEqual(self.env_file.read_text(), 'existing=true\nselfhosted=true\n')
        self.assertEqual([c[1]['timeout'] for c in calls], [300, 120, 120])
        self.assertEqual([c[0][2] for c in calls[1:]], ['shutdown', 'erase'])

    def test_failure_is_reported_and_no_later_success_masks_it(self):
        def fail(args, **kwargs):
            raise SetupError('cleanup fixture failure')
        self.assertEqual(self.run_hook('started', fail), 1)
        self.assertIn('cleanup fixture failure', self.output.getvalue())
        self.assertEqual(self.env_file.read_text(), 'existing=true\n')

    def test_simulator_failure_propagates_from_both_hooks(self):
        def fail(args, **kwargs):
            if args[0] == '/usr/bin/xcrun':
                raise SetupError('simulator fixture failure')
        for phase in ('started', 'completed'):
            with self.subTest(phase=phase):
                self.assertEqual(self.run_hook(phase, fail), 1)
        self.assertIn('simulator fixture failure', self.output.getvalue())

    def test_busy_maintenance_prevents_cleanup(self):
        with patch.object(cleanup.config, 'load', return_value=self.cfg), \
             patch.object(cleanup.util, 'acquire_lock', return_value='busy'), \
             patch.object(cleanup.util, 'release_lock') as release, \
             patch.object(cleanup.util, 'run') as run:
            self.assertEqual(cleanup.main(['started']), 1)
        run.assert_not_called()
        release.assert_not_called()

    def test_hung_file_worker_is_terminated_and_reported(self):
        original = cleanup.util.run
        def stalled(args, **kwargs):
            return original([sys.executable, '-c', 'import time; time.sleep(30)'], **kwargs)
        started = time.monotonic()
        with patch.object(cleanup, 'FILES_TIMEOUT', .1):
            self.assertEqual(self.run_hook('completed', stalled), 1)
        self.assertLess(time.monotonic() - started, 5)
        self.assertIn('timed out', self.output.getvalue())

    def test_shell_hooks_propagate_failure_with_github_bash_invocation(self):
        interpreter = self.root / 'fake-python'
        arguments = self.root / 'arguments'
        interpreter.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$TEST_ARGUMENTS"\nexit 7\n')
        interpreter.chmod(0o700)
        for phase in ('started', 'completed'):
            with self.subTest(phase=phase):
                result = subprocess.run(['/bin/bash', '-e', str(cleanup.ROOT / f'hooks/job-{phase}.sh')],
                                        env=dict(os.environ, CI_SETUP_PYTHON=str(interpreter), TEST_ARGUMENTS=str(arguments)),
                                        capture_output=True, timeout=5)
                self.assertEqual(result.returncode, 7)
                passed = arguments.read_text().splitlines()
                self.assertEqual(passed[0], '-I')
                self.assertEqual(Path(passed[1]).resolve(), cleanup.ROOT / 'bin/runner-cleanup')
                self.assertEqual(passed[2], phase)


if __name__ == '__main__':
    unittest.main()
