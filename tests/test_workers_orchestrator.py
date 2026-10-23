"""Pass 5 (0.7.0): the worker framework, the orchestrator, the
composition layer, and the bespoke mechanics path.

Fast tests cover the registry, hashing, plan construction and
failure routing with fake worker runs; the slow classes run the
real chain in Godot (skipped when Godot is unavailable), like
the rest of the suite.
"""

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import filled_scope_text  # noqa: E402

from weaverbird import cli, generate, godot, mechanics, orchestrator  # noqa: E402
from weaverbird import scope, skeleton, state, workers, world  # noqa: E402

VALIDATE = godot.find_godot()[0] is not None

GENRE = "- REQUIRED Genre / archetype:"
VERB = "- REQUIRED Core verb (what the player does most):"
WIN = "- REQUIRED Win condition:"
LOSE = "- REQUIRED Lose / fail condition:"
COUNTS = "- REQUIRED Content counts (levels / waves / rooms / items, as applicable):"
PALETTE = "- REQUIRED Palette:"


def scoped_project(tmp, replacements=None, approve=True):
    text = filled_scope_text()
    for prefix, line in (replacements or {}).items():
        lines = text.splitlines()
        text = "\n".join(
            line if existing.startswith(prefix) else existing
            for existing in lines) + "\n"
    project = Path(tmp) / "demo"
    project.mkdir(parents=True, exist_ok=True)
    (project / "SCOPE.md").write_text(text, encoding="utf-8")
    state.init_state(project)
    if approve:
        scope.approve_scope(project / "SCOPE.md", by="Tester")
    return project


TITLE = "- REQUIRED Title (working):"
PLATFORMER = {
    # "Star Hop" is one of the seed sweep's proven titles (see
    # test_generate.PlatformerSeedSweepTests): the layout seed
    # comes from the title, and the generic test title seeds a
    # spawn-pit level the competent bot only sometimes beats.
    TITLE: f"{TITLE} Star Hop",
    GENRE: f"{GENRE} 2D platformer",
    VERB: f"{VERB} jump and collect stars",
    WIN: f"{WIN} collect every star and reach the door",
    LOSE: f"{LOSE} lose all 3 lives",
    COUNTS: f"{COUNTS} 2 levels",
}

CRATE = {
    GENRE: f"{GENRE} puzzle",
    VERB: f"{VERB} push crates onto pads",
    WIN: f"{WIN} every crate on a pad",
    LOSE: f"{LOSE} run out of moves",
    COUNTS: f"{COUNTS} 1 puzzles",
}

BESPOKE = {
    GENRE: f"{GENRE} narrative duel",
    VERB: f"{VERB} negotiate dialogue with a rival debater",
    WIN: f"{WIN} win the debate",
    LOSE: f"{LOSE} the argument timer runs out",
    COUNTS: f"{COUNTS} 1 rounds",
}

BESPOKE_FIXTURE = '''extends Node2D

var _game: Node = null
var _config: Dictionary = {}
var _pos := Vector2(200, 324)
var _target := Vector2(900, 324)
var _time_left := 20.0
var _done := false


func setup(game: Node, config: Dictionary) -> void:
    _game = game
    _config = config


func _process(delta: float) -> void:
    if _done or _game == null:
        return
    var direction := Input.get_vector(
        "move_left", "move_right", "move_up", "move_down")
    _pos += direction * 320.0 * delta
    _pos.x = clampf(_pos.x, 20.0, 1132.0)
    _pos.y = clampf(_pos.y, 20.0, 628.0)
    queue_redraw()
    if _pos.distance_to(_target) < 30.0:
        _done = true
        _game.signal_win(1, 1, "Reached the podium and won the debate.")
        return
    _time_left -= delta
    if _time_left <= 0.0:
        _done = true
        _game.signal_lose("The argument timer ran out.")


func _draw() -> void:
    draw_circle(_target, 14, Color(0.25, 0.8, 0.45))
    draw_circle(_pos, 16, Color(0.42, 0.3, 0.95))


func bridge_state() -> Dictionary:
    return {
        "player_pos": [_pos.x, _pos.y],
        "objective_pos": [_target.x, _target.y],
        "hint": "Reach the podium before the argument timer runs out.",
        "time_left": snappedf(_time_left, 0.1),
    }


func bridge_actions() -> Array:
    return ["start", "wait", "move_left", "move_right", "move_up",
            "move_down"]
'''


class RegistryTests(unittest.TestCase):
    def test_every_worker_declares_its_contract(self):
        for name, spec in workers.WORKERS.items():
            self.assertTrue(spec.title, name)
            self.assertTrue(callable(spec.run), name)
            self.assertTrue(spec.outputs, name)
            self.assertIsInstance(spec.inputs, tuple, name)
            for dep in spec.depends_on:
                self.assertIn(dep, workers.WORKERS, name)

    def test_every_pipeline_stage_maps_to_a_worker(self):
        mapping = {
            "scope": "scope", "skeleton": "skeleton",
            "assets": "assets", "headless_test": "testing",
            "playtest": "testing", "finished": "finish",
        }
        for stage, worker in mapping.items():
            self.assertIn(stage, state.STAGES)
            self.assertIn(worker, workers.WORKERS)
        for worker in ("world", "mechanics", "assembly"):
            self.assertIn(worker, workers.WORKERS)

    def test_the_factory_steps_are_all_workers(self):
        for name in ("scope", "skeleton", "world", "mechanics",
                     "assets", "assembly", "testing",
                     "model-playtest", "balance", "repair",
                     "feedback", "patch", "finish"):
            self.assertIn(name, workers.WORKERS)


class CompositionTests(unittest.TestCase):
    def test_presets_round_trip(self):
        for name in generate.ARCHETYPES:
            comp = generate.composition_for_preset(name)
            self.assertEqual(generate.preset_for_composition(comp), name)

    def test_derivation_picks_the_platformer_preset(self):
        values = {
            "Genre / archetype": "2D platformer",
            "Core verb (what the player does most)": "jump and collect",
            "Win condition": "reach the door",
            "Lose / fail condition": "3 lives",
        }
        derived = generate.derive_composition(values)
        self.assertEqual(derived["preset"], "platformer")
        self.assertEqual(derived["composition"]["movement"], "platform")
        self.assertFalse(derived["bespoke"])

    def test_explicit_preset_in_scope_wins(self):
        values = {"Genre / archetype": "preset: crate_pusher, a quiet game"}
        derived = generate.derive_composition(values)
        self.assertEqual(derived["preset"], "crate_pusher")

    def test_bespoke_detection_is_conservative(self):
        bespoke = generate.derive_composition({
            "Genre / archetype": "narrative duel",
            "Core verb (what the player does most)":
                "negotiate dialogue with a rival",
            "Win condition": "win the debate",
            "Lose / fail condition": "lose the argument",
        })
        self.assertTrue(bespoke["bespoke"])
        self.assertIsNone(bespoke["composition"])
        # A vague scope keeps the historical default, never bespoke.
        vague = generate.derive_composition({
            "Genre / archetype": "Filled in for the test",
            "Core verb (what the player does most)": "Filled in for the test",
            "Win condition": "Filled in for the test",
            "Lose / fail condition": "Filled in for the test",
        })
        self.assertFalse(vague["bespoke"])
        self.assertEqual(vague["preset"], "arena_collector")
        # A bespoke signal WITH a primitive signal still composes.
        mixed = generate.derive_composition({
            "Genre / archetype": "detective platformer",
            "Core verb (what the player does most)":
                "jump between rooftops and interrogate suspects",
            "Win condition": "reach the courthouse",
            "Lose / fail condition": "fall",
        })
        self.assertFalse(mixed["bespoke"])


class WorldGateTests(unittest.TestCase):
    def test_valid_platformer_world_passes(self):
        level = generate._platformer_level(__import__("random").Random(7), 0)
        data = {"preset": "platformer", "units": [
            {"index": 0, "kind": "level", **level}]}
        self.assertEqual(world.validate_world(data), [])

    def test_sabotaged_level_fails_with_world_owner(self):
        data = {"preset": "platformer", "units": [
            {"index": 0, "kind": "level", "platforms": [],
             "collectibles": [], "spikes": [], "spawn": [0, 0],
             "exit": [0, 0], "width": 100}]}
        issues = world.validate_world(data)
        self.assertTrue(issues)
        self.assertTrue(all(i["owner"] == "world" for i in issues))

    def test_unsolvable_crate_fails(self):
        # A crate already cornered off its pad can never be solved.
        grid = ["#####", "#$  #", "# .@#", "#####"]
        data = {"preset": "crate_pusher", "units": [
            {"index": 0, "kind": "puzzle", "grid": grid,
             "solution": "", "budget": 40}]}
        issues = world.validate_world(data)
        self.assertTrue(issues)
        self.assertEqual(issues[0]["owner"], "world")

    def test_arena_bounds_checked(self):
        data = {"preset": "arena_collector", "units": [
            {"index": 0, "kind": "round", "targets_per_round": 99,
             "round_seconds": 60, "arena": [1152, 648]}]}
        self.assertTrue(world.validate_world(data))


class MechanicsGateTests(unittest.TestCase):
    def test_unhandled_entity_kind_is_a_mechanics_issue(self):
        data = {"entity_kinds": ["player", "teleporter"]}
        issues = mechanics.check_coverage(data, {"preset": "platformer"})
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0]["owner"], "mechanics")
        self.assertEqual(issues[0]["kind"], "unhandled_entity")

    def test_preset_worlds_are_fully_handled(self):
        for preset in generate.ARCHETYPES:
            data = {"entity_kinds": world.ENTITY_KINDS[preset]}
            self.assertEqual(
                mechanics.check_coverage(data, {"preset": preset}), [])


class HashAndRecordTests(unittest.TestCase):
    def test_scope_worker_record_and_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = scoped_project(tmp, approve=False)
            outcome = workers.run_worker("scope", project)
            self.assertFalse(outcome["ok"])  # unapproved scope
            record = workers.load_record(project, "scope")
            self.assertIsNotNone(record)
            self.assertEqual(record["worker"], "scope")
            self.assertFalse(record["ok"])
            self.assertEqual(record["issues"][0]["owner"], "scope")

    def test_inputs_hash_tracks_declared_inputs_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = scoped_project(tmp)
            assets_spec = workers.WORKERS["assets"]
            scope_spec = workers.WORKERS["scope"]
            assets_before = workers.inputs_hash(project, assets_spec)
            scope_before = workers.inputs_hash(project, scope_spec)
            palette_line = [l for l in filled_scope_text().splitlines()
                            if l.startswith(PALETTE)][0]
            scope.set_field_value(
                project / "SCOPE.md", "Palette",
                "neon #FF0000 and #00FF00")
            self.assertNotEqual(assets_before,
                                workers.inputs_hash(project, assets_spec))
            # The scope worker's slice is completeness, not wording.
            self.assertEqual(scope_before,
                             workers.inputs_hash(project, scope_spec))
            self.assertNotEqual(palette_line, "")


class PlanConstructionTests(unittest.TestCase):
    def test_fresh_project_plan_is_the_full_chain(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = scoped_project(tmp)
            data = orchestrator.plan(project)
            self.assertEqual([s["worker"] for s in data["steps"]],
                             orchestrator.CHAIN)
            self.assertTrue(all(s["status"] == "pending"
                                for s in data["steps"]))
            self.assertTrue((project / ".weaverbird" / "plan.json").exists())

    def test_mid_pipeline_plan_reflects_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = scoped_project(tmp)
            # A valid scope record with the current hash reads done.
            workers.run_worker("scope", project)
            data = orchestrator.plan(project)
            statuses = {s["worker"]: s["status"] for s in data["steps"]}
            self.assertEqual(statuses["scope"], "done")
            self.assertEqual(statuses["skeleton"], "pending")


class FakeChainTests(unittest.TestCase):
    """Skip / un-skip / failure-routing behaviour, with the real
    workers replaced by fast fakes that write their outputs."""

    def setUp(self):
        self._saved = {name: spec.run
                       for name, spec in workers.WORKERS.items()}
        self.calls = []

        def make_fake(name, spec):
            def fake(project, ctx):
                self.calls.append(name)
                for rel in spec.outputs:
                    dest = Path(project) / rel
                    if rel.endswith("/"):
                        dest.mkdir(parents=True, exist_ok=True)
                    elif rel == "SCOPE.md" and dest.exists():
                        continue  # the scope worker does not rewrite it
                    else:
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        # Fresh content per run, like the real
                        # reports' timestamps.
                        dest.write_text(f"{name} output "
                                        f"{len(self.calls)}\n",
                                        encoding="utf-8")
                return workers.result(True, f"{name} ran",
                                      artifacts=list(spec.outputs))
            return fake

        for name, spec in workers.WORKERS.items():
            spec.run = make_fake(name, spec)

    def tearDown(self):
        for name, run in self._saved.items():
            workers.WORKERS[name].run = run

    def test_second_run_skips_everything(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            (project / "SCOPE.md").write_text("scope\n", encoding="utf-8")
            first = orchestrator.run(project)
            self.assertEqual(first["outcome"], "done")
            self.calls.clear()
            second = orchestrator.run(project)
            self.assertEqual(self.calls, [])
            self.assertTrue(all(s["status"] == "skipped"
                                for s in second["steps"]))

    def test_touching_an_input_unskips_exactly_the_dependents(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            (project / "SCOPE.md").write_text("scope\n", encoding="utf-8")
            orchestrator.run(project)
            self.calls.clear()
            world_file = project / "game" / "world" / "world.json"
            world_file.write_text('{"changed": true}\n',
                                  encoding="utf-8")
            plan = orchestrator.run(project)
            self.assertEqual(
                sorted(set(self.calls)),
                ["assembly", "assets", "finish", "mechanics", "testing"])
            statuses = {s["worker"]: s["status"] for s in plan["steps"]}
            self.assertEqual(statuses["world"], "skipped")
            self.assertEqual(statuses["scope"], "skipped")

    def test_failure_routing_stops_the_plan_with_the_owner(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            (project / "SCOPE.md").write_text("scope\n", encoding="utf-8")

            def failing(project, ctx):
                return workers.result(
                    False, "the world gate failed",
                    issues=[workers.issue("world_gate", "bad unit",
                                          "world")])

            workers.WORKERS["world"].run = failing
            plan = orchestrator.run(project)
            self.assertEqual(plan["outcome"], "failed")
            self.assertEqual(plan["escalation"]["worker"], "world")
            statuses = {s["worker"]: s["status"] for s in plan["steps"]}
            self.assertEqual(statuses["world"], "failed")
            self.assertEqual(statuses["mechanics"], "pending")
            self.assertEqual(statuses["assembly"], "pending")
            # The retry happened exactly once (attempt 1 record).
            record = workers.load_record(project, "world")
            self.assertEqual(record["attempt"], 1)

    def test_one_retry_can_recover(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            (project / "SCOPE.md").write_text("scope\n", encoding="utf-8")
            attempts = {"count": 0}

            def flaky(project, ctx):
                attempts["count"] += 1
                if ctx.attempt == 0:
                    return workers.result(
                        False, "first try failed",
                        issues=[workers.issue("world_gate", "bad seed",
                                              "world")])
                return workers.result(True, "recovered with a new seed")

            workers.WORKERS["world"].run = flaky
            plan = orchestrator.run(project)
            self.assertEqual(plan["outcome"], "done")
            self.assertEqual(attempts["count"], 2)


@unittest.skipUnless(VALIDATE, "Godot validation not available")
class FullRunTests(unittest.TestCase):
    def test_orchestrated_platformer_to_assembly(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = scoped_project(tmp, PLATFORMER)
            plan = orchestrator.run(project, target="assembly")
            self.assertEqual(plan["outcome"], "done", plan["escalation"])
            self.assertTrue(
                (project / "game" / "world" / "world.json").exists())
            generation = json.loads(
                (project / ".weaverbird" / "generation_report.json")
                .read_text(encoding="utf-8"))
            self.assertEqual(generation["preset"], "platformer")
            self.assertEqual(generation["composition"]["movement"],
                             "platform")
            self.assertIn("world_manifest", generation)
            self.assertEqual(generation["world_manifest"]["units"], 2)
            config = json.loads(
                (project / "game" / "game_config.json").read_text(
                    encoding="utf-8"))
            self.assertEqual(config["composition"]["structure"], "levels")

    def test_orchestrated_crate_run_skips_and_cascades(self):
        # The crate game is the one the existing suite exercises
        # through the full test stage (balance/patch baselines);
        # the platformer full-suite path is covered to assembly
        # above — its lose bot is seed-sensitive in ways the
        # factory's own suite documents (seed sweep tests the
        # win bot only).
        with tempfile.TemporaryDirectory() as tmp:
            project = scoped_project(tmp, CRATE)
            plan = orchestrator.run(project)
            self.assertEqual(plan["outcome"], "done", plan["escalation"])
            generation = json.loads(
                (project / ".weaverbird" / "generation_report.json")
                .read_text(encoding="utf-8"))
            self.assertEqual(generation["preset"], "crate_pusher")
            self.assertEqual(generation["composition"]["movement"],
                             "grid")
            final = state.load_state(project)
            self.assertEqual(final["stage"], "finished")
            self.assertTrue(all(final["definition_of_done"].values()))

            # Second run: everything skipped, nothing re-ran.
            plan2 = orchestrator.run(project)
            self.assertTrue(all(s["status"] == "skipped"
                                for s in plan2["steps"]), plan2["steps"])

            # Palette edit: only assets + assembly + testing (+ the
            # finish check that reads their fresh reports) re-run.
            scope.set_field_value(project / "SCOPE.md", "Palette",
                                  "neon #FF0055 with #00FFCC accents")
            plan3 = orchestrator.run(project)
            statuses = {s["worker"]: s["status"] for s in plan3["steps"]}
            self.assertEqual(statuses["assets"], "done")
            self.assertEqual(statuses["assembly"], "done")
            self.assertEqual(statuses["testing"], "done")
            self.assertEqual(statuses["world"], "skipped")
            self.assertEqual(statuses["mechanics"], "skipped")
            self.assertEqual(statuses["skeleton"], "skipped")
            self.assertEqual(statuses["scope"], "skipped")

    def test_world_input_sabotage_stops_the_plan_at_world(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = scoped_project(tmp, CRATE)
            skeleton.build_skeleton(project, validate=False)
            # Corrupt the world's source data: a crate already
            # cornered in the build context the world worker
            # generates from, mirrored into world.json.
            build = generate.prepare_build(project, use_brain=False)
            build["config"]["puzzles"][0]["grid"] = [
                "#####", "#$  #", "# .@#", "#####"]
            build["config"]["puzzles"][0]["solution"] = ""
            generate.save_build_context(project, build)
            plan = orchestrator.run(project, target="assembly")
            self.assertEqual(plan["outcome"], "failed")
            self.assertEqual(plan["escalation"]["worker"], "world")
            statuses = {s["worker"]: s["status"] for s in plan["steps"]}
            self.assertEqual(statuses["assembly"], "pending")
            self.assertIsNone(
                workers.load_record(project, "assembly"))


@unittest.skipUnless(VALIDATE, "Godot validation not available")
class BespokeRunTests(unittest.TestCase):
    def test_bespoke_plan_blocks_without_a_brain(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = scoped_project(tmp, BESPOKE)
            reason = orchestrator.blocked_reason(project, "mechanics")
            self.assertIsNotNone(reason)
            self.assertIn("bespoke", reason)

    def test_bespoke_end_to_end_with_a_mocked_brain(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = scoped_project(tmp, BESPOKE)
            plan = orchestrator.run(
                project,
                params={"mechanics": {
                    "brain_fn": lambda prompt: BESPOKE_FIXTURE}})
            self.assertEqual(plan["outcome"], "done", plan["escalation"])
            module = project / "game" / "scripts" / "bespoke_mechanics.gd"
            self.assertTrue(module.exists())
            self.assertIn("func setup(", module.read_text(encoding="utf-8"))
            test_report = json.loads(
                (project / ".weaverbird" / "test_report.json")
                .read_text(encoding="utf-8"))
            self.assertTrue(test_report["ok"], test_report["runs"])
            final = state.load_state(project)
            self.assertEqual(final["stage"], "finished")


class CliTests(unittest.TestCase):
    def test_workers_list(self):
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = cli.main(["workers", "list"])
        self.assertEqual(code, 0)
        out = buffer.getvalue()
        for name in workers.WORKERS:
            self.assertIn(name, out)

    def test_orchestrate_plan_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = scoped_project(tmp)
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                code = cli.main(["orchestrate", str(project),
                                 "--plan-only"])
            self.assertEqual(code, 0)
            self.assertIn("world", buffer.getvalue())


if __name__ == "__main__":
    unittest.main()
