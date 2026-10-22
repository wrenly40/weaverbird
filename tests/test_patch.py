"""Tests for the patch loop (patch.py): version handling, the
deterministic nudge table and its clamps, confusion rewrites, bug
remedy matching, refusals, plan-only outcomes, and a Godot
end-to-end — one shipped patch (version bump, changelog, tag,
save-compatibility) and one rejected patch (tree restored).
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import filled_scope_text  # noqa: E402,F401  (sets sys.path)

from weaverbird import (assets_gen, feedback, finish, generate, patch,  # noqa: E402
                        scope, skeleton, state, testing, vcs)
from weaverbird import godot as godot_module  # noqa: E402

GODOT_PRESENT = godot_module.find_godot()[0] is not None

COUNTS = ("- REQUIRED Content counts (levels / waves / rooms / items, "
          "as applicable): Filled in for the test")
GENRE = "- REQUIRED Genre / archetype: Filled in for the test"
WIN = "- REQUIRED Win condition: Filled in for the test"


def scoped_project(tmp, genre="puzzle", counts="2 puzzles",
                   win="Push every crate onto its pad"):
    text = filled_scope_text().replace(COUNTS, COUNTS.split(":")[0] + f": {counts}")
    text = text.replace(GENRE, GENRE.split(":")[0] + f": {genre}")
    text = text.replace(WIN, WIN.split(":")[0] + f": {win}")
    project = Path(tmp)
    (project / "SCOPE.md").write_text(text, encoding="utf-8")
    state.init_state(project)
    scope.approve_scope(project / "SCOPE.md", by="Tester")
    return project


class VersionTests(unittest.TestCase):
    def test_bump(self):
        self.assertEqual(patch.bump_version("0.1.0"), "0.1.1")
        self.assertEqual(patch.bump_version("1.2.9"), "1.2.10")

    def test_default_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(patch.game_version(tmp), "0.1.0")
            (Path(tmp) / "game").mkdir()
            (Path(tmp) / "game" / "VERSION").write_text("2.0.3\n")
            self.assertEqual(patch.game_version(tmp), "2.0.3")


class NudgeTests(unittest.TestCase):
    def test_arena_ease_and_clamps(self):
        config = {"archetype": "arena_collector", "hazard_speed": 2.4,
                  "round_seconds": 240.0, "player_speed": 330.0}
        new, lines, _note = patch.deterministic_balance(
            "arena_collector", config, "too hard!!")
        self.assertAlmostEqual(new["hazard_speed"], 2.04)
        self.assertEqual(new["round_seconds"], 240.0)  # clamped at cap

    def test_arena_harden(self):
        config = {"archetype": "arena_collector", "hazard_speed": 1.0,
                  "round_seconds": 100.0}
        new, lines, _ = patch.deterministic_balance(
            "arena_collector", config, "too easy, boring")
        self.assertAlmostEqual(new["hazard_speed"], 1.15)
        self.assertAlmostEqual(new["round_seconds"], 90.0)

    def test_crate_budget_floor(self):
        config = {"archetype": "crate_pusher",
                  "puzzles": [{"grid": [], "solution": "x" * 30,
                               "budget": 32}]}
        new, lines = patch._scale_budgets(config, 0.5)
        self.assertEqual(new["puzzles"][0]["budget"], 35)  # len + 5 floor

    def test_no_match_no_change(self):
        config = {"archetype": "arena_collector", "hazard_speed": 1.4}
        new, lines, _ = patch.deterministic_balance(
            "arena_collector", config, "the colours feel off")
        self.assertEqual(lines, [])
        self.assertEqual(new, config)


class ConfusionTests(unittest.TestCase):
    def test_deterministic_hint_from_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = scoped_project(tmp)
            config = {"archetype": "crate_pusher",
                      "objective_hint": "Push crates."}
            hint, how = patch.confusion_change(
                project, config, {"text": "what am I meant to do?"},
                use_brain=False)
            self.assertTrue(hint.startswith("Goal: push every crate"),
                            hint)
            self.assertIn("win condition", how)


class RefusalAndPlanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.project = scoped_project(self.tmp)
        (self.project / "game").mkdir(exist_ok=True)
        patch.save_config(self.project,
                          {"archetype": "crate_pusher", "puzzles": []})

    def _approved(self, text, category):
        item, _ = feedback.add_item(self.project, source="manual", text=text)
        feedback.update_item(self.project, item["id"], category=category,
                             status="approved", severity=2, cluster="x")
        return item

    def test_request_is_refused(self):
        item = self._approved("Please add multiplayer", "request")
        with self.assertRaises(patch.PatchError):
            patch.start_patch(self.project, item["id"])

    def test_unapproved_is_refused(self):
        item, _ = feedback.add_item(
            self.project, source="manual", text="too hard")
        feedback.update_item(self.project, item["id"], category="balance",
                             status="triaged")
        with self.assertRaises(patch.PatchError):
            patch.start_patch(self.project, item["id"])

    def test_bug_without_remedy_plans(self):
        item = self._approved("The frobnicate wobbles at dawn", "bug")
        result = patch.start_patch(self.project, item["id"])
        self.assertEqual(result["outcome"], "planned")
        plan = Path(result["plan"])
        self.assertTrue(plan.exists())
        self.assertIn("Patch plan", plan.read_text(encoding="utf-8"))
        stored = feedback.get_item(self.project, item["id"])
        self.assertEqual(stored["status"], "planned")

    def test_remedy_matching(self):
        (self.project / ".weaverbird").mkdir(exist_ok=True)
        report = {"runs": [{"name": "import", "ok": False, "errors": [
            "ERROR: Failed loading resource: res://assets/player.svg"]}]}
        (self.project / ".weaverbird" / "test_report.json").write_text(
            json.dumps(report), encoding="utf-8")
        remedy, _why = patch.match_bug_remedy(self.project)
        self.assertEqual(remedy, "assets")
        report = {"runs": [{"name": "import", "ok": False, "errors": [
            "SCRIPT ERROR: Parse Error in res://scripts/game.gd"]}]}
        (self.project / ".weaverbird" / "test_report.json").write_text(
            json.dumps(report), encoding="utf-8")
        remedy, _why = patch.match_bug_remedy(self.project)
        self.assertEqual(remedy, "rebuild")
        report = {"runs": [{"name": "import", "ok": True, "errors": []}]}
        (self.project / ".weaverbird" / "test_report.json").write_text(
            json.dumps(report), encoding="utf-8")
        remedy, _why = patch.match_bug_remedy(self.project)
        self.assertIsNone(remedy)


@unittest.skipUnless(GODOT_PRESENT, "Godot not available")
class PatchEndToEndTests(unittest.TestCase):
    """A real crate game: one patch ships (all gates), one is
    rejected by a forced test failure and the tree is restored."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.project = scoped_project(cls._tmp.name)
        skeleton.build_skeleton(cls.project)
        assets_gen.generate_assets(cls.project)
        generate.build_game(cls.project, use_brain=False)
        cls.baseline = testing.run_tests(cls.project)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def _approved_balance_item(self, text):
        item, _ = feedback.add_item(self.project, source="manual", text=text)
        feedback.triage(self.project, use_brain=False)
        feedback.set_status(self.project, item["id"], "approved")
        return feedback.get_item(self.project, item["id"])

    def test_patch_ships(self):
        self.assertTrue(self.baseline["ok"], self.baseline["warnings"])
        item = self._approved_balance_item("It is too hard, I keep failing")
        self.assertEqual(item["category"], "balance")
        before = [p["budget"] for p in
                  patch.load_config(self.project)["puzzles"]]
        version_before = patch.game_version(self.project)
        result = patch.start_patch(self.project, item["id"])
        self.assertEqual(result["outcome"], "patched", result)
        self.assertEqual(result["version"],
                         patch.bump_version(version_before))
        after = [p["budget"] for p in
                 patch.load_config(self.project)["puzzles"]]
        self.assertTrue(any(a > b for a, b in zip(after, before)), (before, after))
        changelog = (self.project / "CHANGELOG.md").read_text(encoding="utf-8")
        self.assertIn(result["version"], changelog)
        self.assertIn(item["id"], changelog)
        self.assertIn(f"known-good/patch-{result['version']}",
                      vcs.list_tags(self.project))
        stored = feedback.get_item(self.project, item["id"])
        self.assertEqual(stored["status"], "patched")
        self.assertEqual(stored["patch_version"], result["version"])
        log = patch.load_patch_log(self.project)
        self.assertTrue(any(r["version"] == result["version"] for r in log))
        # The finish report picks the patch up.
        report_path = finish.build_finish_report(
            self.project, use_brain=False)
        self.assertIn(result["version"],
                      report_path.read_text(encoding="utf-8"))

    def test_patch_rejected_restores(self):
        item = self._approved_balance_item("Still too hard for me")
        config_before = patch.config_path_for(self.project).read_text()
        version_before = patch.game_version(self.project)
        real_run_tests = testing.run_tests
        failing = {"ok": False, "runs": [
            {"name": "competent win", "kind": "scripted", "ok": False,
             "errors": ["forced failure"]}]}
        try:
            testing.run_tests = lambda *a, **k: failing
            result = patch.start_patch(self.project, item["id"])
        finally:
            testing.run_tests = real_run_tests
        self.assertEqual(result["outcome"], "rejected", result)
        self.assertEqual(
            patch.config_path_for(self.project).read_text(), config_before)
        self.assertEqual(patch.game_version(self.project), version_before)
        stored = feedback.get_item(self.project, item["id"])
        self.assertEqual(stored["status"], "approved")
        self.assertIn("rejected", stored["note"])


if __name__ == "__main__":
    unittest.main()
