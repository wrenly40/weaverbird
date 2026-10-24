"""Genre-agnostic generation (0.11.0), end to end.

Three scopes in three different genres build three structurally
different games through the one generic runtime — different
movement and objective in their plans and worlds — and each is
winnable AND losable by the scripted players. No preset or
archetype identifier appears anywhere in the new builds'
config, world, reports, or served project page.

An out-of-vocabulary scope (tower defence) stops at the plan:
the orchestration halts with a plain explanation and no game is
produced; with a mocked brain the bespoke path is taken instead.
A balance patch cycle on a new-runtime game ships.

Slow: real Godot builds. Skipped when Godot is unavailable.
"""

import json
import sys
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import filled_scope_text  # noqa: E402

from weaverbird import app as app_module  # noqa: E402
from weaverbird import feedback, generate, godot, orchestrator  # noqa: E402
from weaverbird import patch, scope, skeleton, state, testing, vcs  # noqa: E402

VALIDATE = godot.find_godot()[0] is not None

TITLE = "- REQUIRED Title (working):"
GENRE = "- REQUIRED Genre / archetype:"
VERB = "- REQUIRED Core verb (what the player does most):"
WIN = "- REQUIRED Win condition:"
LOSE = "- REQUIRED Lose / fail condition:"
COUNTS = "- REQUIRED Content counts (levels / waves / rooms / items, as applicable):"

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

# Identifiers from the retired preset world. They must not
# appear in any artifact a NEW build produces. (The scope file
# itself is the maker's own words and is not an artifact of the
# generator; the interview's field name "Genre / archetype" is
# part of the scope template, so the page check below avoids
# the scope form's own labels by checking rendered report text
# only for the preset names and the "Archetype:"/"Preset:"
# card wordings.)
BANNED = ("arena_collector", "crate_pusher", "arena_3d",
          '"archetype"', '"preset"', '"composition"')


def scoped_project(tmp, replacements):
    text = filled_scope_text()
    for prefix, line in replacements.items():
        lines = text.splitlines()
        text = "\n".join(
            line if existing.startswith(prefix) else existing
            for existing in lines) + "\n"
    project = Path(tmp) / "game-project"
    project.mkdir(parents=True, exist_ok=True)
    (project / "SCOPE.md").write_text(text, encoding="utf-8")
    state.init_state(project)
    scope.approve_scope(project / "SCOPE.md", by="Tester")
    return project


def assert_no_preset_identifiers(case, project):
    for rel in ("game/game_config.json", "game/world/world.json",
                ".weaverbird/generation_report.json",
                ".weaverbird/test_report.json",
                ".weaverbird/assets_report.json"):
        path = project / rel
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        for banned in BANNED:
            case.assertNotIn(banned, text, f"{banned} in {rel}")
        case.assertNotIn("platformer", text, f"preset name in {rel}")


@unittest.skipUnless(VALIDATE, "Godot validation not available")
class GenreBuildTests(unittest.TestCase):
    """One build per genre, shared across the assertions."""

    GENRES = {}

    @classmethod
    def setUpClass(cls):
        cls._tmps = []
        for name, fixture in (("collector", COLLECTOR),
                              ("flag", FLAG), ("crates", CRATES)):
            tmp = tempfile.TemporaryDirectory()
            cls._tmps.append(tmp)
            project = scoped_project(tmp.name, fixture)
            result = orchestrator.run(project, target="assembly")
            assert result["outcome"] == "done", result.get("escalation")
            report = testing.run_tests(project, allow_repair=False)
            cls.GENRES[name] = (project, report)

    @classmethod
    def tearDownClass(cls):
        for tmp in cls._tmps:
            tmp.cleanup()

    def _plan(self, name):
        project, _report = self.GENRES[name]
        config = json.loads(
            (project / "game" / "game_config.json").read_text(
                encoding="utf-8"))
        return config["plan"]

    def test_three_structurally_different_plans(self):
        plans = {name: self._plan(name) for name in self.GENRES}
        self.assertEqual(plans["collector"]["movement"], "top_down_2d")
        self.assertEqual(plans["collector"]["objective"], "collect")
        self.assertEqual(plans["flag"]["movement"], "side_view")
        self.assertEqual(plans["flag"]["objective"], "reach")
        self.assertEqual(plans["crates"]["movement"], "grid_step")
        self.assertEqual(plans["crates"]["objective"], "deliver")
        movements = {p["movement"] for p in plans.values()}
        objectives = {p["objective"] for p in plans.values()}
        self.assertEqual(len(movements), 3)
        self.assertEqual(len(objectives), 3)

    def test_worlds_differ_in_shape(self):
        worlds = {}
        for name, (project, _r) in self.GENRES.items():
            worlds[name] = json.loads(
                (project / "game" / "world" / "world.json").read_text(
                    encoding="utf-8"))
        self.assertEqual(worlds["collector"]["units"][0]["kind"], "round")
        self.assertEqual(worlds["flag"]["units"][0]["kind"], "level")
        self.assertEqual(worlds["crates"]["units"][0]["kind"], "puzzle")
        self.assertIn("collectibles",
                      worlds["collector"]["units"][0])
        self.assertIn("platforms", worlds["flag"]["units"][0])
        self.assertIn("grid", worlds["crates"]["units"][0])

    def test_each_game_is_winnable_and_losable(self):
        for name, (_project, report) in self.GENRES.items():
            self.assertTrue(report["win_proven"],
                            f"{name}: {report.get('warnings')}")
            self.assertTrue(report["lose_proven"],
                            f"{name}: {report.get('warnings')}")
            self.assertTrue(report["scenarios_pass"], name)
            self.assertTrue(report["no_crashes"],
                            f"{name}: {report.get('warnings')}")

    def test_no_preset_identifiers_anywhere(self):
        for name, (project, _r) in self.GENRES.items():
            assert_no_preset_identifiers(self, project)

    def test_hud_speaks_the_makers_nouns(self):
        project, _r = self.GENRES["collector"]
        config = json.loads(
            (project / "game" / "game_config.json").read_text(
                encoding="utf-8"))
        self.assertEqual(config["collectible_label"], "Apples")
        self.assertIn("Apples", config["objective_hint"])

    def test_served_project_page_speaks_plan(self):
        project, _r = self.GENRES["flag"]
        workspace = project.parent
        server = app_module.create_server(workspace, port=0)
        thread = threading.Thread(target=server.serve_forever,
                                  daemon=True)
        thread.start()
        try:
            url = server.url.rstrip("/") + "/project/game-project"
            with urllib.request.urlopen(url, timeout=10) as response:
                page = response.read().decode("utf-8")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
        self.assertIn("side_view", page)
        self.assertNotIn("Archetype:", page)
        self.assertNotIn("Preset:", page)
        for banned in ("arena_collector", "crate_pusher", "arena_3d"):
            self.assertNotIn(banned, page)


@unittest.skipUnless(VALIDATE, "Godot validation not available")
class TowerDefenceStopTests(unittest.TestCase):
    def test_out_of_vocabulary_scope_stops_at_the_plan(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = scoped_project(tmp, TOWER)
            summary = generate.build_summary(project)
            self.assertIsNotNone(summary["bespoke_required"])
            self.assertIn("tower defence",
                          summary["bespoke_required"]["mechanic"])
            self.assertIsNotNone(
                orchestrator.blocked_reason(project, "world"))
            result = orchestrator.run(project)
            self.assertNotEqual(result["outcome"], "done")
            escalation = json.dumps(result.get("escalation") or {})
            self.assertIn("tower defence", escalation)
            # No game was produced: no generation report, no
            # world, no known-good build tag.
            self.assertFalse(
                (project / ".weaverbird" / "generation_report.json")
                .exists())
            self.assertFalse(
                (project / "game" / "world" / "world.json").exists())
            self.assertNotIn("known-good/build", vcs.list_tags(project))

    def test_with_a_brain_the_bespoke_path_is_taken(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = scoped_project(tmp, TOWER)
            self.assertIsNone(orchestrator.blocked_reason(
                project, "world",
                params={"mechanics": {"brain_fn": lambda prompt: "x"}}))
            self.assertIsNone(orchestrator.blocked_reason(
                project, "mechanics",
                params={"mechanics": {"brain_fn": lambda prompt: "x"}}))


@unittest.skipUnless(VALIDATE, "Godot validation not available")
class PlanPatchCycleTests(unittest.TestCase):
    """A balance patch on a new-runtime game: tune → tests →
    save-compat → version bump → known-good tag."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.project = scoped_project(cls._tmp.name, COLLECTOR)
        skeleton.build_skeleton(cls.project)
        generate.build_game(cls.project, use_brain=False)
        cls.baseline = testing.run_tests(cls.project,
                                         allow_repair=False)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_balance_patch_ships_on_a_plan_game(self):
        self.assertTrue(self.baseline["ok"], self.baseline["warnings"])
        item, _ = feedback.add_item(
            self.project, source="manual",
            text="It is too hard, I keep failing")
        feedback.triage(self.project, use_brain=False)
        feedback.set_status(self.project, item["id"], "approved")
        item = feedback.get_item(self.project, item["id"])
        self.assertEqual(item["category"], "balance")
        before = patch.load_config(self.project)
        self.assertIn("plan", before)
        version_before = patch.game_version(self.project)
        result = patch.start_patch(self.project, item["id"])
        self.assertEqual(result["outcome"], "patched", result)
        self.assertEqual(result["version"],
                         patch.bump_version(version_before))
        after = patch.load_config(self.project)
        # The generic tunables moved: hazards slower, rounds longer.
        self.assertLess(after["hazard_speed"], before["hazard_speed"])
        self.assertGreater(after["round_seconds"],
                           before["round_seconds"])
        self.assertIn(f"known-good/patch-{result['version']}",
                      vcs.list_tags(self.project))
        stored = feedback.get_item(self.project, item["id"])
        self.assertEqual(stored["status"], "patched")


if __name__ == "__main__":
    unittest.main()
