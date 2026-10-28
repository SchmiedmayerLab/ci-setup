# This source file is part of the macOS Self-Hosted GitHub Actions Runner open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
# SPDX-License-Identifier: MIT

"""Toolchain selection and simulator probes for explicitly dispatched CI validation."""

import argparse
import json
import os
from pathlib import Path
import re

from . import xcode
from .util import SetupError, output, run


PLATFORMS = (('iOS', 'iPhone', 'iphonesimulator'), ('tvOS', 'Apple TV', 'appletvsimulator'),
             ('watchOS', 'Apple Watch', 'watchsimulator'), ('visionOS', 'Apple Vision', 'xrsimulator'))


def version_key(identifier):
    parsed = xcode.parse_identifier(identifier)
    if parsed is None:
        raise SetupError('Expected an exact Xcode version identifier')
    return xcode.Release(*parsed, identifier, '').sort_key()


def installed_toolchain(installed, selector, default_dir):
    """Resolve one installed toolchain without changing machine-wide selection."""
    if selector == 'default':
        matches = [item for item in installed
                   if (Path(item.path) / 'Contents/Developer').resolve() == Path(default_dir).resolve()]
    else:
        key = version_key(selector)
        matches = [item for item in installed if xcode.parse_identifier(item.identifier) is not None
                   and version_key(item.identifier) == key]
    if len(matches) != 1:
        raise SetupError(f'Expected exactly one installed Xcode for {selector}; found {len(matches)}')
    item = matches[0]
    if not Path(item.path).is_absolute() or not item.path.isprintable():
        raise SetupError('Invalid installed Xcode path')
    return item


def record(message):
    print(message, flush=True)
    summary = os.environ.get('GITHUB_STEP_SUMMARY')
    if summary:
        with Path(summary).open('a') as stream:
            stream.write(message + '\n\n')


def select_xcode(selector):
    runner_default = os.environ.get('DEVELOPER_DIR', '')
    global_default = output(xcode.GLOBAL_SELECTION_COMMAND, timeout=30)
    if not runner_default or Path(runner_default).resolve() != Path(global_default).resolve():
        raise SetupError('Runner DEVELOPER_DIR and global Xcode selection must agree before validation')
    installed = xcode.parse_installed(output(['xcodes', 'installed'], timeout=60))
    item = installed_toolchain(installed, selector, runner_default)
    developer = Path(item.path) / 'Contents/Developer'
    if not developer.is_dir():
        raise SetupError('Selected developer directory is missing')
    actual = output(['/usr/bin/xcodebuild', '-version'],
                    env=dict(os.environ, DEVELOPER_DIR=str(developer)), timeout=60)
    match = re.fullmatch(r'Xcode (\d+(?:\.\d+){0,2})\s+Build version ([A-Za-z0-9]+)', actual)
    if (match is None or match[2] != item.build
            or version_key(match[1])[0] != version_key(item.identifier)[0]):
        raise SetupError('Selected Xcode version/build differs from the installed inventory')
    with Path(os.environ['GITHUB_ENV']).open('a') as stream:
        stream.write(f'DEVELOPER_DIR={developer}\n')
    record(f'Xcode {item.identifier} ({item.build}); developer directory: {developer}')


def simulator_plan(inventory, matches):
    """Select current available runtimes compatible with this toolchain's SDKs."""
    if not isinstance(matches, dict) or not matches:
        raise SetupError('Missing SDK/runtime matches for the selected Xcode')
    plan = []
    for platform, family, platform_id in PLATFORMS:
        trains, builds = set(), set()
        for entry in matches.values():
            if not isinstance(entry, dict):
                raise SetupError('Invalid SDK/runtime match entry')
            train = xcode._runtime_train(entry.get('platform'), entry.get('sdkVersion'))
            if train and train[0] == platform:
                trains.add(train)
                builds.update(entry[key] for key in ('chosenRuntimeBuild', 'defaultBuild')
                              if isinstance(entry.get(key), str) and entry[key])
        candidates = [runtime for runtime in inventory['runtimes']
                      if runtime.get('isAvailable') is True and runtime['name'].startswith(platform + ' ')
                      and (xcode._runtime_train('com.apple.platform.' + platform_id, runtime.get('version')) in trains
                           or runtime.get('buildversion') in builds)]
        if not candidates:
            raise SetupError(f'Missing available {platform} runtime compatible with the selected Xcode SDK')
        # Prefer released runtime builds within the compatible set. An RC SDK
        # may have only prerelease matches; report the exact build in either case.
        released = [runtime for runtime in candidates if not re.search(r'[a-z]$', runtime['buildversion'])]
        runtime = max(released or candidates, key=lambda r: (
            tuple(int(p) for p in r['version'].split('.')), r['buildversion']))
        supported = {device['identifier'] for device in runtime.get('supportedDeviceTypes', [])}
        devices = [device for device in inventory['devicetypes'] if family in device['name']
                   and (not supported or device['identifier'] in supported)]
        if not devices:
            raise SetupError(f'Missing compatible {platform} simulator device type')
        plan.append((platform, runtime, devices[-1]['identifier']))
    return plan


def simctl(*args, timeout=300):
    return output(['xcrun', 'simctl', *args], timeout=timeout)


def boot_simulators():
    inventory = json.loads(simctl('list', '-j'))
    matches = json.loads(simctl('runtime', 'match', 'list', '-j'))
    # Resolve every platform before creating a device, so missing pinned
    # runtimes cannot be hidden by successfully booting the newest toolchain's.
    for platform, runtime, device_type in simulator_plan(inventory, matches):
        device = simctl('create', 'Runner Validation ' + platform, device_type, runtime['identifier'])
        try:
            simctl('boot', device)
            simctl('bootstatus', device, '-b')
            record(f"{platform} {runtime['version']} ({runtime['buildversion']}): booted")
        finally:
            try:
                run(['xcrun', 'simctl', 'shutdown', device], check=False, capture=True, timeout=60)
            finally:
                simctl('delete', device, timeout=60)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('select-xcode').add_argument('selector')
    commands.add_parser('simulators')
    args = parser.parse_args()
    if args.command == 'select-xcode':
        select_xcode(args.selector)
    else:
        boot_simulators()


if __name__ == '__main__':
    main()
