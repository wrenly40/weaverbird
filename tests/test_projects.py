"""Weaverbird 0.9.0 (Pass 7, R): project creation is one code
path — the CLI's init and the app's New project form both land in
weaverbird.projects, and an app-created project is identical on
disk to a CLI-created one.

Run with:  python3 -m unittest tests.test_projects
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from weaverbird import projects, scope, state  # noqa: E402


class SlugifyTests(unittest.TestCase):
    def test_name_with_spaces_and_case(self):
        self.assertEqual(projects.slugify("Star Shepherd"),
                         "star-shepherd")

    def test_runs_of_punctuation_become_one_dash(self):
        self.assertEqual(projects.slugify("Bee  Game!!"), "bee-game")

    def test_edges_trimmed(self):
        self.assertEqual(projects.slugify("  --Bee--  "), "bee")


class FolderValidationTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self._tmp.name)
        (self.ws / "taken").mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def test_good_name_passes(self):
        self.assertIsNone(
            projects.validate_folder_name(self.ws, "star-shepherd"))

    def test_reserved_names_refused(self):
        for reserved in ("global-settings.yaml", ".weaverbird-app"):
            self.assertIsNotNone(
                projects.validate_folder_name(self.ws, reserved),
                reserved)

    def test_existing_folder_refused(self):
        self.assertIsNotNone(
            projects.validate_folder_name(self.ws, "taken"))

    def test_spaces_and_capitals_refused(self):
        self.assertIsNotNone(
            projects.validate_folder_name(self.ws, "Star Shepherd"))


class NewProjectTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_creates_the_same_scaffold_as_init(self):
        created = projects.new_project(self.ws, "Star Shepherd")
        target = self.ws / "star-shepherd"
        self.assertEqual(created["name"], "star-shepherd")
        self.assertTrue((target / "SCOPE.md").exists())
        self.assertTrue((target / "connectors.yaml").exists())
        self.assertTrue((target / ".weaverbird" / "state.json").exists())
        self.assertEqual(state.current_stage(target), "scope")
        # the SCOPE.md is the scaffold's, differing only in the
        # seeded title line (the maker stated the title at creation)
        other = self.ws / "plain"
        projects.create_project(other)
        mine = (target / "SCOPE.md").read_text(encoding="utf-8").splitlines()
        theirs = (other / "SCOPE.md").read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(mine), len(theirs))
        diff = [pair for pair in zip(mine, theirs) if pair[0] != pair[1]]
        self.assertEqual(len(diff), 1)
        self.assertIn("Title (working)", diff[0][0])
        self.assertIn("Star Shepherd", diff[0][0])

    def test_display_name_seeds_the_scope_title(self):
        created = projects.new_project(self.ws, "Star Shepherd")
        parsed = scope.load_scope(created["path"] / "SCOPE.md")
        title = next(f for f in parsed.fields
                     if f.name == "Title (working)")
        self.assertEqual(title.value, "Star Shepherd")
        self.assertEqual(projects.display_name_for(created["path"]),
                         "Star Shepherd")

    def test_prompt_is_stored_for_the_interview(self):
        created = projects.new_project(
            self.ws, "Star Shepherd", prompt="A herding game.")
        parsed = scope.load_scope(created["path"] / "SCOPE.md")
        self.assertIn("A herding game.", parsed.original_prompt)

    def test_explicit_folder_wins_over_the_slug(self):
        created = projects.new_project(self.ws, "Star Shepherd",
                                       folder="custom-folder")
        self.assertEqual(created["name"], "custom-folder")
        self.assertTrue((self.ws / "custom-folder").exists())

    def test_refused_creation_leaves_nothing_behind(self):
        projects.new_project(self.ws, "Star Shepherd")
        with self.assertRaises(projects.ProjectError):
            projects.new_project(self.ws, "Star Shepherd")
        with self.assertRaises(projects.ProjectError):
            projects.new_project(self.ws, "Other", folder="Bad Folder")
        with self.assertRaises(projects.ProjectError):
            projects.new_project(self.ws, "Other",
                                 folder="global-settings.yaml")
        self.assertEqual(
            sorted(p.name for p in self.ws.iterdir()), ["star-shepherd"])

    def test_empty_name_refused(self):
        with self.assertRaises(projects.ProjectError):
            projects.new_project(self.ws, "   ")
        self.assertEqual(list(self.ws.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
