"""Tests for the balance lab (balance.py): per-unit metric
aggregation from bot JSON, the deterministic flag math on
synthetic unit tables, suggestions staying inside the patch
bounds, and — Godot-gated — a compare that restores the game
exactly, and an apply that goes through the patch gates.
"""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import filled_scope_text  # noqa: E402,F401  (sets sys.path)

from weaverbird import balance  # noqa: E402
from weaverbird import patch  # noqa: E402
from weaverbird import testing  # noqa: E402
from weaverbird import godot as godot_module  # noqa: E402

GODOT_PRESENT = godot_module.find_godot()[0] is not None

COUNTS = ("- REQUIRED Content counts (levels / waves / rooms / items, "
          "as applicable): Filled in for the test")
GENRE = "- REQUIRED Genre / archetype: Filled in for the test"
WIN = "- REQUIRED Win condition: Filled in for the test"


def scoped_project(tmp, genre="puzzle", counts="2 puzzles",
                   win="Push every crate onto its pad"):
    from weaverbird import scope, state
    text = filled_scope_text().replace(COUNTS, COUNTS.split(":")[0] + f": {counts}")
    text = text.replace(GENRE, GENRE.split(":")[0] + f": {genre}")
    text = text.replace(WIN, WIN.split(":")[0] + f": {win}")
    project = Path(tmp)
    (project / "SCOPE.md").write_text(text, encoding="utf-8")
    state.init_state(project)
    scope.approve_scope(project / "SCOPE.md", by="Tester")
    return project


def unit(source, n, completed=True, deaths=0, time_s=10.0, **extra):
    record = {"source": source, "unit": n, "completed": completed,
              "deaths": deaths, "time_s": time_s}
    record.update(extra)
    return record


class AggregationTests(unittest.TestCase):
    def test_units_merge_bot_details(self):
        win = {"units": [
            {"unit": 1, "completed": True, "deaths": 0, "time_s": 42.0,
             "moves_used": 30, "move_budget": 75},
            {"unit": 2, "completed": True, "deaths": 1, "time_s": 55.0,
             "moves_used": 41, "move_budget": 90}]}
        first = {"units": [
            {"unit": 1, "completed": False, "deaths": 1, "time_s": 90.0}]}
        units = testing._units_from_details(win, first)
        self.assertEqual(len(units), 3)
        by_key = {(u["unit"], u["source"]): u for u in units}
        self.assertEqual(by_key[(1, "competent")]["moves_used"], 30)
        self.assertEqual(by_key[(2, "competent")]["deaths"], 1)
        self.assertFalse(by_key[(1, "first_time")]["completed"])
        rows = balance.unit_rows(units)
        self.assertEqual([r["unit"] for r in rows], [1, 2])
        self.assertIsNotNone(rows[0]["first_time"])
        self.assertIsNone(rows[1]["first_time"])

    def test_missing_units_are_empty_not_errors(self):
        self.assertEqual(testing._units_from_details({}, None), [])
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(balance.load_units(tmp), [])


class FlagTests(unittest.TestCase):
    def test_spike(self):
        units = [unit("competent", 1, deaths=0), unit("competent", 2, deaths=1),
                 unit("competent", 3, deaths=6)]
        flags = balance.analyse(units)
        spikes = [f for f in flags if f["kind"] == "spike"]
        self.assertEqual(len(spikes), 1)
        self.assertEqual(spikes[0]["unit"], 3)
        self.assertEqual(spikes[0]["numbers"]["deaths"], 6)

    def test_wall(self):
        units = [unit("competent", 1), unit("competent", 2, completed=False)]
        kinds = [f["kind"] for f in balance.analyse(units)]
        self.assertIn("wall", kinds)

    def test_stall_by_time_and_by_deaths(self):
        units = [unit("first_time", 1, time_s=10.0),
                 unit("first_time", 2, time_s=12.0),
                 unit("first_time", 3, time_s=95.0)]
        stalls = [f for f in balance.analyse(units) if f["kind"] == "stall"]
        self.assertEqual([f["unit"] for f in stalls], [3])
        units = [unit("first_time", 1, deaths=3, time_s=10.0)]
        stalls = [f for f in balance.analyse(units) if f["kind"] == "stall"]
        self.assertEqual(len(stalls), 1)

    def test_breeze(self):
        units = [unit("competent", 1, time_s=30.0),
                 unit("competent", 2, time_s=40.0),
                 unit("first_time", 1), unit("first_time", 2)]
        flags = balance.analyse(units)
        self.assertEqual([f["kind"] for f in flags], ["breeze"])

    def test_no_flag_control(self):
        units = [unit("competent", 1, deaths=0), unit("competent", 2, deaths=1),
                 unit("competent", 3, deaths=1),
                 unit("first_time", 1, deaths=1, time_s=20.0),
                 unit("first_time", 2, deaths=0, time_s=22.0)]
        self.assertEqual(balance.analyse(units), [])


class SuggestionTests(unittest.TestCase):
    def _project(self, tmp, units):
        project = Path(tmp)
        (project / "game").mkdir(parents=True)
        # A scope with a real win condition: the hint suggestion
        # is built from it, exactly as the patch loop builds it.
        text = filled_scope_text().replace(
            WIN, WIN.split(":")[0] + ": Collect every spark")
        (project / "SCOPE.md").write_text(text, encoding="utf-8")
        config = {"archetype": "arena_collector", "hazard_speed": 2.0,
                  "round_seconds": 100.0, "player_speed": 330.0,
                  "objective_hint": "Collect the sparks."}
        (project / "game" / "game_config.json").write_text(
            json.dumps(config), encoding="utf-8")
        (project / ".weaverbird").mkdir()
        (project / ".weaverbird" / "test_report.json").write_text(
            json.dumps({"units": units}), encoding="utf-8")
        return project

    def test_ease_suggestion_within_bounds(self):
        with tempfile.TemporaryDirectory() as tmp:
            units = [unit("competent", 1, deaths=0),
                     unit("competent", 2, deaths=1),
                     unit("competent", 3, deaths=6)]
            project = self._project(tmp, units)
            found = balance.suggestions(project)
            self.assertEqual(len(found), 1)
            self.assertEqual(found[0]["kind"], "ease")
            after = found[0]["config_after"]
            bounds = patch.patch_bounds("arena_collector")
            for key, value in after.items():
                if key in bounds:
                    low, high = bounds[key]
                    self.assertTrue(low <= value <= high, (key, value))
            self.assertLess(after["hazard_speed"], 2.0)
            self.assertIn("too hard", found[0]["rationale"])

    def test_stall_proposes_a_hint(self):
        with tempfile.TemporaryDirectory() as tmp:
            units = [unit("first_time", 1, time_s=95.0),
                     unit("first_time", 2, time_s=10.0),
                     unit("first_time", 3, time_s=12.0)]
            project = self._project(tmp, units)
            found = balance.suggestions(project)
            hints = [s for s in found if s["kind"] == "hint"]
            self.assertEqual(len(hints), 1)
            self.assertIn("Goal: collect every spark",
                          hints[0]["config_after"]["objective_hint"])
            self.assertEqual(hints[0]["category"], "confusion")

    def test_compare_refuses_an_identical_candidate(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = self._project(tmp, [])
            with self.assertRaises(balance.BalanceError):
                balance.compare(
                    project,
                    config_overrides={"hazard_speed": 2.0})


@unittest.skipUnless(GODOT_PRESENT, "Godot not available")
class BalanceEndToEndTests(unittest.TestCase):
    """A real crate game: compare must leave the tree exactly as
    it found it; apply must go through the patch gates (ship with
    a version bump, or be rejected with the tree restored)."""

    @classmethod
    def setUpClass(cls):
        from weaverbird import assets_gen, generate, skeleton
        cls._tmp = tempfile.TemporaryDirectory()
        cls.project = scoped_project(cls._tmp.name)
        skeleton.build_skeleton(cls.project)
        assets_gen.generate_assets(cls.project)
        generate.build_game(cls.project, use_brain=False)
        cls.baseline = testing.run_tests(cls.project)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def _git_porcelain(self):
        out = subprocess.run(["git", "-C", str(self.project), "status",
                              "--porcelain"], capture_output=True,
                             text=True, check=True).stdout
        return sorted(line for line in out.splitlines()
                      if "balance_report" not in line
                      and ".godot" not in line)

    def test_baseline_has_units(self):
        self.assertTrue(self.baseline["ok"], self.baseline["warnings"])
        self.assertTrue(self.baseline["units"])
        competent = [u for u in self.baseline["units"]
                     if u["source"] == "competent"]
        self.assertTrue(all("moves_used" in u for u in competent))

    def test_compare_restores_state(self):
        before_fp = balance.config_fingerprint(self.project)
        before_status = self._git_porcelain()
        result = balance.compare(
            self.project,
            config_overrides={"move_budget_multiplier": 1.6})
        self.assertEqual(balance.config_fingerprint(self.project),
                         before_fp)
        self.assertEqual(self._git_porcelain(), before_status)
        self.assertTrue(result["verdict"])
        self.assertIn("units", result["candidate_metrics"])
        report = balance.load_compare_report(self.project)
        self.assertIsNotNone(report)
        self.assertIn("baseline", report)
        self.assertIn("candidate_metrics", report)

    def test_apply_breeze_goes_through_the_gates(self):
        # Craft a breeze report so the lab proposes hardening.
        report_path = self.project / ".weaverbird" / "test_report.json"
        original = report_path.read_bytes()
        crafted = json.loads(original)
        crafted["units"] = [
            {"source": "competent", "unit": 1, "completed": True,
             "deaths": 0, "time_s": 20.0, "moves_used": 10,
             "move_budget": 90},
            {"source": "competent", "unit": 2, "completed": True,
             "deaths": 0, "time_s": 25.0, "moves_used": 12,
             "move_budget": 95},
            {"source": "first_time", "unit": 1, "completed": True,
             "deaths": 0, "time_s": 30.0},
            {"source": "first_time", "unit": 2, "completed": True,
             "deaths": 0, "time_s": 32.0}]
        report_path.write_text(json.dumps(crafted), encoding="utf-8")
        try:
            found = balance.suggestions(self.project)
            harden = [i for i, s in enumerate(found, start=1)
                      if s["kind"] == "harden"]
            self.assertTrue(harden, found)
            fp_before = balance.config_fingerprint(self.project)
            version_before = patch.game_version(self.project)
            result = balance.apply_suggestion(self.project, harden[0])
            if result["outcome"] == "patched":
                self.assertNotEqual(patch.game_version(self.project),
                                    version_before)
                changelog = (self.project / "CHANGELOG.md").read_text(
                    encoding="utf-8")
                self.assertIn("balance lab", changelog.lower())
            else:
                # Rejected or planned by the gates: nothing shipped,
                # the config is exactly as it was.
                self.assertIn(result["outcome"], ("rejected", "planned"))
                self.assertEqual(balance.config_fingerprint(self.project),
                                 fp_before)
        finally:
            report_path.write_bytes(original)


if __name__ == "__main__":
    unittest.main()
