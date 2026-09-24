#
# This source file is part of the SchmiedmayerLab ci-setup open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT
#

"""Inventory tests use temporary files and mock every external probe."""

import copy
import json
import pathlib
import plistlib
import stat
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from cisetup import inventory  # noqa: E402
from cisetup.config import Config  # noqa: E402
from cisetup.util import SetupError  # noqa: E402


class InventoryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = pathlib.Path(temporary.name)
        self.cfg = Config(repo_root=self.root, runner_dir=self.root / "runner")
        (self.root / "config.toml").write_text("shared configuration\n")
        self.prefix = self.root / "brew"
        (self.prefix / "opt").mkdir(parents=True)
        self.formulae = [
            self.formula("node", "26.9.0", ["libuv"]),
            self.formula("libuv", "1.52.1", []),
            self.formula("python@3.15", "3.15.0", [], aliases=["python3"]),
            self.formula("unmanaged", "1.0", []),
        ]
        self.app = self.root / "Xcode-26.6.app"
        (self.app / "Contents/Developer").mkdir(parents=True)
        self.write_plists(self.app)
        self.installed = f"26.6 (17F42) {self.app}\n"
        self.selected = str(self.app / "Contents/Developer")
        self.cfg.runner_dir.mkdir()
        (self.cfg.runner_dir / ".env").write_text(f"DEVELOPER_DIR={self.selected}\nTOKEN=never-copy-this-secret\n")
        self.output = self.patch("output", side_effect=self.command)
        self.patch("runner.installed_version", return_value=(2, 337, 0))
        self.patch("brew.FORMULAE", new=["node", "python3"])
        self.patch("brew.CASKS", new=[])
        self.patch("platform.machine", return_value="arm64")
        self.patch("socket.gethostname", return_value="runner-one")

    def patch(self, name, **kwargs):
        patcher = mock.patch("cisetup.inventory." + name, **kwargs)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def formula(self, name, version, dependencies, **extras):
        keg = self.prefix / "Cellar" / name / version
        keg.mkdir(parents=True)
        (self.prefix / "opt" / name).symlink_to(keg, target_is_directory=True)
        return {"name": name, "full_name": name, "aliases": [], "pinned": False,
                "installed": [{"version": version, "runtime_dependencies": [
                    {"full_name": dependency} for dependency in dependencies]}], **extras}

    def write_plists(self, app, version="26.6.0", build="17F42"):
        with (app / "Contents/Info.plist").open("wb") as file:
            plistlib.dump({"CFBundleShortVersionString": version}, file)
        with (app / "Contents/version.plist").open("wb") as file:
            plistlib.dump({"ProductBuildVersion": build}, file)

    def command(self, cmd, **kwargs):
        if cmd == ["git", "-C", str(self.root), "rev-parse", "HEAD"]:
            return "510bb07302eddec5a28932ff51b2f588ad473206"
        if cmd == ["git", "-C", str(self.root), "status", "--porcelain"]:
            return ""
        if cmd[0] == "brew":
            self.assertEqual(kwargs["env"]["HOMEBREW_NO_AUTO_UPDATE"], "1")
        responses = {
            ("brew", "--prefix"): str(self.prefix),
            ("brew", "info", "--json=v2", "--installed"): json.dumps({"formulae": self.formulae, "casks": []}),
            ("/usr/bin/sw_vers", "-productVersion"): "27.0",
            ("/usr/bin/sw_vers", "-buildVersion"): "26A428",
            ("xcodes", "installed"): self.installed,
            ("/usr/bin/xcode-select", "-p"): self.selected,
        }
        if tuple(cmd) not in responses:
            raise AssertionError(f"unexpected command: {cmd}")
        return responses[tuple(cmd)]

    def test_collects_active_kegs_dependency_closure_and_aliases(self):
        self.formulae[0]["installed"].append({"version": "26.8.1", "runtime_dependencies": []})
        snapshot = inventory.collect(self.cfg)
        self.assertEqual(snapshot["errors"], [])
        formulae = snapshot["comparable"]["homebrew"]["formulae"]
        self.assertEqual(set(formulae), {"node", "libuv", "python@3.15"})
        self.assertEqual(formulae["node"], {"version": "26.9.0", "pinned": False})
        self.assertEqual(snapshot["comparable"]["xcodes"]["selected"], {"version": "26.6", "build": "17F42"})
        self.assertNotIn("xcodebuild", [call.args[0][0] for call in self.output.call_args_list])

    def test_hosts_times_and_equivalent_xcode_app_names_do_not_drift(self):
        first = inventory.collect(self.cfg)
        renamed = self.root / "Xcode-26.6.0.app"
        self.app.rename(renamed)
        self.installed = f"26.6.0 (17F42) {renamed}\n"
        self.selected = str(renamed / "Contents/Developer")
        (self.cfg.runner_dir / ".env").write_text(f"DEVELOPER_DIR={self.selected}\n")
        second = inventory.collect(self.cfg)
        second.update(host="runner-two", captured_at="different time")
        self.assertEqual(inventory.differences(first, second), [])

    def test_runner_override_drift_is_detected_with_unchanged_global_selection(self):
        first = inventory.collect(self.cfg)
        other = self.root / "Other-Xcode.app"
        (other / "Contents/Developer").mkdir(parents=True)
        self.write_plists(other, version="26.5", build="17E1")
        (self.cfg.runner_dir / ".env").write_text(
            f"TOKEN=never-copy-this-secret\nDEVELOPER_DIR={other}/Contents/Developer\n"
        )
        second = inventory.collect(self.cfg)
        self.assertEqual(first["comparable"]["xcodes"]["selected"], second["comparable"]["xcodes"]["selected"])
        changes = inventory.differences(first, second)
        self.assertTrue(any("xcodes.runner.version" in item for item in changes))
        self.assertTrue(any("xcodes.runner.build" in item for item in changes))
        self.assertNotIn("never-copy-this-secret", json.dumps(second))

    def test_disabled_xcode_management_allows_intentionally_absent_xcodes(self):
        self.cfg.xcode_manage = False
        self.selected = ""
        (self.cfg.runner_dir / ".env").unlink()
        snapshot = inventory.collect(self.cfg)
        self.assertEqual(snapshot["errors"], [])
        self.assertEqual(snapshot["comparable"]["xcodes"], {
            "managed": False, "installed": None, "selected": None, "runner": None,
        })
        self.assertEqual(inventory.differences(snapshot, snapshot), [])
        self.assertNotIn(["xcodes", "installed"], [call.args[0] for call in self.output.call_args_list])

    def test_unmanaged_runner_still_reports_explicit_xcode_override(self):
        self.cfg.xcode_manage = False
        self.selected = ""
        snapshot = inventory.collect(self.cfg)
        self.assertEqual(snapshot["errors"], [])
        self.assertEqual(snapshot["comparable"]["xcodes"]["runner"], {
            "version": "26.6", "build": "17F42", "source": "DEVELOPER_DIR",
        })

    def test_comparison_reports_pins_versions_and_revision(self):
        first = inventory.collect(self.cfg)
        second = copy.deepcopy(first)
        second["comparable"]["homebrew"]["formulae"]["node"] = {"version": "26.10.0", "pinned": True}
        second["comparable"]["setup"]["commit"] = "another-commit"
        changes = inventory.differences(first, second)
        self.assertEqual(len(changes), 3)
        self.assertTrue(any("node.pinned" in item for item in changes))
        self.assertTrue(any("node.version" in item for item in changes))
        self.assertTrue(any("setup.commit" in item for item in changes))

    def test_failed_probe_is_recorded_and_other_probes_continue(self):
        self.formulae[0]["installed"][0].pop("runtime_dependencies")
        snapshot = inventory.collect(self.cfg)
        self.assertIn("xcodes", snapshot["comparable"])
        self.assertTrue(any("dependency metadata is missing" in error for error in snapshot["errors"]))
        with self.assertRaisesRegex(SetupError, "incomplete"):
            inventory.differences(snapshot, snapshot)

    def test_broken_active_keg_is_not_mistaken_for_an_installed_version(self):
        (self.prefix / "opt/node").unlink()
        snapshot = inventory.collect(self.cfg)
        self.assertTrue(snapshot["errors"])
        self.assertNotIn("homebrew", snapshot["comparable"])

    def test_rejects_wrong_schema_missing_data_and_dirty_checkouts(self):
        snapshot = inventory.collect(self.cfg)
        for change in (
            {"schema_version": 99},
            {"comparable": {}},
            {"errors": ["probe failed"]},
            {"errors": None},
        ):
            with self.subTest(change=change), self.assertRaises(SetupError):
                inventory.differences(snapshot, dict(snapshot, **change))
        dirty = copy.deepcopy(snapshot)
        dirty["comparable"]["setup"]["dirty"] = True
        with self.assertRaisesRegex(SetupError, "uncommitted"):
            inventory.differences(dirty, dirty)
        invalid = copy.deepcopy(snapshot)
        invalid["comparable"]["runner_version"] = None
        with self.assertRaisesRegex(SetupError, "invalid state fields"):
            inventory.differences(invalid, invalid)

    def test_save_replaces_atomically_with_owner_only_permissions(self):
        snapshot = inventory.collect(self.cfg)
        target = self.root / "reports/inventory.json"
        inventory.save(snapshot, target)
        self.assertEqual(json.loads(target.read_text()), snapshot)
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
        before = target.read_text()
        with mock.patch("cisetup.inventory.os.replace", side_effect=OSError("replace failed")):
            with self.assertRaises(OSError):
                inventory.save({"replacement": True}, target)
        self.assertEqual(target.read_text(), before)
        self.assertEqual(list(target.parent.iterdir()), [target])


if __name__ == "__main__":
    unittest.main()
