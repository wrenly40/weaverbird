"""Tests for the evidence-based finish check and report (finish.py).

The contract under test is honesty: an item is marked only when real
evidence backs it, the evidence is stored next to the mark, and
anything unproven stays open with a plain reason — above all the
losable half of "winnable and losable", which the skeleton's win-only
smoke cannot prove.
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import filled_scope_text  # noqa: E402

from weaverbird import finish, ledger, provenance, scope, state  # noqa: E402

MENU_SCRIPTS = ("main_menu.gd", "settings.gd", "settings_store.gd",
                "pause_input.gd")


def skeleton_report(win=True):
    return {
        "files": ["game/project.godot", "game/scripts/main_menu.gd"],
        "known_good_tag": "known-good/skeleton",
        "validation": {
            "passed": True,
            "godot_version": "4.7.2",
            "runs": [
                {"name": "import", "ok": True, "errors": []},
                {"name": "main scene", "ok": True, "errors": []},
                {"name": "game scene", "ok": True, "errors": []},
                {"name": "flow smoke", "ok": True, "errors": [],
                 "smoke_pass": win},
            ],
        },
    }


class FinishTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.project = Path(self.tmp) / "game1"
        self.project.mkdir()
        (self.project / "SCOPE.md").write_text(filled_scope_text(),
                                               encoding="utf-8")
        scope.approve_scope(self.project / "SCOPE.md", by="Tester")
        state.init_state(self.project)

    def add_game_scripts(self, names=MENU_SCRIPTS):
        scripts = self.project / "game" / "scripts"
        scripts.mkdir(parents=True, exist_ok=True)
        for name in names:
            (scripts / name).write_text("# test fixture\n", encoding="utf-8")

    def add_skeleton_report(self, report=None):
        wb = self.project / ".weaverbird"
        wb.mkdir(exist_ok=True)
        (wb / "skeleton_report.json").write_text(
            json.dumps(report or skeleton_report()), encoding="utf-8")

    def add_test_report(self, report):
        wb = self.project / ".weaverbird"
        wb.mkdir(exist_ok=True)
        (wb / "test_report.json").write_text(json.dumps(report),
                                             encoding="utf-8")


class TestEvidenceMarking(FinishTestCase):
    def test_skeleton_evidence_marks_three_items_with_evidence(self):
        self.add_game_scripts()
        self.add_skeleton_report()
        result = finish.evaluate_finish(self.project)
        for key in ("scenario_tests_pass",
                    "no_crashes_in_full_playthroughs",
                    "menus_save_settings_complete"):
            self.assertIn(key, result["marked_now"], result)
        current = state.load_state(self.project)
        evidence = current["dod_evidence"]
        for key in result["marked_now"]:
            self.assertTrue(current["definition_of_done"][key])
            self.assertEqual(evidence[key]["by"], "weaverbird")
            self.assertTrue(evidence[key]["note"])
            self.assertTrue(evidence[key]["at"])

    def test_second_run_reports_already_marked(self):
        self.add_game_scripts()
        self.add_skeleton_report()
        finish.evaluate_finish(self.project)
        again = finish.evaluate_finish(self.project)
        self.assertEqual(again["marked_now"], {})
        self.assertIn("scenario_tests_pass", again["already_marked"])


class TestHonestGaps(FinishTestCase):
    def test_losable_stays_open_with_only_win_evidence(self):
        self.add_game_scripts()
        self.add_skeleton_report()
        result = finish.evaluate_finish(self.project)
        self.assertIn("game_winnable_and_losable", result["open"])
        reason = result["open"]["game_winnable_and_losable"]
        self.assertIn("win", reason.lower())
        self.assertIn("lose", reason.lower())
        current = state.load_state(self.project)
        self.assertFalse(
            current["definition_of_done"]["game_winnable_and_losable"])

    def test_unmeasurable_items_stay_open_with_reasons(self):
        self.add_game_scripts()
        self.add_skeleton_report()
        result = finish.evaluate_finish(self.project)
        for key in ("performance_targets_met", "content_counts_delivered",
                    "icon_and_polish_gate_passed"):
            self.assertIn(key, result["open"], result)
            self.assertTrue(result["open"][key])

    def test_no_evidence_marks_nothing(self):
        result = finish.evaluate_finish(self.project)
        self.assertEqual(result["marked_now"], {})
        self.assertEqual(len(result["open"]),
                         len(state.DEFINITION_OF_DONE))

    def test_missing_menu_script_keeps_menus_open(self):
        self.add_game_scripts(names=("main_menu.gd",))
        self.add_skeleton_report()
        result = finish.evaluate_finish(self.project)
        self.assertIn("menus_save_settings_complete", result["open"])
        self.assertIn("settings.gd",
                      result["open"]["menus_save_settings_complete"])


class TestTestReportPrecedence(FinishTestCase):
    def test_test_report_flips_the_honest_gaps(self):
        self.add_game_scripts()
        self.add_skeleton_report()
        self.add_test_report({
            "runs": [{"name": "full playthrough", "ok": True}],
            "scenarios_pass": True,
            "no_crashes": True,
            "winnable": True,
            "losable": True,
            "performance_targets_met": True,
            "content_counts_delivered": True,
            "icon_and_polish_gate_passed": True,
        })
        result = finish.evaluate_finish(self.project)
        self.assertEqual(result["open"], {}, result)
        current = state.load_state(self.project)
        self.assertTrue(all(current["definition_of_done"].values()))

    def test_test_report_takes_precedence_over_skeleton(self):
        # A failing test report must not be rescued by the older
        # passing skeleton validation.
        self.add_game_scripts()
        self.add_skeleton_report()
        self.add_test_report({
            "runs": [{"name": "full playthrough", "ok": False}],
            "scenarios_pass": False,
            "no_crashes": False,
        })
        result = finish.evaluate_finish(self.project)
        self.assertIn("scenario_tests_pass", result["open"])
        self.assertIn("no_crashes_in_full_playthroughs", result["open"])


class TestReport(FinishTestCase):
    def setUp(self):
        super().setUp()
        self.add_game_scripts()
        self.add_skeleton_report()
        ledger.add_entry(self.project, "scope", "cheap", 60, 0.0)
        ledger.add_entry(self.project, "skeleton", "cheap", 0, 0.0)
        provenance.add_record(self.project, "game/icon.svg", "code",
                              note="placeholder")

    def test_report_contains_every_section_without_a_brain(self):
        path = finish.build_finish_report(self.project)
        self.assertEqual(path.name, "FINISH_REPORT.md")
        text = path.read_text(encoding="utf-8")
        for heading in ("# Finish report", "## Where the game is",
                        "## What was built", "## Definition of done",
                        "## What it cost", "## Provenance",
                        "## Decisions left for the human"):
            self.assertIn(heading, text)
        # Ledger totals by stage/tier, and the skeleton honesty note.
        self.assertIn("60 tokens", text)
        self.assertIn("skeleton: 0 tokens", text)
        self.assertIn("no model was used", text)
        # The honest gaps are in the report as open items.
        self.assertIn("Game is winnable and losable", text)
        self.assertIn("open", text)
        # Approval-stage decisions are named for the human.
        for word in ("Price", "platform", "store"):
            self.assertIn(word.lower(), text.lower())

    def test_report_runs_the_check_first(self):
        finish.build_finish_report(self.project)
        current = state.load_state(self.project)
        self.assertTrue(
            current["definition_of_done"]["scenario_tests_pass"])
        self.assertIn("scenario_tests_pass", current["dod_evidence"])

    def test_finished_gate_is_controlled_by_evidence(self):
        # Park the pipeline just before "finished": the gate into
        # finished must block on unmarked items…
        current = state.load_state(self.project)
        current["stage"] = "playtest"
        state.save_state(self.project, current)
        reasons = state.gate_reasons(self.project)
        self.assertTrue(
            any("Definition of done" in r for r in reasons), reasons)
        # …and pass once every item is marked by evidence.
        self.add_test_report({
            "runs": [{"name": "full playthrough", "ok": True}],
            "scenarios_pass": True, "no_crashes": True,
            "winnable": True, "losable": True,
            "performance_targets_met": True,
            "content_counts_delivered": True,
            "icon_and_polish_gate_passed": True,
        })
        finish.evaluate_finish(self.project)
        reasons = state.gate_reasons(self.project)
        self.assertFalse(
            any("Definition of done" in r for r in reasons), reasons)


if __name__ == "__main__":
    unittest.main()
