# This source file is part of the macOS Self-Hosted GitHub Actions Runner open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
# SPDX-License-Identifier: MIT

"""Prepare UTM hosts and isolated macOS runners."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import getpass
import hashlib
import json
import os
from pathlib import Path
import platform
import plistlib
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
from urllib.parse import urlparse
import uuid
import zipfile

ROOT = Path(__file__).resolve().parent.parent
UTM = Path('/Applications/UTM.app')
CTL = UTM / 'Contents/MacOS/utmctl'
STATE = Path.home() / 'Library/Application Support/runner-host'
SCRIPT = ROOT / 'host/utm.applescript'
LABEL = 'com.selfhosted-runner.virtual-machines'
UNATTENDED = False
DEFAULT_CPU_RATIO = 1.5


class HostError(Exception):
    pass


class HostBusy(HostError):
    pass


def command(args, *, timeout=60, input=None):
    """Bound every external operation; never echo stdin (which may be a password)."""
    try:
        options = {'stdin': subprocess.DEVNULL} if UNATTENDED and input is None else {}
        if UNATTENDED:
            options['start_new_session'] = True
        result = subprocess.run([str(a) for a in args], input=input, text=True,
                                capture_output=True, timeout=timeout, **options)
    except subprocess.TimeoutExpired as error:
        raise HostError(f'{Path(args[0]).name} timed out; inspect UTM before retrying') from error
    if result.returncode or (str(args[0]) == str(CTL) and 'Error from event:' in result.stderr):
        # Provisioning errors must not echo any possible secret from UTM.
        detail = 'guest provisioning failed' if input is not None else result.stderr.strip()
        raise HostError(f'{Path(args[0]).name}: {detail or "command failed"}')
    return result.stdout.strip()


def applescript(action, *args):
    return command(['/usr/bin/osascript', SCRIPT, action, *args], timeout=120)


def source_branch():
    """Guest bundles require a named branch, even when HEAD is a signed commit."""
    try:
        return command(['git', '-C', str(ROOT), 'symbolic-ref', '--quiet', '--short', 'HEAD'])
    except HostError as error:
        raise HostError(f'{error}\nCannot verify a named local Git branch for guest source transfer. '
                        'If HEAD is detached, attach the approved commit to a branch without changing its revision, '
                        'then retry; see docs/setup.md for the PR checkout procedure.') from error


def hostname(name):
    if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9 -]{0,62}', name) or name != name.strip():
        raise HostError('VM names must start with a letter and contain only letters, digits, spaces, or hyphens')
    return re.sub(r' +', '-', name.lower())


def available_names(existing, count=2):
    occupied = set()
    for name in existing:
        match = re.fullmatch(r'GitHub Runner ([0-9]+)', name, re.IGNORECASE)
        if match:
            occupied.add(int(match.group(1)))
    selected = []
    number = 1
    while len(selected) < count:
        if number not in occupied:
            selected.append(f'GitHub Runner {number}')
        number += 1
    return selected


def runner_credentials(*, interactive=False):
    """Check access before installation; keep the long-lived token on the host."""
    from . import config, github_api
    from .util import SetupError
    cfg = config.load(ROOT)
    keychain = Path.home() / 'Library/Keychains/login.keychain-db'
    lookup = ['/usr/bin/security', 'find-generic-password', '-s', config.KEYCHAIN_SERVICE,
              '-w', str(keychain)]

    def enter_token():
        try:
            entered = subprocess.run([str(ROOT / 'setup'), 'store-pat'], timeout=600)
        except subprocess.TimeoutExpired:
            raise HostError('Token entry timed out; no VMs were created') from None
        if entered.returncode:
            raise HostError('Token entry did not complete; no VMs were created')
        cfg.pat = command(lookup, timeout=15)

    try:
        result = subprocess.run(lookup, capture_output=True, text=True, timeout=15)
    except subprocess.TimeoutExpired as error:
        raise HostError('Token lookup timed out; unlock the login Keychain and approve access') from error
    if result.returncode == 44 and interactive:
        print('GitHub access is needed before choosing runner names. Enter the token privately below.', flush=True)
        enter_token()
    elif result.returncode == 44:
        raise HostError('Store a scoped runner token privately with ./setup store-pat, then retry name discovery')
    elif result.returncode:
        raise HostError('Cannot read the token; unlock the login Keychain and approve access')
    else:
        cfg.pat = result.stdout.strip()
    for attempt in range(3):
        if not cfg.pat:
            raise HostError('Stored token is empty; replace it privately with ./setup store-pat')
        try:
            # Verify each entry before Apple authentication or provisioning.
            # Mint and discard a short-lived token; never register a runner here.
            github_api.registration_token(cfg)
        except SetupError as error:
            if isinstance(error, github_api.GitHubAPIError) and error.status == 401:
                cfg.pat = None
                if interactive and attempt < 2:
                    print('GitHub rejected the token (HTTP 401). Enter a complete active token again; '
                          'Ctrl-C cancels setup.', flush=True)
                    enter_token()
                    continue
                raise HostError('GitHub rejected the stored token (HTTP 401); replace the complete active token with ./setup store-pat') from None
            permission = 'Self-hosted runners: write' if cfg.scope == 'org' else 'Administration: write'
            raise HostError(f'Cannot obtain a registration token; check {permission}, token approval, expiration, and network access') from None
        if interactive:
            print('GitHub token and runner-registration permission verified.', flush=True)
        return cfg


def discover_names(*, interactive=False):
    from . import github_api
    from .util import SetupError
    try:
        cfg = runner_credentials(interactive=interactive)
        names = []
        page = 1
        while True:
            result = github_api.get(f'{cfg.api_base}/actions/runners?per_page=100&page={page}', cfg.pat)
            runners = result.get('runners') if isinstance(result, dict) else None
            if not isinstance(runners, list) or any(not isinstance(r, dict) or not isinstance(r.get('name'), str) for r in runners):
                raise HostError('GitHub returned an invalid runner list; names were not allocated')
            names.extend(r['name'] for r in runners)
            if len(runners) < 100:
                break
            page += 1
        names.extend(entry['name'] for entry in inventory().values())
        return available_names(names)
    except SetupError as error:
        raise HostError('Cannot list runners for the configured destination; check the scoped token and network access') from error


def load(path):
    with path.open('rb') as file:
        data = tomllib.load(file)
    if set(data) - {'image', 'guest', 'vm', 'host'}:
        raise HostError('Unknown host configuration section')
    policy = data.get('host', {})
    if not isinstance(policy, dict) or set(policy) - {'cpu_overcommit'}:
        raise HostError('Unknown host option')
    if 'cpu_overcommit' in policy and type(policy['cpu_overcommit']) is not bool:
        raise HostError('host.cpu_overcommit must be true or false')
    image = data.get('image', {})
    guest = data.get('guest', {})
    vms = data.get('vm', [])
    if not isinstance(image, dict) or not isinstance(guest, dict):
        raise HostError('image and guest must be tables')
    if set(image) - {'path', 'sha256', 'url'} or set(guest) - {'username', 'automatic_login', 'remote_login'}:
        raise HostError('Unknown image or guest option')
    if not isinstance(image.get('path'), str) or not image['path'].strip():
        raise HostError('Set image.path to a local IPSW path')
    image['path'] = str((path.parent / Path(image['path']).expanduser()).resolve())
    if not isinstance(image.get('sha256'), str) or not re.fullmatch(r'[a-fA-F0-9]{64}', image['sha256']):
        raise HostError('Set image.sha256 to the restore image SHA-256 checksum')
    image['sha256'] = image['sha256'].lower()
    if 'url' in image:
        if not isinstance(image['url'], str):
            raise HostError('image.url must be an HTTPS Apple download URL')
        url = urlparse(image['url'])
        if url.scheme != 'https' or url.hostname != 'updates.cdn-apple.com' or url.username or url.password or url.port:
            raise HostError('image.url must use https://updates.cdn-apple.com/')
    guest.setdefault('username', 'runner')
    if not isinstance(guest['username'], str) or not re.fullmatch(r'[a-z][a-z0-9_-]{0,30}', guest['username']):
        raise HostError('guest.username must be a short lowercase account name')
    for key in ('automatic_login', 'remote_login'):
        guest.setdefault(key, False)
        if type(guest[key]) is not bool:
            raise HostError(f'guest.{key} must be true or false')
    if not isinstance(vms, list) or not 1 <= len(vms) <= 2:
        raise HostError('Configure one or two [[vm]] entries')
    names = set()
    for vm in vms:
        if not isinstance(vm, dict) or set(vm) != {'name', 'cpus', 'memory_gib', 'disk_gib'}:
            raise HostError('Each VM needs exactly name, cpus, memory_gib, disk_gib')
        name = vm['name']
        normalized = hostname(name)
        if normalized in names:
            raise HostError('VM names must produce unique macOS hostnames')
        names.add(normalized)
        for key, minimum, maximum in [('cpus', 2, 128), ('memory_gib', 4, 1024), ('disk_gib', 64, 2048)]:
            value = vm[key]
            if type(value) is not int or not minimum <= value <= maximum:
                raise HostError(f'{name}: {key} must be an integer from {minimum} to {maximum}')
    result = {'image': image, 'guest': guest, 'vm': vms}
    if policy:
        result['host'] = policy
    return result


def resource_sizes(ram, cores, total_gib, free_gib, *, count=2, reserve_memory=None, cpu_ratio=DEFAULT_CPU_RATIO):
    """Choose fixed sizes once; later runs use the saved configuration."""
    if reserve_memory is None:
        reserve_memory = min(16, max(8, ram // 4))
    if count not in (1, 2) or reserve_memory < 8 or not 1 <= cpu_ratio <= 2:
        raise HostError('Use one or two VMs, at least 8 GiB host RAM, and a CPU ratio from 1 to 2')
    memory = (ram - reserve_memory) // count
    cpus = min(cores - 2, int((cores - 2) * cpu_ratio / count))
    # Reserve host capacity and current free space independently. Sparse disks
    # must be able to grow fully without consuming the host's remaining space.
    disk = min(512, int(min(total_gib - 128, free_gib - 64) // count // 128) * 128)
    if memory < 4 or cpus < 2 or disk < 256:
        raise HostError('Insufficient capacity for this layout; use fewer guests or free disk space')
    return {'cpus': cpus, 'memory_gib': memory, 'disk_gib': disk}


def plan_config(image, names, *, reserve_memory=None, cpu_ratio=DEFAULT_CPU_RATIO,
                automatic_login=False, remote_login=False):
    """Review resource and image selection without downloading or creating VMs."""
    if platform.system() != 'Darwin' or platform.machine() != 'arm64':
        raise HostError('macOS guests require an Apple silicon Mac')
    if not 1 <= len(names) <= 2 or len({hostname(n) for n in names}) != len(names):
        raise HostError('Choose one or two VM names with unique macOS hostnames')
    ram = int(command(['/usr/sbin/sysctl', '-n', 'hw.memsize'])) // 1024**3
    cores = int(command(['/usr/sbin/sysctl', '-n', 'hw.logicalcpu']))
    disk = shutil.disk_usage(Path.home())
    sizes = resource_sizes(ram, cores, disk.total // 1024**3, disk.free // 1024**3,
                           count=len(names), reserve_memory=reserve_memory, cpu_ratio=cpu_ratio)
    plan = {'host': {'cpu_overcommit': cpu_ratio > 1},
            'guest': {'username': 'runner', 'automatic_login': automatic_login, 'remote_login': remote_login},
            'vm': [dict(sizes, name=name) for name in names]}
    if image is None:
        from . import restore
        info = restore.metadata()
        restore.requirements(info, plan['vm'])
        plan['restore'] = info
        print(f'Selected macOS {info["version"]} ({info["build"]}) from Apple.', flush=True)
    else:
        image = image.expanduser().resolve()
        plan['image'] = {'path': str(image), 'sha256': digest(image)}
    return plan


def materialize_config(config, plan):
    """Download an approved image and publish its exact layout, never replace it."""
    spec = {key: value for key, value in plan.items() if key != 'restore'}
    if 'restore' in plan:
        from . import restore
        info = plan['restore']
        image = restore.download(info).resolve()
        spec['image'] = {'path': str(image), 'sha256': digest(image), 'url': info['url']}
    if config.exists():
        if load(config) != spec:
            raise HostError('Existing configuration differs from the approved plan; inspect before retrying')
        return spec
    content = '[host]\ncpu_overcommit = ' + str(spec.get('host', {}).get('cpu_overcommit', False)).lower() + '\n\n[image]\n'
    content += ''.join(f'{key} = {json.dumps(value)}\n' for key, value in spec['image'].items())
    content += '\n[guest]\n'
    for key, value in spec['guest'].items():
        content += f'{key} = ' + (str(value).lower() if type(value) is bool else json.dumps(value)) + '\n'
    for vm in spec['vm']:
        content += '\n[[vm]]\n' + ''.join(f'{key} = {json.dumps(value)}\n' for key, value in vm.items())
    # An interrupted publication either has no config or the complete approved
    # config. The temporary is private and a concurrently created file wins.
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', dir=config.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, config)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return load(config)


def init_config(config, image, names, *, reserve_memory=None, cpu_ratio=DEFAULT_CPU_RATIO,
                automatic_login=False, remote_login=False, confirm=None):
    if config.exists():
        raise HostError('Configuration already exists; it will not be overwritten')
    plan = plan_config(image, names, reserve_memory=reserve_memory, cpu_ratio=cpu_ratio,
                       automatic_login=automatic_login, remote_login=remote_login)
    if confirm is not None:
        confirm(plan['vm'])
    materialize_config(config, plan)
    print(f'Wrote {config}. Review it, then run host create.')


def password_service(identifier):
    return f'com.selfhosted-runner.vm.{str(uuid.UUID(identifier)).upper()}'


def keychain_password(identifier, username):
    """Create once, then retrieve. Neither subprocess argv nor state holds the secret."""
    service = password_service(identifier)
    keychain = Path.home() / 'Library/Keychains/login.keychain-db'
    args = ['/usr/bin/security', 'find-generic-password', '-s', service, '-a', username, '-w', str(keychain)]
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired as error:
        raise HostError('Guest Keychain access timed out; unlock it and approve access') from error
    if result.returncode == 0:
        if not result.stdout.rstrip('\n'):
            raise HostError('Guest Keychain item is empty; inspect it before provisioning')
        return result.stdout.rstrip('\n')
    if result.returncode != 44:
        raise HostError('Cannot read the guest password; unlock the login Keychain and approve access')
    password = secrets.token_urlsafe(32)
    # security's interactive command input keeps the password off the process
    # command line. No -U: an existing item must never be silently overwritten.
    def quote(value):
        if any(ord(char) < 32 for char in str(value)):
            raise HostError('Control characters are not supported in Keychain item paths')
        return '"' + str(value).replace('\\', '\\\\').replace('"', '\\"') + '"'
    line = ' '.join(quote(value) for value in (
        'add-generic-password', '-s', service, '-a', username, '-w', password,
        '-T', '/usr/bin/security', str(keychain))) + '\n'
    command(['/usr/bin/security', '-i'], input=line)
    # Interactive security can return zero despite a failed subcommand.
    if command(args) != password:
        raise HostError('Could not verify the new guest password in the login Keychain')
    return password


def digest(path):
    with path.open('rb') as file:
        return hashlib.file_digest(file, 'sha256').hexdigest()


def image_ready(spec):
    image = spec['image']
    path = Path(image['path'])
    if not path.exists():
        if not image.get('url'):
            raise HostError(f'Restore image missing: {path}')
        path.parent.mkdir(parents=True, exist_ok=True)
        free = shutil.disk_usage(path.parent).free
        if free < 64 * 1024**3:
            raise HostError('Restore image download needs at least 64 GiB free on its volume')
        # A unique temporary file cannot overwrite someone else's partial download.
        with tempfile.NamedTemporaryFile(dir=path.parent, suffix='.ipsw.part', delete=False) as file:
            temporary = Path(file.name)
        print(f'Downloading restore image to {temporary}', flush=True)
        command(['/usr/bin/curl', '--fail', '--location', '--proto', '=https', '--proto-redir', '=https',
                 '--connect-timeout', '30', '--max-time', '7200', '--silent', '--show-error',
                 '--max-filesize', str(free - 32 * 1024**3),
                 '--output', temporary, image['url']], timeout=7260)
        if digest(temporary) != image['sha256']:
            raise HostError(f'Checksum mismatch; untrusted download retained at {temporary}')
        # No replacement, even if a file appeared while the transfer was running.
        os.link(temporary, path)
        temporary.unlink()
    print('Verifying restore image checksum…', flush=True)
    if digest(path) != image['sha256']:
        raise HostError('Restore image checksum mismatch; no VM was created')
    try:
        with zipfile.ZipFile(path) as archive:
            info = archive.getinfo('Restore.plist')
            if info.file_size > 16 * 1024 * 1024:
                raise HostError('Unexpected restore image manifest size')
            manifest = plistlib.loads(archive.read(info))
        return int(manifest['ProductVersion'].split('.')[0])
    except (zipfile.BadZipFile, KeyError, ValueError, TypeError, AttributeError, plistlib.InvalidFileException) as error:
        raise HostError('Restore image has invalid macOS metadata; no VM was created') from error


def check_machine(*, provision=False):
    if platform.system() != 'Darwin' or platform.machine() != 'arm64':
        raise HostError('macOS guests require an Apple silicon Mac')
    if provision and int(platform.mac_ver()[0].split('.')[0]) < 27:
        raise HostError('Automatic guest setup requires macOS 27+ on the host; update macOS through your normal administration process')


def check_utm():
    if not CTL.is_file():
        raise HostError('UTM is missing; run ./setup host bootstrap')
    info = plistlib.loads((UTM / 'Contents/Info.plist').read_bytes())
    version = tuple(int(x) for x in info['CFBundleShortVersionString'].split('.'))
    if version < (5, 0, 8):
        raise HostError('UTM 5.0.8 or later is required; shut down VMs before updating UTM')


def preflight(spec):
    check_machine()
    check_utm()
    ram = int(command(['/usr/sbin/sysctl', '-n', 'hw.memsize'])) // 1024**3
    cores = int(command(['/usr/sbin/sysctl', '-n', 'hw.logicalcpu']))
    if sum(vm['memory_gib'] for vm in spec['vm']) > ram - 8:
        raise HostError('VM memory would leave less than 8 GiB for the host')
    if any(vm['cpus'] > cores for vm in spec['vm']):
        raise HostError('A guest CPU count cannot exceed the host CPU count')
    if sum(vm['cpus'] for vm in spec['vm']) > cores - 2 and not spec.get('host', {}).get('cpu_overcommit', False):
        raise HostError('VM CPUs would leave fewer than two cores for the host')


def inventory():
    text = command([CTL, 'list'])
    if not text.startswith('UUID ') or 'Status' not in text.splitlines()[0]:
        raise HostError('UTM did not return an inventory; open UTM and allow its automation prompt')
    entries = {}
    for line in text.splitlines()[1:]:
        parts = line.split(None, 2)
        if len(parts) != 3:
            raise HostError('Unexpected UTM inventory output')
        identifier, status, name = parts
        uuid.UUID(identifier)
        entries[identifier.upper()] = {'name': name, 'status': status.lower()}
    return entries


def state_path(config):
    return STATE / (hashlib.sha256(str(config).encode()).hexdigest()[:20] + '.json')


def read_state(path, spec):
    if not path.exists():
        return {'schema': 1, 'spec': spec, 'vms': {}}
    data = json.loads(path.read_text())
    if not isinstance(data, dict) or data.get('schema') != 1 or data.get('spec') != spec or not isinstance(data.get('vms'), dict):
        raise HostError('Host configuration differs from its saved state; use a new config path and new VM names to rebuild')
    names = {vm['name'] for vm in spec['vm']}
    for name, entry in data['vms'].items():
        if name not in names or not isinstance(entry, dict) or entry.get('phase') not in {
            'creating', 'installing', 'installed', 'starting', 'ready'
        }:
            raise HostError('Invalid saved VM state; inspect it before continuing')
        if 'id' in entry:
            uuid.UUID(entry['id'])
        elif entry['phase'] != 'creating':
            raise HostError('Saved VM state has no UUID; inspect it before continuing')
    return data


def save_state(path, data):
    with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, delete=False) as file:
        json.dump(data, file, indent=2)
        file.write('\n')
        file.flush()
        os.fsync(file.fileno())
        temporary = Path(file.name)
    os.replace(temporary, path)


@contextmanager
def locked(*, wait_timeout=0):
    STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    deadline = time.monotonic() + wait_timeout
    with (STATE / 'lock').open('a') as file:
        while True:
            try:
                fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError as error:
                if time.monotonic() >= deadline:
                    raise HostBusy('Another host operation is running; deferred without interrupting it') from error
                time.sleep(1)
        yield


def check_owned(data, live):
    for name, entry in data['vms'].items():
        identifier = entry.get('id')
        if identifier is None or live.get(identifier, {}).get('name') != name:
            raise HostError(f'{name}: incomplete creation or missing/renamed VM; inspect UTM and saved state, no automatic replacement')


def check_other_running(data, live):
    owned = {entry.get('id') for entry in data['vms'].values()}
    if any(identifier not in owned and entry['status'] != 'stopped' for identifier, entry in live.items()):
        raise HostError('An unrelated VM is active; review its resource use before starting this layout. No VMs were stopped')


def create(spec, path, *, provision=False, password=None, keychain_passwords=False):
    preflight(spec)
    data = read_state(path, spec)
    live = inventory()
    check_owned(data, live)
    missing = [vm for vm in spec['vm'] if vm['name'] not in data['vms']]
    if missing or any(entry['phase'] != 'ready' for entry in data['vms'].values()):
        check_other_running(data, live)
    for vm in missing:
        if any(entry['name'].casefold() == vm['name'].casefold() for entry in live.values()):
            raise HostError(f'{vm["name"]}: already exists outside this setup; refusing adoption or overwrite')
    if provision and int(platform.mac_ver()[0].split('.')[0]) < 27:
        raise HostError('Automatic account provisioning requires macOS 27+ on the host')
    if missing:
        # Account for potential full growth, not only initially sparse disks.
        needed = sum(vm['disk_gib'] for vm in missing) + 32
        if shutil.disk_usage(Path.home()).free < needed * 1024**3:
            raise HostError(f'Need at least {needed} GiB free on the home volume for new guest disks')
        major = image_ready(spec)
        if provision and major < 27:
            raise HostError('Automatic account provisioning requires macOS 27+ on both host and guest')
    elif provision and any(x['phase'] in {'installing', 'installed'} for x in data['vms'].values()):
        if image_ready(spec) < 27:
            raise HostError('Automatic account provisioning requires macOS 27+ in the guest')
    if provision and password is None and not keychain_passwords and (missing or any(x['phase'] in {'installing', 'installed'} for x in data['vms'].values())):
        if not sys.stdin.isatty():
            raise HostError('Run create --provision in a terminal to enter the guest password privately')
        password = getpass.getpass('New guest account password (not stored): ')
        if len(password) < 12 or '\n' in password or '\r' in password:
            raise HostError('Use a guest password of at least 12 characters on one line')
        if password != getpass.getpass('Confirm guest password: '):
            raise HostError('Passwords do not match')
    if provision and password is not None:
        if not 12 <= len(password) <= 4096 or '\n' in password or '\r' in password:
            raise HostError('Use a guest password of 12–4096 characters on one line')
    for vm in spec['vm']:
        name = vm['name']
        entry = data['vms'].get(name)
        if entry is None:
            # Persist intent first. An interrupted/ambiguous creation is never retried blindly.
            entry = {'phase': 'creating'}
            data['vms'][name] = entry
            save_state(path, data)
            print(f'Creating {name}…', flush=True)
            identifier = applescript('create', name, spec['image']['path'],
                                     str(vm['memory_gib'] * 1024), str(vm['cpus']), str(vm['disk_gib'] * 1024))
            entry['id'] = str(uuid.UUID(identifier)).upper()
            entry['phase'] = 'installing'
            save_state(path, data)
            # The only install call: a disk created successfully in THIS invocation.
            applescript('install', entry['id'])
        if entry['phase'] == 'installing':
            deadline = time.monotonic() + 7200
            while applescript('progress', entry['id']) != 'installed':
                if time.monotonic() >= deadline:
                    raise HostError(f'{name}: installation timed out; inspect UTM, do not reinstall the disk')
                time.sleep(10)
            entry['phase'] = 'installed'
            save_state(path, data)
        if entry['phase'] == 'installed':
            args = [CTL, 'start', entry['id']]
            if provision:
                if keychain_passwords:
                    password = keychain_password(entry['id'], spec['guest']['username'])
                    entry['password_service'] = password_service(entry['id'])
                if not password:
                    raise HostError('A guest password is required for first boot provisioning')
                args += ['--provision-username', spec['guest']['username'], '--provision-password-stdin']
                if spec['guest']['automatic_login']:
                    args.append('--provision-auto-login')
                if spec['guest']['remote_login']:
                    args.append('--provision-ssh')
            entry['phase'] = 'starting'
            save_state(path, data)
            command(args, input=password + '\n' if provision else None, timeout=300)
            entry['phase'] = 'ready'
            save_state(path, data)
        if entry['phase'] != 'ready':
            raise HostError(f'{name}: interrupted first boot; inspect UTM before continuing')
        print(f'{name}: installed, UUID {entry["id"]}; finish guest setup before registering its runner')


def start(spec, path):
    preflight(spec)
    data = read_state(path, spec)
    if set(data['vms']) != {vm['name'] for vm in spec['vm']}:
        raise HostError('Create all configured VMs before starting them')
    live = inventory()
    check_owned(data, live)
    if any(live[entry['id']]['status'] == 'stopped' for entry in data['vms'].values()):
        check_other_running(data, live)
    for name, entry in data['vms'].items():
        if entry['phase'] != 'ready':
            raise HostError(f'{name}: setup incomplete; run create and inspect UTM')
    for name, entry in data['vms'].items():
        status = live[entry['id']]['status']
        if status == 'stopped':
            command([CTL, 'start', entry['id']], timeout=300)
        elif status != 'started':
            raise HostError(f'{name}: {status}; left unchanged')
        print(f'{name}: running')


def login_agent(config, spec, path):
    data = read_state(path, spec)
    if set(data['vms']) != {vm['name'] for vm in spec['vm']} or any(x['phase'] != 'ready' for x in data['vms'].values()):
        raise HostError('Create and finish configuring all VMs before enabling login startup')
    check_owned(data, inventory())
    target = Path.home() / 'Library/LaunchAgents' / (LABEL + '.plist')
    from . import background
    definition = {'Label': LABEL, 'RunAtLoad': True, **background.properties(),
                  'ProgramArguments': background.arguments(['host', 'start', '--config', str(config)]),
                  'StandardOutPath': str(STATE / 'login.log'), 'StandardErrorPath': str(STATE / 'login.log')}
    content = plistlib.dumps(definition)
    if target.exists():
        if target.read_bytes() != content:
            raise HostError(f'A different login agent already exists: {target}; inspect it before replacement')
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open('xb') as file:
            file.write(content)
    print(f'Installed {target}. It will run at each user login.')
    print('Load the agent once while unlocked to approve its own Automation permission; see docs/hosts.md.')


def verify_utm_trust():
    """Keep signature and Gatekeeper failures distinct; never override trust."""
    command(['/usr/bin/codesign', '--verify', '--deep', '--strict', UTM], timeout=120)
    try:
        command(['/usr/sbin/spctl', '--assess', '--type', 'execute', '--verbose=4', UTM], timeout=120)
    except HostError as error:
        raise HostError(f'{error}\nUTM did not pass Gatekeeper assessment. '
                        'Retain the diagnostic and follow docs/setup.md#utm-trust-checks; '
                        'a valid code signature alone is not Gatekeeper acceptance.') from error


def bootstrap():
    brew = next((p for p in ('/opt/homebrew/bin/brew', '/usr/local/bin/brew') if Path(p).is_file()), None)
    if not brew:
        raise HostError('Run through ./setup host bootstrap to install prerequisites')
    if not UTM.exists():
        # Disable automatic upgrades/cleanup of unrelated software.
        command(['/usr/bin/env', 'HOMEBREW_NO_AUTO_UPDATE=1', 'HOMEBREW_NO_INSTALL_CLEANUP=1',
                 brew, 'install', '--yes', '--cask', 'utm'], timeout=3600)
    verify_utm_trust()
    check_utm()
    print('Host prerequisites ready. Open UTM once, then run host plan with your host configuration.')
    print('Host security, power settings, remote access, and runner services were not changed.')


def main(argv):
    parser = argparse.ArgumentParser(prog='setup host', description=__doc__)
    parser.add_argument('action', choices=['bootstrap', 'init', 'plan', 'create', 'configure', 'xcode-login', 'refresh-xcode', 'xcode-agent', 'start', 'status', 'progress', 'resume', 'login-agent', 'check', 'power'])
    parser.add_argument('--config', type=Path, default=Path.home() / 'runner-host.toml')
    parser.add_argument('--json', action='store_true', help='progress: machine-readable worker state')
    parser.add_argument('--vm', help='configure: operate on only this configured VM name')
    parser.add_argument('--provision', action='store_true', help='create the guest account at first boot (macOS 27+)')
    parser.add_argument('--password-stdin', action='store_true', help='read the new guest password from a pipe; requires --provision')
    parser.add_argument('--keychain-passwords', action='store_true', help='generate a unique password per VM in the host login Keychain; requires --provision')
    parser.add_argument('--image', type=Path, help='init: optional local restore image; otherwise download the latest compatible image from Apple')
    parser.add_argument('--name', action='append', help='init: VM name; repeat for two guests')
    parser.add_argument('--discover-names', action='store_true', help='init: choose the two lowest unused GitHub Runner numbers using the stored token')
    parser.add_argument('--reserve-memory-gib', type=int,
                        help='init: host RAM reserve (default: one quarter of RAM, minimum 8 GiB, maximum 16 GiB)')
    parser.add_argument('--cpu-ratio', type=float, default=DEFAULT_CPU_RATIO,
                        help='init: CPU allocation multiplier, 1–2 (default: 1.5; use 1 to avoid oversubscription)')
    parser.add_argument('--automatic-login', action='store_true', help='init: enable automatic guest login at first boot')
    parser.add_argument('--remote-login', action='store_true', help='init: enable guest SSH at first boot')
    parser.add_argument('--dedicated-host', action='store_true', help='power: confirm and apply AC system sleep and supported power-recovery settings')
    args = parser.parse_args(argv)
    if args.json and args.action != 'progress':
        parser.error('--json is only supported with progress')
    if args.dedicated_host and args.action != 'power':
        parser.error('--dedicated-host is only supported with power')
    if args.vm and args.action != 'configure':
        parser.error('--vm is only supported with configure')
    if args.provision and args.action != 'create':
        parser.error('--provision is only supported with create')
    if args.password_stdin and not args.provision:
        parser.error('--password-stdin requires --provision')
    if args.keychain_passwords and (not args.provision or args.password_stdin):
        parser.error('--keychain-passwords requires --provision and cannot be combined with --password-stdin')
    if args.discover_names and (args.action != 'init' or args.name):
        parser.error('--discover-names is only supported with init and cannot be combined with --name')
    if args.action != 'init' and (args.image or args.name or args.automatic_login or args.remote_login
                                  or args.reserve_memory_gib is not None or args.cpu_ratio != DEFAULT_CPU_RATIO):
        parser.error('image, name, sizing, and login options are only supported with init; edit the config before create')
    try:
        if args.action == 'bootstrap':
            bootstrap()
            return 0
        config = args.config.expanduser().resolve()
        if args.action in ('progress', 'resume'):
            from . import preparation
            if args.action == 'progress':
                return preparation.show(config, as_json=args.json)
            preparation.launch(config)
            return 0
        if args.action in ('check', 'power'):
            from . import host_readiness
            if args.action == 'check':
                return host_readiness.check(config)
            host_readiness.configure_power(apply=args.dedicated_host)
            return 0
        if args.action == 'init':
            if config.exists():
                raise HostError('Configuration already exists; it will not be overwritten')
            names = discover_names(interactive=sys.stdin.isatty()) if args.discover_names else args.name
            if not names:
                parser.error('init requires --discover-names or explicit --name values')
            init_config(config, args.image, names,
                        reserve_memory=args.reserve_memory_gib, cpu_ratio=args.cpu_ratio,
                        automatic_login=args.automatic_login, remote_login=args.remote_login)
            return 0
        if args.action == 'xcode-login':
            from .xcode_cache import login
            with locked():
                login()
            return 0
        spec = load(config)
        path = state_path(config)
        if args.action == 'plan':
            preflight(spec)
            read_state(path, spec)
            print(json.dumps(spec, indent=2))
            print(f'State: {path}')
            print('Create installs macOS only on newly created disks; it never reinstalls existing VMs.')
            return 0
        if args.action == 'status':
            data = read_state(path, spec)
            from .preparation import snapshot
            print(json.dumps({
                'provisioning': snapshot(config),
                'state': data,
                'utm': inventory(),
                'runner_setup': 'unverified; run ./setup status inside each guest',
                'phase_note': 'ready means macOS was installed and first boot was requested, not runner readiness',
            }, indent=2))
            return 0
        if args.action == 'refresh-xcode':
            from .xcode_cache import wait_for_guests
            # launchd gives RunAtLoad agents no ordering guarantee. Never hold
            # the startup lock while waiting for the startup agent's guests.
            wait_for_guests(spec, path)
        with locked(wait_timeout=600 if args.action in {'start', 'refresh-xcode'} else 0):
            if args.action == 'create':
                password = None
                if args.password_stdin:
                    if sys.stdin.isatty():
                        raise HostError('Pipe the password on stdin, or omit --password-stdin for a hidden prompt')
                    password = sys.stdin.readline(4098).removesuffix('\n')
                create(spec, path, provision=args.provision, password=password, keychain_passwords=args.keychain_passwords)
            elif args.action == 'start':
                start(spec, path)
            elif args.action == 'refresh-xcode':
                from .xcode_cache import refresh
                refresh(spec, path)
            elif args.action == 'xcode-agent':
                from .xcode_cache import agent
                agent(config, spec, path)
            elif args.action == 'configure':
                from .guests import configure
                configure(spec, path, args.vm)
            else:
                login_agent(config, spec, path)
        return 0
    except HostBusy as error:
        print(f'host: {error}', file=sys.stderr)
        return 2
    except (HostError, OSError, ValueError, KeyError, zipfile.BadZipFile, plistlib.InvalidFileException) as error:
        print(f'host: {error}', file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print('Interrupted; existing VM disks retained. Inspect UTM before continuing.', file=sys.stderr)
        return 130
