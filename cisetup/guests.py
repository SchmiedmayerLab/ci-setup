# This source file is part of the macOS Self-Hosted GitHub Actions Runner open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
# SPDX-License-Identifier: MIT

"""SSH access to owned UTM guests, using their individual host Keychain entries."""

from __future__ import annotations

import ipaddress
import base64
import json
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import time

from . import host


class GuestConnectionError(host.HostError):
    """A fixed diagnostic without remote output, credentials or addresses."""


class GuestCommandError(host.HostError):
    """Retain a remote exit status without retaining private command output."""

    def __init__(self, returncode):
        self.returncode = returncode
        super().__init__(f'Guest SSH command failed (exit {returncode}); inspect the guest before retrying')


def check_transport(result):
    if result.returncode != 255:
        return
    error = result.stderr or ''
    if isinstance(error, bytes):
        error = error.decode('utf-8', errors='replace')
    if 'no route to host' in error.lower():
        raise GuestConnectionError(
            'Guest SSH could not reach the guest (no route to host). '
            'Check VM networking and Local Network permission for Runner Host (or Python for legacy agents); '
            'foreground SSH success does not prove LaunchAgent access')


class Connection:
    def __init__(self, spec, entry):
        if not spec['guest'].get('remote_login') or entry['phase'] != 'ready':
            raise host.HostError('Guest SSH requires a provisioned guest with remote_login enabled')
        self.identifier = entry['id']
        self.username = spec['guest']['username']
        self.service = host.password_service(self.identifier)
        if entry.get('password_service') != self.service:
            raise host.HostError('Guest password is not managed by this host; no SSH connection attempted')
        addresses = host.command([host.CTL, 'ip-address', self.identifier]).splitlines()
        self.address = None
        for raw in addresses:
            try:
                address = ipaddress.ip_address(raw.strip())
            except ValueError:
                continue
            if address.version == 4 and address.is_private and not address.is_loopback and not address.is_link_local:
                self.address = str(address)
                break
        if self.address is None:
            raise host.HostError('Guest has no private IPv4 address yet; wait for first boot, then retry')
        host.STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.environment = dict(os.environ, SSH_ASKPASS=str(host.ROOT / 'host/ssh-askpass'),
                                SSH_ASKPASS_REQUIRE='force', DISPLAY='runner-provisioning',
                                RUNNER_VM_KEYCHAIN_SERVICE=self.service, RUNNER_VM_ACCOUNT=self.username)
        self.arguments = ['/usr/bin/ssh', '-F', '/dev/null', '-T']
        for option in [
            'ConnectTimeout=10', 'ConnectionAttempts=1', 'ServerAliveInterval=30', 'ServerAliveCountMax=3',
            'StrictHostKeyChecking=accept-new', f'UserKnownHostsFile={host.STATE / "known_hosts"}',
            f'HostKeyAlias={self.identifier}', 'GlobalKnownHostsFile=/dev/null', 'UpdateHostKeys=no',
            'IdentityAgent=none', 'IdentityFile=none', 'IdentitiesOnly=yes', 'PubkeyAuthentication=no',
            'PreferredAuthentications=password', 'NumberOfPasswordPrompts=1',
            'ForwardAgent=no', 'ClearAllForwardings=yes',
        ]:
            self.arguments += ['-o', option]
        self.arguments += [f'{self.username}@{self.address}']

    def run(self, arguments, *, input=None, timeout=120):
        try:
            result = subprocess.run(self.arguments + [shlex.join(str(x) for x in arguments)],
                                    input=input, text=True, capture_output=True,
                                    env=self.environment, timeout=timeout, start_new_session=True)
        except subprocess.TimeoutExpired as error:
            raise host.HostError('Guest SSH command timed out; inspect guest state before retrying') from error
        check_transport(result)
        if result.returncode:
            # Remote command output can contain a registration token or password.
            raise GuestCommandError(result.returncode)
        return result.stdout.strip()

    def password(self):
        # Retrieval only: a missing recovery credential must never be replaced.
        return host.command(['/usr/bin/security', 'find-generic-password', '-s', self.service,
                             '-a', self.username, '-w', Path.home() / 'Library/Keychains/login.keychain-db'], timeout=15)

    def upload(self, content: bytes, destination: str):
        """Transfer an ordinary file without shared folders or local SSH config."""
        self.run(['/bin/sh', '-c', 'umask 077; /usr/bin/base64 -D > "$1"', 'upload', destination],
                 input=base64.b64encode(content).decode('ascii'), timeout=300)

    def logged(self, arguments, log, *, input=None, timeout=7200, defer_on_busy=False):
        print(f'Guest log: {log}', flush=True)
        try:
            self.run(['/bin/sh', '-c', 'umask 077; log=$1; shift; exec "$@" > "$log" 2>&1',
                      'provision', log, *arguments], input=input, timeout=timeout)
        except GuestCommandError as error:
            # Only known maintenance commands use exit 2 for deferral. Do not
            # interpret a generic remote shell/transfer failure as busy work.
            if defer_on_busy and error.returncode == 2:
                raise host.HostBusy('Guest maintenance deferred: active jobs or another maintenance run; retry when idle') from None
            raise

    def upload_file(self, source, destination, expected_digest):
        """Stream a large archive, verify it remotely, then publish atomically."""
        script = r'''import hashlib, os, pathlib, shutil, sys
path = pathlib.Path(sys.argv[1]); expected = sys.argv[2]; size = int(sys.argv[3])
def digest(p):
    with p.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()
if path.is_file() and not path.is_symlink() and digest(path) == expected:
    print('cached')
else:
    if shutil.disk_usage(path.parent).free < size + 40 * 1024 ** 3:
        raise SystemExit('Insufficient guest space for archive and extraction')
    print('upload')
'''
        args = ['/opt/homebrew/bin/python3', '-c', script, destination, expected_digest, str(source.stat().st_size)]
        if self.run(args, timeout=600) == 'cached':
            return
        script = r'''import hashlib, os, pathlib, sys, tempfile
path = pathlib.Path(sys.argv[1]); expected = sys.argv[2]
fd, temporary = tempfile.mkstemp(prefix=path.name + '.', suffix='.part', dir=path.parent)
try:
    digest = hashlib.sha256()
    with os.fdopen(fd, 'wb') as stream:
        while chunk := sys.stdin.buffer.read(1024 * 1024):
            stream.write(chunk); digest.update(chunk)
    if digest.hexdigest() != expected:
        raise SystemExit('Archive transfer checksum mismatch')
    os.replace(temporary, path)
finally:
    if os.path.exists(temporary):
        os.unlink(temporary)
'''
        args = ['/opt/homebrew/bin/python3', '-c', script, destination, expected_digest]
        try:
            with source.open('rb') as stream:
                result = subprocess.run(self.arguments + [shlex.join(args)], stdin=stream,
                                        capture_output=True, env=self.environment,
                                        timeout=3600, start_new_session=True)
        except subprocess.TimeoutExpired as error:
            raise host.HostError('Xcode archive transfer timed out; retry configuration') from error
        check_transport(result)
        if result.returncode:
            raise host.HostError('Xcode archive transfer failed; the previous archive was retained')


def connections(spec, state):
    """Refuse missing, renamed, stopped, or unrelated guests before connecting."""
    live = host.inventory()
    host.check_owned(state, live)
    result = []
    for vm in spec['vm']:
        entry = state['vms'].get(vm['name'])
        if not entry or live[entry['id']]['status'] != 'started':
            raise host.HostError(f'{vm["name"]}: guest must be running before configuration')
        result.append((vm, Connection(spec, entry)))
    return result


def wait_for_runner(connection, checkout, name):
    """Allow launchd to start the listener before recording a ready guest."""
    deadline = time.monotonic() + 60
    while True:
        snapshot = json.loads(connection.run([checkout + '/setup', 'status', '--json'], timeout=300))
        identity = snapshot.get('runner', {})
        if snapshot.get('errors') or identity.get('name') != name:
            raise host.HostError(f'{name}: runner inventory is incomplete')
        if identity.get('activity', {}).get('state') in ('idle', 'busy'):
            return
        if time.monotonic() >= deadline:
            raise host.HostError(f'{name}: runner listener did not start; inspect the guest service logs')
        time.sleep(2)


def configure(spec, state_path, vm_name=None):
    """Install committed source and tools, then register and start owned guests."""
    from . import github_api, config, xcode_cache
    from .util import SetupError

    if not spec['guest'].get('automatic_login'):
        raise host.HostError('Unattended runners require automatic guest login in the initial configuration')
    git = ['git', '-C', str(host.ROOT)]
    if host.command([*git, 'status', '--porcelain', '--untracked-files=normal']):
        raise host.HostError('Commit the reviewed setup changes before configuring guests; only committed source is transferred')
    branch = host.source_branch()
    revision = host.command([*git, 'rev-parse', 'HEAD'])
    state = host.read_state(state_path, spec)
    targets = connections(spec, state)
    if vm_name is not None:
        targets = [(vm, connection) for vm, connection in targets if vm['name'] == vm_name]
        if not targets:
            raise host.HostError('The requested VM is not in this host configuration')
    credentials = host.runner_credentials()
    archives = xcode_cache.prepare(config.load(host.ROOT))
    with tempfile.TemporaryDirectory(prefix='runner-source-') as temporary:
        bundle = Path(temporary) / 'source.bundle'
        host.command([*git, 'bundle', 'create', bundle, branch], timeout=300)
        for vm, connection in targets:
            name = vm['name']
            home = f'/Users/{connection.username}'
            staging = home + '/Library/Application Support/runner-bootstrap'
            checkout = home + '/runner-setup'
            print(f'{name}: installing prerequisites and runner tools…', flush=True)
            connection.run(['/bin/mkdir', '-p', staging])
            connection.run(['/bin/chmod', '700', staging])
            script = staging + '/bootstrap-guest.sh'
            connection.upload((host.ROOT / 'host/bootstrap-guest.sh').read_bytes(), script)
            connection.logged(['/bin/bash', script, name, host.hostname(name)], staging + '/bootstrap.log',
                              input=connection.password() + '\n')
            # A retained bundle gives the guest a valid, pinned upstream even
            # when the host's commit has not been published yet.
            source = staging + '/source.bundle'
            connection.upload(bundle.read_bytes(), source + '.new')
            connection.run(['/bin/sh', '-eu', '-c', '''
checkout=$1; bundle=$2; branch=$3; revision=$4
if [ -e "$checkout" ]; then
  test -d "$checkout/.git"
  test -z "$(git -C "$checkout" status --porcelain)"
  test "$(git -C "$checkout" rev-parse HEAD)" = "$revision"
else
  mv "$bundle.new" "$bundle"
  git clone --branch "$branch" "$bundle" "$checkout"
fi
test "$(git -C "$checkout" rev-parse HEAD)" = "$revision"
''', 'source', checkout, source, branch, revision], timeout=300)
            connection.logged(['/bin/bash', script, name, host.hostname(name), checkout],
                              staging + '/privileged-setup.log', input=connection.password() + '\n')
            try:
                token = github_api.registration_token(credentials)
                connection.logged([checkout + '/setup', 'register', '--runner-name', name,
                                   '--registration-token-stdin'], staging + '/registration.log', input=token + '\n')
            except SetupError as error:
                raise host.HostError('Could not obtain a registration token; check the host PAT') from error
            finally:
                token = None
            xcode_cache.stage(connection, archives, staging)
            connection.logged(['/bin/bash', script, '--converge', checkout],
                              staging + '/converge.log', input=connection.password() + '\n', timeout=12 * 3600, defer_on_busy=True)
            wait_for_runner(connection, checkout, name)
            state['vms'][name]['runner_revision'] = revision
            host.save_state(state_path, state)
            print(f'{name}: guest tools and runner service configured at {revision[:12]}', flush=True)
