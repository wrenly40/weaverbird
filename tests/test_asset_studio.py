"""Tests for the asset studio: the manifest/registry-driven
listing, single-asset regeneration (only that file changes, a
new provenance record), the palette parse/write round-trip
through the app endpoint, and the gallery file route (serves
SVG + audio, refuses anything outside game/assets + the icon).
"""

import hashlib
import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import filled_scope_text  # noqa: E402,F401  (sets sys.path)

from weaverbird import app as app_module  # noqa: E402
from weaverbird import assets_gen  # noqa: E402
from weaverbird import provenance  # noqa: E402
from weaverbird import scope  # noqa: E402
from weaverbird import skeleton  # noqa: E402
from weaverbird import state  # noqa: E402

COUNTS = ("- REQUIRED Content counts (levels / waves / rooms / items, "
          "as applicable): Filled in for the test")
GENRE = "- REQUIRED Genre / archetype: Filled in for the test"
WIN = "- REQUIRED Win condition: Filled in for the test"


def scoped_project(tmp, genre="puzzle", counts="2 puzzles",
                   win="Push every crate onto its pad"):
    text = filled_scope_text().replace(COUNTS, COUNTS.split(":")[0] + f": {counts}")
    text = text.replace(GENRE, GENRE.split(":")[0] + f": {genre}")
    text = text.replace(WIN, WIN.split(":")[0] + f": {win}")
    project = Path(tmp)
    (project / "SCOPE.md").write_text(text, encoding="utf-8")
    state.init_state(project)
    scope.approve_scope(project / "SCOPE.md", by="Tester")
    return project


def _hash_tree(project):
    hashes = {}
    for path in sorted((project / "game").rglob("*")):
        if path.is_file() and ".godot" not in path.parts:
            hashes[str(path.relative_to(project))] = hashlib.sha256(
                path.read_bytes()).hexdigest()
    return hashes


class StudioProjectTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.project = scoped_project(self._tmp.name)
        skeleton.build_skeleton(self.project, validate=False)
        assets_gen.generate_assets(self.project)

    def tearDown(self):
        self._tmp.cleanup()

    def test_listing_is_registry_driven_and_flags_missing(self):
        listing = assets_gen.list_assets(self.project)
        names = {entry["name"] for entry in listing}
        self.assertIn("player", names)
        self.assertIn("icon", names)
        self.assertIn("music", names)
        self.assertTrue(all(entry["regenerable"] for entry in listing))
        kinds = {entry["name"]: entry["kind"] for entry in listing}
        self.assertEqual(kinds["icon"], "icon")
        self.assertEqual(kinds["player"], "sprite")
        self.assertEqual(kinds["music"], "audio")
        self.assertTrue(all(not entry["missing"] for entry in listing))
        (self.project / "game" / "assets" / "crate.svg").unlink()
        after = {e["name"]: e for e in assets_gen.list_assets(self.project)}
        self.assertTrue(after["crate"]["missing"])
        self.assertIsNone(after["crate"]["size"])

    def test_manifest_only_asset_is_listed_not_regenerable(self):
        report = assets_gen.load_report(self.project)
        ghost = self.project / "game" / "assets" / "ghost.svg"
        ghost.write_text("<svg/>", encoding="utf-8")
        report["files"].append("game/assets/ghost.svg")
        (self.project / ".weaverbird" / "assets_report.json").write_text(
            json.dumps(report), encoding="utf-8")
        listing = {e["name"]: e for e in assets_gen.list_assets(self.project)}
        self.assertIn("ghost", listing)
        self.assertFalse(listing["ghost"]["regenerable"])

    def test_regenerate_changes_only_that_file(self):
        player = self.project / "game" / "assets" / "player.svg"
        player.write_text("corrupted", encoding="utf-8")
        before = _hash_tree(self.project)
        result = assets_gen.regenerate_asset(self.project, "player")
        after = _hash_tree(self.project)
        self.assertNotEqual(before["game/assets/player.svg"],
                            after["game/assets/player.svg"])
        for rel, digest in before.items():
            if rel != "game/assets/player.svg":
                self.assertEqual(after[rel], digest, rel)
        self.assertEqual(result["name"], "player")
        records = {r["asset"]: r
                   for r in provenance.list_records(self.project)}
        self.assertIn("regenerated",
                      records["game/assets/player.svg"]["note"])
        self.assertNotIn("rel", result)  # shape stays {name, path, size}

    def test_regenerate_unknown_name_lists_valid_ones(self):
        with self.assertRaises(assets_gen.AssetsError) as ctx:
            assets_gen.regenerate_asset(self.project, "nope")
        self.assertIn("player", str(ctx.exception))

    def test_palette_round_trip_through_scope(self):
        state_before = assets_gen.palette_state(self.project)
        hexes = ["#112233", "#445566", "#778899", "#AABBCC", "#DDEEFF"]
        state_after = assets_gen.save_palette(self.project, hexes)
        self.assertEqual(state_after["palette"]["accent_hex"], "#112233")
        loaded = scope.load_scope(self.project / "SCOPE.md")
        values = {f.name: f.value for f in loaded.fields}
        self.assertIn("112233", values["Palette"])
        assets_gen.regenerate_asset(self.project, "player")
        svg = (self.project / "game" / "assets" / "player.svg").read_text(
            encoding="utf-8")
        self.assertIn("112233", svg)

    def test_save_palette_rejects_bad_hexes(self):
        with self.assertRaises(assets_gen.AssetsError):
            assets_gen.save_palette(
                self.project,
                ["red", "#445566", "#778899", "#AABBCC", "#DDEEFF"])


def _request(url, fields=None):
    if fields is None:
        request = urllib.request.Request(url, method="GET")
    else:
        data = urllib.parse.urlencode(fields).encode("utf-8")
        request = urllib.request.Request(url, data=data, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return (response.status, response.headers.get_content_type(),
                    response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers.get_content_type(), exc.read()


class StudioAppTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self._tmp.name)
        self.project = self.workspace / "bee-game"
        self.project.mkdir()
        scoped = scoped_project(self.project)
        skeleton.build_skeleton(scoped, validate=False)
        assets_gen.generate_assets(scoped)
        self.server = app_module.create_server(self.workspace, port=0)
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self._tmp.cleanup()

    def test_gallery_serves_svg_and_audio(self):
        status, ctype, body = _request(
            self.base + "/project/bee-game/asset-file/game/assets/player.svg")
        self.assertEqual(status, 200)
        self.assertEqual(ctype, "image/svg+xml")
        self.assertIn(b"<svg", body)
        status, ctype, body = _request(
            self.base + "/project/bee-game/asset-file/game/assets/audio/click.wav")
        self.assertEqual(status, 200)
        self.assertEqual(ctype, "audio/wav")
        self.assertTrue(body.startswith(b"RIFF"))
        status, ctype, _body = _request(
            self.base + "/project/bee-game/asset-file/game/icon.svg")
        self.assertEqual(status, 200)

    def test_gallery_refuses_traversal(self):
        for rest in ("game/assets/../../SCOPE.md",
                     "SCOPE.md",
                     "game/project.godot",
                     "../bee-game/SCOPE.md"):
            status, _ctype, _body = _request(
                self.base + "/project/bee-game/asset-file/" + rest)
            self.assertEqual(status, 404, rest)

    def test_palette_post_writes_scope(self):
        status, _ctype, body = _request(
            self.base + "/project/bee-game/assets/palette",
            {"accent": "#102030", "target": "#405060",
             "hazard": "#708090", "platform": "#0a0b0c",
             "crate": "#0d0e0f"})
        self.assertEqual(status, 200)
        self.assertIn(b"Palette saved", body)
        loaded = scope.load_scope(self.project / "SCOPE.md")
        values = {f.name: f.value for f in loaded.fields}
        self.assertIn("102030", values["Palette"])
        state_now = assets_gen.palette_state(self.project)
        self.assertEqual(state_now["palette"]["accent_hex"], "#102030")

    def test_regenerate_button_runs_a_job(self):
        import re
        import time as _time
        status, _ctype, body = _request(
            self.base + "/project/bee-game/assets/regenerate",
            {"asset": "player"})
        self.assertEqual(status, 200)
        match = re.search(rb'data-job-id="([^"]+)"', body)
        self.assertIsNotNone(match)
        job_id = match.group(1).decode()
        deadline = _time.monotonic() + 30
        job = None
        while _time.monotonic() < deadline:
            _s, _c, payload = _request(f"{self.base}/api/jobs/{job_id}")
            job = json.loads(payload)
            if job["status"] in ("done", "failed"):
                break
            _time.sleep(0.1)
        self.assertEqual(job["status"], "done", job)
        self.assertIn("Regenerated", job["result"])


if __name__ == "__main__":
    unittest.main()
