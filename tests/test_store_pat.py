# This source file is part of the macOS Self-Hosted GitHub Actions Runner open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
# SPDX-License-Identifier: MIT

"""Exercise hidden token entry with a fake Keychain; never access real credentials."""

import json
import os
from pathlib import Path
import pty
import select
import subprocess
import sys
import tempfile
import termios
import time
import unittest


@unittest.skipUnless(sys.platform == 'darwin' and os.geteuid() != 0, 'macOS user shell required')
class StorePatTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.record = self.root / 'record.json'
        security = self.root / 'security'
        security.write_text(f'''#!{sys.executable}
import json, pathlib, shlex, sys
record = pathlib.Path({str(self.record)!r})
if sys.argv[1:] == ['-i']:
    words = shlex.split(sys.stdin.read())
    record.write_text(json.dumps({{'argv': sys.argv[1:], 'words': words}}))
else:
    data = json.loads(record.read_text())
    data['lookup'] = sys.argv[1:]
    record.write_text(json.dumps(data))
    print(data['words'][data['words'].index('-w') + 1])
''')
        security.chmod(0o700)
        source = (Path(__file__).resolve().parents[1] / 'setup').read_text()
        source = source.replace('/usr/bin/security', str(security))
        self.script = self.root / 'setup'
        self.script.write_text(source)
        self.script.chmod(0o700)

    def test_token_passes_over_stdin_and_is_verified_without_echo(self):
        token = 'test_only_token_123'
        result = subprocess.run([self.script, 'store-pat', token], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        record = json.loads(self.record.read_text())
        self.assertEqual(record['argv'], ['-i'])
        self.assertNotIn(token, record['lookup'])
        self.assertNotIn(token, result.stdout + result.stderr)
        self.assertEqual(record['words'][-1], str(Path.home() / 'Library/Keychains/login.keychain-db'))

    def test_hidden_prompt_does_not_echo_token(self):
        token = b'test_only_hidden_token_456'
        master, slave = pty.openpty()
        self.addCleanup(os.close, master)
        self.addCleanup(os.close, slave)
        process = subprocess.Popen([self.script, 'store-pat'], stdin=slave, stdout=slave, stderr=slave)
        output = b''
        entered = False
        try:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if select.select([master], [], [], 0.05)[0]:
                    output += os.read(master, 65536)
                if not entered and b'GitHub token (hidden): ' in output:
                    if not termios.tcgetattr(slave)[3] & termios.ECHO:
                        os.write(master, token + b'\n')
                        entered = True
                if process.poll() is not None:
                    while select.select([master], [], [], 0)[0]:
                        output += os.read(master, 65536)
                    break
            self.assertTrue(entered, output.decode())
            self.assertEqual(process.poll(), 0, output.decode())
            self.assertNotIn(token, output)
            self.assertIn(b'PAT stored in the login Keychain', output)
        finally:
            if process.poll() is None:
                process.kill()
            process.wait()

    def test_interactive_security_failure_cannot_report_success(self):
        security = self.root / 'security'
        security.write_text('#!/bin/sh\nif [ "$1" = "-i" ]; then cat >/dev/null; exit 0; fi\nexit 44\n')
        result = subprocess.run([self.script, 'store-pat', 'test_only_token'], capture_output=True, text=True, timeout=10)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('could not verify', result.stderr)
        self.assertNotIn('PAT stored', result.stdout)
