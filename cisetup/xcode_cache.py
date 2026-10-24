# This source file is part of the macOS Self-Hosted GitHub Actions Runner open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
# SPDX-License-Identifier: MIT

"""Download Xcode once on the host; send only archives to runner guests."""

import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys

from . import host, xcode
from .util import SetupError


def cli():
    for path in (Path('/opt/homebrew/bin/xcodes'), Path('/usr/local/bin/xcodes')):
        if path.is_file():
            return str(path)
    raise host.HostError('Install the host download tool with brew install xcodes, then run host xcode-login')


def login():
    if not sys.stdin.isatty():
        raise host.HostError('Run host xcode-login in your terminal for private Apple authentication')
    print('Sign in to Apple on this host. xcodes retains the account in the host Keychain.', flush=True)
    result = subprocess.run([cli(), 'update', '--data-source', 'apple'])
    if result.returncode:
        raise host.HostError('Apple authentication did not finish; retry in your terminal')


def command(arguments, timeout=600):
    # Authentication failures can contain account identifiers or session URLs.
    # Never forward captured output to logs or to guests.
    try:
        result = subprocess.run([cli(), *arguments], stdin=subprocess.DEVNULL,
                                capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as error:
        raise host.HostError('Host Xcode operation timed out; retry after checking the network and Apple session') from error
    if result.returncode:
        raise host.HostError('Host Xcode operation failed; run ./setup host xcode-login in your terminal and retry')
    return result.stdout


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def prepare(cfg):
    """Refresh the release list and return a complete set of cached archives."""
    if not cfg.xcode_manage:
        return None
    command(['update'])
    listing = command(['list'])
    try:
        desired, preferred = xcode.select_desired(xcode.parse_list(listing), cfg.xcode_install_beta)
    except SetupError as error:
        raise host.HostError(str(error)) from error
    cache = host.STATE / 'xcode-cache'
    cache.mkdir(parents=True, exist_ok=True, mode=0o700)
    result = []
    for release in desired:
        directory = cache / release.build
        directory.mkdir(exist_ok=True, mode=0o700)
        receipt = directory / 'archive.json'
        if receipt.exists():
            item = json.loads(receipt.read_text())
            path = Path(item['path'])
            if (path.parent != directory or path.is_symlink() or not path.is_file()
                    or item['build'] != release.build
                    or digest(path) != item['sha256']):
                raise host.HostError(f'Cached Xcode {release.identifier} failed verification; inspect {directory}')
        else:
            # Keep failed or interrupted downloads for inspection; do not adopt
            # an unrecorded file as a complete, verified download.
            if list(directory.iterdir()):
                raise host.HostError(f'Unrecorded Xcode download in {directory}; inspect before retrying')
            if shutil.disk_usage(cache).free < 40 * 1024 ** 3:
                raise host.HostError('Xcode download requires at least 40 GiB free on the host cache volume')
            print(f'Downloading Xcode {release.identifier} once on the host…', flush=True)
            command(['download', release.identifier, '--directory', str(directory)], timeout=4 * 3600)
            archives = list(directory.glob('*.xip'))
            if len(archives) != 1 or archives[0].is_symlink():
                raise host.HostError(f'Expected one downloaded Xcode archive in {directory}')
            path = archives[0]
            path.chmod(0o600)
            item = {'identifier': release.identifier, 'build': release.build,
                    'path': str(path), 'sha256': digest(path)}
            with receipt.open('x') as stream:
                json.dump(item, stream)
            receipt.chmod(0o600)
        # Apple can promote an RC to a final release without changing its build.
        # Reuse the verified bytes while publishing the current release name.
        result.append(dict(item, identifier=release.identifier))
    print(f'Default Xcode: {preferred.identifier} ({preferred.build})', flush=True)
    return {'schema': 1, 'archives': result}


def stage(connection, manifest, staging):
    if manifest is None:
        return
    entries = []
    for item in manifest['archives']:
        destination = staging + '/Xcode-' + item['build'] + '.xip'
        connection.upload_file(Path(item['path']), destination, item['sha256'])
        entries.append(dict(item, path=destination))
    # Publishing the manifest is atomic. Active maintenance continues using
    # its earlier snapshot; build-specific archive paths never change.
    destination = staging + '/xcode-archives.json'
    connection.upload(json.dumps({'schema': 1, 'archives': entries}).encode(), destination + '.new')
    connection.run(['/bin/mv', destination + '.new', destination])


def refresh(spec, state_path):
    """Refresh already configured guests; their own locks defer active jobs."""
    from . import config, guests
    state = host.read_state(state_path, spec)
    if not state['vms'] or any(not entry.get('runner_revision') for entry in state['vms'].values()):
        raise host.HostError('Finish host configure before refreshing Xcode')
    targets = guests.connections(spec, state)
    manifest = prepare(config.load(host.ROOT))
    if manifest is None:
        return
    failures = []
    for vm, connection in targets:
        staging = f'/Users/{connection.username}/Library/Application Support/runner-bootstrap'
        try:
            stage(connection, manifest, staging)
            connection.logged(['/bin/bash', staging + '/bootstrap-guest.sh', '--converge',
                               f'/Users/{connection.username}/runner-setup'],
                              staging + '/converge.log', input=connection.password() + '\n', timeout=12 * 3600)
        except host.HostError:
            failures.append(vm['name'])
    if failures:
        raise host.HostError('Xcode maintenance incomplete in: ' + ', '.join(failures) + '; inspect guest converge logs')


def agent(config, spec, state_path):
    """Install a separate opt-in refresh agent; never replace an existing one."""
    import plistlib
    state = host.read_state(state_path, spec)
    if any(not entry.get('runner_revision') for entry in state['vms'].values()) or not state['vms']:
        raise host.HostError('Finish host configure before enabling scheduled Xcode refresh')
    label = 'com.selfhosted-runner.xcode-downloads'
    target = Path.home() / 'Library/LaunchAgents' / (label + '.plist')
    definition = {'Label': label, 'RunAtLoad': True, 'StartInterval': 21600,
                  'ProgramArguments': [sys.executable, str(host.ROOT / 'bin/ci-setup'),
                                       'host', 'refresh-xcode', '--config', str(config)],
                  'StandardOutPath': str(host.STATE / 'xcode-refresh.log'),
                  'StandardErrorPath': str(host.STATE / 'xcode-refresh.log')}
    content = plistlib.dumps(definition)
    if target.exists():
        if target.read_bytes() != content:
            raise host.HostError(f'A different Xcode refresh agent already exists: {target}')
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open('xb') as stream:
            stream.write(content)
    print(f'Installed {target}. Load it once in your host login session; see docs/hosts.md.')
