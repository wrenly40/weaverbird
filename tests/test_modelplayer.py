"""Tests for the model-driven playtester (modelplayer.py) and the
vision seam in brain.py: provider payload shapes for images
(mocked transport), the plain refusal when no vision tier is
configured, and a Godot end-to-end where a scripted fake brain
plays an arena game through the real bridge and wins.
"""

import base64
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import filled_scope_text  # noqa: E402,F401  (sets sys.path)

from weaverbird import brain, feedback, generate, modelplayer, scope, skeleton, state, testing  # noqa: E402
from weaverbird import assets_gen  # noqa: E402
from weaverbird import godot as godot_module  # noqa: E402

GODOT_PRESENT = godot_module.find_godot()[0] is not None
XVFB_PRESENT = shutil.which("xvfb-run") is not None

# A real 1x1 PNG.
PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
    "+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")

SETTINGS = (
    "budgets:\n  tokens_per_run: 1000\n  tokens_per_game: 100000\n"
    "models:\n"
    "  mid: { provider: openai, model: vision-x, base_url: null, "
    "api_key_env: WB_MP_KEY, cost_per_1k_tokens: null }\n"
    "  frontier: { provider: anthropic, model: vision-a, base_url: null, "
    "api_key_env: WB_MP_AKEY, cost_per_1k_tokens: null }\n")


class VisionShapeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.project = Path(self.tmp) / "proj"
        self.project.mkdir()
        (self.project / ".weaverbird").mkdir()
        self.settings = Path(self.tmp) / "global-settings.yaml"
        self.settings.write_text(SETTINGS, encoding="utf-8")
        self.image = Path(self.tmp) / "shot.png"
        self.image.write_bytes(PNG_BYTES)
        self.captured = {}

    def _send(self, url, headers, body, timeout=60):
        self.captured["url"] = url
        self.captured["body"] = json.loads(body.decode("utf-8"))
        if "anthropic" in url:
            return 200, json.dumps({
                "content": [{"type": "text", "text": "ok"}],
                "usage": {"input_tokens": 10, "output_tokens": 2}})
        return 200, json.dumps({
            "choices": [{"message": {"content": "ok"}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 2}})

    def test_openai_image_payload(self):
        with mock.patch.dict(os.environ, {"WB_MP_KEY": "k"}), \
                mock.patch.object(brain, "_send", self._send):
            result = brain.complete_with_image(
                self.project, "mid", "What do you see?", self.image,
                stage="playtest", settings_path=self.settings)
        self.assertTrue(result.ok, result.error)
        content = self.captured["body"]["messages"][0]["content"]
        self.assertIsInstance(content, list)
        kinds = [part["type"] for part in content]
        self.assertEqual(kinds, ["text", "image_url"])
        self.assertTrue(content[1]["image_url"]["url"].startswith(
            "data:image/png;base64,"))

    def test_anthropic_image_payload(self):
        with mock.patch.dict(os.environ, {"WB_MP_AKEY": "k"}), \
                mock.patch.object(brain, "_send", self._send):
            result = brain.complete_with_image(
                self.project, "frontier", "What do you see?", self.image,
                stage="playtest", settings_path=self.settings)
        self.assertTrue(result.ok, result.error)
        content = self.captured["body"]["messages"][0]["content"]
        kinds = [part["type"] for part in content]
        self.assertEqual(kinds, ["text", "image"])
        self.assertEqual(content[1]["source"]["media_type"], "image/png")
        self.assertEqual(content[1]["source"]["type"], "base64")

    def test_missing_image_is_a_plain_result(self):
        result = brain.complete_with_image(
            self.project, "mid", "hi", Path(self.tmp) / "nope.png",
            stage="playtest", settings_path=self.settings)
        self.assertFalse(result.ok)
        self.assertIn("missing", result.error)


class UnavailableTests(unittest.TestCase):
    def test_no_brain_configured_refuses_plainly(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            game = project / "game"
            (game / "tests").mkdir(parents=True)
            (game / "project.godot").write_text(
                "[application]\nconfig/name=\"No Brain\"\n", encoding="utf-8")
            (game / "tests" / "model_bridge.gd").write_text(
                "extends Node\n", encoding="utf-8")
            with self.assertRaises(modelplayer.ModelPlayerError) as ctx:
                modelplayer.run_model_playtest(project)
            message = str(ctx.exception)
            self.assertIn("tier", message)
            self.assertIn("vision", message)
            # Nothing was launched, no report written.
            self.assertFalse(modelplayer.report_path_for(project).exists())


COUNTS = ("- REQUIRED Content counts (levels / waves / rooms / items, "
          "as applicable): Filled in for the test")


def scoped_project(tmp, counts="1 rounds"):
    text = filled_scope_text().replace(
        COUNTS, COUNTS.split(":")[0] + f": {counts}")
    project = Path(tmp)
    (project / "SCOPE.md").write_text(text, encoding="utf-8")
    state.init_state(project)
    scope.approve_scope(project / "SCOPE.md", by="Tester")
    return project


def steering_brain(confuse_once: bool = False):
    """A scripted policy standing in for the model: start from the
    menu, then plan a route to the nearest target over the honestly
    exposed state facts. Hazards patrol horizontal segments (also
    exposed), and one touch loses the run, so the plan only rests
    where the whole patrol segment stays clear — crossing a patrol
    line is done along a column the segment never reaches."""
    calls = {"n": 0}
    step_px = 118.0  # one decision of held input: ~0.35s at 330px/s
    moves = {"move_left": (-step_px, 0.0), "move_right": (step_px, 0.0),
             "move_up": (0.0, -step_px), "move_down": (0.0, step_px)}
    bounds = (39.0, 1113.0, 39.0, 609.0)  # arena clamp for this game

    def dist(a, b):
        return ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5

    def seg_dist(point, seg):
        x1, y, x2 = seg[0], seg[1], seg[2]
        if x1 <= point[0] <= x2:
            dx = 0.0
        else:
            dx = min(abs(point[0] - x1), abs(point[0] - x2))
        return (dx ** 2 + (point[1] - y) ** 2) ** 0.5

    def path_dist(a, b, seg):
        """Exact distance between the (axis-aligned) step a→b and a
        horizontal patrol segment — midpoint sampling misses the
        crossing point, and crossing a patrol line within reach of
        its end is exactly how the naive policy died."""
        x1, y, x2 = seg[0], seg[1], seg[2]
        if a[0] == b[0]:  # vertical step at fixed x
            dx = 0.0 if x1 <= a[0] <= x2 else min(
                abs(a[0] - x1), abs(a[0] - x2))
            lo, hi = min(a[1], b[1]), max(a[1], b[1])
            dy = 0.0 if lo <= y <= hi else min(abs(y - lo), abs(y - hi))
        else:  # horizontal step at fixed y
            dy = abs(a[1] - y)
            lo, hi = min(a[0], b[0]), max(a[0], b[0])
            if lo <= x2 and x1 <= hi:
                dx = 0.0
            else:
                dx = min(abs(lo - x1), abs(lo - x2), abs(hi - x1),
                         abs(hi - x2))
        return (dx ** 2 + dy ** 2) ** 0.5

    def pass_dist(a, b, point):
        """Distance from a point to the (axis-aligned) step a→b.
        Collection is checked by the game every frame, so a step
        that passes within reach of a target collects it mid-move —
        the plan must count those edges as reaching the goal."""
        if a[0] == b[0]:
            dx = abs(point[0] - a[0])
            lo, hi = min(a[1], b[1]), max(a[1], b[1])
            dy = 0.0 if lo <= point[1] <= hi else min(
                abs(point[1] - lo), abs(point[1] - hi))
        else:
            dy = abs(point[1] - a[1])
            lo, hi = min(a[0], b[0]), max(a[0], b[0])
            dx = 0.0 if lo <= point[0] <= hi else min(
                abs(point[0] - lo), abs(point[0] - hi))
        return (dx ** 2 + dy ** 2) ** 0.5

    def plan(player, goal, segments):
        """Breadth-first over one-decision steps; returns the first
        move of a route whose resting points keep 34px from every
        patrol segment (a touch needs 27px), or None."""
        start = (float(player[0]), float(player[1]))

        def safe(point):
            return all(seg_dist(point, seg) >= 34.0 for seg in segments)

        def step_safe(a, b):
            return safe(b) and all(
                path_dist(a, b, seg) >= 30.0 for seg in segments)

        queue = [(start, None)]
        seen = {start}
        while queue:
            node, first = queue.pop(0)
            if dist(node, goal) <= 30.0 and first is not None:
                return first
            for name, step in moves.items():
                nxt = (min(max(node[0] + step[0], bounds[0]), bounds[1]),
                       min(max(node[1] + step[1], bounds[2]), bounds[3]))
                nxt = (round(nxt[0]), round(nxt[1]))
                if not step_safe(node, nxt):
                    continue
                if pass_dist(node, nxt, goal) <= 24.0:
                    return first or name
                if nxt in seen:
                    continue
                seen.add(nxt)
                queue.append((nxt, first or name))
        return None

    def brain_fn(prompt, image_path, game_state):
        calls["n"] += 1
        scene = str(game_state.get("scene", ""))
        confused = False
        note = ""
        if scene.endswith("main_menu.tscn"):
            action = "start"
        else:
            targets = game_state.get("targets_pos") or []
            segments = game_state.get("hazards_patrol") or [
                [h[0], h[1], h[0], h[1]]
                for h in (game_state.get("hazards_pos") or [])]
            player = game_state.get("player_pos") or [0, 0]
            if not targets:
                action = "wait"
            else:
                goal = min(targets, key=lambda t: dist(player, t))
                action = plan(player, goal, segments) or "wait"
            if confuse_once and calls["n"] == 3:
                confused = True
                note = "not sure which glowing dot is the target"
        return json.dumps({"action": action, "reason": "policy",
                           "confused": confused, "note": note})
    return brain_fn


@unittest.skipUnless(GODOT_PRESENT and XVFB_PRESENT,
                     "Godot or Xvfb not available")
class BridgeEndToEndTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.project = scoped_project(cls._tmp.name)
        skeleton.build_skeleton(cls.project)
        assets_gen.generate_assets(cls.project)
        generate.build_game(cls.project, use_brain=False)
        # A short arena game for the bridge: one round, 3 targets,
        # a generous timer.
        from weaverbird import patch as patch_module, vcs
        config = patch_module.load_config(cls.project)
        config.update({"rounds": 1, "targets_per_round": 3,
                       "round_seconds": 180})
        patch_module.save_config(cls.project, config)
        vcs.commit_all(cls.project, "bridge fixture: short arena game")
        cls.baseline = testing.run_tests(cls.project)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_fake_brain_wins_through_the_bridge(self):
        self.assertTrue(self.baseline["ok"], self.baseline["warnings"])
        record = modelplayer.run_model_playtest(
            self.project, brain_fn=steering_brain(confuse_once=True),
            max_decisions=120)
        self.assertEqual(record["outcome"], "finished", record)
        self.assertEqual(record["result"], "win")
        self.assertGreaterEqual(record["decisions"], 3)
        shots = list((self.project / ".weaverbird" / "model_playtest"
                      / "shots").glob("*.png"))
        self.assertTrue(shots, "no screenshots were kept")
        self.assertTrue(modelplayer.report_path_for(self.project).exists())
        # The transcript's first decision starts the game from the menu.
        self.assertEqual(record["transcript"][0]["action"], "start")
        # The run folded into the test report's playtesters.
        report = testing.load_report(self.project)
        self.assertIn("model", report["playtesters"])
        self.assertTrue(report["playtesters"]["model"]["finished"])
        # The confusion note became a feedback item.
        store = feedback.load_store(self.project)
        filed = [i for i in store["items"]
                 if i["source"] == "model-playtest"]
        self.assertTrue(filed, store["items"])
        self.assertEqual(filed[0]["category"], "confusion")

    def test_garbage_brain_stops_on_invalid(self):
        record = modelplayer.run_model_playtest(
            self.project,
            brain_fn=lambda prompt, image, state: "I feel like moving",
            max_decisions=10)
        self.assertEqual(record["outcome"], "invalid")
        self.assertEqual(record["decisions"], 3)


if __name__ == "__main__":
    unittest.main()
