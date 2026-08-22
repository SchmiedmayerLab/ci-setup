"""Unit tests for pure helpers in cisetup.util."""

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from cisetup.util import _redact, fmt_version, vtuple  # noqa: E402


class RedactTests(unittest.TestCase):
    def test_redacts_token_values(self):
        argv = ["./config.sh", "--url", "https://github.com/x/y", "--token", "AAAA", "--name", "mac"]
        self.assertEqual(
            _redact(argv),
            ["./config.sh", "--url", "https://github.com/x/y", "--token", "<redacted>", "--name", "mac"],
        )

    def test_redacts_w_flag(self):
        self.assertEqual(
            _redact(["security", "add-generic-password", "-w", "secret"]),
            ["security", "add-generic-password", "-w", "<redacted>"],
        )

    def test_no_secret_flags(self):
        argv = ["brew", "install", "jq"]
        self.assertEqual(_redact(argv), argv)

    def test_trailing_flag_without_value(self):
        self.assertEqual(_redact(["security", "-w"]), ["security", "-w"])


class VersionTests(unittest.TestCase):
    def test_roundtrip(self):
        self.assertEqual(vtuple("2.328.0"), (2, 328, 0))
        self.assertEqual(fmt_version((2, 328, 0)), "2.328.0")

    def test_comparison(self):
        self.assertLess(vtuple("2.319.1"), vtuple("2.328.0"))

    def test_invalid(self):
        with self.assertRaises(ValueError):
            vtuple("v2.328.0")


if __name__ == "__main__":
    unittest.main()
