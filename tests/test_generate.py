"""Tests for real game generation (generate.py).

Pure-logic tests (plan derivation, the crate solver, level
invariants, brain-proposal clamping) run everywhere; the builds
that validate in Godot run when Godot is available, like the
skeleton tests. The legacy deterministic-config path that old
projects still build through is covered as compatibility.
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

from weaverbird import generate, ledger, plan, provenance, scope, skeleton, state  # noqa: E402
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


TITLE = "- REQUIRED Title (working): Filled in for the test"
GENRE = "- REQUIRED Genre / archetype: Filled in for the test"
VERB = "- REQUIRED Core verb (what the player does most): Filled in for the test"
COUNTS = ("- REQUIRED Content counts (levels / waves / rooms / items, "
          "as applicable): Filled in for the test")


class DetectionTests(unittest.TestCase):
    """The plan derivation that replaced archetype detection:
    the same scope words, read as movement + objective."""

    def values(self, **overrides):
        base = {
            "Genre / archetype": "", "Core verb (what the player does most)": "",
            "Win condition": "", "Lose / fail condition": "",
        }
        base.update(overrides)
        return base

    def test_platform_words_choose_side_view(self):
        derived = plan.derive_plan(
            self.values(**{"Genre / archetype": "2D platformer"}))
        self.assertEqual(derived["movement"], "side_view")
        self.assertTrue(derived["derivations"])

    def test_jump_verb_chooses_side_view(self):
        derived = plan.derive_plan(self.values(
            **{"Core verb (what the player does most)": "run and jump"}))
        self.assertEqual(derived["movement"], "side_view")

    def test_crate_puzzle_words_choose_grid_step(self):
        derived = plan.derive_plan(self.values(
            **{"Genre / archetype": "puzzle game with crates to push"}))
        self.assertEqual(derived["movement"], "grid_step")
        self.assertEqual(derived["objective"], "deliver")

    def test_no_signal_defaults_to_top_down(self):
        derived = plan.derive_plan(
            self.values(**{"Genre / archetype": "arena survivor"}))
        self.assertEqual(derived["movement"], "top_down_2d")
        self.assertIn("default", " ".join(derived["derivations"]))

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
    """Generator invariants, checked without Godot across many seeds.

    The physics the generated player ships with (generate.py):
    speed 280, jump 545, gravity 1400 — a jump clears ~106px, and
    a rise of R px can be landed between ~40 and ~177px of travel
    after takeoff. Traversable means a chain of such jumps exists
    from the ground to every floating step — a missed jump must
    cost a fall and a re-climb, never the run. (Regression: levels
    once chained steps up to 232px above the ground with no way
    back up after a fall, and the competent bot hopped in place
    under a star until the runner killed it.)
    """

    def _surfaces(self, level):
        return [(p[0], p[0] + p[2], p[1]) for p in level["platforms"]]

    def _reachable(self, level):
        surfaces = self._surfaces(level)
        reached = {i for i, s in enumerate(surfaces) if s[2] == 560}
        changed = True
        while changed:
            changed = False
            for j, target in enumerate(surfaces):
                if j in reached:
                    continue
                for i in list(reached):
                    start = surfaces[i]
                    rise = start[2] - target[2]
                    gap = max(target[0] - start[1], start[0] - target[1])
                    if rise <= 80 and gap <= 150:
                        reached.add(j)
                        changed = True
                        break
        return reached

    def test_levels_are_traversable_by_construction(self):
        import random
        for seed in range(40):
            rng = random.Random(seed)
            for index in range(3):
                level = generate._platformer_level(rng, index)
                where = f"seed {seed} level {index}"
                self.assertTrue(level["spikes"], where)
                self.assertTrue(level["collectibles"], where)
                self.assertEqual(level["exit"][0], level["width"] - 70,
                                 where)
                surfaces = self._surfaces(level)
                ground = [s for s in surfaces if s[2] == 560]
                self.assertTrue(
                    any(s[0] <= level["exit"][0] <= s[1] for s in ground),
                    f"exit not on the ground: {where}")
                floats = sorted((s for s in surfaces if s[2] != 560),
                                key=lambda s: s[0])
                # The staircase base is within jump reach of the ground.
                self.assertLessEqual(560 - floats[0][2], 80, where)
                # Consecutive steps: a true stair — overlapping in x
                # or a small gap, never a leap, and a bounded rise
                # or drop.
                for prev, nxt in zip(floats, floats[1:]):
                    gap = nxt[0] - prev[1]
                    self.assertGreaterEqual(gap, -24, where)
                    self.assertLessEqual(gap, 36, where)
                    self.assertLessEqual(abs(nxt[2] - prev[2]), 64, where)
                # Every step is reachable from the ground through the
                # jump graph — no perfect-run-only chains.
                reached = self._reachable(level)
                for i, surface in enumerate(surfaces):
                    self.assertIn(i, reached,
                                  f"surface {surface} unreachable: {where}")
                # Every star but the last sits on a step; the last
                # sits on the ground by the exit.
                for star in level["collectibles"][:-1]:
                    self.assertTrue(
                        any(s[2] == star[1] + 30 and s[0] <= star[0] <= s[1]
                            for s in floats),
                        f"star {star} not on a step: {where}")
                self.assertEqual(level["collectibles"][-1],
                                 [level["width"] - 170, 528], where)
                self.assertTrue(
                    any(s[0] <= level["width"] - 170 <= s[1]
                        for s in ground),
                    f"final star not over the ground: {where}")
                # Spike pits are jumpable gaps in the ground, and
                # no low step hangs over a pit or its takeoff: a
                # ground jump's head peaks at ~418px, so a step
                # whose top is below 396 (underside within that
                # arc) over the pit turns the jump into a bonk.
                for zone in level["spikes"]:
                    self.assertGreater(zone[2], 0, where)
                    self.assertLessEqual(zone[2], 105, where)
                    for s in floats:
                        if s[2] > 396:
                            self.assertFalse(
                                zone[0] - 160 < s[1]
                                and s[0] < zone[0] + zone[2],
                                f"low step {s} over pit {zone}: {where}")


class ConfigTests(unittest.TestCase):
    """Legacy compatibility: projects built by older versions
    still resolve their deterministic configs (the repair loop
    rebuilds them through this path)."""

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

    def test_plan_tuning_is_validated_and_clamped(self):
        # The genre-agnostic path: the brain tunes the plan's
        # numbers, clamped to the plan bounds for its movement.
        brain_module.complete = lambda *a, **k: brain_module.BrainResult(
            ok=True, text='{"player_speed": 9999, "hazard_speed": 0.01}',
            provider="openai", model="fake")
        with tempfile.TemporaryDirectory() as tmp:
            project = scoped_project(tmp, {
                "- REQUIRED Core verb (what the player does most): Filled in for the test":
                    "- REQUIRED Core verb (what the player does most): gather apples while wasps patrol the orchard",
                "- REQUIRED Win condition: Filled in for the test":
                    "- REQUIRED Win condition: Collect 6 apples in each round",
                COUNTS: "- REQUIRED Content counts (levels / waves / rooms / items, as applicable): 1 rounds",
            })
            skeleton.build_skeleton(project, validate=False)
            ctx = generate.prepare_build(project)
            self.assertTrue(ctx["brain_used"], ctx["brain_note"])
            self.assertEqual(ctx["config"]["player_speed"], 420.0)
            self.assertEqual(ctx["config"]["hazard_speed"], 0.6)
            self.assertEqual(ctx["plan"]["numbers"]["player_speed"], 420.0)

    def test_plan_tuning_never_moves_a_pinned_count(self):
        brain_module.complete = lambda *a, **k: brain_module.BrainResult(
            ok=True, text='{"targets_per_round": 2}',
            provider="openai", model="fake")
        with tempfile.TemporaryDirectory() as tmp:
            project = scoped_project(tmp, {
                "- REQUIRED Win condition: Filled in for the test":
                    "- REQUIRED Win condition: Collect 6 apples in each round",
                COUNTS: "- REQUIRED Content counts (levels / waves / rooms / items, as applicable): 1 rounds",
            })
            skeleton.build_skeleton(project, validate=False)
            ctx = generate.prepare_build(project)
            self.assertEqual(ctx["config"]["targets_per_round"], 6)


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
            self.assertEqual(report["plan"]["movement"], "side_view")
            self.assertNotIn("archetype", report)
            self.assertEqual(report["units_delivered"], 2)
            game = project / "game"
            config = json.loads((game / "game_config.json").read_text())
            self.assertEqual(config["plan"]["movement"], "side_view")
            self.assertNotIn("archetype", config)
            self.assertNotIn("levels", config)
            world = json.loads((game / "world" / "world.json").read_text())
            self.assertEqual(len(world["units"]), 2)
            self.assertEqual(world["plan"]["movement"], "side_view")
            for rel in ("tests/competent_win.gd", "tests/competent_lose.gd",
                        "tests/breaker.gd", "tests/first_time.gd",
                        "smoke/smoke.gd", "scripts/asset_loader.gd",
                        "scripts/star.gd", "scripts/player.gd",
                        "scripts/marker.gd", "scripts/door_draw.gd"):
                self.assertTrue((game / rel).exists(), rel)
            game_gd = (game / "scripts/game.gd").read_text()
            self.assertIn("game_config.json", game_gd)
            records = {r["asset"] for r in provenance.list_records(project)}
            self.assertIn("game/game_config.json", records)
            summary = ledger.summarise(project)
            self.assertEqual(summary["by_stage"]["build"]["tokens"], 0)


@unittest.skipUnless(VALIDATE, "Godot validation not available")
class BuildValidationTests(unittest.TestCase):
    """One real validated build per movement family, small scopes."""

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
            self.assertEqual(report["plan"]["movement"], "grid_step")
            self.assertTrue(report["validation"]["passed"],
                            report["validation"])

    def test_arena_build_validates(self):
        with tempfile.TemporaryDirectory() as tmp:
            _project, report = self._build(tmp, {
                COUNTS: "- REQUIRED Content counts (levels / waves / rooms / items, as applicable): 1 rounds",
            })
            self.assertEqual(report["plan"]["movement"], "top_down_2d")
            self.assertTrue(report["validation"]["passed"],
                            report["validation"])

    def test_handler_passes_with_validated_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            project, _report = self._build(tmp, {
                COUNTS: "- REQUIRED Content counts (levels / waves / rooms / items, as applicable): 1 rounds",
            })
            self.assertTrue(generate.build_stage_handler(project))


@unittest.skipUnless(VALIDATE, "Godot validation not available")
class PlatformerSeedSweepTests(unittest.TestCase):
    """Regression sweep for the competent-bot hang: the platformer
    layout seed comes from the game title, so sweep titles. For
    every seed the competent bot must WIN a 2-level game — finish,
    not be killed — comfortably inside its own 90s cap (the runner
    allows 110s here; the factory's build gate allows 120s)."""

    TITLES = [
        "Hopper", "Star Hop", "Pebble Dash", "Cloud Steps",
        "Fern Gully Run", "Mossy Ascent", "Tin Roof Runner",
        "Bracken Leap", "Slate Stairs", "Willow Wisp Way",
        "Copper Canyon Hop", "Dewdrop Climb", "Ember Steps",
        "Foxglove Flight", "Granite Garden", "Hazel Heights",
        "Ivy Overpass", "Juniper Jump", "Kestrel Keep", "Lichen Loft",
    ]

    def test_competent_bot_wins_across_seeds(self):
        godot_bin, _version = godot_module.find_godot()
        counts = ("- REQUIRED Content counts (levels / waves / rooms / "
                  "items, as applicable): 2 levels")
        failures = []
        for title in self.TITLES:
            with tempfile.TemporaryDirectory() as tmp:
                project = scoped_project(tmp, {
                    TITLE: f"- REQUIRED Title (working): {title}",
                    GENRE: ("- REQUIRED Genre / archetype: 2D platformer "
                            "— run, jump, reach the exit"),
                    COUNTS: counts,
                })
                skeleton.build_skeleton(project, validate=False)
                generate.build_game(project, validate=False,
                                    use_brain=False)
                game_dir = project / "game"
                imported = skeleton._run_godot(
                    godot_bin, game_dir, ["--import"], timeout=60)
                if not imported["ok"]:
                    failures.append((title, "import", imported["errors"]))
                    continue
                run = skeleton._run_godot(
                    godot_bin, game_dir,
                    ["--script", "tests/competent_win.gd"], timeout=110)
                if not (run["ok"] and run["smoke_pass"]):
                    failures.append((title, "competent bot",
                                     run["errors"] or run["returncode"]))
        self.assertEqual(failures, [])


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
