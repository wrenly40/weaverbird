"""Tests for self-repair (repair.py): failure classification,
pass counting, strategy bounds, the keep-best/restore guarantee
(mocked suite), and a Godot end-to-end where a crate game is
sabotaged unwinnable via its move budgets and repair restores a
passing game.
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import filled_scope_text  # noqa: E402,F401  (sets sys.path)

from weaverbird import generate, repair, scope, skeleton, state, testing, vcs  # noqa: E402
from weaverbird import assets_gen  # noqa: E402
from weaverbird import godot as godot_module  # noqa: E402
from weaverbird import patch as patch_module  # noqa: E402

GODOT_PRESENT = godot_module.find_godot()[0] is not None

COUNTS = ("- REQUIRED Content counts (levels / waves / rooms / items, "
          "as applicable): Filled in for the test")
GENRE = "- REQUIRED Genre / archetype: Filled in for the test"
VERB = ("- REQUIRED Core verb (what the player does most): "
        "Filled in for the test")
WIN = "- REQUIRED Win condition: Filled in for the test"
LOSE = "- REQUIRED Lose / fail condition: Filled in for the test"


def scoped_project(tmp, genre="puzzle", counts="2 puzzles",
                   verb=None, win=None, lose=None):
    text = filled_scope_text().replace(COUNTS, COUNTS.split(":")[0] + f": {counts}")
    text = text.replace(GENRE, GENRE.split(":")[0] + f": {genre}")
    for prefix, line in ((VERB, verb), (WIN, win), (LOSE, lose)):
        if line is not None:
            text = text.replace(prefix, prefix.split(":")[0] + f": {line}")
    project = Path(tmp)
    (project / "SCOPE.md").write_text(text, encoding="utf-8")
    state.init_state(project)
    scope.approve_scope(project / "SCOPE.md", by="Tester")
    return project


def failing_report(**overrides):
    report = {
        "runs": [
            {"name": "import", "kind": "base", "ok": True, "errors": []},
            {"name": "main scene", "kind": "base", "ok": True, "errors": []},
            {"name": "game scene", "kind": "base", "ok": True, "errors": []},
            {"name": "competent win", "kind": "scripted", "ok": False,
             "errors": []},
            {"name": "competent lose", "kind": "scripted", "ok": True,
             "errors": []},
        ],
        "win_proven": False, "lose_proven": True,
        "performance": {"ok": True, "avg_fps": 60, "target_fps": 30},
        "playtesters": {"breaker": {"findings": [], "ok": True}},
        "ok": False,
    }
    report.update(overrides)
    return report


class ClassificationTests(unittest.TestCase):
    def test_each_failure_class(self):
        classes = [f["class"] for f in repair.classify_failures(failing_report())]
        self.assertEqual(classes, ["win_not_proven"])

        crash = failing_report()
        crash["runs"][0] = {"name": "import", "kind": "base", "ok": False,
                            "errors": ["SCRIPT ERROR: Parse Error in game.gd"]}
        classes = [f["class"] for f in repair.classify_failures(crash)]
        self.assertIn("crash_or_error", classes)

        missing = failing_report()
        missing["runs"][2] = {"name": "game scene", "kind": "base",
                              "ok": False,
                              "errors": ["ERROR: Failed loading resource: res://assets/x.svg"]}
        classes = [f["class"] for f in repair.classify_failures(missing)]
        self.assertEqual(classes[0], "missing_assets")

        slow = failing_report(performance={"ok": False, "avg_fps": 9,
                                           "target_fps": 30})
        classes = [f["class"] for f in repair.classify_failures(slow)]
        self.assertIn("performance_low", classes)

    def test_pass_count(self):
        # 3 base runs ok + lose proven (win is not) + performance ok.
        self.assertEqual(repair.pass_count(failing_report()), 3 + 0 + 1 + 1)
        self.assertEqual(repair.pass_count(None), 0)
        self.assertEqual(repair.pass_count({}), 0)


class StrategyBoundTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.project = Path(self.tmp)
        (self.project / "game").mkdir(parents=True)

    def _config(self, config):
        path = patch_module.config_path_for(self.project)
        path.write_text(json.dumps(config), encoding="utf-8")
        return json.loads(path.read_text())

    def test_harden_at_bounds_escalates(self):
        config = {"archetype": "arena_collector", "hazard_speed": 2.4,
                  "round_seconds": 30.0}
        self._config(config)
        with self.assertRaises(repair.RepairError):
            repair._harden_config(self.project, config, "arena_collector")

    def test_reduce_load_floor_escalates(self):
        config = {"archetype": "arena_collector", "targets_per_round": 3}
        self._config(config)
        with self.assertRaises(repair.RepairError):
            repair._reduce_load(self.project, config, "arena_collector")

    def test_regenerate_game_refuses_unknown_signature(self):
        config = {"archetype": "arena_collector"}
        self._config(config)
        (self.project / ".weaverbird").mkdir(exist_ok=True)
        (self.project / ".weaverbird" / "test_report.json").write_text(
            json.dumps(failing_report()), encoding="utf-8")
        with self.assertRaises(repair.RepairError):
            repair._regenerate_game(self.project, config, "arena_collector")


class NeverWorseTests(unittest.TestCase):
    """A sabotaged config + a suite that never passes: repair must
    exhaust, escalate, and leave the config exactly as it started."""

    def test_exhaustion_restores_start(self):
        tmp = tempfile.mkdtemp()
        project = Path(tmp)
        (project / "game").mkdir(parents=True)
        (project / "game" / "project.godot").write_text(
            "[application]\nconfig/name=\"Never Worse\"\n", encoding="utf-8")
        config = {"archetype": "crate_pusher",
                  "puzzles": [{"grid": [], "solution": "udlr",
                               "sabotage": "", "budget": 100}]}
        patch_module.save_config(project, config)
        original = patch_module.config_path_for(project).read_text()
        (project / ".weaverbird").mkdir(exist_ok=True)
        (project / ".weaverbird" / "test_report.json").write_text(
            json.dumps(failing_report()), encoding="utf-8")

        real_run_tests = testing.run_tests
        try:
            testing.run_tests = lambda *a, **k: failing_report()
            result = repair.run_repair(project, max_attempts=2)
        finally:
            testing.run_tests = real_run_tests
        self.assertEqual(result["outcome"], "escalated")
        self.assertEqual(len(result["attempts"]), 2)
        self.assertEqual(
            patch_module.config_path_for(project).read_text(), original)
        report_text = (project / "REPAIR_REPORT.md").read_text(
            encoding="utf-8")
        self.assertIn("Needs the user", report_text)
        self.assertIn("win_not_proven", report_text)

    def test_zero_attempts_escalates_without_touching(self):
        tmp = tempfile.mkdtemp()
        project = Path(tmp)
        (project / "game").mkdir(parents=True)
        (project / "game" / "project.godot").write_text(
            "[application]\nconfig/name=\"Zero\"\n", encoding="utf-8")
        patch_module.save_config(
            project, {"archetype": "arena_collector", "hazard_speed": 1.4})
        (project / ".weaverbird").mkdir(exist_ok=True)
        (project / ".weaverbird" / "test_report.json").write_text(
            json.dumps(failing_report()), encoding="utf-8")
        result = repair.run_repair(project, max_attempts=0)
        self.assertEqual(result["outcome"], "escalated")
        self.assertEqual(result["attempts"], [])


@unittest.skipUnless(GODOT_PRESENT, "Godot not available")
class SabotageEndToEndTests(unittest.TestCase):
    """Build a crate game, cut its move budgets below the verified
    solution length (deterministically unwinnable: the competent
    player replays the solution and runs out of moves), then let
    repair restore a passing game."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        # A scope whose words derive the grid deliver plan — the
        # move-budget sabotage below only bites there.
        cls.project = scoped_project(
            cls._tmp.name,
            verb="push crates onto the dock pads",
            win="push every crate onto a pad",
            lose="run out of moves")
        skeleton.build_skeleton(cls.project)
        assets_gen.generate_assets(cls.project)
        generate.build_game(cls.project, use_brain=False)
        cls.baseline = testing.run_tests(cls.project)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_sabotage_then_repair(self):
        self.assertTrue(self.baseline["ok"], self.baseline["warnings"])
        config = patch_module.load_config(self.project)
        # The runtime derives each puzzle's move budget from this
        # multiplier (budget = solution length x multiplier);
        # 0.85 puts every budget below its solution length.
        config["move_budget_multiplier"] = 0.85
        patch_module.save_config(self.project, config)
        vcs.commit_all(self.project, "sabotage: budgets below solution")

        broken = testing.run_tests(self.project)
        self.assertFalse(broken["ok"])
        self.assertFalse(broken["win_proven"])

        result = repair.run_repair(self.project, max_attempts=5)
        self.assertEqual(result["outcome"], "repaired", result["note"])
        healed = patch_module.load_config(self.project)
        multiplier = healed["move_budget_multiplier"]
        world = json.loads(
            (self.project / "game" / "world" / "world.json").read_text(
                encoding="utf-8"))
        for unit in world["units"]:
            budget = max(1, int(len(unit["solution"]) * multiplier))
            self.assertGreaterEqual(budget, len(unit["solution"]))
        final = testing.load_report(self.project)
        self.assertTrue(final["ok"])
        self.assertIn("known-good/repair", vcs.list_tags(self.project))
        report_text = (self.project / "REPAIR_REPORT.md").read_text(
            encoding="utf-8")
        self.assertIn("repaired", report_text)
        self.assertIn("ease_config", report_text)


if __name__ == "__main__":
    unittest.main()
