# This source file is part of the macOS Self-Hosted GitHub Actions Runner open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
# SPDX-License-Identifier: MIT

"""Guided preparation followed by the existing unattended provisioning paths."""

import argparse
from contextlib import contextmanager
import getpass
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import tempfile
import time
import uuid

from . import config, guests, host, host_readiness, maintenance, power, restore, runner, runlog, util, xcode, xcode_cache


def apple_access(interactive=False):
    try:
        xcode_cache.command(['update', '--data-source', 'apple'], timeout=120)
    except host.HostError:
        if not interactive:
            raise
        xcode_cache.login()
        xcode_cache.command(['update', '--data-source', 'apple'], timeout=120)


def probe(layout, result):
    """Run under launchd; write only fixed diagnostics, never command output."""
    stage = 'GitHub token and login Keychain'
    try:
        cfg = host.runner_credentials()
        if cfg.xcode_manage:
            stage = 'Apple session and login Keychain'
            apple_access()
        if layout == 'host':
            stage = 'UTM command-line automation'
            host.inventory()
            stage = 'UTM AppleScript automation'
            host.command(['/usr/bin/osascript', '-e', 'tell application "UTM" to get version'], timeout=30)
        payload = {'ok': True}
    except Exception:
        payload = {'ok': False, 'stage': stage}
    pending = result.with_suffix('.pending')
    pending.write_text(json.dumps(payload))
    pending.chmod(0o600)
    pending.replace(result)


def background_check(layout):
    """Exercise credentials and Automation in the same interpreter as the agents."""
    host.STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    domain = f'gui/{os.getuid()}'
    host.command(['/bin/launchctl', 'print', domain], timeout=15)
    with tempfile.TemporaryDirectory(prefix='preflight-', dir=host.STATE) as directory:
        root = Path(directory)
        result = root / 'result.json'
        label = 'com.selfhosted-runner.preflight.' + uuid.uuid4().hex
        plist = root / 'probe.plist'
        plist.write_bytes(plistlib.dumps({
            'Label': label, 'RunAtLoad': True,
            'WorkingDirectory': str(host.ROOT),
            'ProgramArguments': [sys.executable, '-m', 'cisetup.install', '_probe', layout, str(result)],
            'StandardOutPath': '/dev/null', 'StandardErrorPath': '/dev/null',
        }))
        plist.chmod(0o600)
        print('Checking background access. Approve any macOS Keychain or Automation prompts now.', flush=True)
        try:
            host.command(['/bin/launchctl', 'bootstrap', domain, plist], timeout=30)
            deadline = time.monotonic() + 240
            while not result.exists():
                if time.monotonic() >= deadline:
                    raise host.HostError('Background checks timed out; check login, Keychain and Automation permissions, then retry')
                time.sleep(1)
            payload = json.loads(result.read_text())
            if not payload.get('ok'):
                raise host.HostError(f'Background check failed: {payload["stage"]}. Resolve access in this login session, then retry')
        finally:
            subprocess.run(['/bin/launchctl', 'bootout', f'{domain}/{label}'],
                           capture_output=True, timeout=30)


def prerequisites(layout, cfg):
    if layout == 'host':
        host.bootstrap()
        host.command(['/usr/bin/open', '-a', str(host.UTM)])
        print('Finish UTM first-launch dialogs if shown. The next checks request its Automation access.', flush=True)
    if cfg.xcode_manage:
        try:
            xcode_cache.cli()
        except host.HostError:
            brew = shutil.which('brew')
            if not brew:
                raise host.HostError('Homebrew is missing; run this command through ./setup')
            host.command(['/usr/bin/env', 'HOMEBREW_NO_AUTO_UPDATE=1', 'HOMEBREW_NO_INSTALL_CLEANUP=1',
                          brew, 'install', 'xcodes'], timeout=3600)


class SetupCancelled(Exception):
    pass


def confirm_host(vms):
    print(json.dumps(vms, indent=2))
    print('Guests use automatic login and SSH with unique passwords in this host’s login Keychain.\n'
          'Anyone who can open a guest console can use its logged-in session. Host login and SSH settings stay unchanged.')
    if input('Download any missing image and set up these runner VMs? [y/N] ').strip().lower() != 'y':
        raise SetupCancelled()


def host_plan(args, *, confirm=False):
    path = args.config.expanduser().resolve()
    if not path.exists():
        image = args.image
        names = args.name or host.discover_names()
        with host.locked():
            host.init_config(path, image, names, reserve_memory=args.reserve_memory_gib,
                             cpu_ratio=args.cpu_ratio, automatic_login=True, remote_login=True,
                             confirm=confirm_host if confirm else None)
    elif args.image or args.name:
        raise host.HostError('Existing host configuration retained; omit --image and --name when resuming')
    elif confirm:
        spec = host.load(path)
        host.preflight(spec)
        confirm_host(spec['vm'])
    spec = host.load(path)
    check_host_layout(path, spec, allow_download=True)
    return path, spec


def check_host_layout(path, spec, *, allow_download=False):
    host.preflight(spec)
    host.check_machine(provision=True)
    if not all(spec['guest'].get(key) for key in ('automatic_login', 'remote_login')):
        raise host.HostError('Guided VM setup requires guest automatic_login and remote_login in the initial configuration')
    state = host.read_state(host.state_path(path), spec)
    live = host.inventory()
    host.check_owned(state, live)
    if len(state['vms']) != len(spec['vm']):
        host.check_other_running(state, live)
        for vm in spec['vm']:
            if vm['name'] not in state['vms'] and any(entry['name'].casefold() == vm['name'].casefold() for entry in live.values()):
                raise host.HostError(f'{vm["name"]}: already exists outside this setup; choose a different name')
        missing = [vm for vm in spec['vm'] if vm['name'] not in state['vms']]
        if shutil.disk_usage(Path.home()).free < (sum(vm['disk_gib'] for vm in missing) + 32) * 1024**3:
            raise host.HostError('Not enough free space for the configured guest disks and host reserve')
        if not allow_download and not Path(spec['image']['path']).is_file():
            raise host.HostError('Pinned restore image is missing; rerun install host to retrieve its configured URL')
        if host.image_ready(spec) < 27:
            raise host.HostError('Automatic guest setup requires a macOS 27+ restore image')
        restore.requirements(restore.metadata(Path(spec['image']['path'])), spec['vm'])


def wait_for_guests(spec, state_path, timeout=600):
    deadline = time.monotonic() + timeout
    while True:
        try:
            for _, connection in guests.connections(spec, host.read_state(state_path, spec)):
                connection.run(['/usr/bin/true'], timeout=20)
            return
        except host.HostError:
            if time.monotonic() >= deadline:
                raise host.HostError('Guests did not become reachable over SSH; inspect first boot in UTM, then rerun the same command') from None
            time.sleep(5)


def load_agent(label, *, wait=False):
    domain = f'gui/{os.getuid()}'
    loaded = subprocess.run(['/bin/launchctl', 'print', f'{domain}/{label}'],
                            capture_output=True, timeout=15)
    if loaded.returncode:
        host.command(['/bin/launchctl', 'bootstrap', domain,
                      Path.home() / 'Library/LaunchAgents' / (label + '.plist')], timeout=30)
    if wait:
        deadline = time.monotonic() + 60
        while True:
            status = host.command(['/bin/launchctl', 'print', f'{domain}/{label}'], timeout=15)
            if 'state = running' not in status:
                return
            if time.monotonic() >= deadline:
                raise host.HostError('VM startup agent is still running; retry after it finishes')
            time.sleep(1)


def install_host(path, spec):
    state = host.state_path(path)
    with host.locked():
        host.create(spec, state, provision=True, keychain_passwords=True)
        host.start(spec, state)
        wait_for_guests(spec, state)
        guests.configure(spec, state)
        host.login_agent(path, spec, state)
        xcode_cache.agent(path, spec, state)
    # RunAtLoad jobs take the same host lock. Load them only after releasing it.
    load_agent(host.LABEL, wait=True)
    load_agent('com.selfhosted-runner.xcode-downloads')


@contextmanager
def administrator():
    """Keep one initial administrator credential in memory for this run only."""
    password = getpass.getpass('This dedicated runner Mac’s administrator password (not saved): ')
    if not password or '\n' in password or '\r' in password:
        raise host.HostError('Enter a nonempty administrator password on one line')
    runlog.register_secret(password)
    result = subprocess.run(['/usr/bin/sudo', '-k', '-S', '-p', '', '-v'],
                            input=password + '\n', capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise host.HostError('Administrator authentication failed; no runner installation started')
    with tempfile.TemporaryDirectory(prefix='runner-admin-') as directory:
        askpass = Path(directory) / 'askpass'
        askpass.write_text('#!/bin/sh\n[ -n "$RUNNER_BOOTSTRAP_PASSWORD" ] || exit 1\nprintf "%s\\n" "$RUNNER_BOOTSTRAP_PASSWORD"\n')
        askpass.chmod(0o700)
        saved = {key: os.environ.get(key) for key in ('RUNNER_BOOTSTRAP_PASSWORD', 'SUDO_ASKPASS')}
        os.environ.update(RUNNER_BOOTSTRAP_PASSWORD=password, SUDO_ASKPASS=str(askpass))
        try:
            yield
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
            subprocess.run(['/usr/bin/sudo', '-k'], capture_output=True, timeout=15)


def install_runner(cfg, name):
    with administrator():
        if util.acquire_lock() is not None:
            raise host.HostError('Runner maintenance is already running; retry when it finishes')
        previous = util.INTERACTIVE
        try:
            util.INTERACTIVE = True
            with maintenance.paused(cfg):
                config.save_runner_name(name)
                xcode.ensure_sudoless_select()
                xcode.ensure_wwdr_certificate()
                power.ensure(cfg)
        finally:
            util.INTERACTIVE = previous
            util.release_lock()
        # The child takes its own lock and guard. Its Xcode phase clears the
        # temporary credential before runner registration or service startup.
        return subprocess.run([str(host.ROOT / 'setup'), 'converge', '--non-interactive'],
                              stdin=subprocess.DEVNULL, start_new_session=True,
                              env=dict(os.environ, CI_SETUP_UPDATED='1')).returncode


def main(argv):
    parser = argparse.ArgumentParser(prog='setup install', description=__doc__)
    parser.add_argument('layout', choices=['runner', 'host'])
    parser.add_argument('--check', action='store_true', help='check existing prerequisites and background access without provisioning')
    parser.add_argument('--config', type=Path, default=Path.home() / 'runner-host.toml', help='host configuration; preserved on rerun')
    parser.add_argument('--image', type=Path, help='host: optional local restore image; otherwise download from Apple')
    parser.add_argument('--name', action='append', help='runner name; repeat for two VMs (host default: discover unused numbers)')
    parser.add_argument('--reserve-memory-gib', type=int, default=8)
    parser.add_argument('--cpu-ratio', type=float, default=1.0)
    parser.add_argument('--dedicated-host', action='store_true', help='host: review and apply AC power settings before provisioning')
    args = parser.parse_args(argv)
    if args.dedicated_host and (args.layout != 'host' or args.check):
        parser.error('--dedicated-host requires install host without --check')
    try:
        cfg = config.load(host.ROOT)
        if args.layout == 'host':
            host.check_machine(provision=True)
        elif args.image or args.reserve_memory_gib != 8 or args.cpu_ratio != 1.0 or args.config != Path.home() / 'runner-host.toml':
            raise host.HostError('Image, host configuration and VM sizing options apply only to install host')
        if args.layout == 'runner' and args.name and len(args.name) != 1:
            raise host.HostError('A physical runner takes exactly one --name')
        if not args.check and not sys.stdin.isatty():
            raise host.HostError('Start guided installation in Terminal while logged in and unlocked; use --check for unattended checks')
        if not args.check and cfg.brew_extra_casks:
            raise host.HostError('Custom Homebrew casks can require late dialogs; install them separately and remove them from managed casks before guided setup')
        if args.layout == 'host' and not args.check:
            if host.command(['git', '-C', host.ROOT, 'status', '--porcelain', '--untracked-files=normal']):
                raise host.HostError('Commit reviewed setup changes before installing guests; only committed source is transferred')
        print(f'Layout: {args.layout}. GitHub destination: {cfg.github_url}', flush=True)
        if not args.check:
            prerequisites(args.layout, cfg)
            if args.layout == 'host':
                if args.dedicated_host and not host_readiness.configure_power(apply=True):
                    raise SetupCancelled()
                host_readiness.require_awake()
        host.runner_credentials(interactive=not args.check)
        if cfg.xcode_manage:
            apple_access(interactive=not args.check)
        background_check(args.layout)
        if args.check:
            if args.layout == 'host':
                path = args.config.expanduser().resolve()
                check_host_layout(path, host.load(path))
            print('Background access checks passed. No runners or VMs were provisioned.')
            return host_readiness.check(path) if args.layout == 'host' else 0
        if args.layout == 'host':
            path, spec = host_plan(args, confirm=True)
            install_host(path, spec)
            return host_readiness.check(path)
        else:
            name = config.validate_runner_name(args.name[0] if args.name else cfg.runner_name)
            if config.saved_runner_name() not in (None, name):
                raise host.HostError('A different runner name is saved; review identity changes separately')
            cfg.runner_name = name
            if runner.is_registered(cfg) and not runner.registration_matches(cfg):
                raise host.HostError('Existing runner registration differs; review migration before guided setup')
            print(f'This Mac will become the dedicated runner {name!r}.\n'
                  'Setup installs build tools, enables job cleanup, and applies config.toml machine settings.')
        if input('Continue with this setup? [y/N] ').strip().lower() != 'y':
            print('Stopped before provisioning. Prerequisites and saved configuration are retained.')
            return 0
        return install_runner(cfg, name)
    except SetupCancelled:
        print('Stopped before image download or provisioning. Installed prerequisites are retained.')
        return 0
    except (host.HostError, util.SetupError, OSError, ValueError, subprocess.TimeoutExpired) as error:
        print(f'setup install: {error}', file=sys.stderr)
        return 1
    except (EOFError, KeyboardInterrupt):
        print('Setup interrupted; rerun the same command to resume safely.', file=sys.stderr)
        return 130


if __name__ == '__main__':
    if len(sys.argv) == 4 and sys.argv[1] == '_probe' and sys.argv[2] in ('host', 'runner'):
        probe(sys.argv[2], Path(sys.argv[3]))
    else:
        raise SystemExit(main(sys.argv[1:]))
