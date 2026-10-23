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


class AuthoritativeAnswerTests(unittest.TestCase):
    """Dogfood finding D1: an answer the maker submits is
    authoritative. "None" for Telemetry is a complete answer
    (no telemetry), not an unspecified field — before 0.10.1 it
    was dropped by validation and the group re-asked forever,
    silently."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.project = make_project(self.tmp)

    def _drive_to_telemetry(self):
        state_dict = interview.start(self.project)
        for answer in ANSWERS[:-1]:
            state_dict = interview.respond(self.project, answer)
        self.assertEqual(state_dict["pending_fields"], ["Telemetry"])
        self.assertEqual(state_dict["missing_count"], 1)
        return state_dict

    def test_none_completes_telemetry_in_one_turn(self):
        self._drive_to_telemetry()
        out = interview.respond(self.project, "None")
        self.assertEqual(out["applied"].get("Telemetry"), "no")
        self.assertTrue(out["complete"])
        values = {f.name: f.value for f in
                  scope.load_scope(self.project / "SCOPE.md").fields}
        self.assertEqual(values["Telemetry"], "no")
        reflections = [t["text"] for t in out["turns"]
                       if t.get("kind") == "reflection"]
        self.assertTrue(
            any("Telemetry: no" in text for text in reflections),
            reflections)

    def test_telemetry_none_by_field_line(self):
        interview.start(self.project)
        out = interview.respond(self.project, "Telemetry: None")
        self.assertEqual(out["applied"].get("Telemetry"), "no")

    def test_placeholder_filter_applies_only_to_guesses(self):
        # A guess-shaped value ("2D / 3D" is the template's hint
        # text) is still filtered for non-authoritative values…
        self.assertIsNone(interview.validate_value(
            "Genre / archetype", "2D / 3D"))
        # …but the same words submitted as the maker's answer
        # are taken as written.
        self.assertEqual(
            interview.validate_value("Genre / archetype", "2D / 3D",
                                     authoritative=True),
            "2D / 3D")

    def test_a_turn_that_captures_nothing_says_so(self):
        self._drive_to_telemetry()
        out = interview.respond(self.project, "banana")
        self.assertEqual(out["applied"], {})
        self.assertEqual(out["uncaptured"], ["banana"])
        self.assertFalse(out["complete"])
        reflections = [t["text"] for t in out["turns"]
                       if t.get("kind") == "reflection"]
        self.assertTrue(
            any("nothing was captured" in text
                and "Telemetry" in text for text in reflections),
            reflections)
        # And the field is re-asked, with the explanation above
        # it — not silently looped.
        self.assertEqual(out["pending_fields"], ["Telemetry"])


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


class SurplusAnswerTests(unittest.TestCase):
    """0.9.0 (F4): a message with MORE answer lines than the
    pending group has open fields applies the first lines
    positionally, discards the extras, and says what was ignored
    — answers can never silently shift onto the wrong fields."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.project = make_project(self.tmp)
        interview.start(self.project)
        # Identity group (2 fields) answered with one 2-line
        # message: no surplus, the ordinary in-order fill.
        interview.respond(
            self.project, "Bubble Bounce\nBounce bubbles to the sky.")

    def values(self):
        return {f.name: f.value for f in
                scope.load_scope(self.project / "SCOPE.md").fields}

    def test_surplus_lines_apply_positionally_and_extras_are_quoted(self):
        # The Shape group has eight fields; nine lines arrive.
        # Line 1 sits in the Dimension slot but is not a dimension:
        # positionally it is dropped, NOT walked onward to Genre
        # (the old walk shifted it — and every answer after it —
        # one field along). Line 9 is surplus and must be quoted
        # back as ignored, never captured.
        state = interview.respond(
            self.project,
            "Arena collector\n"        # Dimension slot: unusable
            "Arena battler\n"           # Genre / archetype
            "Bounce bubbles upward\n"   # Core verb
            "Collect 10 bubbles\n"      # Win condition
            "The timer runs out\n"      # Lose / fail condition
            "About 5 minutes\n"         # Session length
            "Micro\n"                   # Scope class
            "3 rounds\n"                # Content counts
            "This extra line stays unused")
        values = self.values()
        self.assertEqual(values["Genre / archetype"], "Arena battler")
        self.assertEqual(
            values["Core verb (what the player does most)"],
            "Bounce bubbles upward")
        self.assertEqual(values["Win condition"], "Collect 10 bubbles")
        self.assertEqual(values["Lose / fail condition"],
                         "The timer runs out")
        self.assertEqual(values["Session length (one sitting)"],
                         "About 5 minutes")
        self.assertEqual(values["Scope class"], "Micro")
        self.assertEqual(
            values["Content counts (levels / waves / rooms / items, "
                   "as applicable)"], "3 rounds")
        # Dimension holds nothing — not line 1, not line 9.
        self.assertIn("Dimension", state["missing"])
        self.assertIn("Dimension", state["dropped"])
        self.assertEqual(state["ignored"],
                         ["This extra line stays unused"])
        for value in values.values():
            self.assertNotEqual(value, "This extra line stays unused")
        # The interviewer's next message quotes the ignored line.
        reflection = state["turns"][-2]
        self.assertEqual(reflection["kind"], "reflection")
        self.assertIn('"This extra line stays unused"',
                      reflection["text"])
        self.assertIn("aside", reflection["text"])

    def test_fewer_lines_behave_as_before(self):
        project = make_project(tempfile.mkdtemp())
        interview.start(project)
        state = interview.respond(project, "Solo Title\nOnly a pitch")
        values = {f.name: f.value for f in
                  scope.load_scope(project / "SCOPE.md").fields}
        self.assertEqual(values["Title (working)"], "Solo Title")
        self.assertEqual(values["One-line pitch"], "Only a pitch")
        self.assertEqual(state["ignored"], [])
        self.assertIn("Dimension", state["missing"])


class StyleLockTests(unittest.TestCase):
    """0.9.0 (F5): once a style sample is chosen the decision
    locks — a further pick (stale page, double submit) is
    refused politely and changes nothing; Change style re-opens
    the choice as the pending turn, and only then do picks work
    again."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.project = make_project(self.tmp)
        interview.start(self.project)
        for answer in ANSWERS[:13]:
            interview.respond(self.project, answer)
        state = interview.respond(self.project, "bright-chunky")
        self.assertIn("bright-chunky", json.dumps(state["applied"]))

    def values(self):
        return {f.name: f.value for f in
                scope.load_scope(self.project / "SCOPE.md").fields}

    def test_choice_is_recorded_and_locks(self):
        state = interview.respond(self.project, "")
        self.assertEqual(state["style_chosen"], "bright-chunky")
        self.assertFalse(state["style_reopen"])

    def test_a_second_pick_is_refused_and_changes_nothing(self):
        before = self.values()
        state = interview.respond(self.project, "soft-pastel")
        self.assertTrue(state.get("style_refused"))
        self.assertEqual(state["applied"], {})
        self.assertEqual(self.values(), before)
        notes = [t for t in state["turns"] if t.get("kind") == "info"]
        self.assertIn("already chosen and locked", notes[-1]["text"])

    def test_ordinary_answers_still_work_after_the_lock(self):
        # The next group's first answer mentions a style word but
        # is not an exact pick — it must land as a normal answer.
        state = interview.respond(self.project, "Bright chiptune")
        self.assertFalse(state.get("style_refused"))
        self.assertIn("Audio mood (music + SFX character)",
                      state["applied"])

    def test_change_style_reopens_and_a_new_pick_lands(self):
        interview.respond(self.project, "soft-pastel")  # refused
        state = interview.respond(self.project, "change-style")
        self.assertTrue(state.get("style_reopened"))
        self.assertTrue(state["style_reopen"])
        samples_turns = [t for t in state["turns"]
                         if t.get("kind") == "samples"]
        self.assertEqual(samples_turns[-1]["fields"],
                         [interview.STYLE_FIELD])
        state = interview.respond(self.project, "soft-pastel")
        self.assertFalse(state.get("style_refused"))
        self.assertIn("soft-pastel",
                      self.values()[interview.STYLE_FIELD])
        self.assertIn("#9B8CF2", self.values()["Palette"])
        self.assertEqual(state["style_chosen"], "soft-pastel")
        self.assertFalse(state["style_reopen"])

    def test_change_style_before_any_choice_is_a_polite_noop(self):
        project = make_project(tempfile.mkdtemp())
        interview.start(project)
        state = interview.respond(project, "change-style")
        self.assertFalse(state.get("style_reopened"))
        self.assertEqual(state["applied"], {})
        notes = [t for t in state["turns"] if t.get("kind") == "info"]
        self.assertIn("No style has been chosen yet",
                      notes[-1]["text"])
        values = {f.name: f.value for f in
                  scope.load_scope(project / "SCOPE.md").fields}
        self.assertTrue(all(not v for v in values.values()))


class PromptTidyingTests(unittest.TestCase):
    """0.9.0 (F3): prompt marker captures are tidied only where
    the capture was cut short by the next field marker, and the
    recap never doubles sentence punctuation."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.project = Path(self.tmp) / "tidy-game"
        self.project.mkdir(parents=True)
        (self.project / "SCOPE.md").write_text(template_scope_text(),
                                              encoding="utf-8")

    def values_after_prompt(self, prompt):
        scope.set_original_prompt(self.project / "SCOPE.md", prompt)
        interview.start(self.project)
        return {f.name: f.value for f in
                scope.load_scope(self.project / "SCOPE.md").fields}

    def test_marker_terminated_values_lose_the_sentence_stop(self):
        values = self.values_after_prompt(
            "My game. Title (working): Star Shepherd. "
            "Genre / archetype: Arena collector, Dimension: 2D")
        self.assertEqual(values["Title (working)"], "Star Shepherd")
        self.assertEqual(values["Genre / archetype"], "Arena collector")
        self.assertEqual(values["Dimension"], "2D")

    def test_end_of_prompt_segment_keeps_its_punctuation(self):
        values = self.values_after_prompt(
            "Genre / archetype: Arena collector. "
            "Title (working): Star Shepherd.")
        self.assertEqual(values["Title (working)"], "Star Shepherd.")

    def test_abbreviation_and_double_dot_are_never_stripped(self):
        values = self.values_after_prompt(
            "Title (working): Option A. Genre / archetype: Arena")
        self.assertEqual(values["Title (working)"], "Option A.")
        project = Path(self.tmp) / "tidy-two"
        project.mkdir(parents=True)
        (project / "SCOPE.md").write_text(template_scope_text(),
                                          encoding="utf-8")
        scope.set_original_prompt(
            project / "SCOPE.md",
            "Title (working): Wait.. Genre / archetype: Arena")
        interview.start(project)
        values = {f.name: f.value for f in
                  scope.load_scope(project / "SCOPE.md").fields}
        self.assertEqual(values["Title (working)"], "Wait..")

    def test_recap_does_not_double_sentence_punctuation(self):
        scope.set_original_prompt(
            self.project / "SCOPE.md",
            "One-line pitch: A herding game among the stars.")
        state = interview.start(self.project)
        recap = state["turns"][0]["text"]
        self.assertIn("One-line pitch: A herding game among the "
                      "stars.", recap)
        self.assertNotIn("stars..", recap)
        self.assertNotIn("stars.;", recap)


class PromptFirstTests(unittest.TestCase):
    """The interview consumes the starting prompt BEFORE asking
    anything: deterministic 'Field: value' statements are applied
    without a brain, the opening is a recap, question groups
    cover only the gaps, a fully-covering prompt asks nothing,
    and consumption happens exactly once."""

    SPECIAL = {
        "Dimension": "2D",
        "Scope class": "Micro",
        "Telemetry": "no",
        interview.STYLE_FIELD: "bright-chunky",
        interview.PALETTE_FIELD: "#FF7A1A",
    }

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.project = Path(self.tmp) / "prompt-game"
        self.project.mkdir(parents=True)
        (self.project / "SCOPE.md").write_text(template_scope_text(),
                                               encoding="utf-8")

    def set_prompt(self, text):
        scope.set_original_prompt(self.project / "SCOPE.md", text)

    def prompt_for(self, names):
        parsed = scope.load_scope(self.project / "SCOPE.md")
        values = {f.name: self.SPECIAL.get(f.name,
                                           "Given in the prompt.")
                  for f in parsed.missing_fields if f.name in names}
        return "\n".join(f"{name}: {value}"
                         for name, value in values.items())

    def test_partial_prompt_recaps_and_asks_only_gaps(self):
        filled = {"Title (working)", "One-line pitch", "Dimension"}
        self.set_prompt(self.prompt_for(filled))
        state = interview.start(self.project)
        turns = state["turns"]
        recap = turns[0]
        self.assertEqual(recap["kind"], "info")
        self.assertTrue(
            recap["text"].startswith("From your prompt I filled in:"),
            recap["text"])
        # The value was cut from mid-prompt prose ('Given in the
        # prompt. One-line pitch: …'), so its sentence full stop
        # is tidied off (0.9.0 F3) — the recap lists it cleanly.
        self.assertIn("Title (working): Given in the prompt",
                      recap["text"])
        self.assertNotIn("Given in the prompt..", recap["text"])
        self.assertNotIn("Given in the prompt.;", recap["text"])
        self.assertIn("Dimension: 2D", recap["text"])
        asked = [name for turn in turns
                 if turn.get("kind") in ("question", "samples")
                 for name in turn.get("fields", [])]
        self.assertTrue(asked, "gaps should still be asked about")
        for name in filled:
            self.assertNotIn(name, asked)
        by_name = {f["name"]: f for f in state["fields"]}
        for name in filled:
            self.assertTrue(by_name[name]["filled"], name)
        session = json.loads(
            (self.project / interview.SESSION_REL).read_text())
        self.assertTrue(session["prompt_extracted"])

    def test_full_prompt_asks_nothing(self):
        parsed = scope.load_scope(self.project / "SCOPE.md")
        self.set_prompt(self.prompt_for(
            {f.name for f in parsed.missing_fields}))
        state = interview.start(self.project)
        self.assertTrue(state["complete"])
        self.assertEqual(state["missing_count"], 0)
        kinds = [turn.get("kind") for turn in state["turns"]]
        self.assertNotIn("question", kinds)
        self.assertNotIn("samples", kinds)
        recap = state["turns"][0]
        self.assertIn("covers everything", recap["text"])

    def test_prompt_is_consumed_exactly_once(self):
        self.set_prompt(self.prompt_for({"Title (working)"}))
        interview.start(self.project)
        session = json.loads(
            (self.project / interview.SESSION_REL).read_text())
        turn_count = len(session["turns"])
        interview.start(self.project)
        interview.respond(self.project, "")
        session = json.loads(
            (self.project / interview.SESSION_REL).read_text())
        recaps = [t for t in session["turns"]
                  if t.get("text", "").startswith("From your prompt")]
        self.assertEqual(len(recaps), 1)
        self.assertEqual(len(session["turns"]), turn_count)

    def test_no_prompt_behaves_as_before(self):
        state = interview.start(self.project)
        self.assertEqual(state["turns"][0]["text"], interview._OPENING)
        self.assertIn("question",
                      [t.get("kind") for t in state["turns"]])


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

    def test_style_cards_lock_after_the_pick(self):
        """0.9.0 (F5) at the app surface: after a pick, every
        rendering of the samples locks — the chosen card shows
        '✓ Chosen' with a Change style button, no card keeps a
        live Pick button — a stale pick post is refused, and
        Change style re-opens the choice."""
        self.chat("")
        for answer in ANSWERS[:13]:
            self.chat(answer)
        _s, page = self.get(f"{self.base}/project/chat-game/scope")
        self.assertIn(">Pick ", page)  # live pick buttons pre-choice
        self.chat("bright-chunky")
        _s, page = self.get(f"{self.base}/project/chat-game/scope")
        self.assertIn("✓ Chosen", page)
        self.assertIn("Change style", page)
        self.assertNotIn(">Pick ", page)
        # A stale page's pick post is refused; the scope is unmoved.
        self.chat("soft-pastel")
        values = {f.name: f.value for f in
                  scope.load_scope(self.project / "SCOPE.md").fields}
        self.assertIn("bright-chunky", values[interview.STYLE_FIELD])
        _s, page = self.get(f"{self.base}/project/chat-game/scope")
        self.assertIn("already chosen and locked", page)
        # Change style re-opens: pick buttons are live again and a
        # new pick lands.
        self.chat("change-style")
        _s, page = self.get(f"{self.base}/project/chat-game/scope")
        self.assertIn(">Pick ", page)
        self.chat("soft-pastel")
        values = {f.name: f.value for f in
                  scope.load_scope(self.project / "SCOPE.md").fields}
        self.assertIn("soft-pastel", values[interview.STYLE_FIELD])
        _s, page = self.get(f"{self.base}/project/chat-game/scope")
        self.assertIn("✓ Chosen", page)
        self.assertNotIn(">Pick ", page)

    def test_scope_panel_specified_missing_chips(self):
        """Every field row carries an unambiguous status chip:
        '✓ Specified' (green) when filled, 'Missing' (warn) when
        not — in the interview panel and in the form view — and
        the panel summary uses the Specified X of Y framing."""
        self.chat("")  # Begin
        self.chat("Chip Game")  # fills Title (working)
        _s, page = self.get(f"{self.base}/project/chat-game/scope")
        self.assertIn("Specified 1 of", page)
        self.assertIn("missing</p>", page)
        self.assertIn(
            '<span class="chip green specchip">✓ Specified</span>',
            page)
        self.assertIn('<span class="chip warn specchip">Missing</span>',
                      page)
        _s, form_page = self.get(
            f"{self.base}/project/chat-game/scope/form")
        self.assertIn(
            '<span class="chip green specchip">✓ Specified</span>',
            form_page)
        self.assertIn('<span class="chip warn specchip">Missing</span>',
                      form_page)

    def test_long_transcript_page_is_height_bounded(self):
        """The chat layout contract: however long the transcript
        gets, the interview page must fit the viewport — thread in
        its own scroll box, composer pinned in the chat column,
        scope panel scrolling independently. Structural: seeds a
        ~40-turn session and asserts on the served HTML + CSS."""
        import re
        turns = []
        for i in range(20):
            turns.append({"role": "assistant",
                          "text": f"Question turn {i + 1} of twenty."})
            turns.append({"role": "user",
                          "text": f"Answer turn {i + 1} of twenty."})
        session = {"version": 1, "turns": turns, "pending_fields": [],
                   "prompt_extracted": False, "samples": None}
        session_file = self.project / interview.SESSION_REL
        session_file.parent.mkdir(parents=True, exist_ok=True)
        session_file.write_text(json.dumps(session), encoding="utf-8")

        _s, page = self.get(f"{self.base}/project/chat-game/scope")
        # All 40 turns render, oldest and latest alike.
        self.assertIn("Question turn 1 of twenty.", page)
        self.assertIn("Answer turn 20 of twenty.", page)
        # The page opts into the fixed-viewport shell, the thread
        # is the named scroll container, the composer sits inside
        # the chat column after the thread, the panel is separate.
        self.assertIn('class="app fitview"', page)
        self.assertIn('<div class="thread" id="chat-thread">', page)
        self.assertIn('class="stack action composer"', page)
        self.assertLess(page.index('id="chat-thread"'),
                        page.index('class="stack action composer"'))
        self.assertLess(page.index('class="stack action composer"'),
                        page.index('class="card scopepanel"'))
        # The CSS contract behind those classes.
        _s, css = self.get(f"{self.base}/style.css")

        def rule(selector):
            match = re.search(re.escape(selector) + r"\s*\{([^}]*)\}",
                              css)
            self.assertIsNotNone(match, f"no CSS rule for {selector}")
            return match.group(1)

        self.assertIn("height:100vh", rule(".app.fitview"))
        main_rule = rule(".app.fitview .main")
        self.assertIn("flex-direction:column", main_rule)
        self.assertIn("overflow:hidden", main_rule)
        wrap_rule = rule(".app.fitview .main > .chatwrap")
        self.assertIn("min-height:0", wrap_rule)
        thread_rule = rule(".chatthread .thread")
        self.assertIn("overflow-y:auto", thread_rule)
        self.assertIn("min-height:0", thread_rule)
        self.assertIn("flex:none", rule(".chatthread form.composer"))
        panel_rule = rule(".app.fitview .scopepanel")
        self.assertIn("overflow-y:auto", panel_rule)
        # Other pages do NOT opt in (the project page is unscoped).
        _s, project_page = self.get(f"{self.base}/project/chat-game")
        self.assertNotIn("fitview", project_page)
        # app.js pins the thread to its latest message on load.
        _s, js = self.get(f"{self.base}/app.js")
        self.assertIn('getElementById("chat-thread")', js)
        self.assertIn("scrollTop", js)


if __name__ == "__main__":
    unittest.main()
