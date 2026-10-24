"""Tests for the game plan derivation (plan.py, 0.11.0).

The plan is derived from the scope's own words: movement,
objective, fail rules, entities with behaviours, and numbers —
each with a derivation note quoting the scope. There is no
preset registry and no matcher: these tests pin the vocabulary
and the honest bespoke boundary.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import filled_scope_text  # noqa: E402

from weaverbird import plan, scope  # noqa: E402


def values_for(replacements=None):
    text = filled_scope_text()
    for prefix, line in (replacements or {}).items():
        lines = text.splitlines()
        text = "\n".join(
            line if existing.startswith(prefix) else existing
            for existing in lines) + "\n"
    parsed = scope.parse_scope_text(text)
    return {f.name: f.value for f in parsed.fields}


TITLE = "- REQUIRED Title (working):"
GENRE = "- REQUIRED Genre / archetype:"
VERB = "- REQUIRED Core verb (what the player does most):"
WIN = "- REQUIRED Win condition:"
LOSE = "- REQUIRED Lose / fail condition:"
COUNTS = "- REQUIRED Content counts (levels / waves / rooms / items, as applicable):"
PITCH = "- REQUIRED One-line pitch:"

COLLECTOR = {
    TITLE: f"{TITLE} Orchard Run",
    GENRE: f"{GENRE} top-down orchard adventure",
    VERB: f"{VERB} gather apples while wasps patrol the orchard",
    WIN: f"{WIN} Collect 6 apples in each round",
    LOSE: f"{LOSE} the round timer runs out",
    COUNTS: f"{COUNTS} 2 rounds",
}

FLAG = {
    TITLE: f"{TITLE} Star Hop",
    GENRE: f"{GENRE} 2D side-view platformer",
    VERB: f"{VERB} leap between rooftops and climb to the flag",
    WIN: f"{WIN} reach the flag on the highest rooftop; you may "
         "gather fallen stars on the way",
    LOSE: f"{LOSE} fall into a spike pit or lose all 3 lives",
    COUNTS: f"{COUNTS} 2 levels",
}

CRATES = {
    TITLE: f"{TITLE} Dock Hands",
    GENRE: f"{GENRE} grid puzzle",
    VERB: f"{VERB} push crates onto the dock pads",
    WIN: f"{WIN} push every crate onto a pad",
    LOSE: f"{LOSE} run out of moves",
    COUNTS: f"{COUNTS} 2 puzzles",
}

TOWER = {
    TITLE: f"{TITLE} Last Bastion",
    GENRE: f"{GENRE} tower defence",
    VERB: f"{VERB} place towers and upgrade them between waves",
    WIN: f"{WIN} survive all 10 waves",
    LOSE: f"{LOSE} enemies reach the base",
    COUNTS: f"{COUNTS} 10 waves",
}


class DerivationTests(unittest.TestCase):
    def test_collector_plan(self):
        derived = plan.derive_plan(values_for(COLLECTOR))
        self.assertEqual(derived["movement"], "top_down_2d")
        self.assertEqual(derived["objective"], "collect")
        self.assertIn("timer", derived["fail"])
        self.assertEqual(derived["numbers"]["targets_per_round"], 6)
        self.assertTrue(derived["numbers"]["targets_pinned"])
        self.assertEqual(derived["collectible_label"], "Apples")
        kinds = [e["kind"] for e in derived["entities"]]
        self.assertEqual(kinds, ["player", "collectible", "hazard"])
        hazard = derived["entities"][2]
        self.assertEqual(hazard["label"], "Wasps")
        self.assertEqual(hazard["behaviour"], "patrol")
        self.assertIsNone(derived["bespoke_required"])

    def test_flag_plan_is_reach_with_optional_collectibles(self):
        derived = plan.derive_plan(values_for(FLAG))
        self.assertEqual(derived["movement"], "side_view")
        self.assertEqual(derived["objective"], "reach")
        self.assertIn("fall", derived["fail"])
        self.assertEqual(derived["lives"], 3)
        by_kind = {e["kind"]: e for e in derived["entities"]}
        self.assertEqual(by_kind["goal"]["label"], "Flag")
        self.assertTrue(by_kind["collectible"]["optional"])
        self.assertEqual(derived["collectible_label"], "Fallen Stars")

    def test_crates_plan_is_deliver(self):
        derived = plan.derive_plan(values_for(CRATES))
        self.assertEqual(derived["movement"], "grid_step")
        self.assertEqual(derived["objective"], "deliver")
        kinds = [e["kind"] for e in derived["entities"]]
        self.assertEqual(kinds, ["player", "pushable", "zone", "wall"])
        by_kind = {e["kind"]: e for e in derived["entities"]}
        self.assertEqual(by_kind["pushable"]["label"], "Crates")
        self.assertEqual(by_kind["zone"]["label"], "Docks")

    def test_every_choice_carries_a_derivation_note(self):
        for fixture in (COLLECTOR, FLAG, CRATES):
            derived = plan.derive_plan(values_for(fixture))
            self.assertTrue(derived["derivations"])
            joined = " ".join(derived["derivations"])
            self.assertIn("scope", joined)
            self.assertIn(derived["movement"], joined)

    def test_default_scope_is_a_plain_collector(self):
        derived = plan.derive_plan(values_for())
        self.assertEqual(derived["movement"], "top_down_2d")
        self.assertEqual(derived["objective"], "collect")
        self.assertIsNone(derived["bespoke_required"])

    def test_survive_plan_forces_a_hazard(self):
        derived = plan.derive_plan(values_for({
            TITLE: f"{TITLE} Night Watch",
            VERB: f"{VERB} run from the drifting wraiths",
            WIN: f"{WIN} survive until dawn",
            LOSE: f"{LOSE} the wraiths catch you three times",
            COUNTS: f"{COUNTS} 1 rounds",
        }))
        self.assertEqual(derived["objective"], "survive")
        kinds = [e["kind"] for e in derived["entities"]]
        self.assertIn("hazard", kinds)
        self.assertNotIn("timer", derived["fail"])

    def test_3d_scope_uses_the_3d_movement(self):
        derived = plan.derive_plan(values_for({
            TITLE: f"{TITLE} Comet Herder",
            GENRE: f"{GENRE} 3D herding game",
            "- REQUIRED Dimension:": "- REQUIRED Dimension: 3D",
            WIN: f"{WIN} Collect 8 comet pups each round",
            LOSE: f"{LOSE} the timer runs out",
            COUNTS: f"{COUNTS} 3 rounds",
        }))
        self.assertEqual(derived["movement"], "top_down_3d")
        self.assertEqual(derived["numbers"]["targets_per_round"], 8)


class BespokeBoundaryTests(unittest.TestCase):
    def test_tower_defence_is_bespoke_and_names_the_mechanic(self):
        derived = plan.derive_plan(values_for(TOWER))
        bespoke = derived["bespoke_required"]
        self.assertIsNotNone(bespoke)
        self.assertIn("tower defence", bespoke["mechanic"])
        self.assertIn("tower defence", bespoke["because"])
        self.assertIsNone(derived["movement"])
        self.assertEqual(derived["entities"], [])

    def test_ordered_checkpoints_are_bespoke(self):
        derived = plan.derive_plan(values_for({
            TITLE: f"{TITLE} Grand Tour",
            GENRE: f"{GENRE} racing game",
            VERB: f"{VERB} drive through checkpoints in order",
            WIN: f"{WIN} pass every checkpoint in order and finish "
                 "3 laps first",
            LOSE: f"{LOSE} finish last",
            COUNTS: f"{COUNTS} 3 laps",
        }))
        self.assertIsNotNone(derived["bespoke_required"])
        self.assertIn("checkpoint", derived["bespoke_required"]["mechanic"])

    def test_dialogue_game_is_bespoke(self):
        derived = plan.derive_plan(values_for({
            TITLE: f"{TITLE} The Debate",
            GENRE: f"{GENRE} narrative duel",
            VERB: f"{VERB} negotiate dialogue with a rival debater",
            WIN: f"{WIN} win the debate",
            LOSE: f"{LOSE} the argument timer runs out",
            COUNTS: f"{COUNTS} 1 rounds",
        }))
        self.assertIsNotNone(derived["bespoke_required"])

    def test_match_three_is_bespoke(self):
        derived = plan.derive_plan(values_for({
            TITLE: f"{TITLE} Gem Cascade",
            GENRE: f"{GENRE} match-3 puzzle",
            VERB: f"{VERB} swap gems to match three and trigger cascades",
            WIN: f"{WIN} score 5000 points",
            LOSE: f"{LOSE} run out of moves",
            COUNTS: f"{COUNTS} 1 rounds",
        }))
        self.assertIsNotNone(derived["bespoke_required"])
        self.assertIn("match-3", derived["bespoke_required"]["mechanic"])


class LabelTests(unittest.TestCase):
    def test_collectible_label_from_the_win_sentence(self):
        derived = plan.derive_plan(values_for({
            WIN: f"{WIN} Collect 8 comet pups each round before "
                 "the timer runs out",
        }))
        self.assertEqual(derived["collectible_label"], "Comet Pups")

    def test_collectible_label_falls_back_to_targets(self):
        self.assertEqual(plan.collectible_label({}), "Targets")

    def test_objective_hint_uses_the_makers_words(self):
        derived = plan.derive_plan(values_for(COLLECTOR))
        hint = plan.objective_hint(derived, values_for(COLLECTOR))
        self.assertIn("Apples", hint)


class BoundsTests(unittest.TestCase):
    def test_every_movement_has_patch_bounds(self):
        for movement in ("top_down_2d", "side_view", "grid_step",
                         "top_down_3d"):
            self.assertIn(movement, plan.PLAN_BOUNDS)

    def test_unit_words(self):
        self.assertEqual(plan.unit_word({"movement": "side_view"}), "levels")
        self.assertEqual(plan.unit_word({"movement": "grid_step"}), "puzzles")
        self.assertEqual(plan.unit_word({"movement": "top_down_2d"}), "rounds")
        self.assertEqual(plan.unit_word({"movement": "top_down_3d"}), "rounds")


if __name__ == "__main__":
    unittest.main()
