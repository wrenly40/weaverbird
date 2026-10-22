"""Tests for the headless test stage (testing.py) and its finish
integration: after skeleton → assets → build → test, the finish
check should be able to mark every definition-of-done item from
evidence — including the ones that stayed honestly open in v0.3.
"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import filled_scope_text  # noqa: E402

from weaverbird import assets_gen, finish, generate, scope, skeleton, state, testing  # noqa: E402
from weaverbird import godot as godot_module  # noqa: E402

GODOT_PRESENT = godot_module.find_godot()[0] is not None
COUNTS = ("- REQUIRED Content counts (levels / waves / rooms / items, "
          "as applicable): Filled in for the test")


def scoped_project(tmp, counts="1 rounds"):
    text = filled_scope_text().replace(
        COUNTS,
        "- REQUIRED Content counts (levels / waves / rooms / items, "
        f"as applicable): {counts}")
    project = Path(tmp)
    (project / "SCOPE.md").write_text(text, encoding="utf-8")
    state.init_state(project)
    scope.approve_scope(project / "SCOPE.md", by="Tester")
    return project


class TestingRefusalTests(unittest.TestCase):
    def test_refuses_without_a_game(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = scoped_project(tmp)
            with self.assertRaises(testing.TestingError):
                testing.run_tests(project)

    def test_refuses_before_the_build_stage(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = scoped_project(tmp)
            skeleton.build_skeleton(project, validate=False)
            with self.assertRaises(testing.TestingError) as ctx:
                testing.run_tests(project)
            self.assertIn("build", str(ctx.exception).lower())


class TargetFpsTests(unittest.TestCase):
    def test_default_target_is_30(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = scoped_project(tmp)
            self.assertEqual(testing._target_fps(project), 30)

    def test_target_reads_the_global_settings_testing_section(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = scoped_project(tmp)
            (project / "global-settings.yaml").write_text(
                "budgets:\n  tokens_per_run: 1000\n  tokens_per_game: 5000\n"
                "testing:\n  target_fps: 60\n",
                encoding="utf-8")
            self.assertEqual(testing._target_fps(project), 60)


@unittest.skipUnless(GODOT_PRESENT, "Godot not available")
class FullSuiteTests(unittest.TestCase):
    """One full pipeline run on a one-round arena game, shared."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.project = scoped_project(cls._tmp.name)
        skeleton.build_skeleton(cls.project)
        assets_gen.generate_assets(cls.project)
        generate.build_game(cls.project, use_brain=False)
        cls.report = testing.run_tests(cls.project)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_report_shape_and_verdict(self):
        report = self.report
        self.assertTrue(report["ok"], report["warnings"])
        names = [r["name"] for r in report["runs"]]
        self.assertEqual(names, ["import", "main scene", "game scene",
                                 "competent win", "competent lose",
                                 "breaker", "first-time"])
        for run in report["runs"]:
            self.assertEqual(run["errors"], [], run)
        self.assertTrue(report["win_proven"])
        self.assertTrue(report["lose_proven"])
        self.assertTrue(testing.load_report(self.project))

    def test_playtesters_and_performance(self):
        report = self.report
        players = report["playtesters"]
        self.assertEqual(set(players), {"competent", "breaker", "first_time"})
        self.assertTrue(players["competent"]["ok"])
        self.assertIsNotNone(players["first_time"].get("first_objective_s"))
        perf = report["performance"]
        self.assertTrue(perf["ok"], perf)
        self.assertGreaterEqual(perf["avg_fps"], perf["target_fps"])
        self.assertIsNotNone(perf["load_time_s"])

    def test_digest_lines_summarise(self):
        lines = testing.digest_lines(self.report)
        text = "\n".join(lines)
        self.assertIn("Verdict: PASS", text)
        self.assertIn("Win proven: yes", text)
        self.assertIn("Lose proven: yes", text)

    def test_finish_check_marks_all_seven_from_evidence(self):
        result = finish.evaluate_finish(self.project)
        self.assertEqual(result["open"], {}, result)
        current = state.load_state(self.project)
        self.assertTrue(all(current["definition_of_done"].values()))
        for key, evidence in current["dod_evidence"].items():
            self.assertEqual(evidence["by"], "weaverbird", key)
            self.assertTrue(evidence["note"], key)


class FinishWithStageReportsTests(unittest.TestCase):
    """The finish check's new evidence paths, without Godot: a test
    report on record plus generation/assets reports."""

    def _project(self, tmp):
        project = Path(tmp)
        (project / "SCOPE.md").write_text(filled_scope_text(),
                                          encoding="utf-8")
        scope.approve_scope(project / "SCOPE.md", by="Tester")
        state.init_state(project)
        scripts = project / "game" / "scripts"
        scripts.mkdir(parents=True)
        for name in ("main_menu.gd", "settings.gd", "settings_store.gd",
                     "pause_input.gd"):
            (scripts / name).write_text("# fixture\n", encoding="utf-8")
        (project / "game" / "icon.svg").write_text("<svg/>\n",
                                                  encoding="utf-8")
        return project

    def _write(self, project, name, data):
        import json
        wb = project / ".weaverbird"
        wb.mkdir(exist_ok=True)
        (wb / name).write_text(json.dumps(data), encoding="utf-8")

    def test_content_and_icon_marked_from_stage_reports(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = self._project(tmp)
            self._write(project, "test_report.json", {
                "runs": [{"name": "competent win", "ok": True}],
                "scenarios_pass": True, "no_crashes": True,
                "winnable": True, "losable": True,
                "performance": {"ok": True, "avg_fps": 120.0,
                                "target_fps": 30, "load_time_s": 0.3},
            })
            self._write(project, "generation_report.json", {
                "archetype": "platformer", "units_delivered": 5,
                "units_requested": 5,
            })
            self._write(project, "assets_report.json", {
                "icon_generated": True, "icon": "game/icon.svg",
            })
            result = finish.evaluate_finish(project)
            self.assertEqual(result["open"], {}, result)
            self.assertIn("content_counts_delivered", result["marked_now"])
            self.assertIn("icon_and_polish_gate_passed",
                          result["marked_now"])
            self.assertIn("120", result["marked_now"]
                          ["performance_targets_met"])

    def test_short_content_stays_open_with_the_numbers(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = self._project(tmp)
            self._write(project, "test_report.json", {
                "runs": [{"name": "competent win", "ok": True}],
                "scenarios_pass": True, "no_crashes": True,
                "winnable": True, "losable": True,
                "performance": {"ok": True},
            })
            self._write(project, "generation_report.json", {
                "archetype": "platformer", "units_delivered": 2,
                "units_requested": 5,
            })
            self._write(project, "assets_report.json", {
                "icon_generated": True,
            })
            result = finish.evaluate_finish(project)
            self.assertIn("content_counts_delivered", result["open"])
            self.assertIn("2", result["open"]["content_counts_delivered"])

    def test_no_claim_without_a_test_report(self):
        # Generation + assets alone must not mark content/icon: the
        # test report is part of the evidence chain.
        with tempfile.TemporaryDirectory() as tmp:
            project = self._project(tmp)
            self._write(project, "generation_report.json", {
                "archetype": "platformer", "units_delivered": 5,
                "units_requested": 5,
            })
            self._write(project, "assets_report.json", {
                "icon_generated": True,
            })
            result = finish.evaluate_finish(project)
            self.assertIn("content_counts_delivered", result["open"])
            self.assertIn("icon_and_polish_gate_passed", result["open"])


if __name__ == "__main__":
    unittest.main()
