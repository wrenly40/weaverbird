"""Tests for the bring-your-own-key brain (brain.py).

The provider HTTP call is replaced through brain._send — the one
transport seam — so no test touches the network. The sentinel-key
test is the security contract: a key value may appear in the
Authorization header of the request and nowhere else — never in the
ledger, the state file, the scope file, a report, or a result.
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import template_scope_text  # noqa: E402

import yaml  # noqa: E402

from weaverbird import brain, finish, ledger, scope, state  # noqa: E402

SENTINEL = "sk-test-SENTINEL-do-not-leak-12345"

SETTINGS = {
    "budgets": {
        "tokens_per_run": 100000,
        "tokens_per_game": 100000,
        "cost_ceiling_per_month": None,
        "max_build_iterations": 40,
        "warn_at_percent_of_cap": 80,
    },
    "models": {
        "cheap": {"provider": "openai", "model": "cheap-model",
                  "base_url": None, "api_key_env": "WB_TEST_KEY",
                  "cost_per_1k_tokens": None},
        "mid": {"provider": "openai", "model": "mid-model",
                "base_url": None, "api_key_env": "WB_TEST_KEY",
                "cost_per_1k_tokens": None},
        "frontier": {"provider": "anthropic", "model": "frontier-model",
                     "base_url": None, "api_key_env": "WB_TEST_AKEY",
                     "cost_per_1k_tokens": 2.0},
    },
}


def openai_reply(text="hello", prompt=11, completion=7):
    def send(url, headers, body, timeout=60):
        return 200, json.dumps({
            "choices": [{"message": {"content": text}}],
            "usage": {"prompt_tokens": prompt, "completion_tokens": completion},
        })
    return send


def anthropic_reply(text="hi", prompt=5, completion=3):
    def send(url, headers, body, timeout=60):
        return 200, json.dumps({
            "content": [{"type": "text", "text": text}],
            "usage": {"input_tokens": prompt, "output_tokens": completion},
        })
    return send


class BrainTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.project = Path(self.tmp) / "game1"
        self.project.mkdir()
        (self.project / "SCOPE.md").write_text(template_scope_text(),
                                               encoding="utf-8")
        state.init_state(self.project)
        self.settings_path = Path(self.tmp) / "global-settings.yaml"
        self.settings_path.write_text(yaml.safe_dump(SETTINGS),
                                      encoding="utf-8")

    def complete(self, tier="cheap", settings=None, **kwargs):
        return brain.complete(
            self.project, tier,
            [{"role": "user", "content": "Say hello"}],
            stage="test-stage",
            settings_path=settings or self.settings_path, **kwargs)


class TestComplete(BrainTestCase):
    def test_openai_call_records_real_tokens_in_ledger(self):
        with mock.patch.dict(os.environ, {"WB_TEST_KEY": "k"}), \
                mock.patch.object(brain, "_send", openai_reply()):
            result = self.complete()
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.text, "hello")
        self.assertEqual(result.prompt_tokens, 11)
        self.assertEqual(result.completion_tokens, 7)
        self.assertEqual(result.total_tokens, 18)
        self.assertEqual(result.cost, 0.0)  # no price configured
        entries = ledger.read_entries(self.project)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["tokens"], 18)
        self.assertEqual(entries[0]["stage"], "test-stage")
        self.assertEqual(entries[0]["tier"], "cheap")

    def test_anthropic_call_parses_usage_and_prices_it(self):
        with mock.patch.dict(os.environ, {"WB_TEST_AKEY": "k"}), \
                mock.patch.object(brain, "_send", anthropic_reply()):
            result = self.complete(tier="frontier")
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.total_tokens, 8)
        # 8 tokens at 2.0 per 1k = 0.016
        self.assertAlmostEqual(result.cost, 0.016)
        entries = ledger.read_entries(self.project)
        self.assertEqual(entries[0]["tier"], "frontier")
        self.assertAlmostEqual(entries[0]["cost"], 0.016)

    def test_anthropic_system_message_is_split_out(self):
        captured = {}

        def send(url, headers, body, timeout=60):
            captured["url"] = url
            captured["headers"] = headers
            captured["body"] = json.loads(body.decode("utf-8"))
            return anthropic_reply()(url, headers, body, timeout)

        with mock.patch.dict(os.environ, {"WB_TEST_AKEY": "k"}), \
                mock.patch.object(brain, "_send", send):
            result = brain.complete(
                self.project, "frontier",
                [{"role": "system", "content": "Be terse"},
                 {"role": "user", "content": "Hi"}],
                stage="test-stage", settings_path=self.settings_path)
        self.assertTrue(result.ok, result.error)
        self.assertEqual(captured["body"]["system"], "Be terse")
        self.assertEqual(captured["body"]["messages"],
                         [{"role": "user", "content": "Hi"}])
        self.assertEqual(captured["headers"]["anthropic-version"],
                         "2023-06-01")

    def test_local_base_url_needs_no_key(self):
        data = yaml.safe_load(yaml.safe_dump(SETTINGS))
        data["models"]["cheap"] = {
            "provider": "openai", "model": "local-model",
            "base_url": "http://localhost:11434/v1",
            "api_key_env": "WB_TEST_KEY", "cost_per_1k_tokens": None}
        local_settings = Path(self.tmp) / "local-settings.yaml"
        local_settings.write_text(yaml.safe_dump(data), encoding="utf-8")
        env = dict(os.environ)
        env.pop("WB_TEST_KEY", None)
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(brain, "_send", openai_reply()):
            result = self.complete(settings=local_settings)
        self.assertTrue(result.ok, result.error)


class TestUnavailable(BrainTestCase):
    def test_no_settings_file_is_a_plain_refusal(self):
        # Force total absence: an isolated tree with no settings file
        # in the project, its parent, the cwd or HOME.
        isolated = Path(tempfile.mkdtemp())
        project = isolated / "game1"
        project.mkdir()
        (project / "SCOPE.md").write_text(template_scope_text(),
                                          encoding="utf-8")
        state.init_state(project)
        env = dict(os.environ)
        env.pop("WEAVERBIRD_SETTINGS", None)
        env["HOME"] = str(isolated)
        old_cwd = os.getcwd()
        os.chdir(isolated)
        try:
            with mock.patch.dict(os.environ, env, clear=True):
                result = brain.complete(
                    project, "cheap",
                    [{"role": "user", "content": "hi"}], stage="s",
                    settings_path=isolated / "missing.yaml")
        finally:
            os.chdir(old_cwd)
        self.assertFalse(result.ok)
        self.assertIn("No global settings file", result.error)
        self.assertEqual(ledger.read_entries(project), [])

    def test_unconfigured_tier(self):
        data = yaml.safe_load(yaml.safe_dump(SETTINGS))
        data["models"]["cheap"]["model"] = ""
        path = Path(self.tmp) / "unconfigured.yaml"
        path.write_text(yaml.safe_dump(data), encoding="utf-8")
        with mock.patch.dict(os.environ, {"WB_TEST_KEY": "k"}):
            result = self.complete(settings=path)
        self.assertFalse(result.ok)
        self.assertIn("not usable", result.error)

    def test_missing_key(self):
        env = dict(os.environ)
        env.pop("WB_TEST_KEY", None)
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(brain, "_send", openai_reply()):
            result = self.complete()
        self.assertFalse(result.ok)
        self.assertIn("WB_TEST_KEY", result.error)
        self.assertEqual(ledger.read_entries(self.project), [])

    def test_http_error_is_a_plain_failure(self):
        def send(url, headers, body, timeout=60):
            return 500, "server error"
        with mock.patch.dict(os.environ, {"WB_TEST_KEY": "k"}), \
                mock.patch.object(brain, "_send", send):
            result = self.complete()
        self.assertFalse(result.ok)
        self.assertIn("HTTP 500", result.error)
        self.assertEqual(ledger.read_entries(self.project), [])


class TestBudgets(BrainTestCase):
    def _settings_with_cap(self, cap):
        data = yaml.safe_load(yaml.safe_dump(SETTINGS))
        data["budgets"]["tokens_per_game"] = cap
        path = Path(self.tmp) / f"cap-{cap}.yaml"
        path.write_text(yaml.safe_dump(data), encoding="utf-8")
        return path

    def test_refused_at_cap_before_any_call(self):
        path = self._settings_with_cap(100)
        ledger.add_entry(self.project, "earlier", "cheap", 100, 0.0)

        def send(url, headers, body, timeout=60):
            raise AssertionError("transport must never be called")

        with mock.patch.dict(os.environ, {"WB_TEST_KEY": "k"}), \
                mock.patch.object(brain, "_send", send):
            with self.assertRaises(brain.BrainBudgetError):
                self.complete(settings=path)

    def test_crossing_the_cap_warns_loudly(self):
        path = self._settings_with_cap(15)
        with mock.patch.dict(os.environ, {"WB_TEST_KEY": "k"}), \
                mock.patch.object(brain, "_send", openai_reply()):
            result = self.complete(settings=path)
        self.assertTrue(result.ok, result.error)
        self.assertIn("WARNING", result.warning)
        self.assertIn("cap", result.warning)


class TestInterview(BrainTestCase):
    def test_extraction_fills_only_empty_fields(self):
        scope.set_field_value(self.project / "SCOPE.md",
                              "Title (working)", "Human Title")
        reply = json.dumps({
            "Title (working)": "Model Title",
            "One-line pitch": "A bee collects stars.",
            "Dimension": "",
        })

        def send(url, headers, body, timeout=60):
            return 200, json.dumps({
                "choices": [{"message": {"content": reply}}],
                "usage": {"prompt_tokens": 40, "completion_tokens": 20}})

        with mock.patch.dict(os.environ, {"WB_TEST_KEY": "k"}), \
                mock.patch.object(brain, "_send", send):
            outcome = brain.interview_from_prompt(
                self.project, "A tiny game about a bee collecting stars",
                settings_path=self.settings_path)
        self.assertTrue(outcome["brain_used"])
        self.assertEqual(outcome["applied"],
                         {"One-line pitch": "A bee collects stars."})
        parsed = scope.load_scope(self.project / "SCOPE.md")
        values = {f.name: f.value for f in parsed.fields}
        self.assertEqual(values["Title (working)"], "Human Title")
        self.assertEqual(values["One-line pitch"], "A bee collects stars.")
        self.assertIn("bee collecting stars", parsed.original_prompt)
        entries = ledger.read_entries(self.project)
        self.assertEqual(entries[-1]["stage"], "scope")
        self.assertEqual(entries[-1]["tokens"], 60)

    def test_degraded_mode_without_a_brain(self):
        env = dict(os.environ)
        env.pop("WB_TEST_KEY", None)
        with mock.patch.dict(os.environ, env, clear=True):
            outcome = brain.interview_from_prompt(
                self.project, "A game about soup",
                settings_path=self.settings_path)
        self.assertFalse(outcome["brain_used"])
        self.assertEqual(outcome["applied"], {})
        self.assertIn("No model is configured", outcome["note"])
        self.assertTrue(outcome["questions"])
        parsed = scope.load_scope(self.project / "SCOPE.md")
        self.assertIn("soup", parsed.original_prompt)


class TestKeyNeverLeaks(BrainTestCase):
    def test_sentinel_key_appears_nowhere_on_disk_or_in_results(self):
        reply = json.dumps({"One-line pitch": "A bee collects stars."})

        def send(url, headers, body, timeout=60):
            # The key is allowed exactly here: the request header.
            if url.endswith("/v1/messages"):  # anthropic
                self.assertEqual(headers.get("x-api-key"), SENTINEL)
                return 200, json.dumps({
                    "content": [{"type": "text", "text": "A summary."}],
                    "usage": {"input_tokens": 9, "output_tokens": 4}})
            self.assertEqual(headers.get("Authorization"),
                             f"Bearer {SENTINEL}")
            return 200, json.dumps({
                "choices": [{"message": {"content": reply}}],
                "usage": {"prompt_tokens": 9, "completion_tokens": 4}})

        env = {"WB_TEST_KEY": SENTINEL, "WB_TEST_AKEY": SENTINEL}
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(brain, "_send", send):
            result = self.complete()
            outcome = brain.interview_from_prompt(
                self.project, "A bee game", settings_path=self.settings_path)
            report_path = finish.build_finish_report(
                self.project, settings_path=self.settings_path)
        self.assertTrue(result.ok, result.error)
        haystacks = {
            "ledger": (self.project / ".weaverbird" / "ledger.jsonl")
            .read_text(encoding="utf-8"),
            "state": (self.project / ".weaverbird" / "state.json")
            .read_text(encoding="utf-8"),
            "scope": (self.project / "SCOPE.md").read_text(encoding="utf-8"),
            "report": report_path.read_text(encoding="utf-8"),
            "result repr": repr(result) + repr(outcome),
        }
        for name, text in haystacks.items():
            self.assertNotIn(SENTINEL, text, f"key leaked into {name}")


class TestBrainStatus(BrainTestCase):
    def test_status_reports_presence_never_value(self):
        with mock.patch.dict(os.environ, {"WB_TEST_KEY": SENTINEL}):
            status = brain.tier_status(SETTINGS, "cheap")
        self.assertTrue(status["configured"])
        self.assertTrue(status["key_present"])
        self.assertTrue(status["usable"])
        self.assertNotIn(SENTINEL, repr(status))
        self.assertEqual(status["api_key_env"], "WB_TEST_KEY")


if __name__ == "__main__":
    unittest.main()
