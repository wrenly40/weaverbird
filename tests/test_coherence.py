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

from weaverbird import finish, generate, scope, state, world  # noqa: E402
from weaverbird import plan as plan_module  # noqa: E402

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
    def _project(self, tmp, win_text, built_targets, world_counts=None):
        project = scoped_project(tmp, {WIN: f"- REQUIRED Win condition: {win_text}"})
        game_dir = project / "game"
        game_dir.mkdir(exist_ok=True)
        if built_targets is not None:
            (game_dir / "game_config.json").write_text(
                json.dumps({"targets_per_round": built_targets}),
                encoding="utf-8")
        if world_counts is not None:
            world_dir = game_dir / "world"
            world_dir.mkdir(exist_ok=True)
            units = [{"index": i, "kind": "round",
                      "collectibles": [[0, 0]] * count}
                     for i, count in enumerate(world_counts)]
            (world_dir / "world.json").write_text(
                json.dumps({"preset": "arena_collector",
                            "units": units}), encoding="utf-8")
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

    LOSE = "- REQUIRED Lose / fail condition: Filled in for the test"

    def _survive_project(self, tmp, win_text, lose_text, built_units):
        project = scoped_project(tmp, {
            WIN: f"- REQUIRED Win condition: {win_text}",
            self.LOSE: f"- REQUIRED Lose / fail condition: {lose_text}",
        })
        game_dir = project / "game"
        game_dir.mkdir(exist_ok=True)
        (game_dir / "game_config.json").write_text(
            json.dumps({"units": built_units,
                        "plan": {"movement": "top_down_2d",
                                 "objective": "survive",
                                 "numbers": {"units": built_units}}}),
            encoding="utf-8")
        return project

    def test_survive_units_statement_is_a_read_number(self):
        # D2-4's second half (0.13.0): Emberwatch's exact Win/Lose
        # text. A survive game has no per-round target count; its
        # "3 nights" is the units count the plan derived and the
        # config built, and "3 lives" is the plan's lives — neither
        # is an unread number, so nothing blocks finish.
        with tempfile.TemporaryDirectory() as tmp:
            project = self._survive_project(
                tmp,
                "Survive until dawn in each of the 3 nights; "
                "finish all 3 nights to win the game",
                "A storm wisp touch costs one life; lose all "
                "3 lives and the game ends",
                built_units=3)
            self.assertIsNone(finish.scope_number_reconciliation(project))

    def test_survive_units_mismatch_still_blocks(self):
        # The strip is equality-gated: a scope saying 5 nights
        # over a 3-night build is a number the build did not use.
        with tempfile.TemporaryDirectory() as tmp:
            project = self._survive_project(
                tmp, "Survive all 5 nights to win the game",
                "Lose all 3 lives and the game ends",
                built_units=3)
            recon = finish.scope_number_reconciliation(project)
            self.assertEqual(recon["status"], "unparseable")

    def test_survive_other_numbers_still_block(self):
        # Units read, but an unrelated stated number remains
        # unread and keeps blocking.
        with tempfile.TemporaryDirectory() as tmp:
            project = self._survive_project(
                tmp, "Survive the 3 nights and reach 100% completion",
                "Lose all 3 lives and the game ends",
                built_units=3)
            recon = finish.scope_number_reconciliation(project)
            self.assertEqual(recon["status"], "unparseable")

    def test_world_counts_matching_the_pin_reconcile(self):
        # D6: config AND world agree with the scope — the line
        # quotes the actual per-round placement.
        with tempfile.TemporaryDirectory() as tmp:
            project = self._project(
                tmp, "Collect 8 comet pups each round", 8,
                world_counts=[8, 8, 8])
            recon = finish.scope_number_reconciliation(project)
            self.assertEqual(recon["status"], "match")
            self.assertEqual(recon["world_counts"], [8, 8, 8])
            self.assertIn("exactly 8", recon["line"])

    def test_world_escalating_past_the_pin_is_a_mismatch(self):
        # The dogfood hole (D6): config said 8 and the old check
        # passed, while the world placed 8/9/10 and the game
        # required collecting them all.
        with tempfile.TemporaryDirectory() as tmp:
            project = self._project(
                tmp, "Collect 8 comet pups each round", 8,
                world_counts=[8, 9, 10])
            recon = finish.scope_number_reconciliation(project)
            self.assertEqual(recon["status"], "mismatch")
            self.assertEqual(recon["world_counts"], [8, 9, 10])
            self.assertIn("8, 9, 10", recon["line"])

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


class PinnedWorldCountsTests(unittest.TestCase):
    """D6: a scope-pinned count is placed in EVERY round; the
    ramp is hazards, not collectibles. Unpinned builds keep the
    escalation. (0.11.0: exercised through plan worlds.)"""

    def _plan(self, dimension, win_text):
        vals = {
            "Title (working)": "Pup Game",
            "Session length (one sitting)": "10 minutes",
            "Content counts (levels / waves / rooms / items, "
            "as applicable)": "3 rounds",
            "Win condition": win_text,
            "Lose / fail condition": "Time runs out",
            "Dimension": dimension,
        }
        return plan_module.derive_plan(vals)

    def _counts(self, derived):
        data = world.world_from_plan(derived)
        self.assertEqual(world.validate_world(data), [])
        return [len(u["collectibles"]) for u in data["units"]], data

    def test_pinned_2d_world_places_the_count_every_round(self):
        counts, data = self._counts(
            self._plan("2D", "Collect 8 comet pups each round"))
        self.assertEqual(counts, [8, 8, 8])
        self.assertTrue(all(u["targets_pinned"] for u in data["units"]))

    def test_pinned_3d_world_places_the_count_every_round(self):
        counts, data = self._counts(
            self._plan("3D", "Collect 8 comet pups each round"))
        self.assertEqual(counts, [8, 8, 8])
        self.assertTrue(all(u["targets_pinned"] for u in data["units"]))

    def test_unpinned_worlds_still_escalate(self):
        for dimension in ("2D", "3D"):
            counts, data = self._counts(
                self._plan(dimension, "Collect every pup"))
            self.assertEqual(counts, [5, 6, 7], dimension)
            self.assertFalse(any(u["targets_pinned"]
                                 for u in data["units"]))

    def test_gate_catches_a_pinned_round_with_the_wrong_count(self):
        _counts, data = self._counts(
            self._plan("2D", "Collect 8 comet pups each round"))
        data["units"][1]["collectibles"].pop()
        issues = world.validate_world(data)
        self.assertTrue(any("needs 8" in i["detail"] for i in issues),
                        issues)


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
            self.assertEqual(summary["plan"]["movement"], "top_down_2d")
            self.assertNotIn("preset", summary)
            numbers = {n["label"]: n for n in summary["numbers"]}
            # The row speaks the maker's nouns (the scope's stars).
            targets = numbers["Stars per round"]
            # D6: the summary states the per-round requirement —
            # a pinned count holds in every round.
            self.assertEqual(targets["value"], "10 in every round")
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
            # Unpinned: the summary shows the escalation the
            # rounds will require (D6), flagged as a default.
            # (The filled fixture states no content count, so the
            # summary's default unit count is 5 rounds.)
            self.assertEqual(numbers["Targets per round"]["value"],
                             "5, 6, 7, 8, 9 across rounds")
            self.assertEqual(numbers["Targets per round"]["source"],
                             "default")

    def test_summary_shows_the_escalation_for_unpinned_rounds(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = scoped_project(tmp, {
                WIN: "- REQUIRED Win condition: Collect every star",
                COUNTS: ("- REQUIRED Content counts (levels / waves / "
                         "rooms / items, as applicable): 3 rounds"),
            })
            summary = generate.build_summary(project)
            numbers = {n["label"]: n for n in summary["numbers"]}
            self.assertEqual(numbers["Targets per round"]["value"],
                             "5, 6, 7 across rounds")
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
