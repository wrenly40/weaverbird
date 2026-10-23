"""Tests for the coherence pack (0.10.0): F7 scope-number
reconciliation (a scope-stated target count is parsed, used and
checked against the built config) and the pre-build summary.

Pure-logic tests run everywhere; the finish-reconciliation tests
work on records on disk and need no Godot.
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import filled_scope_text, template_scope_text  # noqa: E402

from weaverbird import finish, generate, scope, state  # noqa: E402

WIN = "- REQUIRED Win condition: Filled in for the test"
COUNTS = ("- REQUIRED Content counts (levels / waves / rooms / items, "
          "as applicable): Filled in for the test")


def scoped_project(tmp, replacements=None):
    text = filled_scope_text()
    for old, new in (replacements or {}).items():
        assert old in text, old
        text = text.replace(old, new)
    project = Path(tmp)
    (project / "SCOPE.md").write_text(text, encoding="utf-8")
    state.init_state(project)
    scope.approve_scope(project / "SCOPE.md", by="Tester")
    return project


def values(**overrides):
    base = {"Win condition": "", "Lose / fail condition": ""}
    base.update(overrides)
    return base


class ScopeTargetCountTests(unittest.TestCase):
    def test_collect_number_in_win_condition(self):
        found = generate.scope_target_count(
            values(**{"Win condition": "Collect 10 stars each round"}))
        self.assertIsNotNone(found)
        self.assertEqual(found["stated"], 10)
        self.assertEqual(found["used"], 10)
        self.assertEqual(found["field"], "Win condition")

    def test_gather_and_pick_up_verbs(self):
        for text in ("Gather 7 orbs", "Pick up 4 coins to win"):
            found = generate.scope_target_count(
                values(**{"Win condition": text}))
            self.assertIsNotNone(found, text)
            self.assertIn(found["stated"], (7, 4))

    def test_number_before_noun(self):
        found = generate.scope_target_count(
            values(**{"Win condition": "Score 25 points to win"}))
        self.assertEqual(found["stated"], 25)
        self.assertEqual(found["used"], 12)  # clamped to the gate range

    def test_lose_condition_is_read_when_win_has_none(self):
        found = generate.scope_target_count(values(**{
            "Win condition": "Survive the rounds",
            "Lose / fail condition": "Drop below 3 stars collected",
        }))
        self.assertIsNotNone(found)
        self.assertEqual(found["stated"], 3)
        self.assertEqual(found["field"], "Lose / fail condition")

    def test_no_number_no_claim(self):
        self.assertIsNone(generate.scope_target_count(
            values(**{"Win condition": "Collect every star"})))

    def test_rounds_are_not_a_target_count(self):
        # "3 rounds" is a content count, not a per-round target.
        self.assertIsNone(generate.scope_target_count(
            values(**{"Win condition": "Win all 3 rounds"})))

    def test_deterministic_config_uses_the_scope_number(self):
        vals = {
            "Title (working)": "Star Game",
            "Session length (one sitting)": "10 minutes",
            "Content counts (levels / waves / rooms / items, "
            "as applicable)": "3 rounds",
            "Win condition": "Collect 10 stars each round",
            "Lose / fail condition": "Time runs out",
        }
        config, _ = generate.deterministic_config(
            "arena_collector", vals, generate._palette(vals), 3)
        self.assertEqual(config["targets_per_round"], 10)
        record = config["scope_numbers"]["targets_per_round"]
        self.assertEqual(record["stated"], 10)
        self.assertEqual(record["used"], 10)

    def test_deterministic_config_default_without_a_number(self):
        vals = {"Title (working)": "Star Game",
                "Session length (one sitting)": "10 minutes",
                "Win condition": "Collect every star",
                "Lose / fail condition": "Time runs out"}
        config, _ = generate.deterministic_config(
            "arena_collector", vals, generate._palette(vals), 3)
        self.assertEqual(config["targets_per_round"], 5)
        self.assertNotIn("scope_numbers", config)


class ReconciliationTests(unittest.TestCase):
    def _project(self, tmp, win_text, built_targets):
        project = scoped_project(tmp, {WIN: f"- REQUIRED Win condition: {win_text}"})
        game_dir = project / "game"
        game_dir.mkdir(exist_ok=True)
        if built_targets is not None:
            (game_dir / "game_config.json").write_text(
                json.dumps({"targets_per_round": built_targets}),
                encoding="utf-8")
        return project

    def test_no_number_no_reconciliation(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = self._project(tmp, "Collect every star", 5)
            self.assertIsNone(finish.scope_number_reconciliation(project))

    def test_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = self._project(tmp, "Collect 10 stars each round", 10)
            recon = finish.scope_number_reconciliation(project)
            self.assertEqual(recon["status"], "match")
            self.assertIn("10", recon["line"])

    def test_mismatch(self):
        # The app-test case (F7): scope said 10, the build used 5.
        with tempfile.TemporaryDirectory() as tmp:
            project = self._project(tmp, "Collect 10 stars each round", 5)
            recon = finish.scope_number_reconciliation(project)
            self.assertEqual(recon["status"], "mismatch")
            self.assertIn("10", recon["line"])
            self.assertIn("5", recon["line"])

    def test_unparseable_numeric_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = self._project(tmp, "Reach 100% completion", 5)
            recon = finish.scope_number_reconciliation(project)
            self.assertEqual(recon["status"], "unparseable")

    def test_mismatch_blocks_the_content_item(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = self._project(tmp, "Collect 10 stars each round", 5)
            # A full passing test report would normally mark the
            # content item — the mismatch must still block it.
            report = {"runs": [{"name": "win", "ok": True}],
                      "scenarios_pass": True, "no_crashes": True,
                      "winnable": True, "losable": True,
                      "content_counts_delivered": True}
            (project / ".weaverbird" / "test_report.json").write_text(
                json.dumps(report), encoding="utf-8")
            evaluation = finish.evaluate_finish(project)
            self.assertIn("content_counts_delivered", evaluation["open"])
            self.assertIn("MISMATCH",
                          evaluation["open"]["content_counts_delivered"])

    def test_match_is_cited_as_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = self._project(tmp, "Collect 10 stars each round", 10)
            report = {"runs": [{"name": "win", "ok": True}],
                      "scenarios_pass": True, "no_crashes": True,
                      "winnable": True, "losable": True,
                      "content_counts_delivered": True}
            (project / ".weaverbird" / "test_report.json").write_text(
                json.dumps(report), encoding="utf-8")
            evaluation = finish.evaluate_finish(project)
            marked = evaluation["marked_now"].get(
                "content_counts_delivered",
                evaluation["already_marked"].get(
                    "content_counts_delivered", ""))
            self.assertIn("reconciled", marked)


class BuildSummaryTests(unittest.TestCase):
    def test_summary_flags_scope_numbers_before_any_build(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = scoped_project(tmp, {
                WIN: "- REQUIRED Win condition: Collect 10 stars each round",
                COUNTS: ("- REQUIRED Content counts (levels / waves / "
                         "rooms / items, as applicable): 3 rounds"),
            })
            summary = generate.build_summary(project)
            self.assertTrue(summary["complete"])
            self.assertEqual(summary["preset"], "arena_collector")
            numbers = {n["label"]: n for n in summary["numbers"]}
            targets = numbers["Targets per round"]
            self.assertEqual(targets["value"], 10)
            self.assertEqual(targets["source"], "scope")
            units = numbers["Content units (rounds)"]
            self.assertEqual(units["value"], 3)
            self.assertEqual(units["source"], "scope")
            self.assertIn("collectible", summary["asset_kinds"])

    def test_summary_defaults_are_flagged_as_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = scoped_project(tmp)
            summary = generate.build_summary(project)
            numbers = {n["label"]: n for n in summary["numbers"]}
            self.assertEqual(numbers["Targets per round"]["value"], 5)
            self.assertEqual(numbers["Targets per round"]["source"],
                             "default")

    def test_summary_survives_an_incomplete_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            text = template_scope_text().replace(
                "- REQUIRED Title (working):",
                "- REQUIRED Title (working): Half Done")
            (project / "SCOPE.md").write_text(text, encoding="utf-8")
            state.init_state(project)
            summary = generate.build_summary(project)
            self.assertTrue(summary["scope_present"])
            self.assertFalse(summary["complete"])


if __name__ == "__main__":
    unittest.main()
