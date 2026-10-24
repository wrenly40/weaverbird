"""3D support (0.11.0: plan-based).

The scope's dimension is part of the game plan: a scope asking
for 3D derives the top_down_3d movement, executed by the same
generic runtime as every other plan. The 3D world is data
(world.json on the XZ plane) with its own gate, the assets
worker emits a material palette spec instead of sprites, and a
generated 3D game must pass the full testing stage — win AND
lose proven — under Godot.
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from helpers import filled_scope_text  # noqa: E402

from weaverbird import assets_gen, generate, godot, orchestrator  # noqa: E402
from weaverbird import plan as plan_module  # noqa: E402
from weaverbird import scope, state, world  # noqa: E402

VALIDATE = godot.find_godot()[0] is not None

DIM = "- REQUIRED Dimension:"
GENRE = "- REQUIRED Genre / archetype:"
VERB = "- REQUIRED Core verb (what the player does most):"
WIN = "- REQUIRED Win condition:"
LOSE = "- REQUIRED Lose / fail condition:"
COUNTS = "- REQUIRED Content counts (levels / waves / rooms / items, as applicable):"
TITLE = "- REQUIRED Title (working):"

ARENA_3D = {
    TITLE: f"{TITLE} Orb Fields",
    DIM: f"{DIM} 3D",
    GENRE: f"{GENRE} 3D arena collector",
    VERB: f"{VERB} collect orbs",
    WIN: f"{WIN} collect every orb in each round",
    LOSE: f"{LOSE} the timer runs out or a hazard touches you",
    COUNTS: f"{COUNTS} 3 rounds",
}


def scoped_project(tmp, replacements=None):
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
    scope.approve_scope(project / "SCOPE.md", by="Tester")
    return project


class DimensionDerivationTests(unittest.TestCase):
    def _values(self, project):
        values = {}
        parsed = scope.load_scope(project / "SCOPE.md")
        return {f.name: f.value for f in parsed.fields}

    def test_dimension_field_lifts_arena_to_3d(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = scoped_project(tmp, ARENA_3D)
            derived = plan_module.derive_plan(self._values(project))
            self.assertEqual(derived["dimension"], "3d")
            self.assertEqual(derived["movement"], "top_down_3d")

    def test_3d_in_genre_text_also_lifts(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = scoped_project(tmp, {
                **ARENA_3D, DIM: f"{DIM} 2D"})
            # Dimension says 2D, genre says 3D: the field wins.
            derived = plan_module.derive_plan(self._values(project))
            self.assertEqual(derived["movement"], "top_down_2d")
            project = scoped_project(tmp, {
                **ARENA_3D, DIM: f"{DIM} (either)"})
            derived = plan_module.derive_plan(self._values(project))
            self.assertEqual(derived["movement"], "top_down_3d")

    def test_default_is_2d(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = scoped_project(tmp, {
                TITLE: f"{TITLE} Orb Fields",
                GENRE: f"{GENRE} arena collector",
                VERB: f"{VERB} collect orbs",
                WIN: f"{WIN} collect every orb in each round",
                LOSE: f"{LOSE} the timer runs out",
                COUNTS: f"{COUNTS} 3 rounds",
            })
            derived = plan_module.derive_plan(self._values(project))
            self.assertEqual(derived["movement"], "top_down_2d")
            self.assertEqual(derived["dimension"], "2d")

    def test_3d_platformer_request_is_honest(self):
        # 3D + side-view signals cannot be expressed in the
        # vocabulary: the plan says so instead of silently
        # building a different (2D) game.
        with tempfile.TemporaryDirectory() as tmp:
            project = scoped_project(tmp, {
                **ARENA_3D,
                GENRE: f"{GENRE} 3D platformer",
                VERB: f"{VERB} jump and collect stars",
                WIN: f"{WIN} collect every star and reach the door",
                LOSE: f"{LOSE} lose all 3 lives",
            })
            derived = plan_module.derive_plan(self._values(project))
            self.assertIsNotNone(derived["bespoke_required"])
            self.assertIn("3D platforming",
                          derived["bespoke_required"]["mechanic"])


class World3DTests(unittest.TestCase):
    def _world(self):
        values = {
            "Title (working)": "Orb Fields",
            "Palette": "accent #FF7A1A",
            "Dimension": "3D",
            "Core verb (what the player does most)": "collect orbs",
            "Win condition": "collect every orb in each round",
            "Lose / fail condition": "the timer runs out or a hazard "
                                     "touches you",
            "Content counts (levels / waves / rooms / items, as "
            "applicable)": "3 rounds",
        }
        derived = plan_module.derive_plan(values)
        self.assertEqual(derived["movement"], "top_down_3d")
        return world.world_from_plan(derived)

    def test_world_shape_and_gate(self):
        data = self._world()
        self.assertEqual(data["entity_kinds"],
                         ["player", "collectible", "hazard", "obstacle"])
        unit = data["units"][0]
        self.assertEqual(unit["kind"], "round3d")
        self.assertEqual(unit["ground"], [40, 24])
        self.assertTrue(unit["collectibles"])
        self.assertTrue(unit["hazards"])
        self.assertTrue(unit["obstacles"])
        self.assertEqual(world.validate_world(data), [])

    def test_units_record_their_layout(self):
        # D5: the 3D path writes the same provenance the 2D
        # path does — placer name, per-round seed, gate outcome.
        data = self._world()
        seed = data["plan"]["numbers"]["world_seed"]
        for index, unit in enumerate(data["units"]):
            layout = unit["layout"]
            self.assertEqual(layout["pattern"], "scatter3d")
            self.assertEqual(layout["seed"], seed + index * 101)
            self.assertEqual(layout["gate"], "passed")

    def test_gate_catches_collectible_inside_obstacle(self):
        data = self._world()
        obstacle = data["units"][0]["obstacles"][0]
        data["units"][0]["collectibles"][0] = obstacle["pos"]
        issues = world.validate_world(data)
        self.assertTrue(any("inside an obstacle" in i["detail"]
                            for i in issues), issues)

    def test_gate_catches_out_of_bounds(self):
        data = self._world()
        data["units"][0]["collectibles"][0] = [99, 0]
        issues = world.validate_world(data)
        self.assertTrue(any("outside the ground" in i["detail"]
                            for i in issues), issues)


class Assets3DTests(unittest.TestCase):
    def test_requests_are_materials_not_sprites(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = scoped_project(tmp, ARENA_3D)
            values = {}
            parsed = scope.load_scope(project / "SCOPE.md")
            values = {f.name: f.value for f in parsed.fields}
            requests = assets_gen.asset_requests(project, values)
            self.assertEqual(requests["dimension"], "3d")
            self.assertEqual(requests["sprites"], [])
            names = [m["name"] for m in requests["materials"]]
            self.assertEqual(names, ["player", "collectible", "hazard",
                                     "ground", "obstacle"])
            for material in requests["materials"]:
                self.assertRegex(material["hex"], r"^#[0-9A-Fa-f]{6}$")


@unittest.skipUnless(VALIDATE, "Godot binary not available")
class Full3DBuildTests(unittest.TestCase):
    def test_orchestrated_3d_game_passes_testing(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = scoped_project(tmp, ARENA_3D)
            plan = orchestrator.run(project, target="testing")
            self.assertEqual(plan["outcome"], "done", plan["escalation"])
            generation = json.loads(
                (project / ".weaverbird" / "generation_report.json")
                .read_text(encoding="utf-8"))
            self.assertEqual(generation["plan"]["movement"], "top_down_3d")
            self.assertEqual(generation["plan"]["dimension"], "3d")
            config = json.loads(
                (project / "game" / "game_config.json").read_text(
                    encoding="utf-8"))
            self.assertEqual(config["dimension"], "3d")
            world_data = json.loads(
                (project / "game" / "world" / "world.json").read_text(
                    encoding="utf-8"))
            self.assertEqual(world_data["units"][0]["kind"], "round3d")
            # The game scene is the Node3D one the mechanics
            # worker wrote, not the skeleton's Node2D scene.
            scene = (project / "game" / "scenes" / "game.tscn").read_text(
                encoding="utf-8")
            self.assertIn("Node3D", scene)
            # The assets report carries materials, not sprites.
            assets = json.loads(
                (project / ".weaverbird" / "assets_report.json")
                .read_text(encoding="utf-8"))
            self.assertEqual(assets["dimension"], "3d")
            self.assertEqual(assets["requests"]["sprites"], [])
            self.assertEqual(len(assets["requests"]["materials"]), 5)
            # Testing passed with win AND lose proven.
            report = json.loads(
                (project / ".weaverbird" / "test_report.json")
                .read_text(encoding="utf-8"))
            self.assertTrue(report["ok"], report.get("warnings"))
            self.assertTrue(report["win_proven"])
            self.assertTrue(report["lose_proven"])
            names = [r["name"] for r in report["runs"]]
            self.assertEqual(names, ["import", "main scene", "game scene",
                                     "competent win", "competent lose",
                                     "breaker", "first-time"])
            for run in report["runs"]:
                self.assertEqual(run["errors"], [], run)
            units = [u for u in report.get("units", [])
                     if u.get("source") == "competent"]
            self.assertEqual(len(units), 3, report.get("units"))
            self.assertTrue(all(u["completed"] for u in units))


class PaletteSkyTests(unittest.TestCase):
    """Dogfood finding D9: the 3D game's sky and materials come
    from the style-lock palette. The palette hexes live at the
    top of game_config.json; the 3D templates used to read a
    nested "palette" object that never exists, so every 3D
    game silently rendered with fallback colours under a
    hard-coded light-blue sky."""

    def _template(self, name):
        path = (Path(generate.__file__).parent / "game_templates"
                / "runtime" / name)
        return path.read_text(encoding="utf-8")

    def test_no_hardcoded_default_sky(self):
        self.assertNotIn("#BFE3F2", self._template("game3d.gd"))

    def test_background_derives_from_the_palette_accent(self):
        text = self._template("game3d.gd")
        env_body = text.split("func _build_environment()")[1]
        self.assertIn("accent", env_body)
        self.assertIn("darkened", env_body)
        self.assertIn("background_color", env_body)

    def test_palette_is_read_from_the_config_top_level(self):
        text = self._template("game3d.gd")
        # The old line, as a statement of its own, is gone.
        self.assertNotIn('\n\tpalette = config.get("palette", {})',
                         text)
        self.assertIn('"accent_hex"', text)
        player = self._template("player3d.gd")
        self.assertIn('parsed.get("accent_hex"', player)


if __name__ == "__main__":
    unittest.main()
