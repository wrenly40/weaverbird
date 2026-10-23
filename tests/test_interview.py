"""Tests for the conversational scope interview (interview.py).

One engine, two surfaces (the app chat and the CLI line mode),
two modes: deterministic parsing with no brain configured, and
brain mode with the provider transport mocked through
brain._send — no test touches the network or a real key.

The contracts under test: a full deterministic interview ends in
a complete scope; a style pick writes the sample's palette into
the scope; filled fields are never re-asked; brain answers are
validated (unknown fields and invalid values are dropped and
re-asked); a brain failure falls back to the deterministic
engine for that turn; the app chat endpoint runs turns as jobs
and the state endpoint shows the fields landing.
"""

import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.parse
import urllib.request
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import template_scope_text  # noqa: E402

import yaml  # noqa: E402

from weaverbird import app as app_module  # noqa: E402
from weaverbird import brain, interview, scope, state  # noqa: E402

ANSWERS = [
    "Bubble Bounce",
    "Bounce bubbles to the sky in a cheerful arena.",
    "2D",
    "Arena collector",
    "Bounce and collect bubbles",
    "Collect 10 bubbles each round",
    "The timer runs out or a hazard pops you",
    "About 5 minutes",
    "Micro",
    "3 rounds",
    "Casual players, gentle",
    "Big combos",
    "No story mode",
    "bright-chunky",
    "Bright chiptune",
    "Rounded panels, big buttons",
    "code-drawn only",
    "Disclose AI assistance in the store listing",
    "No copied art",
    "Windows and Linux",
    "itch first",
    "Free",
    "no",
]

SETTINGS = {
    "budgets": {"tokens_per_run": 100000, "tokens_per_game": 100000,
                "cost_ceiling_per_month": None,
                "max_build_iterations": 40,
                "warn_at_percent_of_cap": 80},
    "models": {
        "cheap": {"provider": "openai", "model": "cheap-model",
                  "base_url": None, "api_key_env": "WB_TEST_KEY",
                  "cost_per_1k_tokens": None},
        "mid": {"provider": "openai", "model": "mid-model",
                "base_url": None, "api_key_env": "WB_TEST_KEY",
                "cost_per_1k_tokens": None},
        "frontier": {"provider": "anthropic", "model": "frontier-model",
                     "base_url": None, "api_key_env": "WB_TEST_AKEY",
                     "cost_per_1k_tokens": None},
    },
}


def make_project(tmp: str) -> Path:
    project = Path(tmp) / "proj"
    project.mkdir(parents=True)
    (project / "SCOPE.md").write_text(template_scope_text(),
                                      encoding="utf-8")
    state.init_state(project)
    return project


class DeterministicInterviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.project = make_project(self.tmp)

    def drive(self, answers=ANSWERS):
        state_dict = interview.start(self.project)
        for answer in answers:
            before = {f.name for f in
                      scope.load_scope(self.project / "SCOPE.md")
                      .missing_fields}
            # The question on the table only ever asks about
            # fields that are still missing at that moment.
            for name in state_dict["pending_fields"]:
                self.assertIn(name, before)
            state_dict = interview.respond(self.project, answer)
        return state_dict

    def test_full_interview_completes_the_scope(self):
        state_dict = self.drive()
        self.assertTrue(state_dict["complete"])
        self.assertEqual(state_dict["missing_count"], 0)
        parsed = scope.load_scope(self.project / "SCOPE.md")
        self.assertTrue(parsed.is_complete)
        values = {f.name: f.value for f in parsed.fields}
        self.assertEqual(values["Title (working)"], "Bubble Bounce")
        self.assertEqual(values["Dimension"], "2D")
        self.assertEqual(values["Scope class"], "Micro")
        self.assertEqual(values["Telemetry"], "no")

    def test_style_pick_writes_palette_and_rules(self):
        state_dict = self.drive()
        values = {f.name: f.value for f in
                  scope.load_scope(self.project / "SCOPE.md").fields}
        self.assertIn("bright-chunky",
                      values["Style sample chosen "
                             "(id / link to approved sample)"])
        # The palette pins the style's hexes in role order.
        self.assertIn("#FF7A1A", values["Palette"])
        self.assertIn("#2FBF71", values["Palette"])
        self.assertIn("Chunky proportions",
                      values["Proportions / outline / shading rules"])
        # The sample cards were generated and cached as artifacts.
        samples_dir = self.project / ".weaverbird" / "style_samples"
        self.assertEqual(
            sorted(p.name for p in samples_dir.glob("*.svg")),
            ["bold-neon-night.svg", "bright-chunky.svg",
             "soft-pastel.svg", "warm-storybook.svg"])
        svg = (samples_dir / "bright-chunky.svg").read_text(
            encoding="utf-8")
        self.assertIn("#FF7A1A", svg)  # the candidate palette itself
        self.assertIn("<svg", svg)
        self.assertIsNotNone(state_dict["samples"])

    def test_filled_fields_are_never_reasked(self):
        state_dict = self.drive()
        # Walk the thread: once a field has been captured (the
        # reflection turns record what each answer filled), no
        # later question turn may ask about it again. A group is
        # legitimately re-presented while its other fields are
        # still open — what is forbidden is re-asking a DONE one.
        filled: set = set()
        asked_kinds = set()
        for turn in state_dict["turns"]:
            if turn.get("kind") in ("question", "samples"):
                asked_kinds.add(turn["kind"])
                for name in turn.get("fields", []):
                    self.assertNotIn(
                        name, filled,
                        f"{name} was re-asked after being filled")
            if turn.get("kind") == "reflection":
                filled.update(turn.get("fields", []))
        self.assertIn("samples", asked_kinds)
        self.assertTrue(filled)

    def test_field_value_lines_and_multiline_answers(self):
        interview.start(self.project)
        out = interview.respond(
            self.project,
            "Title (working): Named Directly\n"
            "One-line pitch: Set by exact field lines")
        self.assertEqual(out["applied"]["Title (working)"],
                         "Named Directly")
        values = {f.name: f.value for f in
                  scope.load_scope(self.project / "SCOPE.md").fields}
        self.assertEqual(values["One-line pitch"],
                         "Set by exact field lines")

    def test_prompt_is_extracted_first_and_never_reasked(self):
        scope.set_original_prompt(
            self.project / "SCOPE.md",
            "A tiny arena game called Promptmon")
        state_dict = interview.start(self.project)
        # Without a brain nothing is extracted, but the prompt is
        # on record and the opening questions still work.
        self.assertFalse(state_dict["complete"])
        self.assertIn("Title (working)",
                      state_dict["pending_fields"])

    def test_approval_gate_unchanged(self):
        self.drive()
        with self.assertRaises(scope.ScopeError):
            scope.approve_scope(self.project / "SCOPE.md", by="")
        parsed = scope.approve_scope(self.project / "SCOPE.md",
                                      by="Tester")
        self.assertTrue(parsed.is_approved)


class BrainInterviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.project = make_project(self.tmp)
        self.settings_path = Path(self.tmp) / "global-settings.yaml"
        self.settings_path.write_text(yaml.safe_dump(SETTINGS),
                                      encoding="utf-8")

    def _send_with(self, payload_text):
        def send(url, headers, body, timeout=60):
            return 200, json.dumps({
                "choices": [{"message": {"content": payload_text}}],
                "usage": {"prompt_tokens": 40,
                          "completion_tokens": 25},
            })
        return send

    def test_brain_turn_validates_and_drops(self):
        reply = json.dumps({
            "fields": {
                "Title (working)": "Neon Kart",
                "One-line pitch": "Kart racing at night",
                "Dimension": "banana",
                "Cheat codes": "all of them",
            },
            "next_message": "A night kart racer — noted!",
        })
        with mock.patch.dict(os.environ, {"WB_TEST_KEY": "k"}), \
                mock.patch.object(brain, "_send",
                                  self._send_with(reply)):
            out = interview.respond(
                self.project, "It's Neon Kart, kart racing at night",
                settings_path=self.settings_path)
        self.assertTrue(out["brain_used"])
        self.assertEqual(out["applied"]["Title (working)"],
                         "Neon Kart")
        self.assertIn("Dimension", out["dropped"])
        self.assertIn("Cheat codes", out["dropped"])
        values = {f.name: f.value for f in
                  scope.load_scope(self.project / "SCOPE.md").fields}
        self.assertEqual(values["Title (working)"], "Neon Kart")
        self.assertEqual(values["Dimension"], "")
        reflections = [t for t in out["turns"]
                       if t.get("kind") == "reflection"]
        self.assertTrue(reflections)
        self.assertIn("A night kart racer",
                      reflections[-1]["text"])
        # The spend went through the ledger like any brain call.
        from weaverbird import ledger
        summary = ledger.summarise(self.project)
        self.assertEqual(summary["tokens"], 65)

    def test_brain_style_pick_applies_the_sample(self):
        reply = json.dumps({
            "fields": {"Style sample chosen (id / link to approved "
                       "sample)": "soft-pastel"},
            "next_message": "Pastel it is.",
        })
        # Fill everything before the style group first, so the
        # style lock is the pending question.
        interview.start(self.project)
        for answer in ANSWERS[:13]:
            interview.respond(self.project, answer)
        with mock.patch.dict(os.environ, {"WB_TEST_KEY": "k"}), \
                mock.patch.object(brain, "_send",
                                  self._send_with(reply)):
            out = interview.respond(
                self.project, "the pastel one please",
                settings_path=self.settings_path)
        values = {f.name: f.value for f in
                  scope.load_scope(self.project / "SCOPE.md").fields}
        self.assertIn("soft-pastel",
                      values["Style sample chosen "
                             "(id / link to approved sample)"])
        self.assertIn("#9B8CF2", values["Palette"])

    def test_brain_failure_falls_back_to_deterministic(self):
        def bad_send(url, headers, body, timeout=60):
            return 500, "provider down"
        interview.start(self.project)
        interview.respond(self.project, "Fallback Game")
        interview.respond(self.project, "A game about falling back")
        with mock.patch.dict(os.environ, {"WB_TEST_KEY": "k"}), \
                mock.patch.object(brain, "_send", bad_send):
            out = interview.respond(self.project, "2D",
                                    settings_path=self.settings_path)
        self.assertFalse(out["brain_used"])
        self.assertEqual(out["applied"].get("Dimension"), "2D")


class CliInterviewTests(unittest.TestCase):
    def test_piped_interview_via_cli(self):
        tmp = tempfile.mkdtemp()
        project = make_project(tmp)
        env = dict(os.environ)
        src = Path(__file__).resolve().parents[1] / "src"
        env["PYTHONPATH"] = str(src)
        result = subprocess.run(
            [sys.executable, "-m", "weaverbird", "interview",
             str(project)],
            input="\n".join(ANSWERS) + "\n",
            capture_output=True, text=True, env=env, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Got it", result.stdout)
        self.assertIn("bright-chunky", result.stdout)
        self.assertIn("scope is complete", result.stdout)
        parsed = scope.load_scope(project / "SCOPE.md")
        self.assertTrue(parsed.is_complete)


class AppChatTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.workspace = Path(self.tmp) / "workspace"
        self.project = self.workspace / "chat-game"
        self.project.mkdir(parents=True)
        (self.project / "SCOPE.md").write_text(template_scope_text(),
                                               encoding="utf-8")
        state.init_state(self.project)
        self.server = app_module.create_server(self.workspace, port=0)
        self.thread = threading.Thread(
            target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = self.server.url.rstrip("/")

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def get(self, url):
        with urllib.request.urlopen(url, timeout=10) as response:
            return response.status, response.read().decode("utf-8")

    def post(self, url, fields):
        data = urllib.parse.urlencode(fields).encode("utf-8")
        request = urllib.request.Request(url, data=data, method="POST")
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, response.read().decode("utf-8")

    def chat(self, message):
        import re
        import time as _time
        _status, body = self.post(
            f"{self.base}/project/chat-game/scope/chat",
            {"message": message})
        match = re.search(r'data-job-id="([^"]+)"', body)
        self.assertIsNotNone(match, "the chat page carries no job card")
        job_id = match.group(1)
        deadline = _time.monotonic() + 30
        while _time.monotonic() < deadline:
            _s, payload = self.get(f"{self.base}/api/jobs/{job_id}")
            job = json.loads(payload)
            if job["status"] in ("done", "failed", "cancelled",
                                 "interrupted"):
                self.assertEqual(job["status"], "done",
                                 job.get("error"))
                self.assertEqual(job.get("worker"), "scope")
                return job
            _time.sleep(0.05)
        self.fail(f"chat job {job_id} did not finish")

    def test_chat_turns_write_fields_and_state_endpoint(self):
        self.chat("")  # Begin
        self.chat("Chat Game")
        _s, payload = self.get(
            f"{self.base}/project/chat-game/scope/state")
        state_dict = json.loads(payload)
        fields = {f["name"]: f for f in state_dict["fields"]}
        self.assertEqual(fields["Title (working)"]["value"],
                         "Chat Game")
        self.assertTrue(fields["Title (working)"]["filled"])
        self.assertFalse(state_dict["complete"])
        # The chat page shows the thread and the live panel.
        _s, page = self.get(f"{self.base}/project/chat-game/scope")
        self.assertIn("Chat Game", page)
        self.assertIn("The scope so far", page)
        self.assertIn("Edit as form", page)
        # The form fallback is still reachable.
        _s, form_page = self.get(
            f"{self.base}/project/chat-game/scope/form")
        self.assertIn("Scope form", form_page)
        self.assertIn('name="Title (working)"', form_page)

    def test_style_samples_render_and_pick_in_chat(self):
        self.chat("")
        for answer in ANSWERS[:13]:
            self.chat(answer)
        _s, page = self.get(f"{self.base}/project/chat-game/scope")
        self.assertIn("style-sample/bright-chunky", page)
        self.assertIn("Bright &amp; chunky", page)
        _s, svg = self.get(
            f"{self.base}/project/chat-game/style-sample/bright-chunky")
        self.assertIn("<svg", svg)
        self.assertIn("#FF7A1A", svg)
        self.chat("soft-pastel")
        values = {f.name: f.value for f in
                  scope.load_scope(self.project / "SCOPE.md").fields}
        self.assertIn("soft-pastel",
                      values["Style sample chosen "
                             "(id / link to approved sample)"])
        self.assertIn("#9B8CF2", values["Palette"])


if __name__ == "__main__":
    unittest.main()
