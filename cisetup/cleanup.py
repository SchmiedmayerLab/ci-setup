# This source file is part of the macOS Self-Hosted GitHub Actions Runner open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
# SPDX-License-Identifier: MIT

"""Bounded job cleanup confined to the registered runner's workspace."""

from contextlib import contextmanager
import json
import os
from pathlib import Path
import pwd
import stat
import sys

from . import config, util
from .util import SetupError


ROOT = Path(__file__).resolve().parent.parent
FILES_TIMEOUT = 300
SIMULATOR_TIMEOUT = 120
# GitHub prepares these before the start hook and still uses them after the end hook.
CONTROL_DIRS = {'_temp', '_actions', '_tool', '_PipelineMapping'}


def home_directory():
    return Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()


def work_path(cfg, folder=None):
    """Validate configuration without creating directories or trusting job variables."""
    root = cfg.runner_dir.expanduser().absolute()
    home = home_directory()
    if '..' in root.parts or any(c in str(root) for c in '\r\n\0'):
        raise SetupError('cleanup requires an unambiguous runner directory')
    if root.is_symlink() or root.resolve() in (Path('/'), home) or root.resolve() in home.parents:
        raise SetupError('cleanup refuses a root, home or linked runner directory')
    value = cfg.work_dir if folder is None else folder
    if not isinstance(value, str) or not value.strip() or any(c in value for c in '\r\n\0'):
        raise SetupError('cleanup requires an unambiguous work directory')
    work = Path(value)
    if '..' in work.parts:
        raise SetupError('cleanup refuses parent traversal in the work directory')
    if not work.is_absolute():
        work = root / work
    try:
        parts = work.relative_to(root).parts
    except ValueError as error:
        raise SetupError('cleanup work directory must be inside the runner directory') from error
    if (not parts or parts[0].startswith('.') or parts[0].casefold() in {'bin', 'externals', '_diag', '_update'}
            or parts[0].casefold().startswith(('bin.', 'externals.'))):
        raise SetupError('cleanup refuses the runner installation or its internal directories')
    current = root
    for part in parts:
        current /= part
        if current.is_symlink():
            raise SetupError('cleanup refuses a symlink in the work directory path')
    root, work = root.resolve(), work.resolve()
    for protected in (home, cfg.repo_root.resolve()):
        if protected == work or work in protected.parents:
            raise SetupError('cleanup work directory contains the home or setup checkout')
    return root, work


@contextmanager
def directory(root, parts=()):
    """Open each component without following symlinks, retaining an anchored handle."""
    handles = []
    try:
        fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        handles.append(fd)
        device = os.fstat(fd).st_dev
        for part in parts:
            fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            handles.append(fd)
            if os.fstat(fd).st_dev != device:
                raise SetupError('cleanup refuses to cross a filesystem boundary')
        if os.fstat(fd).st_uid != os.getuid():
            raise SetupError('cleanup target is not owned by the runner user')
        yield fd
    finally:
        for handle in reversed(handles):
            os.close(handle)


def remove_children(fd, *, keep=frozenset(), prefix=(), skip=frozenset()):
    """Remove entries, including dotfiles; unlink symlinks without visiting their targets."""
    device = os.fstat(fd).st_dev
    kept = {tuple(part.casefold() for part in path) for path in keep}
    skipped = {name.casefold() for name in skip}
    with os.scandir(fd) as entries:
        names = [entry.name for entry in entries]
    for name in names:
        relative = prefix + (name,)
        preserve = tuple(part.casefold() for part in relative) in kept
        if name.casefold() in skipped:
            continue
        try:
            info = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                try:
                    actual = os.fstat(child)
                    if (actual.st_dev, actual.st_ino) != (info.st_dev, info.st_ino) or actual.st_dev != device:
                        raise SetupError('cleanup directory changed or crosses a filesystem boundary')
                    remove_children(child, keep=keep, prefix=relative)
                finally:
                    os.close(child)
                if not preserve:
                    os.rmdir(name, dir_fd=fd)
            elif preserve:
                raise SetupError('cleanup workspace directory was replaced by a file or symlink')
            else:
                os.unlink(name, dir_fd=fd)
        except FileNotFoundError:
            # Another completed process may have already removed this entry.
            continue


def context(cfg):
    if os.environ.get('GITHUB_ACTIONS') != 'true' or os.environ.get('RUNNER_ENVIRONMENT') != 'self-hosted':
        raise SetupError('cleanup only runs as a self-hosted GitHub job hook')
    if not cfg.cleanup_hooks:
        raise SetupError('cleanup hooks are disabled; reconcile the runner environment')
    root, work = work_path(cfg)
    with directory(root) as fd:
        metadata = os.open('.runner', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        with os.fdopen(metadata) as file:
            if not stat.S_ISREG(os.fstat(file.fileno()).st_mode):
                raise SetupError('cleanup registration metadata is not a regular file')
            raw = file.read(65537)
            if len(raw) > 65536:
                raise SetupError('cleanup registration metadata is too large')
            registration = json.loads(raw.lstrip('\ufeff'))
    if not isinstance(registration, dict) or 'workFolder' not in registration:
        raise SetupError('cleanup requires registered runner workspace metadata')
    if work_path(cfg, registration['workFolder'])[1] != work:
        raise SetupError('cleanup configuration differs from the registered workspace')
    if os.environ.get('CI_SETUP_WORK_DIR') != str(work):
        raise SetupError('cleanup environment differs from the configured workspace; rerun setup')
    pipeline = Path(os.environ.get('RUNNER_WORKSPACE', ''))
    workspace = Path(os.environ.get('GITHUB_WORKSPACE', ''))
    if (pipeline.parent != work or pipeline.name.casefold() in {name.casefold() for name in CONTROL_DIRS}
            or not workspace.is_relative_to(pipeline)):
        raise SetupError('cleanup job workspace is outside its registered pipeline directory')
    if '..' in workspace.parts:
        raise SetupError('cleanup refuses parent traversal in the job workspace')
    relative = workspace.relative_to(work).parts
    # Keep the hook's current working directory and the runner's prepared workspace.
    keep = {relative[:index] for index in range(len(relative) + 1)}
    cwd = Path.cwd()
    if tuple(part.casefold() for part in cwd.parts[:len(pipeline.parts)]) == tuple(part.casefold() for part in pipeline.parts):
        parts = cwd.parts[len(work.parts):]
        keep.update(parts[:index] for index in range(len(parts) + 1))
    with directory(root, work.relative_to(root).parts + relative):
        pass
    return root, work, keep


def clean_files(cfg):
    root, work, keep = context(cfg)
    util.log('Cleaning job workspaces')
    with directory(root, work.relative_to(root).parts) as fd:
        remove_children(fd, keep=keep, skip=CONTROL_DIRS)
    util.ok('job workspaces cleaned; runner control directories retained')
    # This fixed cache is the only deletion outside the registered work directory.
    home = home_directory()
    cache = ('Library', 'Caches', 'com.github.peripheryapp')
    util.log('Cleaning Periphery cache')
    try:
        with directory(home, cache) as fd:
            remove_children(fd)
    except FileNotFoundError:
        pass
    util.ok('Periphery cache cleaned')


def mark_started(root, work):
    path = Path(os.environ.get('GITHUB_ENV', ''))
    try:
        relative = path.relative_to(work / '_temp')
    except ValueError as error:
        raise SetupError('cleanup environment file must be inside the runner temporary directory') from error
    if not relative.parts or '..' in relative.parts:
        raise SetupError('cleanup environment file has an invalid path')
    with directory(root, work.relative_to(root).parts + ('_temp',) + relative.parent.parts) as fd:
        output = os.open(relative.name, os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        with os.fdopen(output, 'a') as file:
            info = os.fstat(file.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid():
                raise SetupError('cleanup environment file must be a runner-owned regular file without hard links')
            file.write('selfhosted=true\n')


def main(argv):
    if len(argv) != 1 or argv[0] not in ('started', 'completed', '_files'):
        print('Usage: runner-cleanup started|completed', file=sys.stderr)
        return 2
    locked = False
    try:
        cfg = config.load(ROOT)
        if argv[0] == '_files':
            clean_files(cfg)
            return 0
        root, work, _ = context(cfg)
        if util.acquire_lock() is not None:
            raise SetupError('cleanup deferred: setup maintenance is already running')
        locked = True
        util.run([sys.executable, '-I', ROOT / 'bin/runner-cleanup', '_files'],
                 timeout=FILES_TIMEOUT, stdin_devnull=True, start_new_session=True)
        for action in ('shutdown', 'erase'):
            util.run(['/usr/bin/xcrun', 'simctl', action, 'all'], timeout=SIMULATOR_TIMEOUT,
                     capture=True, stdin_devnull=True, start_new_session=True)
            util.ok(f'simulators: {action} completed')
        if argv[0] == 'started':
            mark_started(root, work)
        util.ok(f'{argv[0]} cleanup completed')
        return 0
    except SetupError as error:
        util.err(f'job cleanup failed: {error}')
        return 1
    except OSError as error:
        util.err(f'job cleanup failed: filesystem operation refused (errno {error.errno})')
        return 1
    except (ValueError, KeyError):
        util.err('job cleanup failed: invalid registration or job metadata')
        return 1
    finally:
        if locked:
            util.release_lock()
