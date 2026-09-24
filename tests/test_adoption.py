# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
# SPDX-License-Identifier: MIT

"""Registration adoption operates only on temporary metadata in these tests."""

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from cisetup import adoption, config, runner, util
from cisetup.util import SetupError


class AdoptionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.legacy = self.root / "runner"
        self.legacy.mkdir()
        self.state_dir = self.root / "state"
        self.cfg = config.Config(
            repo_root=self.root / "setup", scope="org", owner="example",
            runner_name="new-hostname", runner_dir=self.root / "actions-runner",
        )
        self.registration = {
            "agentId": 7, "agentName": "existing-vm-1", "poolId": 1,
            "poolName": "Default", "gitHubUrl": "https://github.com/example",
            "workFolder": "_work", "serverUrl": "https://runner-service.example/",
        }
        self.write_registration()
        for name in (".credentials", ".credentials_rsaparams"):
            (self.legacy / name).write_text("secret-must-not-be-read")
        self.addCleanup(patch.stopall)
        patch.object(util, "STATE_DIR", self.state_dir).start()
        patch("subprocess.Popen", side_effect=AssertionError("host commands forbidden")).start()
        patch.object(runner.github_api, "post", side_effect=AssertionError("GitHub mutations forbidden")).start()

    def write_registration(self):
        (self.legacy / ".runner").write_text(json.dumps(self.registration))

    def test_prepare_retains_identity_without_writes_or_credential_reads(self):
        read_text = Path.read_text

        def metadata_only(path, *args, **kwargs):
            if path.name.startswith(".credentials"):
                self.fail("credential contents must not be read")
            return read_text(path, *args, **kwargs)

        before = (self.legacy / ".runner").read_bytes()
        with patch.object(Path, "read_text", metadata_only):
            candidate = adoption.prepare(self.cfg, self.legacy)
        self.assertEqual(candidate.runner_dir, self.legacy)
        self.assertEqual(candidate.runner_name, "existing-vm-1")
        self.assertEqual(candidate.work_dir, "_work")
        self.assertEqual(candidate.group, "Default")
        self.assertEqual(self.cfg.runner_dir, self.root / "actions-runner")
        self.assertFalse(self.state_dir.exists())
        self.assertFalse((self.legacy / runner._STATE_FILE).exists())
        self.assertEqual((self.legacy / ".runner").read_bytes(), before)

    def test_save_is_private_atomic_idempotent_and_survives_reload(self):
        candidate = adoption.prepare(self.cfg, self.legacy)
        adoption.save(candidate)
        state_path = self.state_dir / "adopted-runner.json"
        stat = state_path.stat()
        content = state_path.read_text()
        self.assertEqual(stat.st_mode & 0o777, 0o600)
        self.assertNotIn("secret-must-not-be-read", content)
        self.assertEqual(list(self.state_dir.iterdir()), [state_path])
        adoption.save(candidate)
        self.assertEqual(state_path.stat().st_mtime_ns, stat.st_mtime_ns)
        reloaded = adoption.apply(self.cfg)
        self.assertEqual(reloaded, candidate)
        self.assertIs(adoption.prepare(reloaded, self.legacy), reloaded)

    def test_absent_adoption_does_not_change_config(self):
        self.assertIs(adoption.apply(self.cfg), self.cfg)

    def test_config_load_applies_adoption_without_dirtying_checkout(self):
        self.cfg.repo_root.mkdir()
        config_file = self.cfg.repo_root / "config.toml"
        config_file.write_text('[github]\nscope="org"\nowner="example"\n')
        before = config_file.read_bytes()
        cfg = config.load(self.cfg.repo_root)
        adoption.save(adoption.prepare(cfg, self.legacy))
        reloaded = config.load(self.cfg.repo_root)
        self.assertEqual(reloaded.runner_dir, self.legacy)
        self.assertEqual(reloaded.runner_name, self.registration["agentName"])
        self.assertEqual(config_file.read_bytes(), before)
        self.assertEqual(list(self.cfg.repo_root.iterdir()), [config_file])

    def test_wrong_scope_never_saves_state(self):
        for url in ("https://github.com/other", "https://github.com/example/repo"):
            with self.subTest(url=url):
                self.registration["gitHubUrl"] = url
                self.write_registration()
                with self.assertRaisesRegex(SetupError, "scope differs"):
                    adoption.prepare(self.cfg, self.legacy)
        self.assertFalse(self.state_dir.exists())

    def test_scope_comparison_tolerates_github_case_and_trailing_slash(self):
        self.registration["gitHubUrl"] = "https://github.com/EXAMPLE/"
        self.write_registration()
        self.assertEqual(adoption.prepare(self.cfg, self.legacy).runner_name, "existing-vm-1")

    def test_missing_credential_file_refuses_adoption(self):
        (self.legacy / ".credentials_rsaparams").unlink()
        with self.assertRaisesRegex(SetupError, "credential file is missing"):
            adoption.prepare(self.cfg, self.legacy)

    def test_malformed_registration_refuses_adoption(self):
        for raw in ("not json", "[]", '{"agentId": 7}'):
            with self.subTest(raw=raw):
                (self.legacy / ".runner").write_text(raw)
                with self.assertRaises(SetupError):
                    adoption.prepare(self.cfg, self.legacy)

    def test_unsafe_work_folders_are_rejected(self):
        outside = self.root / "outside"
        outside.mkdir()
        (self.legacy / "linked-work").symlink_to(outside)
        for folder in (".", "../outside", str(outside), "linked-work", "_work\nBAD=value"):
            with self.subTest(folder=folder):
                self.registration["workFolder"] = folder
                self.write_registration()
                with self.assertRaises(SetupError):
                    adoption.prepare(self.cfg, self.legacy)

    def test_shared_policy_changes_require_explicit_migration(self):
        adoption.save(adoption.prepare(self.cfg, self.legacy))
        for changes in (
            {"labels": ["new-label"]}, {"owner": "other"}, {"runner_name": "renamed"},
            {"work_dir": "other-work"}, {"group": "Other"},
            {"runner_dir": self.root / "another-runner"},
        ):
            with self.subTest(changes=changes), self.assertRaisesRegex(SetupError, "policy changed"):
                adoption.apply(replace(self.cfg, **changes))

    def test_unrelated_shared_config_changes_are_allowed(self):
        adoption.save(adoption.prepare(self.cfg, self.legacy))
        updated = adoption.apply(replace(self.cfg, log_retention_days=10, cleanup_hooks=False))
        self.assertEqual(updated.log_retention_days, 10)
        self.assertFalse(updated.cleanup_hooks)
        self.assertEqual(updated.runner_name, "existing-vm-1")

    def test_local_identity_change_is_not_silently_adopted(self):
        candidate = adoption.prepare(self.cfg, self.legacy)
        adoption.save(candidate)
        self.registration["agentId"] = 8
        self.write_registration()
        with self.assertRaisesRegex(SetupError, "registration changed"):
            adoption.apply(self.cfg)
        self.assertFalse(runner.registration_matches(candidate))

    def test_missing_registration_or_credentials_fail_on_reload(self):
        candidate = adoption.prepare(self.cfg, self.legacy)
        adoption.save(candidate)
        for name in (".runner", ".credentials", ".credentials_rsaparams"):
            path = self.legacy / name
            original = path.read_bytes()
            path.unlink()
            with self.subTest(name=name), self.assertRaises(SetupError):
                adoption.apply(self.cfg)
            path.write_bytes(original)

    def test_corrupt_saved_state_is_not_ignored(self):
        self.state_dir.mkdir()
        (self.state_dir / "adopted-runner.json").write_text("[]")
        with self.assertRaises(SetupError):
            adoption.apply(self.cfg)

    def test_cannot_adopt_another_directory_over_existing_state(self):
        candidate = adoption.prepare(self.cfg, self.legacy)
        with self.assertRaisesRegex(SetupError, "different runner is already adopted"):
            adoption.prepare(candidate, self.root / "another-runner")

    def test_adopted_registration_never_requests_tokens_even_with_pat(self):
        candidate = adoption.prepare(self.cfg, self.legacy)
        for pat in (None, "available-pat"):
            with self.subTest(pat=bool(pat)), patch.object(
                runner, "_registration_token", side_effect=AssertionError("token forbidden")
            ), patch.object(
                runner, "_removal_token", side_effect=AssertionError("token forbidden")
            ), patch.object(runner, "_register") as register, patch.object(runner, "deregister") as remove:
                runner.ensure_registered(replace(candidate, pat=pat))
                register.assert_not_called()
                remove.assert_not_called()
        self.assertTrue(runner.registration_matches(candidate))
        self.assertFalse((self.legacy / runner._STATE_FILE).exists())

    def test_adopted_drift_fails_before_registration_even_with_pat(self):
        candidate = replace(adoption.prepare(self.cfg, self.legacy), pat="available-pat")
        self.registration["agentName"] = "unexpected-name"
        self.write_registration()
        with patch.object(runner, "_register") as register, patch.object(runner, "deregister") as remove:
            with self.assertRaisesRegex(SetupError, "registration changed"):
                runner.ensure_registered(candidate)
            register.assert_not_called()
            remove.assert_not_called()


if __name__ == "__main__":
    unittest.main()
