"""Tests for the 2D arena layout variety (0.10.0): collectible
positions by deterministic pattern instead of the old fixed
spot list whose first five spots formed a dead-straight row.

Pure logic — the positions live in world.json and the checks are
the world worker's own; no Godot needed.
"""

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import helpers  # noqa: E402,F401  (puts src/ on sys.path)

from weaverbird import world  # noqa: E402


def arena_config(rounds=3, targets=5, seed=1234):
    return {"rounds": rounds, "targets_per_round": targets,
            "round_seconds": 60, "world_seed": seed}


def collinear(points) -> bool:
    """True when every point sits on one straight line."""
    if len(points) < 3:
        return True
    (x0, y0), (x1, y1) = points[0], points[1]
    return all((x - x0) * (y1 - y0) == (y - y0) * (x1 - x0)
               for x, y in points[2:])


class ArenaLayoutTests(unittest.TestCase):
    def test_rounds_carry_positions_hazards_and_a_layout_record(self):
        data = world.world_from_config(
            "arena_collector", arena_config(), None)
        self.assertEqual(len(data["units"]), 3)
        for index, unit in enumerate(data["units"]):
            self.assertEqual(len(unit["collectibles"]),
                             min(5 + index, 12))
            self.assertEqual(len(unit["hazards"]), min(2 + index, 5))
            self.assertEqual(unit["spawn"], world.SPAWN_2D)
            self.assertIn(unit["layout"]["pattern"],
                          world.LAYOUT_PATTERNS)
            self.assertEqual(unit["layout"]["seed"], 1234)

    def test_the_app_test_case_is_not_a_straight_row(self):
        # The app-test game placed its 5 round-1 targets in a
        # dead-straight row (all at y≈110). That exact case:
        data = world.world_from_config(
            "arena_collector", arena_config(rounds=1, targets=5), None)
        points = data["units"][0]["collectibles"]
        self.assertEqual(len(points), 5)
        self.assertFalse(collinear(points), points)
        self.assertGreater(len({p[1] for p in points}), 1)

    def test_generated_worlds_pass_the_world_gate(self):
        for seed in (1, 42, 1234, 987654):
            data = world.world_from_config(
                "arena_collector", arena_config(seed=seed), None)
            self.assertEqual(world.validate_world(data), [],
                             f"seed {seed}")

    def test_same_seed_same_layout(self):
        one = world.world_from_config(
            "arena_collector", arena_config(seed=77), None)
        two = world.world_from_config(
            "arena_collector", arena_config(seed=77), None)
        self.assertEqual(
            [u["collectibles"] for u in one["units"]],
            [u["collectibles"] for u in two["units"]])

    def test_different_seed_different_layout(self):
        one = world.world_from_config(
            "arena_collector", arena_config(seed=77), None)
        two = world.world_from_config(
            "arena_collector", arena_config(seed=78), None)
        self.assertNotEqual(
            [u["collectibles"] for u in one["units"]],
            [u["collectibles"] for u in two["units"]])

    def test_every_pattern_produces_a_checked_layout(self):
        for pattern in world.LAYOUT_PATTERNS:
            positions, record = world.arena_layout(
                6, seed=5, round_index=0, patterns=(pattern,))
            hazards = world.HAZARD_SPOTS_2D[:2]
            self.assertEqual(
                world.arena_layout_issues(
                    positions, hazards, world.SPAWN_2D), [],
                f"pattern {pattern}")
            self.assertEqual(record["pattern"], pattern)

    def test_a_failing_pattern_falls_back_and_records_it(self):
        original = world._layout_positions
        first = world.LAYOUT_PATTERNS[0]

        def sabotaged(pattern, count, rng, hazards=(), spawn=None):
            if pattern == first:
                # All points on one spot: guaranteed to fail the
                # spacing checks.
                return [[576, 300]] * count
            return original(pattern, count, rng, hazards, spawn)

        with mock.patch.object(world, "_layout_positions", sabotaged):
            positions, record = world.arena_layout(
                5, seed=0, round_index=0)
        self.assertEqual(record["fallbacks"], [first])
        self.assertNotEqual(record["pattern"], first)
        self.assertEqual(world.arena_layout_issues(
            positions, world.HAZARD_SPOTS_2D[:2], world.SPAWN_2D), [])


class LayoutGateTests(unittest.TestCase):
    def _world_with(self, collectibles):
        return {
            "preset": "arena_collector", "bespoke": False,
            "entity_kinds": ["player", "collectible", "hazard"],
            "units": [{
                "index": 0, "kind": "round", "round": 1,
                "targets_per_round": 5, "round_seconds": 60,
                "arena": [1152, 648], "spawn": list(world.SPAWN_2D),
                "collectibles": collectibles,
                "hazards": [dict(h) for h in world.HAZARD_SPOTS_2D[:2]],
            }],
        }

    def test_gate_catches_a_collectible_on_a_patrol_line(self):
        # (400, 330) is the first hazard's centre — on its line.
        points = [[400, 330], [150, 120], [1000, 130], [180, 520],
                  [1020, 500]]
        issues = world.validate_world(self._world_with(points))
        self.assertTrue(any("hazard" in i["detail"] for i in issues),
                        issues)

    def test_gate_catches_wrong_count_and_outside_points(self):
        issues = world.validate_world(self._world_with([[30, 30]]))
        details = " ".join(i["detail"] for i in issues)
        self.assertIn("needs 5", details)
        self.assertIn("outside", details)

    def test_gate_catches_crowded_collectibles(self):
        points = [[200, 200], [210, 205], [800, 150], [900, 500],
                  [300, 520]]
        issues = world.validate_world(self._world_with(points))
        self.assertTrue(any("closer" in i["detail"] for i in issues),
                        issues)

    def test_rounds_without_positions_still_pass(self):
        # World files written before layouts existed carry no
        # positions — the gate must not fail them.
        data = self._world_with([[200, 200], [800, 150], [900, 500],
                                 [300, 520], [600, 140]])
        del data["units"][0]["collectibles"]
        self.assertEqual(world.validate_world(data), [])


if __name__ == "__main__":
    unittest.main()
