import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import filled_scope_text, template_scope_text  # noqa: E402

from weaverbird import cli, ledger, provenance, scope, skeleton, state, vcs  # noqa: E402
from weaverbird import godot as godot_module  # noqa: E402

GODOT_PRESENT = godot_module.find_godot()[0] is not None
VALIDATE = GODOT_PRESENT and os.environ.get("WEAVERBIRD_SKIP_GODOT_TESTS") != "1"

EXPECTED_FILES = [
    "game/project.godot",
    "game/icon.svg",
    "game/README.md",
    "game/.gitignore",
    "game/scenes/main_menu.tscn",
    "game/scenes/game.tscn",
    "game/scenes/settings.tscn",
    "game/scenes/game_over.tscn",
    "game/scenes/win.tscn",
    "game/scripts/main_menu.gd",
    "game/scripts/game.gd",
    "game/scripts/player.gd",
    "game/scripts/marker.gd",
    "game/scripts/settings.gd",
    "game/scripts/settings_store.gd",
    "game/scripts/run_info.gd",
    "game/scripts/ui_theme.gd",
    "game/scripts/input_setup.gd",
    "game/scripts/pause_input.gd",
    "game/scripts/win.gd",
    "game/scripts/game_over.gd",
    "game/smoke/smoke.gd",
]


def _project(tmp, scope_text, approve=False):
    project = Path(tmp)
    (project / "SCOPE.md").write_text(scope_text, encoding="utf-8")
    state.init_state(project)
    if approve:
        scope.approve_scope(project / "SCOPE.md", by="Tester")
    return project


def _run_cli(*argv):
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        code = cli.main(list(argv))
    return code, buffer.getvalue()


class FlowSmokeTimeoutTests(unittest.TestCase):
    """D2-4: the flow smoke plays the whole game to a win, so its
    cap scales with the plan's own total round time — a survive
    game (3 x 75s) can finish assembly, a short game keeps the
    snappy base cap, and a hard ceiling bounds the allowance."""

    def _game_dir(self, config=None):
        tmp = tempfile.mkdtemp()
        game_dir = Path(tmp) / "game"
        game_dir.mkdir()
        if config is not None:
            import json
            (game_dir / "game_config.json").write_text(
                json.dumps(config), encoding="utf-8")
        return game_dir

    def test_no_config_keeps_the_base_cap(self):
        self.assertEqual(
            skeleton._flow_smoke_timeout(self._game_dir()), 120)

    def test_emberwatch_numbers_scale_the_cap(self):
        # The preserved repro: survive, 3 rounds of 75 seconds.
        config = {"plan": {"objective": "survive",
                           "numbers": {"units": 3,
                                       "round_seconds": 75}}}
        self.assertEqual(
            skeleton._flow_smoke_timeout(self._game_dir(config)), 345)

    def test_declared_round_time_extends_the_cap_by_the_slack(self):
        # The cap is an allowance, not a wait — a run ends when
        # the game does, so declared round time always adds the
        # base slack on top.
        config = {"plan": {"numbers": {"units": 2,
                                       "round_seconds": 20}}}
        self.assertEqual(
            skeleton._flow_smoke_timeout(self._game_dir(config)), 160)

    def test_hard_ceiling(self):
        config = {"plan": {"numbers": {"units": 40,
                                       "round_seconds": 300}}}
        self.assertEqual(
            skeleton._flow_smoke_timeout(self._game_dir(config)), 900)

    def test_broken_config_is_the_base_cap(self):
        game_dir = self._game_dir()
        (game_dir / "game_config.json").write_text("{ nope",
                                                   encoding="utf-8")
        self.assertEqual(skeleton._flow_smoke_timeout(game_dir), 120)


class SkeletonRefusalTests(unittest.TestCase):
    def test_refuses_incomplete_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = _project(tmp, template_scope_text())
            with self.assertRaises(skeleton.SkeletonError) as ctx:
                skeleton.build_skeleton(project)
            self.assertIn("incomplete", str(ctx.exception))
            self.assertFalse((project / "game").exists())

    def test_refuses_unapproved_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = _project(tmp, filled_scope_text())
            with self.assertRaises(skeleton.SkeletonError) as ctx:
                skeleton.build_skeleton(project)
            self.assertIn("not approved", str(ctx.exception))
            self.assertFalse((project / "game").exists())

    def test_refuses_missing_scope_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(skeleton.SkeletonError):
                skeleton.build_skeleton(tmp)


class SkeletonBuildTests(unittest.TestCase):
    """One real build, shared by the assertions in this class."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.project = _project(cls._tmp.name, filled_scope_text(), approve=True)
        cls.report = skeleton.build_skeleton(cls.project, validate=VALIDATE)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_expected_files_exist(self):
        for rel in EXPECTED_FILES:
            self.assertTrue((self.project / rel).exists(), rel)
        self.assertEqual(sorted(self.report["files"]), sorted(EXPECTED_FILES))

    def test_project_godot_parses_with_scope_title(self):
        info = godot_module.parse_project_godot(self.project / "game")
        self.assertEqual(info["config_version"], 5)
        self.assertEqual(info["name"], "Filled in for the test")

    def test_no_unreplaced_tokens_or_todos(self):
        game_dir = self.project / "game"
        for path in game_dir.rglob("*"):
            if path.is_file() and ".godot" not in path.parts:
                text = path.read_text(encoding="utf-8", errors="ignore")
                self.assertNotIn("{{", text, str(path))
                if path.suffix == ".gd":
                    self.assertNotIn("TODO", text, str(path))

    def test_provenance_records_every_generated_file(self):
        records = provenance.list_records(self.project)
        by_asset = {r["asset"]: r for r in records}
        for rel in EXPECTED_FILES:
            self.assertIn(rel, by_asset)
            self.assertEqual(by_asset[rel]["source"], "code")
            self.assertEqual(by_asset[rel]["note"], skeleton.PROVENANCE_NOTE)

    def test_ledger_has_zero_cost_skeleton_entry(self):
        summary = ledger.summarise(self.project)
        self.assertIn("skeleton", summary["by_stage"])
        self.assertEqual(summary["by_stage"]["skeleton"]["tokens"], 0)
        self.assertEqual(summary["by_stage"]["skeleton"]["cost"], 0.0)
        self.assertEqual(summary["tokens"], 0)

    def test_report_written(self):
        report = skeleton.load_report(self.project)
        self.assertIsNotNone(report)
        self.assertEqual(report["stage"], "skeleton")

    @unittest.skipUnless(VALIDATE, "Godot validation not available")
    def test_godot_validation_passed_and_tagged(self):
        validation = self.report["validation"]
        self.assertTrue(validation["ran"])
        self.assertTrue(validation["passed"], validation)
        self.assertIn("4.7", validation["godot_version"])
        run_names = [run["name"] for run in validation["runs"]]
        self.assertEqual(
            run_names, ["import", "main scene", "game scene", "flow smoke"]
        )
        for run in validation["runs"]:
            self.assertTrue(run["ok"], run)
            self.assertEqual(run["errors"], [])
        self.assertTrue(vcs.is_repo(self.project))
        self.assertIn(skeleton.KNOWN_GOOD_TAG, vcs.list_tags(self.project))


class SkeletonStyleTests(unittest.TestCase):
    def test_scope_hex_colours_are_used(self):
        text = filled_scope_text()
        text = text.replace(
            "- REQUIRED Palette: Filled in for the test",
            "- REQUIRED Palette: deep #112233 with a bright #AABBCC accent",
        )
        with tempfile.TemporaryDirectory() as tmp:
            project = _project(tmp, text, approve=True)
            skeleton.build_skeleton(project, validate=False)
            player = (project / "game/scripts/player.gd").read_text(encoding="utf-8")
            # #112233 -> Color(0.067, 0.133, 0.2)
            self.assertIn("Color(0.067, 0.133, 0.2)", player)
            game = (project / "game/scripts/game.gd").read_text(encoding="utf-8")
            # #AABBCC -> Color(0.667, 0.733, 0.8)
            self.assertIn("Color(0.667, 0.733, 0.8)", game)
            icon = (project / "game/icon.svg").read_text(encoding="utf-8")
            self.assertIn("#112233", icon)


class SkeletonPipelineTests(unittest.TestCase):
    def tearDown(self):
        state.clear_handlers()

    @unittest.skipUnless(VALIDATE, "Godot validation not available")
    def test_advance_scope_to_skeleton_builds(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = _project(tmp, filled_scope_text(), approve=True)
            skeleton.register_stage_handlers()
            self.assertIsNotNone(state.get_handler("skeleton"))
            result = state.advance(project)
            self.assertEqual(result["stage"], "skeleton")
            self.assertTrue((project / "game/project.godot").exists())
            report = skeleton.load_report(project)
            self.assertTrue(report["validation"]["passed"])

    def test_handler_passes_when_report_shows_validated_skeleton(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = _project(tmp, filled_scope_text(), approve=True)
            skeleton.build_skeleton(project, validate=False)
            report = skeleton.load_report(project)
            report["validation"] = {"ran": True, "passed": True}
            skeleton._write_report(project, report)
            # A passed report plus an existing project: no rebuild.
            self.assertTrue(skeleton.skeleton_stage_handler(project))


class SkeletonCliTests(unittest.TestCase):
    def tearDown(self):
        state.clear_handlers()

    def test_skeleton_build_refuses_incomplete_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = _project(tmp, template_scope_text())
            code, out = _run_cli("skeleton", "build", str(project))
            self.assertEqual(code, 1)
            self.assertIn("incomplete", out)

    @unittest.skipUnless(VALIDATE, "Godot validation not available")
    def test_skeleton_build_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "demo"
            code, _ = _run_cli("init", str(project))
            self.assertEqual(code, 0)
            (project / "SCOPE.md").write_text(filled_scope_text(), encoding="utf-8")
            code, _ = _run_cli("scope", "approve", str(project), "--by", "Tester")
            self.assertEqual(code, 0)
            code, out = _run_cli("skeleton", "build", str(project))
            self.assertEqual(code, 0)
            self.assertIn("grey-box Godot skeleton", out)
            self.assertIn("Godot validation passed", out)
            self.assertTrue((project / "game/project.godot").exists())


if __name__ == "__main__":
    unittest.main()
