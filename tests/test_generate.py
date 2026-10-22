"""Tests for real game generation (generate.py).

Pure-logic tests (archetype detection, the crate solver, level
invariants, brain-proposal clamping) run everywhere; the builds
that validate in Godot run when Godot is available, like the
skeleton tests.
"""

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import filled_scope_text, template_scope_text  # noqa: E402

from weaverbird import generate, ledger, provenance, scope, skeleton, state  # noqa: E402
from weaverbird import brain as brain_module  # noqa: E402
from weaverbird import godot as godot_module  # noqa: E402

GODOT_PRESENT = godot_module.find_godot()[0] is not None
VALIDATE = GODOT_PRESENT


def scoped_project(tmp, replacements=None, approve=True):
    text = filled_scope_text()
    for old, new in (replacements or {}).items():
        assert old in text, old
        text = text.replace(old, new)
    project = Path(tmp)
    (project / "SCOPE.md").write_text(text, encoding="utf-8")
    state.init_state(project)
    if approve:
        scope.approve_scope(project / "SCOPE.md", by="Tester")
    return project


GENRE = "- REQUIRED Genre / archetype: Filled in for the test"
VERB = "- REQUIRED Core verb (what the player does most): Filled in for the test"
COUNTS = ("- REQUIRED Content counts (levels / waves / rooms / items, "
          "as applicable): Filled in for the test")


class DetectionTests(unittest.TestCase):
    def values(self, **overrides):
        base = {
            "Genre / archetype": "", "Core verb (what the player does most)": "",
            "Win condition": "", "Lose / fail condition": "",
        }
        base.update(overrides)
        return base

    def test_platform_words_choose_platformer(self):
        archetype, why = generate.detect_archetype(
            self.values(**{"Genre / archetype": "2D platformer"}))
        self.assertEqual(archetype, "platformer")
        self.assertTrue(why)

    def test_jump_verb_chooses_platformer(self):
        archetype, _ = generate.detect_archetype(self.values(
            **{"Core verb (what the player does most)": "run and jump"}))
        self.assertEqual(archetype, "platformer")

    def test_crate_puzzle_words_choose_crate_pusher(self):
        archetype, _ = generate.detect_archetype(self.values(
            **{"Genre / archetype": "puzzle game with crates to push"}))
        self.assertEqual(archetype, "crate_pusher")

    def test_no_signal_defaults_to_arena(self):
        archetype, why = generate.detect_archetype(
            self.values(**{"Genre / archetype": "arena survivor"}))
        self.assertEqual(archetype, "arena_collector")
        self.assertIn("default", why)

    def test_scope_note_beats_detection(self):
        archetype, _ = generate.detect_archetype(self.values(
            **{"Genre / archetype": "platformer — archetype: crate_pusher"}))
        self.assertEqual(archetype, "crate_pusher")

    def test_override_beats_everything(self):
        archetype, why = generate.detect_archetype(
            self.values(**{"Genre / archetype": "platformer"}),
            override="arena")
        self.assertEqual(archetype, "arena_collector")
        self.assertIn("override", why)

    def test_unknown_override_is_refused(self):
        with self.assertRaises(generate.GenerationError):
            generate.detect_archetype(self.values(), override="shmup")

    def test_content_units_parse_and_clamp(self):
        field = "Content counts (levels / waves / rooms / items, as applicable)"
        self.assertEqual(generate.content_units({field: "3 levels"}), 3)
        self.assertEqual(generate.content_units({field: "99 levels"}), 20)
        self.assertEqual(generate.content_units({field: "no number"}), 5)


def simulate_moves(grid_text, moves):
    """Play a move string through the crate rules in pure Python."""
    parsed = generate.parse_grid(grid_text)
    walls, pads = set(parsed["walls"]), set(parsed["pads"])
    crates = set(parsed["crates"])
    player = parsed["player"]
    for move in moves:
        dx, dy = generate._MOVE_DIRS[move]
        nxt = (player[0] + dx, player[1] + dy)
        if nxt in walls:
            continue
        if nxt in crates:
            beyond = (nxt[0] + dx, nxt[1] + dy)
            if beyond in walls or beyond in crates:
                continue
            crates.discard(nxt)
            crates.add(beyond)
        player = nxt
    return player, crates, pads


class CrateSolverTests(unittest.TestCase):
    def test_every_built_in_layout_is_solvable_and_solution_wins(self):
        for layout in generate.CRATE_LAYOUTS:
            solution = generate.solve_puzzle(layout)
            self.assertIsNotNone(solution, layout)
            _player, crates, pads = simulate_moves(layout, solution)
            self.assertEqual(crates, pads)

    def test_mirrored_layouts_are_solvable_too(self):
        puzzles = generate.crate_puzzles(len(generate.CRATE_LAYOUTS) + 2)
        self.assertEqual(len(puzzles), len(generate.CRATE_LAYOUTS) + 2)
        for puzzle in puzzles:
            _player, crates, pads = simulate_moves(
                "\n".join(puzzle["grid"]), puzzle["solution"])
            self.assertEqual(crates, pads)
            self.assertGreaterEqual(puzzle["budget"], len(puzzle["solution"]))

    def test_sabotage_corners_a_crate(self):
        layout = generate.CRATE_LAYOUTS[0]
        sabotage = generate.find_sabotage(layout)
        self.assertTrue(sabotage)
        _player, crates, pads = simulate_moves(layout, sabotage)
        walls = set(generate.parse_grid(layout)["walls"])
        self.assertTrue(
            any(generate._cornered(c, walls, pads) for c in crates),
            "the sabotage line should leave a crate cornered off its pad")


class PlatformerLevelTests(unittest.TestCase):
    def test_levels_are_traversable_by_construction(self):
        import random
        rng = random.Random(1234)
        for index in range(4):
            level = generate._platformer_level(rng, index)
            self.assertTrue(level["spikes"], "every level needs a pit")
            self.assertTrue(level["collectibles"])
            self.assertEqual(level["exit"][0], level["width"] - 70)
            floats = [p for p in level["platforms"] if p[1] != 560]
            floats.sort(key=lambda p: p[0])
            # From the ground to the first platform, and between
            # consecutive platforms, the upward step stays inside
            # jump reach (the generated jump clears ~100px).
            previous_top = 560
            for platform in floats:
                step_up = previous_top - platform[1]
                self.assertLessEqual(step_up, 92,
                                     "step up beyond jump reach")
                previous_top = platform[1]
            for zone in level["spikes"]:
                self.assertGreater(zone[2], 0)


class ConfigTests(unittest.TestCase):
    def test_deterministic_configs_carry_units(self):
        values = {
            "Content counts (levels / waves / rooms / items, as applicable)":
                "3 levels",
            "Session length (one sitting)": "10",
            "Title (working)": "T", "Lose / fail condition": "out of lives",
            "Palette": "#112233 and #AABBCC",
        }
        palette = generate._palette(values)
        config, _ = generate.deterministic_config(
            "platformer", values, palette, 3)
        self.assertEqual(len(config["levels"]), 3)
        self.assertEqual(config["lives"], 3)
        self.assertEqual(generate.units_delivered("platformer", config), 3)
        config, _ = generate.deterministic_config(
            "arena_collector", values, palette, 3)
        self.assertEqual(config["rounds"], 3)
        config, _ = generate.deterministic_config(
            "crate_pusher", values, palette, 3)
        self.assertEqual(len(config["puzzles"]), 3)
        for puzzle in config["puzzles"]:
            self.assertTrue(puzzle["solution"])


class BrainProposalTests(unittest.TestCase):
    def setUp(self):
        self._orig_complete = brain_module.complete
        self._orig_status = brain_module.tier_status
        self._orig_resolve = brain_module.resolve_settings
        brain_module.tier_status = lambda data, tier: {
            "usable": True, "reason": "", "provider": "openai",
            "model": "fake", "api_key_env": "X", "key_present": True,
            "configured": True, "base_url": None}
        brain_module.resolve_settings = lambda sp, pd: (
            Path("s.yaml"), {"models": {"mid": {}}})

    def tearDown(self):
        brain_module.complete = self._orig_complete
        brain_module.tier_status = self._orig_status
        brain_module.resolve_settings = self._orig_resolve

    def _project(self, tmp):
        project = Path(tmp)
        state.init_state(project)
        return project

    def test_proposal_is_validated_and_clamped(self):
        brain_module.complete = lambda *a, **k: brain_module.BrainResult(
            ok=True, text='{"player_speed": 9999, "hazard_speed": 0.01}',
            provider="openai", model="fake")
        with tempfile.TemporaryDirectory() as tmp:
            config = {"player_speed": 330.0, "hazard_speed": 1.4}
            tuned, used, note = generate.brain_proposal(
                self._project(tmp), "arena_collector", config, {})
            self.assertTrue(used)
            self.assertEqual(tuned["player_speed"], 420.0)  # clamped high
            self.assertEqual(tuned["hazard_speed"], 0.6)    # clamped low
            self.assertIn("clamped", note)

    def test_failed_brain_falls_back_to_deterministic(self):
        brain_module.complete = lambda *a, **k: brain_module.BrainResult(
            ok=False, error="no network")
        with tempfile.TemporaryDirectory() as tmp:
            config = {"player_speed": 330.0, "hazard_speed": 1.4}
            tuned, used, note = generate.brain_proposal(
                self._project(tmp), "arena_collector", config, {})
            self.assertFalse(used)
            self.assertEqual(tuned, config)
            self.assertIn("failed", note)


class BuildRefusalTests(unittest.TestCase):
    def test_refuses_without_a_skeleton(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = scoped_project(tmp)
            with self.assertRaises(generate.GenerationError) as ctx:
                generate.build_game(project, validate=False)
            self.assertIn("skeleton", str(ctx.exception).lower())

    def test_refuses_unapproved_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = scoped_project(tmp, approve=False)
            # A skeleton game dir made by hand: the builder itself
            # refuses unapproved scopes, so generate's own check is
            # what is under test here.
            game = project / "game"
            game.mkdir()
            (game / "project.godot").write_text("; fixture\n",
                                                encoding="utf-8")
            with self.assertRaises(generate.GenerationError) as ctx:
                generate.build_game(project, validate=False)
            self.assertIn("not approved", str(ctx.exception))


class BuildShapeTests(unittest.TestCase):
    """A deterministic build (no Godot) writes the right files."""

    def test_build_writes_config_tests_and_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = scoped_project(tmp, {
                GENRE: "- REQUIRED Genre / archetype: 2D platformer",
                COUNTS: "- REQUIRED Content counts (levels / waves / rooms / items, as applicable): 2 levels",
            })
            skeleton.build_skeleton(project, validate=False)
            report = generate.build_game(project, validate=False,
                                         use_brain=False)
            self.assertEqual(report["archetype"], "platformer")
            self.assertEqual(report["units_delivered"], 2)
            game = project / "game"
            config = json.loads((game / "game_config.json").read_text())
            self.assertEqual(config["archetype"], "platformer")
            self.assertEqual(len(config["levels"]), 2)
            for rel in ("tests/competent_win.gd", "tests/competent_lose.gd",
                        "tests/breaker.gd", "tests/first_time.gd",
                        "smoke/smoke.gd", "scripts/asset_loader.gd",
                        "scripts/star.gd", "scripts/player.gd"):
                self.assertTrue((game / rel).exists(), rel)
            game_gd = (game / "scripts/game.gd").read_text()
            self.assertIn("game_config.json", game_gd)
            records = {r["asset"] for r in provenance.list_records(project)}
            self.assertIn("game/game_config.json", records)
            summary = ledger.summarise(project)
            self.assertEqual(summary["by_stage"]["build"]["tokens"], 0)


@unittest.skipUnless(VALIDATE, "Godot validation not available")
class BuildValidationTests(unittest.TestCase):
    """One real validated build per archetype, small scopes."""

    def _build(self, tmp, replacements):
        project = scoped_project(tmp, replacements)
        skeleton.build_skeleton(project)
        report = generate.build_game(project, use_brain=False)
        return project, report

    def test_platformer_build_validates_and_tags(self):
        with tempfile.TemporaryDirectory() as tmp:
            project, report = self._build(tmp, {
                GENRE: "- REQUIRED Genre / archetype: 2D platformer",
                COUNTS: "- REQUIRED Content counts (levels / waves / rooms / items, as applicable): 1 levels",
            })
            self.assertTrue(report["validation"]["passed"],
                            report["validation"])
            self.assertEqual(report["known_good_tag"], "known-good/build")
            from weaverbird import vcs
            self.assertIn("known-good/build", vcs.list_tags(project))

    def test_crate_build_validates(self):
        with tempfile.TemporaryDirectory() as tmp:
            _project, report = self._build(tmp, {
                GENRE: "- REQUIRED Genre / archetype: puzzle — push crates onto pads",
                COUNTS: "- REQUIRED Content counts (levels / waves / rooms / items, as applicable): 1 puzzles",
            })
            self.assertEqual(report["archetype"], "crate_pusher")
            self.assertTrue(report["validation"]["passed"],
                            report["validation"])

    def test_arena_build_validates(self):
        with tempfile.TemporaryDirectory() as tmp:
            _project, report = self._build(tmp, {
                COUNTS: "- REQUIRED Content counts (levels / waves / rooms / items, as applicable): 1 rounds",
            })
            self.assertEqual(report["archetype"], "arena_collector")
            self.assertTrue(report["validation"]["passed"],
                            report["validation"])

    def test_handler_passes_with_validated_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            project, _report = self._build(tmp, {
                COUNTS: "- REQUIRED Content counts (levels / waves / rooms / items, as applicable): 1 rounds",
            })
            self.assertTrue(generate.build_stage_handler(project))


class GenerateCliTests(unittest.TestCase):
    def test_build_game_refuses_without_skeleton(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = scoped_project(tmp)
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                from weaverbird import cli
                code = cli.main(["build", "game", str(project)])
            self.assertEqual(code, 1)
            self.assertIn("skeleton", buffer.getvalue().lower())


if __name__ == "__main__":
    unittest.main()
