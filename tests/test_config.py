#
# This source file is part of the SchmiedmayerLab ci-setup open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT
#

"""Reject invalid retention settings before creating a run log."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cisetup.config import load
from cisetup.util import SetupError


class ConfigTests(unittest.TestCase):
    def load_config(self, extra):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "config.toml").write_text('[github]\nscope="org"\nowner="example"\n' + extra)
            with patch("subprocess.Popen", side_effect=AssertionError("commands forbidden")):
                return load(root)

    def test_default_and_custom_retention(self):
        self.assertEqual(self.load_config("").log_retention_days, 30)
        cfg = self.load_config("[logging]\nretention_days=7\nmax_bytes=4096\n")
        self.assertEqual((cfg.log_retention_days, cfg.log_max_bytes), (7, 4096))

    def test_invalid_limits_raise_user_facing_error(self):
        for setting in ("retention_days=0", "retention_days=true", "max_bytes=100", 'max_bytes="large"'):
            with self.subTest(setting=setting), self.assertRaises(SetupError):
                self.load_config("[logging]\n" + setting)

    def test_non_table_config_is_rejected(self):
        with self.assertRaisesRegex(SetupError, "logging must be a table"):
            self.load_bad_table()

    def load_bad_table(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "config.toml").write_text('logging=3\n[github]\nscope="org"\nowner="example"\n')
            return load(root)
