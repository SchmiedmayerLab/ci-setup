"""Unit tests for the pure Xcode release-selection logic.

Run with:  python3 -m unittest discover -s tests
"""

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from cisetup.xcode import (  # noqa: E402
    Release,
    parse_identifier,
    parse_installed,
    parse_list,
    select_desired,
)

LISTING_BETA_AHEAD = """\
12.5.1 (12E507)
16.2 (16C5032a)
16.3 (16E140)
16.4 (16F6) (Installed)
26.0 Beta 3 (17A5276g)
26.0 Beta 5 (17A5295f)
"""

LISTING_AFTER_STABLE = LISTING_BETA_AHEAD + "26.0 (17A324)\n"


class ParseListTests(unittest.TestCase):
    def test_parses_stable_beta_and_annotations(self):
        releases = parse_list(LISTING_BETA_AHEAD)
        identifiers = [r.identifier for r in releases]
        self.assertEqual(
            identifiers,
            ["12.5.1", "16.2", "16.3", "16.4", "26.0 Beta 3", "26.0 Beta 5"],
        )
        installed_stable = next(r for r in releases if r.identifier == "16.4")
        self.assertEqual(installed_stable.version, (16, 4))
        self.assertEqual(installed_stable.pre, ())
        self.assertEqual(installed_stable.build, "16F6")
        beta = next(r for r in releases if r.identifier == "26.0 Beta 5")
        self.assertEqual(beta.pre, ("beta", 5))
        self.assertTrue(beta.prerelease)

    def test_release_candidate_parsing(self):
        releases = parse_list("26.1 Release Candidate (17B35)\n26.1 Release Candidate 2 (17B40)\n")
        self.assertEqual(releases[0].pre, ("rc", 1))
        self.assertEqual(releases[1].pre, ("rc", 2))
        self.assertEqual(releases[0].identifier, "26.1 Release Candidate")
        self.assertEqual(releases[1].identifier, "26.1 Release Candidate 2")

    def test_ignores_garbage_lines(self):
        releases = parse_list("something else\n\nUpdated Xcode list\n16.4 (16F6)\n")
        self.assertEqual([r.identifier for r in releases], ["16.4"])

    def test_xcodes_2x_architecture_labels(self):
        # xcodes 2.0.1+ appends [Universal]/[Apple Silicon]/[Intel] after the
        # build, before any (Installed...) annotation, plus a trailing hint.
        listing = (
            "16.4 (16F6) [Universal]\n"
            "26.0 Beta 5 (17A5295f) [Apple Silicon]\n"
            "26.1 (17B35) [Universal] (Installed, Selected)\n"
            "Showing Xcodes for this Mac by default. "
            "Switch with `--architecture arm64`.\n"
        )
        releases = parse_list(listing)
        self.assertEqual(
            [r.identifier for r in releases],
            ["16.4", "26.0 Beta 5", "26.1"],
        )
        self.assertEqual(releases[2].build, "17B35")
        self.assertEqual(releases[2].pre, ())


class SelectDesiredTests(unittest.TestCase):
    def _ids(self, listing, install_beta=True):
        desired, latest = select_desired(parse_list(listing), install_beta)
        return [r.identifier for r in desired], latest.identifier

    def test_beta_ahead_of_stable(self):
        desired, latest = self._ids(LISTING_BETA_AHEAD)
        self.assertEqual(desired, ["16.4", "16.3", "26.0 Beta 5"])
        self.assertEqual(latest, "16.4")

    def test_beta_not_newer_than_stable_is_dropped(self):
        desired, latest = self._ids(LISTING_AFTER_STABLE)
        self.assertEqual(desired, ["26.0", "16.4"])
        self.assertEqual(latest, "26.0")

    def test_install_beta_false(self):
        desired, _ = self._ids(LISTING_BETA_AHEAD, install_beta=False)
        self.assertEqual(desired, ["16.4", "16.3"])

    def test_patch_releases_group_into_minor_trains(self):
        listing = "16.3 (16E140)\n16.4 (16F6)\n16.4.1 (16F8)\n"
        desired, latest = self._ids(listing, install_beta=False)
        self.assertEqual(desired, ["16.4.1", "16.3"])
        self.assertEqual(latest, "16.4.1")

    def test_rc_preferred_over_beta_of_same_version(self):
        listing = (
            "16.4 (16F6)\n"
            "26.0 Beta 7 (17A5305f)\n"
            "26.0 Release Candidate (17A321)\n"
        )
        desired, _ = self._ids(listing)
        self.assertIn("26.0 Release Candidate", desired)
        self.assertNotIn("26.0 Beta 7", desired)

    def test_newer_rc_train(self):
        listing = "26.0 (17A324)\n16.4 (16F6)\n26.1 Release Candidate (17B35)\n"
        desired, latest = self._ids(listing)
        self.assertEqual(desired, ["26.0", "16.4", "26.1 Release Candidate"])
        self.assertEqual(latest, "26.0")

    def test_single_stable_release_has_no_previous(self):
        desired, latest = self._ids("16.4 (16F6)\n", install_beta=False)
        self.assertEqual(desired, ["16.4"])
        self.assertEqual(latest, "16.4")


class ParseInstalledTests(unittest.TestCase):
    def test_parses_paths_and_identifiers(self):
        text = (
            "16.4 (16F6)\t/Applications/Xcode-16.4.0.app\n"
            "26.0 Beta 5 (17A5295f)\t/Applications/Xcode-26.0.0-Beta.5.app\n"
        )
        installed = parse_installed(text)
        self.assertEqual(installed[0].identifier, "16.4")
        self.assertEqual(installed[0].path, "/Applications/Xcode-16.4.0.app")
        self.assertEqual(installed[1].identifier, "26.0 Beta 5")
        self.assertEqual(installed[1].path, "/Applications/Xcode-26.0.0-Beta.5.app")

    def test_tolerates_annotations_and_spaces_in_path(self):
        text = "16.4 (16F6) (Selected)  /Applications/Xcode 16.4.app\n"
        installed = parse_installed(text)
        self.assertEqual(installed[0].identifier, "16.4")
        self.assertEqual(installed[0].path, "/Applications/Xcode 16.4.app")

    def test_xcodes_2x_architecture_labels(self):
        text = (
            "16.4 (16F6) [Universal]\t/Applications/Xcode-16.4.0.app\n"
            "26.0 (17A324) [Apple Silicon] (Selected)  /Applications/Xcode.app\n"
            "26.0 Beta 5 (17A5295f) [Apple Silicon]\t/Applications/Xcode-26.0.0-Beta.5.app\n"
        )
        installed = parse_installed(text)
        self.assertEqual(
            [(i.identifier, i.path) for i in installed],
            [
                ("16.4", "/Applications/Xcode-16.4.0.app"),
                ("26.0", "/Applications/Xcode.app"),
                ("26.0 Beta 5", "/Applications/Xcode-26.0.0-Beta.5.app"),
            ],
        )
        self.assertEqual(installed[1].build, "17A324")


class ParseIdentifierTests(unittest.TestCase):
    def test_stable(self):
        self.assertEqual(parse_identifier("16.4"), ((16, 4), ()))
        self.assertEqual(parse_identifier("16.4.1"), ((16, 4, 1), ()))

    def test_prerelease(self):
        self.assertEqual(parse_identifier("26.0 Beta 5"), ((26, 0), ("beta", 5)))
        self.assertEqual(parse_identifier("26.1 Release Candidate"), ((26, 1), ("rc", 1)))

    def test_unrecognizable_is_none(self):
        self.assertIsNone(parse_identifier("garbage"))
        self.assertIsNone(parse_identifier("16.4 [Universal]"))


class SortKeyTests(unittest.TestCase):
    def test_ordering(self):
        beta = Release(version=(26, 0), pre=("beta", 7), identifier="26.0 Beta 7", build="a")
        rc = Release(version=(26, 0), pre=("rc", 1), identifier="26.0 Release Candidate", build="b")
        stable = Release(version=(26, 0), pre=(), identifier="26.0", build="c")
        self.assertLess(beta.sort_key(), rc.sort_key())
        self.assertLess(rc.sort_key(), stable.sort_key())


if __name__ == "__main__":
    unittest.main()
