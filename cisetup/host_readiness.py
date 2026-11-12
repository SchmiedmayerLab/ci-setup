# This source file is part of the macOS Self-Hosted GitHub Actions Runner open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
# SPDX-License-Identifier: MIT

"""Read-only host checks and explicitly approved AC power settings."""

import os
from pathlib import Path
import plistlib
import re
import shlex
import subprocess
import sys

from . import background, host

POWER = {'sleep': '0', 'autorestart': '1'}
REFRESH = 'com.selfhosted-runner.xcode-downloads'


def probe(args):
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise host.HostError(f'{Path(args[0]).name} could not be read; retry or inspect the host') from error


def ac_settings():
    result = probe(['/usr/bin/pmset', '-g', 'custom'])
    if result.returncode:
        raise host.HostError('Cannot read host power settings; no settings were changed')
    settings = {}
    in_ac = False
    for line in result.stdout.splitlines():
        text = line.strip()
        if text.endswith('Power:'):
            in_ac = text == 'AC Power:'
        elif in_ac:
            parts = text.split()
            if parts and parts[0] in POWER:
                if len(parts) != 2 or not parts[1].isascii() or not parts[1].isdigit() or (parts[0] == 'autorestart' and parts[1] not in ('0', '1')):
                    raise host.HostError('Malformed AC power settings; inspect pmset -g custom')
                if parts[0] in settings:
                    raise host.HostError('Ambiguous AC power settings; inspect pmset -g custom')
                settings[parts[0]] = parts[1]
    if 'sleep' not in settings:
        raise host.HostError('AC sleep setting is unavailable; inspect pmset -g custom before configuring this host')
    return settings


def sleep_check(settings):
    if settings.get('sleep') == '0':
        return ('OK', 'Idle sleep', 'disabled on AC power; display sleep and screen locking may remain enabled')
    result = probe(['/usr/bin/defaults', 'read', 'com.utmapp.UTM', 'PreventIdleSleep'])
    if result.returncode == 0 and result.stdout.strip() in ('1', 'true', 'YES'):
        return ('OK', 'Idle sleep', 'UTM is configured to prevent idle sleep while a VM runs; not a live assertion test')
    return ('ATTENTION', 'Idle sleep', 'enable UTM’s Prevent system from sleeping when any VM is running setting, '
            'or review setup host power --dedicated-host; no sleep prevention was confirmed')


def require_awake():
    status, name, detail = sleep_check(ac_settings())
    print(f'{status}: {name}: {detail}')
    if status != 'OK':
        raise host.HostError('Resolve host sleep prevention before provisioning; see docs/hosts.md')


def configure_power(*, apply=False):
    """Preview by default; apply only after a terminal confirmation and readback."""
    before = ac_settings()
    changes = {key: value for key, value in POWER.items() if key in before and before[key] != value}
    if 'autorestart' not in before:
        print('INFO: Restart after power failure is not reported by this hardware; left unchanged.')
    if not changes:
        print('Supported dedicated-host AC power settings already match; no changes needed.')
        return True
    command = ['/usr/bin/sudo', '/usr/bin/pmset', '-c']
    for key, value in changes.items():
        print(f'AC {key}: {before[key]} -> {value}')
        command += [key, value]
    undo = ['/usr/bin/sudo', '/usr/bin/pmset', '-c']
    for key in changes:
        undo += [key, before[key]]
    print('Apply: ' + shlex.join(command))
    print('Restore previous values: ' + shlex.join(undo))
    print('Only AC system idle sleep and supported power-failure restart settings change. '
          'Battery/UPS policy, display sleep, screen lock, login and encryption remain unchanged. '
          'Preventing idle sleep increases energy use; power recovery still needs FileVault unlock and login.')
    if not apply:
        print('Preview only. On a dedicated host, use setup host power --dedicated-host to confirm and apply.')
        return True
    if not sys.stdin.isatty():
        raise host.HostError('Dedicated-host power settings require confirmation in an interactive terminal')
    if input('Apply these power settings to this dedicated VM host? [y/N] ').strip().lower() != 'y':
        print('Power settings left unchanged.')
        return False
    if ac_settings() != before:
        raise host.HostError('Power settings changed during review; rerun to inspect the new plan')
    try:
        subprocess.run(command, check=True, timeout=120)
    except (OSError, subprocess.SubprocessError) as error:
        raise host.HostError('Power configuration did not complete; inspect pmset -g custom and the restore command above') from error
    after = ac_settings()
    if any(after.get(key) != POWER[key] for key in before):
        raise host.HostError('Power settings did not match after applying; inspect the host before retrying')
    print('Dedicated-host AC power settings applied and verified.')
    return True


def agent_check(label, action, config, disabled):
    path = Path.home() / 'Library/LaunchAgents' / (label + '.plist')
    try:
        definition = plistlib.loads(path.read_bytes())
    except (OSError, ValueError, plistlib.InvalidFileException):
        return ('ATTENTION', label, 'missing or unreadable agent; rerun guided host setup after inspection')
    expected = background.arguments(['host', action, '--config', str(config)])
    args = definition.get('ProgramArguments') if isinstance(definition, dict) else None
    valid = (isinstance(args, list) and all(isinstance(arg, str) for arg in args)
             and args == expected and Path(args[0]).is_absolute() and os.access(args[0], os.X_OK)
             and definition.get('Label') == label and definition.get('RunAtLoad') is True
             and not definition.get('Disabled', False)
             and all(definition.get(key) == value for key, value in background.properties().items()))
    if action == 'refresh-xcode':
        valid = valid and definition.get('StartInterval') == 21600
    if not valid:
        return ('ATTENTION', label, 'agent definition differs from this checkout/configuration or interpreter is missing; inspect before replacing')
    if disabled is None:
        return ('ATTENTION', label, 'cannot read launchd disabled overrides')
    if re.search(r'"' + re.escape(label) + r'"\s*=>\s*true', disabled):
        return ('ATTENTION', label, 'disabled in launchd; review the override before enabling')
    result = probe(['/bin/launchctl', 'print', f'gui/{os.getuid()}/{label}'])
    if result.returncode:
        return ('ATTENTION', label, 'not loaded in this login session; rerun guided setup or follow the documented loading steps')
    loaded_args = re.search(r'^\s*arguments = \{\n(.*?)^\s*\}', result.stdout, re.M | re.S)
    if not loaded_args or [line.strip() for line in loaded_args[1].splitlines() if line.strip()] != args:
        return ('ATTENTION', label, 'loaded command differs from the saved agent; inspect and reload when idle')
    properties = re.search(r'^\s*properties = (.+)$', result.stdout, re.M)
    if not properties or 'runatload' not in properties[1].split(' | '):
        return ('ATTENTION', label, 'loaded agent does not have RunAtLoad; inspect and reload when idle')
    if action == 'refresh-xcode' and not re.search(r'^\s*run interval = 21600 seconds\s*$', result.stdout, re.M):
        return ('ATTENTION', label, 'loaded refresh interval is not six hours; inspect and reload when idle')
    last = re.search(r'^\s*last exit code = (-?\d+)\s*$', result.stdout, re.M)
    if last and last[1] != '0':
        return ('ATTENTION', label, f'last exit code {last[1]}; inspect its log before retrying')
    running = re.search(r'^\s*state = running\s*$', result.stdout, re.M)
    if not last and not running:
        return ('ATTENTION', label, 'loaded but no successful execution is recorded')
    return ('OK', label, 'loaded; currently running' if running else 'loaded; last exit code 0')


def check(config):
    """No credential access, UTM launch, agent installation or setting changes."""
    checks = []
    try:
        settings = ac_settings()
        checks.append(sleep_check(settings))
        if 'autorestart' not in settings:
            checks.append(('INFO', 'Power recovery', 'autorestart is not reported; automatic power recovery is unverified on this hardware'))
        else:
            checks.append(('OK' if settings['autorestart'] == '1' else 'ATTENTION', 'Power recovery',
                           'restart after power failure enabled' if settings['autorestart'] == '1' else 'disabled; review setup host power'))
    except host.HostError as error:
        checks.append(('ATTENTION', 'Power settings', str(error)))
    try:
        host.load(config)
        checks.append(('OK', 'Configuration', 'host configuration is readable'))
    except (host.HostError, OSError, ValueError, KeyError):
        checks.append(('ATTENTION', 'Configuration', 'missing or invalid; use the saved host configuration path'))
    try:
        result = probe(['/bin/launchctl', 'print-disabled', f'gui/{os.getuid()}'])
        disabled = result.stdout if result.returncode == 0 else None
    except host.HostError:
        disabled = None
    for label, action in ((host.LABEL, 'start'), (REFRESH, 'refresh-xcode')):
        try:
            checks.append(agent_check(label, action, config, disabled))
        except host.HostError as error:
            checks.append(('ATTENTION', label, str(error)))
    for status, name, detail in checks:
        print(f'{status}: {name}: {detail}')
    print('Host checks do not test guest health, credentials or cold-boot recovery. '
          'FileVault unlock and host login remain manual; locked screens are supported, logout/sleep are not.')
    return int(any(status == 'ATTENTION' for status, _, _ in checks))
