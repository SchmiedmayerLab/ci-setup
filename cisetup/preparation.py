# This source file is part of the macOS Self-Hosted GitHub Actions Runner open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
# SPDX-License-Identifier: MIT

"""Interactive host preparation and a persistent, non-interactive installer."""

import argparse
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import plistlib
import re
import shlex
import subprocess
import sys
import threading
import time

from . import background, config, guests, host, host_readiness, install, util, xcode_cache

SCHEMA = 1
TERMINAL = {'configured', 'needs_attention', 'cancelled'}
RECOVERY = {
    'access': 'Run prepare host in the local Terminal to renew credentials or permissions; inspect host power settings.',
    'image': 'Inspect restore-image cache and available space; resume the same approved configuration.',
    'guests': 'Inspect UTM and saved ownership state; never delete state or recreate an existing VM to bypass an error.',
    'guest_access': 'Check first boot and Local Network/Keychain access for the Runner Host app, then resume.',
    'configure': 'Inspect the named guest provisioning phase locally, preserve registrations, then resume.',
    'agents': 'Inspect host agent definitions and logs; a loaded or running agent is not a successful execution.',
    'verify': 'Inspect guest status and GitHub availability before reporting installation ready.',
}


def paths(path):
    stem = host.state_path(path).stem
    return (host.STATE / (stem + '.preparation.json'), host.STATE / (stem + '.progress.json'),
            host.STATE / (stem + '.provision.log'))


def label(path):
    return 'com.selfhosted-runner.provision.' + host.state_path(path).stem


def read_json(path):
    if path.is_symlink():
        raise host.HostError('Setup state must not be a symlink; inspect it before continuing')
    return json.loads(path.read_text())


def write_json(path, value):
    if path.is_symlink():
        raise host.HostError('Setup state must not be a symlink; inspect it before continuing')
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    host.save_state(path, value)
    path.chmod(0o600)


def binding(path):
    root = host.ROOT.resolve()
    for protected in ('Desktop', 'Documents', 'Downloads'):
        if root.is_relative_to(Path.home() / protected) or path.is_relative_to(Path.home() / protected):
            raise host.HostError('Keep the checkout and host configuration outside Desktop, Documents and Downloads before preparation')
    git = ['git', '-C', str(root)]
    if host.command([*git, 'status', '--porcelain', '--untracked-files=normal']):
        raise host.HostError('Commit the reviewed setup changes before preparation; guests require clean committed source')
    host.source_branch()
    return {'uid': os.getuid(), 'root': str(root), 'revision': host.command([*git, 'rev-parse', 'HEAD']),
            'python': str(Path(sys.executable).resolve()), 'config_path': str(path),
            'settings_sha256': host.digest(root / 'config.toml')}


def read_receipt(path, *, verify=True):
    receipt = read_json(paths(path)[0])
    if not isinstance(receipt, dict) or receipt.get('schema') != SCHEMA or not isinstance(receipt.get('plan'), dict):
        raise host.HostError('Missing or invalid preparation record; run prepare host in the local Terminal')
    if verify and receipt.get('binding') != binding(path):
        raise host.HostError('Source, interpreter, settings or user changed since preparation; review with prepare host again')
    if path.exists():
        spec = host.load(path)
        expected = receipt.get('materialized')
        if expected is None and 'restore' not in receipt['plan']:
            expected = receipt['plan']
        if verify and expected is not None and spec != expected:
            raise host.HostError('Host configuration changed since preparation; review it before resuming')
    for name, identifier in receipt.get('owned', {}).items():
        if not path.exists():
            raise host.HostError('An owned layout is missing; restore its configuration before resuming')
        state = host.read_state(host.state_path(path), host.load(path))
        if state['vms'].get(name, {}).get('id') != identifier:
            raise host.HostError('Owned VM identity changed since preparation; inspect state before continuing')
    return receipt


@contextmanager
def exclusive(path):
    host.STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = host.STATE / (host.state_path(path).stem + '.provision.lock')
    if lock.is_symlink():
        raise host.HostError('Provisioning lock must not be a symlink')
    with lock.open('a') as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise host.HostError('Provisioning or preparation is already running for this configuration') from None
        yield


class Progress:
    def __init__(self, path):
        self.path = path
        previous = snapshot(path)
        self.data = {'schema': SCHEMA, 'state': 'running', 'phase': 'access',
                     'started_at': time.time(), 'phase_started_at': time.time(),
                     'last_completed_phase': previous.get('last_completed_phase'),
                     'attempt': previous.get('attempt', 0) + 1, 'pid': os.getpid()}
        self.lock = threading.Lock()
        self.stopped = threading.Event()

    def save(self, **changes):
        with self.lock:
            self.data.update(changes, heartbeat_at=time.time())
            write_json(paths(self.path)[1], self.data)

    def phase(self, name):
        self.save(phase=name, phase_started_at=time.time(), recovery=RECOVERY.get(name, 'Inspect host progress and resume.'))
        print('Provisioning phase: ' + name, flush=True)

    def done(self, name):
        self.save(last_completed_phase=name, last_completed_at=time.time())

    def heartbeat(self):
        while not self.stopped.wait(15):
            self.save()

    def __enter__(self):
        self.save()
        self.thread = threading.Thread(target=self.heartbeat, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.stopped.set()
        self.thread.join(timeout=5)


def snapshot(path):
    progress = paths(path)[1]
    if not progress.exists():
        return {'state': 'not_prepared', 'attempt': 0}
    data = read_json(progress)
    if not isinstance(data, dict) or data.get('schema') != SCHEMA:
        raise host.HostError('Invalid provisioning progress file; inspect it before continuing')
    result = dict(data)
    if data.get('state') == 'running' and time.time() - data.get('heartbeat_at', 0) > 90:
        result['state'] = 'interrupted_or_unresponsive'
    result['log'] = str(paths(path)[2])
    result['resume'] = shlex.join([str(host.ROOT / 'setup'), 'host', 'resume', '--config', str(path)])
    result['ci_validation'] = 'not performed by the installer; run approved real workflows separately'
    return result


def show(path, *, as_json=False):
    data = snapshot(path)
    if as_json:
        print(json.dumps(data, indent=2))
    else:
        for key in ('state', 'phase', 'last_completed_phase', 'attempt', 'recovery', 'log', 'resume', 'ci_validation'):
            if key in data:
                print(f'{key.replace("_", " ")}: {data[key]}')
        if 'started_at' in data:
            print(f'Elapsed this attempt: {int(time.time() - data["started_at"])} seconds')
        if 'heartbeat_at' in data:
            print(f'Worker heartbeat: {int(time.time() - data["heartbeat_at"])} seconds ago (not download progress)')
    return 1 if data['state'] in {'needs_attention', 'interrupted_or_unresponsive'} else 0


def agent_definition(path):
    return {'Label': label(path), 'RunAtLoad': True, 'ProcessType': 'Background',
            'ProgramArguments': background.arguments(['install', 'host', '--prepared', '--agent', '--config', str(path)]),
            **background.properties(),
            'StandardInPath': '/dev/null', 'StandardOutPath': str(paths(path)[2]),
            'StandardErrorPath': str(paths(path)[2]), 'Umask': 0o077,
            'EnvironmentVariables': {'PYTHONUNBUFFERED': '1', 'HOMEBREW_NO_AUTO_UPDATE': '1',
                                     'HOMEBREW_NO_INSTALL_CLEANUP': '1'}}


def launch(path):
    """Start or resume exactly one approved worker; never kill a running worker."""
    read_receipt(path)
    definition = agent_definition(path)
    agent = Path.home() / 'Library/LaunchAgents' / (label(path) + '.plist')
    content = plistlib.dumps(definition)
    if agent.exists() and (agent.is_symlink() or agent.read_bytes() != content):
        raise host.HostError('A different provisioning agent exists; inspect it before replacing')
    agent.parent.mkdir(parents=True, exist_ok=True)
    if not agent.exists():
        with agent.open('xb') as stream:
            stream.write(content)
        agent.chmod(0o600)
    log = paths(path)[2]
    if log.is_symlink():
        raise host.HostError('Provisioning log must not be a symlink')
    log.touch(mode=0o600, exist_ok=True)
    log.chmod(0o600)
    domain = f'gui/{os.getuid()}'
    service = domain + '/' + label(path)
    loaded = subprocess.run(['/bin/launchctl', 'print', service], capture_output=True, text=True, timeout=15)
    if loaded.returncode == 0:
        arguments = re.search(r'^\s*arguments = \{\n(.*?)^\s*\}', loaded.stdout, re.M | re.S)
        if not arguments or [line.strip() for line in arguments[1].splitlines() if line.strip()] != definition['ProgramArguments']:
            raise host.HostError('Loaded provisioning command differs; inspect it before resuming')
        if re.search(r'^\s*state = running\s*$', loaded.stdout, re.M):
            print('Provisioning is already running. Use host progress to inspect it.')
            return
    with exclusive(path):
        # Explicit prepare/resume is the only way out of a terminal state. A
        # login restart may continue an interrupted run but never an error loop.
        prior = snapshot(path)
        write_json(paths(path)[1], dict(prior, schema=SCHEMA, state='queued', heartbeat_at=time.time()))
    if loaded.returncode:
        host.command(['/bin/launchctl', 'bootstrap', domain, agent], timeout=30)
    else:
        host.command(['/bin/launchctl', 'kickstart', service], timeout=30)
    print('Background provisioning started. This terminal can now be closed.', flush=True)
    show(path)


def safe_failure(error, phase):
    if isinstance(error, guests.GuestConnectionError):
        return 'Guest network connection failed; check Local Network access for the Runner Host app and VM networking.'
    if isinstance(error, xcode_cache.AppleAccessUnavailable):
        return 'Apple or download-tool availability failed; retry later without replacing credentials blindly.'
    return RECOVERY.get(phase, 'Run prepare host again to review the source, saved configuration and access.')


@contextmanager
def unattended():
    previous = host.UNATTENDED, util.INTERACTIVE
    host.UNATTENDED, util.INTERACTIVE = True, False
    try:
        yield
    finally:
        host.UNATTENDED, util.INTERACTIVE = previous


def cancel_pending(path):
    try:
        with exclusive(path):
            if paths(path)[0].exists():
                prior = snapshot(path)
                write_json(paths(path)[1], dict(prior, schema=SCHEMA, state='cancelled', heartbeat_at=time.time(),
                                               recovery='Preparation cancelled; explicitly prepare or resume when ready.'))
    except host.HostError:
        # An already-running worker belongs to a previous invocation; never
        # cancel it as a side effect of cancelling a competing prepare command.
        pass


def run(path, *, agent=False):
    """Never read interactive input; use only previously reviewed host preparation."""
    try:
        with exclusive(path):
            if agent:
                state = snapshot(path)['state']
                # An older RunAtLoad agent may still exist after a new
                # --no-start preparation. Login is not approval to launch it.
                if state == 'prepared':
                    return 0
                if state in TERMINAL:
                    return 0 if state == 'configured' else 1
            with unattended(), Progress(path) as progress:
                try:
                    receipt = read_receipt(path)
                    host.check_machine(provision=True)
                    host_readiness.require_awake()
                    host.runner_credentials()
                    if config.load(host.ROOT).xcode_manage:
                        install.apple_access()
                    progress.done('access')
                    with host.locked():
                        progress.phase('image')
                        spec = host.materialize_config(path, receipt.get('materialized') or receipt['plan'])
                        receipt['materialized'] = spec
                        write_json(paths(path)[0], receipt)
                        install.check_host_layout(path, spec, allow_download=True)
                        progress.done('image')
                    install.install_host(path, spec, progress=progress, verify_agents=True)
                    progress.save(state='configured', recovery='Installation configured. Complete coordinated recovery and approved real CI validation.')
                    print('Installation configured; real CI validation remains separate.', flush=True)
                    return 0
                except host.HostBusy:
                    progress.save(state='deferred', recovery='Host or guest maintenance is busy, or guests are not ready; inspect service logs and resume when ready.')
                    return 2
                except (KeyboardInterrupt, SystemExit):
                    progress.save(state='cancelled', recovery='Inspect retained VM state, then explicitly resume when ready.')
                    return 130
                except Exception as error:
                    # Third-party output can contain credentials. Persist only
                    # fixed categories, never repr(error), traceback or stderr.
                    detail = safe_failure(error, progress.data['phase'])
                    progress.save(state='needs_attention', recovery=detail)
                    print('Setup needs attention: ' + detail, file=sys.stderr, flush=True)
                    return 1
    except (host.HostError, OSError, ValueError):
        print('Cannot start worker: preparation is missing, invalid, or another worker is active. Inspect host progress.', file=sys.stderr)
        return 1


def prepare(args):
    path = args.config.expanduser().resolve()
    if not sys.stdin.isatty():
        raise host.HostError('Run prepare host in a local Terminal while logged in and unlocked')
    with exclusive(path), host.locked():
        current = binding(path)
        cfg = config.load(host.ROOT)
        if cfg.brew_extra_casks:
            raise host.HostError('Custom managed casks can request late prompts; install them separately before preparation')
        host.check_machine(provision=True)
        install.prerequisites('host', cfg)
        if args.dedicated_host and not host_readiness.configure_power(apply=True):
            raise install.SetupCancelled()
        host_readiness.require_awake()
        host.runner_credentials(interactive=True)
        if cfg.xcode_manage:
            install.apple_access(interactive=True)
        background.ensure()
        background.request_network_access()
        install.background_check('host')
        previous = read_receipt(path, verify=False) if paths(path)[0].exists() else None
        if path.exists():
            if args.image or args.name or args.reserve_memory_gib is not None or args.cpu_ratio != host.DEFAULT_CPU_RATIO:
                raise host.HostError('Existing configuration retained; omit image, name and sizing options when resuming')
            plan = host.load(path)
            host.preflight(plan)
            state = host.read_state(host.state_path(path), plan)
            host.check_owned(state, host.inventory())
        elif previous and previous.get('binding') == current and not args.image and not args.name and args.reserve_memory_gib is None and args.cpu_ratio == host.DEFAULT_CPU_RATIO:
            plan = previous['plan']
            state = {'vms': {}}
        else:
            plan = host.plan_config(args.image, args.name or host.discover_names(),
                                    reserve_memory=args.reserve_memory_gib, cpu_ratio=args.cpu_ratio,
                                    automatic_login=True, remote_login=True)
            state = {'vms': {}}
        host.preflight(plan)
        live = host.inventory()
        if not state['vms']:
            host.check_other_running(state, live)
            if any(vm['name'].casefold() == item['name'].casefold() for vm in plan['vm'] for item in live.values()):
                raise host.HostError('A planned name already belongs to another VM; review names before handoff')
        if not all(plan['guest'].get(key) for key in ('automatic_login', 'remote_login')):
            raise host.HostError('Prepared hosts require the reviewed guest automatic-login and SSH settings')
        unchanged = (previous and previous.get('binding') == current
                     and (previous.get('materialized') or previous['plan']) == plan)
        print(f'Destination: {cfg.github_url}; group: {cfg.group or "Default"}', flush=True)
        if not unchanged:
            install.confirm_host(plan['vm'])
        else:
            print('Reusing the unchanged approved plan.', flush=True)
        receipt = {'schema': SCHEMA, 'binding': current, 'plan': plan,
                   'owned': {name: entry['id'] for name, entry in state['vms'].items()},
                   'prepared_at': time.time()}
        if path.exists():
            receipt['materialized'] = plan
        if state['vms'] and all(live[entry['id']]['status'] == 'started' for entry in state['vms'].values()):
            install.background_check('guest', path)
        write_json(paths(path)[0], receipt)
        write_json(paths(path)[1], {'schema': SCHEMA, 'state': 'prepared', 'phase': 'access',
                                  'heartbeat_at': time.time(), 'attempt': 0})
    print('Interactive preparation complete; no host administrator password was saved.', flush=True)
    print('New guests still need a background SSH check after first boot. Later consent or expired access stops with needs attention.', flush=True)
    if args.no_start:
        print('Prepared only. Run: ' + shlex.join([str(host.ROOT / 'setup'), 'host', 'resume', '--config', str(path)]))
    else:
        launch(path)
    return 0


def main(argv):
    parser = argparse.ArgumentParser(prog='setup prepare', description=__doc__)
    parser.add_argument('layout', choices=['host'])
    parser.add_argument('--config', type=Path, default=Path.home() / 'runner-host.toml')
    parser.add_argument('--dedicated-host', action='store_true')
    parser.add_argument('--no-start', action='store_true', help='save preparation without launching the worker')
    parser.add_argument('--image', type=Path)
    parser.add_argument('--name', action='append')
    parser.add_argument('--reserve-memory-gib', type=int)
    parser.add_argument('--cpu-ratio', type=float, default=host.DEFAULT_CPU_RATIO)
    args = parser.parse_args(argv)
    try:
        return prepare(args)
    except install.SetupCancelled:
        cancel_pending(args.config.expanduser().resolve())
        print('Preparation cancelled. Existing prerequisites and owned resources retained; no worker started.')
        return 0
    except (KeyboardInterrupt, EOFError):
        cancel_pending(args.config.expanduser().resolve())
        print('Preparation interrupted; no new worker launched. Inspect progress before resuming.', file=sys.stderr)
        return 130
    except (host.HostError, util.SetupError, OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        # Preparation is interactive but still must not echo raw API/session errors.
        print('Preparation stopped: ' + (str(error) if isinstance(error, host.HostError) else
              'check the current preparation phase and retry in the local Terminal'), file=sys.stderr)
        return 1
