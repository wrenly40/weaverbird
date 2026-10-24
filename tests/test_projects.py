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

    def test_creation_with_a_prompt_starts_the_interview(self):
        # D2-1: the creation prompt IS the first message — the
        # interview has begun (extraction consumed, first group
        # pending) before the scope page is ever opened.
        from weaverbird import interview
        created = projects.new_project(
            self.ws, "Star Shepherd",
            prompt="A herding game about guiding sheep home")
        session = interview.load_session(created["path"])
        self.assertIsNotNone(session)
        state = interview.public_state(created["path"])
        self.assertTrue(state["started"])
        self.assertTrue(state["pending_fields"])
        self.assertFalse(state["complete"])

    def test_creation_without_a_prompt_leaves_interview_unstarted(self):
        from weaverbird import interview
        created = projects.new_project(self.ws, "Star Shepherd")
        self.assertIsNone(interview.load_session(created["path"]))
        state = interview.public_state(created["path"])
        self.assertFalse(state["started"])
        self.assertEqual(state["pending_fields"], [])

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


class DeleteProjectTests(unittest.TestCase):
    """0.12.0 (2/4): permanent deletion behind guardrails — one
    code path for the app's Danger zone and `weaverbird delete`."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self._tmp.name)
        self.project = self.ws / "bee-game"
        (self.project / ".weaverbird").mkdir(parents=True)
        (self.project / "SCOPE.md").write_text("# scope\n",
                                              encoding="utf-8")
        (self.project / "game").mkdir()
        (self.project / "game" / "main.gd").write_text(
            "extends Node\n", encoding="utf-8")

    def tearDown(self):
        self._tmp.cleanup()

    def test_happy_path_removes_the_folder(self):
        out = projects.delete_project(self.ws, "bee-game", "bee-game")
        self.assertEqual(out["name"], "bee-game")
        self.assertFalse(self.project.exists())

    def test_wrong_name_refuses_and_deletes_nothing(self):
        with self.assertRaises(projects.ProjectError) as ctx:
            projects.delete_project(self.ws, "bee-game", "bee-gam")
        self.assertIn("does not match", str(ctx.exception))
        self.assertTrue((self.project / "SCOPE.md").exists())

    def test_not_a_project_refuses(self):
        plain = self.ws / "plain-folder"
        plain.mkdir()
        (plain / "notes.txt").write_text("hi", encoding="utf-8")
        with self.assertRaises(projects.ProjectError) as ctx:
            projects.delete_project(self.ws, "plain-folder",
                                    "plain-folder")
        self.assertIn("does not look like a Weaverbird project",
                      str(ctx.exception))
        self.assertTrue((plain / "notes.txt").exists())

    def test_running_job_refuses(self):
        with self.assertRaises(projects.ProjectError) as ctx:
            projects.delete_project(
                self.ws, "bee-game", "bee-game",
                running_jobs=[{"id": "j1", "status": "running"}])
        self.assertIn("job is currently running",
                      str(ctx.exception))
        self.assertTrue(self.project.exists())

    def test_path_climbing_out_of_the_workspace_refuses(self):
        for bad in ("../outside", "..", ".", "a/b"):
            with self.assertRaises(projects.ProjectError):
                projects.delete_project(self.ws, bad, bad)
        self.assertTrue(self.project.exists())

    def test_reserved_workspace_names_refuse(self):
        with self.assertRaises(projects.ProjectError):
            projects.delete_project(self.ws, ".weaverbird-app",
                                    ".weaverbird-app")

    def test_missing_folder_refuses_plainly(self):
        with self.assertRaises(projects.ProjectError) as ctx:
            projects.delete_project(self.ws, "ghost", "ghost")
        self.assertIn("no folder named", str(ctx.exception))

    def test_running_jobs_for_reads_the_job_store(self):
        from weaverbird import jobs as jobs_module
        store = jobs_module.JobStore(self.ws)
        store.save({"id": "j-run", "project": "bee-game",
                    "status": "running", "created": "2026-10-24"})
        store.save({"id": "j-done", "project": "bee-game",
                    "status": "done", "created": "2026-10-24"})
        store.save({"id": "j-other", "project": "other-game",
                    "status": "queued", "created": "2026-10-24"})
        found = projects.running_jobs_for(self.ws, "bee-game")
        self.assertEqual([j["id"] for j in found], ["j-run"])


if __name__ == "__main__":
    unittest.main()
