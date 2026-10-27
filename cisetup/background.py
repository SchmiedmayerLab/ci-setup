# This source file is part of the macOS Self-Hosted GitHub Actions Runner open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
# SPDX-License-Identifier: MIT

"""A consent-bearing app identity shared by host background operations."""

import json
import os
from pathlib import Path
import plistlib
import shutil
import sys
import tempfile
import uuid

from . import host

BUNDLE_ID = 'org.schmiedmayerlab.runner-host'


def app_path():
    return host.STATE / 'Runner Host.app'


def executable():
    return app_path() / 'Contents/MacOS/runner-host'


def expected():
    return {'root': str(host.ROOT.resolve()), 'python': str(Path(sys.executable).resolve()),
            'source_sha256': host.digest(host.ROOT / 'host/runner-host.swift')}


def verify(*, current=True):
    app = app_path()
    if app.is_symlink() or executable().is_symlink():
        raise host.HostError('Background app must not be a symlink; inspect the host state')
    try:
        saved = json.loads((host.STATE / 'background-app.json').read_text())
        info = plistlib.loads((app / 'Contents/Info.plist').read_bytes())
        if ((current and saved.get('binding') != expected()) or saved.get('executable_sha256') != host.digest(executable())
                or saved.get('info_sha256') != host.digest(app / 'Contents/Info.plist')
                or info.get('CFBundleIdentifier') != BUNDLE_ID):
            raise ValueError('binding mismatch')
    except (OSError, ValueError, KeyError, plistlib.InvalidFileException):
        raise host.HostError('Background app changed or belongs to another interpreter/source; inspect it before preparing again') from None
    return executable()


def arguments(args):
    if app_path().exists():
        return [str(verify()), *map(str, args)]
    return [sys.executable, str(host.ROOT / 'bin/ci-setup'), *map(str, args)]


def properties():
    return {'AssociatedBundleIdentifiers': [BUNDLE_ID]} if app_path().exists() else {}


def ensure():
    if app_path().exists():
        verify(current=False)
        host.command(['/usr/bin/codesign', '--verify', '--deep', '--strict', app_path()], timeout=60)
        saved = json.loads((host.STATE / 'background-app.json').read_text())
        if saved['binding'].get('root') != expected()['root']:
            raise host.HostError('Background app belongs to another checkout; do not retarget its existing agents')
        if saved['binding'] == expected():
            return
    host.STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.TemporaryDirectory(prefix='background-app-', dir=host.STATE) as temporary:
        app = Path(temporary) / 'Runner Host.app'
        binary = app / 'Contents/MacOS/runner-host'
        binary.parent.mkdir(parents=True)
        identity = expected()
        info = {'CFBundleIdentifier': BUNDLE_ID, 'CFBundleExecutable': 'runner-host',
                'CFBundleName': 'Runner Host', 'CFBundleDisplayName': 'Runner Host',
                'CFBundlePackageType': 'APPL', 'CFBundleVersion': '1', 'LSUIElement': True,
                'NSLocalNetworkUsageDescription': 'Connect to the owned runner VMs over SSH to install and maintain build tools.',
                'NSAppleEventsUsageDescription': 'Control UTM to create, inspect and start the owned runner VMs.',
                'RunnerRoot': identity['root'], 'RunnerPython': identity['python']}
        (app / 'Contents/Info.plist').write_bytes(plistlib.dumps(info))
        host.command(['/usr/bin/xcrun', 'swiftc', host.ROOT / 'host/runner-host.swift', '-o', binary], timeout=180)
        host.command(['/usr/bin/codesign', '--force', '--sign', '-', '--identifier', BUNDLE_ID, app], timeout=60)
        host.command(['/usr/bin/codesign', '--verify', '--deep', '--strict', app], timeout=60)
        receipt = {'binding': identity, 'executable_sha256': host.digest(binary),
                   'info_sha256': host.digest(app / 'Contents/Info.plist')}

        # The app is private to this host user. Keep it stable across retries;
        # changes require review and possibly renewed macOS consent.
        if app_path().exists():
            verify(current=False)
            backups = host.STATE / 'background-app-versions'
            backups.mkdir(exist_ok=True, mode=0o700)
            backup = backups / (uuid.uuid4().hex + '.bundle-backup')
            app_path().rename(backup)
            try:
                shutil.move(str(app), str(app_path()))
            except OSError:
                backup.rename(app_path())
                raise
            print('Background app updated; the previous generated bundle was retained. Renew macOS consent if requested.', flush=True)
        else:
            shutil.move(str(app), str(app_path()))
        os.chmod(app_path(), 0o700)
        host.save_state(host.STATE / 'background-app.json', receipt)
    print('Background identity prepared: Runner Host. Approve its macOS permission prompts while unlocked.', flush=True)


def request_network_access():
    """Request consent up front without sending traffic, then test under launchd."""
    import subprocess
    import time
    import re
    binary = verify()
    print('Approve Runner Host Local Network access now. The consent probe connects a UDP socket but sends no traffic.', flush=True)
    # Opening through Launch Services registers the responsible app identity.
    # open's exit status alone is not the result of the app's network probe.
    host.command(['/usr/bin/open', '-W', '-n', '-a', app_path(), '--args', '_network-consent'], timeout=150)
    domain = f'gui/{os.getuid()}'
    label = BUNDLE_ID + '.network-probe.' + uuid.uuid4().hex
    with tempfile.TemporaryDirectory(prefix='network-probe-', dir=host.STATE) as directory:
        plist = Path(directory) / 'probe.plist'
        plist.write_bytes(plistlib.dumps({'Label': label, 'RunAtLoad': True,
            'ProgramArguments': [str(binary), '_network-consent'], **properties(),
            'StandardInPath': '/dev/null', 'StandardOutPath': '/dev/null', 'StandardErrorPath': '/dev/null'}))
        try:
            host.command(['/bin/launchctl', 'bootstrap', domain, plist], timeout=30)
            deadline = time.monotonic() + 150
            while time.monotonic() < deadline:
                state = host.command(['/bin/launchctl', 'print', domain + '/' + label], timeout=15)
                last = re.search(r'^\s*last exit code = (-?\d+)\s*$', state, re.M)
                if last:
                    if last[1] != '0':
                        raise host.HostError('Local Network consent probe failed; check Runner Host permissions and a connected local network')
                    print('Background Local Network consent probe passed; actual guest SSH is checked after first boot.', flush=True)
                    return
                time.sleep(1)
            raise host.HostError('Local Network permission check timed out; resolve consent before handing off')
        finally:
            subprocess.run(['/bin/launchctl', 'bootout', domain + '/' + label], capture_output=True, timeout=30)


def migrate_agents(config):
    """After plan approval, migrate only exact, idle legacy host definitions."""
    import subprocess
    import re
    for name, action, logfile in ((host.LABEL, 'start', 'login.log'),
                                  ('com.selfhosted-runner.xcode-downloads', 'refresh-xcode', 'xcode-refresh.log')):
        target = Path.home() / 'Library/LaunchAgents' / (name + '.plist')
        if not target.exists():
            continue
        if target.is_symlink():
            raise host.HostError('Host agent is a symlink; inspect it before migration')
        args = ['host', action, '--config', str(config)]
        legacy = {'Label': name, 'RunAtLoad': True,
                  'ProgramArguments': [sys.executable, str(host.ROOT / 'bin/ci-setup'), *args],
                  'StandardOutPath': str(host.STATE / logfile), 'StandardErrorPath': str(host.STATE / logfile)}
        if action == 'refresh-xcode':
            legacy['StartInterval'] = 21600
        desired = dict(legacy, ProgramArguments=arguments(args), **properties())
        old = target.read_bytes()
        actual = plistlib.loads(old)
        if actual == desired:
            continue
        if actual != legacy:
            raise host.HostError('Existing host agent is not the exact managed legacy definition; inspect it before migration')
        service = f'gui/{os.getuid()}/{name}'
        loaded = subprocess.run(['/bin/launchctl', 'print', service], capture_output=True, text=True, timeout=15)
        if loaded.returncode == 0:
            command = re.search(r'^\s*arguments = \{\n(.*?)^\s*\}', loaded.stdout, re.M | re.S)
            if not command or [x.strip() for x in command[1].splitlines() if x.strip()] != legacy['ProgramArguments']:
                raise host.HostError('Loaded host agent differs from its saved definition; migration refused')
            if re.search(r'^\s*state = running\s*$', loaded.stdout, re.M):
                raise host.HostError('Host maintenance is running; wait before migrating its background identity')
        backups = host.STATE / 'agent-backups'
        backups.mkdir(exist_ok=True, mode=0o700)
        backup = backups / (name + '.' + uuid.uuid4().hex + '.plist')
        with backup.open('xb') as stream:
            stream.write(old)
        backup.chmod(0o600)
        if loaded.returncode == 0:
            host.command(['/bin/launchctl', 'bootout', service], timeout=30)
        try:
            with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as stream:
                stream.write(plistlib.dumps(desired))
                temporary = Path(stream.name)
            os.replace(temporary, target)
        except OSError:
            target.write_bytes(old)
            if loaded.returncode == 0:
                host.command(['/bin/launchctl', 'bootstrap', f'gui/{os.getuid()}', target], timeout=30)
            raise
        print(f'Migrated {name}; previous definition retained. The worker will load and verify it after guest configuration.', flush=True)
