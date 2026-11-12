# This source file is part of the macOS Self-Hosted GitHub Actions Runner open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
# SPDX-License-Identifier: MIT

"""Discover compatible Apple restore media and retain verified, pinned downloads."""

import json
import os
from pathlib import Path
import re
import shutil
import tempfile
from urllib.parse import urlparse

from . import host


def metadata(path=None):
    # Apple's image-loading API requires the virtualization entitlement even
    # for metadata. Sign only this temporary, locally compiled helper.
    with tempfile.TemporaryDirectory(prefix='runner-restore-') as directory:
        helper = Path(directory) / 'restore-image'
        host.command(['/usr/bin/xcrun', 'swiftc', host.ROOT / 'host/restore-image.swift',
                      '-o', helper], timeout=180)
        host.command(['/usr/bin/codesign', '--sign', '-', '--entitlements',
                      host.ROOT / 'host/restore-image.entitlements', helper])
        arguments = [helper]
        if path is not None:
            arguments.append(Path(path).expanduser().resolve())
        result = json.loads(host.command(arguments, timeout=180))
    if (not isinstance(result, dict) or not re.fullmatch(r'[A-Za-z0-9]+', result.get('build', ''))
            or not re.fullmatch(r'\d+\.\d+\.\d+', result.get('version', ''))
            or any(type(result.get(key)) is not int or result[key] <= 0
                   for key in ('minimum_cpus', 'minimum_memory_bytes'))):
        raise host.HostError('Apple returned incomplete restore image metadata')
    if int(result['version'].split('.')[0]) < 27:
        raise host.HostError('This restore image predates automatic account provisioning; macOS 27+ is required in the guest')
    if path is None:
        apple_url(result.get('url'))
    return result


def apple_url(value):
    if not isinstance(value, str):
        raise host.HostError('Restore image discovery did not return an Apple download URL')
    url = urlparse(value)
    if (url.scheme != 'https' or url.hostname != 'updates.cdn-apple.com'
            or url.username or url.password or url.port or url.fragment):
        raise host.HostError('Restore images must download from https://updates.cdn-apple.com/')
    return value


def requirements(info, vms):
    for vm in vms:
        if vm['cpus'] < info['minimum_cpus'] or vm['memory_gib'] * 1024**3 < info['minimum_memory_bytes']:
            raise host.HostError(f'{vm["name"]}: resources are below the restore image’s minimum CPU or memory requirements')


def download(info):
    """Cache one build, without repinning damaged or unrecorded existing files."""
    url = apple_url(info['url'])
    cache = host.STATE / 'restore-images'
    cache.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = cache / (info['build'] + '.ipsw')
    receipt = target.with_suffix('.json')
    if receipt.exists():
        saved = json.loads(receipt.read_text())
        if (not isinstance(saved, dict) or target.is_symlink() or not target.is_file() or saved.get('build') != info['build']
                or saved.get('url') != url or saved.get('sha256') != host.digest(target)):
            raise host.HostError(f'Cached restore image failed verification; inspect {target}')
        loaded = metadata(target)
        if loaded['build'] != info['build'] or loaded['version'] != info['version']:
            raise host.HostError('Cached restore image does not match Apple’s selected build')
        return target
    if target.exists():
        raise host.HostError(f'Unrecorded restore image exists; inspect {target} before retrying')
    free = shutil.disk_usage(cache).free
    if free < 64 * 1024**3:
        raise host.HostError('Restore image download needs at least 64 GiB free on the host cache volume')
    print(f'Downloading macOS {info["version"]} ({info["build"]}) from Apple…', flush=True)
    # Virtualization rejects restore media without an .ipsw extension.
    with tempfile.NamedTemporaryFile(dir=cache, suffix='.partial.ipsw', delete=False) as file:
        partial = Path(file.name)
    # Failed transfers remain identifiable for inspection; never use partial bytes.
    effective = host.command([
        '/usr/bin/curl', '--fail', '--location', '--proto', '=https', '--proto-redir', '=https',
        '--connect-timeout', '30', '--max-time', '7200', '--silent', '--show-error',
        '--max-filesize', str(free - 32 * 1024**3),
        '--write-out', '%{url_effective}', '--output', partial, url,
    ], timeout=7260)
    apple_url(effective)
    loaded = metadata(partial)
    if loaded['build'] != info['build'] or loaded['version'] != info['version']:
        raise host.HostError(f'Downloaded restore image differs from Apple’s selected build; retained at {partial}')
    checksum = host.digest(partial)
    # Exclusive publication cannot replace files from another invocation.
    os.link(partial, target)
    partial.unlink()
    with receipt.open('x') as file:
        json.dump(dict(info, sha256=checksum), file)
    receipt.chmod(0o600)
    return target
