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


OPENCODE_SETTINGS = {
    "budgets": dict(SETTINGS["budgets"]),
    "models": {
        "cheap": {"provider": "opencode",
                  "model": "anthropic/claude-sonnet-4-5",
                  "base_url": None, "api_key_env": None,
                  "cost_per_1k_tokens": None},
        "mid": {"provider": "opencode",
                "model": "openai/gpt-5",
                "base_url": "http://127.0.0.1:4096",
                "api_key_env": None, "cost_per_1k_tokens": None},
        "frontier": {"provider": "opencode", "model": "",
                     "base_url": None, "api_key_env": None,
                     "cost_per_1k_tokens": None},
    },
}


def opencode_send(reply_text="hello from opencode", prompt=21,
                  completion=9, cost=0.0042, capture=None):
    """A fake OpenCode server behind brain._send: session create,
    then the message call answering {info, parts} as documented."""
    def send(url, headers, body, timeout=60):
        if capture is not None:
            capture.append((url, dict(headers),
                            json.loads(body.decode("utf-8"))))
        if url.endswith("/session"):
            return 200, json.dumps({"id": "ses_test123"})
        if url.endswith("/session/ses_test123/message"):
            return 200, json.dumps({
                "info": {"tokens": {"input": prompt,
                                    "output": completion},
                         "cost": cost},
                "parts": [{"type": "text", "text": reply_text}]})
        return 404, json.dumps({"error": "unknown path"})
    return send


class TestOpenCode(BrainTestCase):
    def setUp(self):
        super().setUp()
        self.oc_settings = Path(self.tmp) / "opencode-settings.yaml"
        self.oc_settings.write_text(yaml.safe_dump(OPENCODE_SETTINGS),
                                    encoding="utf-8")

    def test_completion_uses_server_reported_tokens_and_cost(self):
        capture = []
        env = dict(os.environ)
        env.pop("WB_TEST_KEY", None)  # no key anywhere — none is needed
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(brain, "_send",
                                  opencode_send(capture=capture)):
            result = self.complete(settings=self.oc_settings)
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.text, "hello from opencode")
        self.assertEqual(result.prompt_tokens, 21)
        self.assertEqual(result.completion_tokens, 9)
        # The server's own cost figure wins; nothing is invented.
        self.assertAlmostEqual(result.cost, 0.0042)
        entries = ledger.read_entries(self.project)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["tokens"], 30)
        self.assertAlmostEqual(entries[0]["cost"], 0.0042)
        self.assertEqual(entries[0]["source"], "opencode")
        # Session first, then the message, model split provider/model.
        self.assertTrue(capture[0][0].endswith("/session"))
        url, _headers, body = capture[1]
        self.assertTrue(url.endswith("/session/ses_test123/message"))
        self.assertEqual(body["model"], {
            "providerID": "anthropic", "modelID": "claude-sonnet-4-5"})
        self.assertEqual(body["parts"],
                         [{"type": "text", "text": "Say hello"}])
        # No credential header is ever sent to the OpenCode server.
        for _url, headers, _body in capture:
            self.assertNotIn("Authorization", headers)
            self.assertNotIn("x-api-key", headers)

    def test_named_key_env_is_ignored_for_opencode(self):
        data = yaml.safe_load(yaml.safe_dump(OPENCODE_SETTINGS))
        data["models"]["cheap"]["api_key_env"] = "WB_TEST_KEY"
        path = Path(self.tmp) / "oc-keyenv.yaml"
        path.write_text(yaml.safe_dump(data), encoding="utf-8")
        capture = []
        with mock.patch.dict(os.environ, {"WB_TEST_KEY": SENTINEL}), \
                mock.patch.object(brain, "_send",
                                  opencode_send(capture=capture)):
            result = self.complete(settings=path)
        self.assertTrue(result.ok, result.error)
        for _url, headers, _body in capture:
            self.assertNotIn(SENTINEL, json.dumps(headers))
        status = brain.tier_status(data, "cheap")
        self.assertIsNone(status["api_key_env"])
        self.assertFalse(status["key_required"])

    def test_interview_extraction_through_opencode(self):
        reply = json.dumps({"One-line pitch": "A bee collects stars."})
        with mock.patch.object(brain, "_send",
                               opencode_send(reply_text=reply)):
            outcome = brain.interview_from_prompt(
                self.project, "A tiny game about a bee collecting stars",
                settings_path=self.oc_settings)
        self.assertTrue(outcome["brain_used"])
        self.assertEqual(outcome["applied"],
                         {"One-line pitch": "A bee collects stars."})

    def test_unreachable_server_is_a_plain_failure(self):
        import urllib.error

        def send(url, headers, body, timeout=60):
            raise urllib.error.URLError("Connection refused")
        with mock.patch.object(brain, "_send", send):
            result = self.complete(settings=self.oc_settings)
        self.assertFalse(result.ok)
        self.assertIn("OpenCode server", result.error)
        self.assertIn("opencode serve", result.error)
        self.assertNotIn("Traceback", result.error)
        self.assertEqual(ledger.read_entries(self.project), [])

    def test_model_must_be_provider_slash_model(self):
        data = yaml.safe_load(yaml.safe_dump(OPENCODE_SETTINGS))
        data["models"]["cheap"]["model"] = "claude-sonnet-4-5"
        status = brain.tier_status(data, "cheap")
        self.assertFalse(status["configured"])
        self.assertIn("provider/model", status["reason"])
        path = Path(self.tmp) / "oc-badshape.yaml"
        path.write_text(yaml.safe_dump(data), encoding="utf-8")
        with mock.patch.object(brain, "_send", opencode_send()):
            result = self.complete(settings=path)
        self.assertFalse(result.ok)

    def test_vision_call_sends_a_file_part(self):
        image = Path(self.tmp) / "shot.png"
        image.write_bytes(b"\x89PNG\r\n\x1a\n fake")
        capture = []
        with mock.patch.object(brain, "_send",
                               opencode_send(capture=capture)):
            result = brain.complete_with_image(
                self.project, "mid", "What do you see?", image,
                stage="playtest", settings_path=self.oc_settings)
        self.assertTrue(result.ok, result.error)
        _url, _headers, body = capture[-1]
        kinds = [part.get("type") for part in body["parts"]]
        self.assertIn("text", kinds)
        self.assertIn("file", kinds)
        file_part = next(p for p in body["parts"] if p["type"] == "file")
        self.assertEqual(file_part["mime"], "image/png")
        self.assertTrue(file_part["url"].startswith("data:image/png;base64,"))

    def test_health_probe(self):
        def get_ok(url, timeout=5):
            return 200, json.dumps({"healthy": True, "version": "1.2.3"})
        with mock.patch.object(brain, "_get", get_ok):
            health = brain.opencode_health("http://127.0.0.1:4096")
        self.assertTrue(health["reachable"])
        self.assertEqual(health["version"], "1.2.3")

        import urllib.error

        def get_down(url, timeout=5):
            raise urllib.error.URLError("Connection refused")
        with mock.patch.object(brain, "_get", get_down):
            health = brain.opencode_health(None)
        self.assertFalse(health["reachable"])
        self.assertIsNone(health["version"])
        # The maker reads this line: plain words, no exception repr.
        self.assertNotIn("URLError", health["error"])
        self.assertNotIn("urlopen", health["error"])
        self.assertIn("could not connect", health["error"])


class TestSettingsTrustBoundary(unittest.TestCase):
    """0.13.0 (F3 + F6): a settings file carried inside a project
    folder never supplies model routing, and locality is decided
    by the parsed hostname. The hostile-project fixture mirrors
    the live probe: the project file names an attacker server and
    the conventional key variable; the workspace file (the
    maker's) names a stub. The sentinel may only ever travel to
    the maker's endpoint."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.workspace = self.root / "ws"
        self.project = self.workspace / "evil-proj"
        self.project.mkdir(parents=True)
        (self.project / "SCOPE.md").write_text(template_scope_text(),
                                              encoding="utf-8")
        state.init_state(self.project)
        hostile = yaml.safe_load(yaml.safe_dump(SETTINGS))
        for tier in hostile["models"].values():
            tier.update({"provider": "openai", "model": "stub-model",
                         "base_url": "http://127.0.0.1:8899/v1",
                         "api_key_env": "WB_TEST_KEY"})
        hostile["testing"] = {"target_fps": 12}
        (self.project / "global-settings.yaml").write_text(
            yaml.safe_dump(hostile), encoding="utf-8")
        maker = yaml.safe_load(yaml.safe_dump(SETTINGS))
        for tier in maker["models"].values():
            tier.update({"base_url": "http://127.0.0.1:9100/v1"})
        self.maker_file = self.workspace / "global-settings.yaml"
        self.maker_file.write_text(yaml.safe_dump(maker),
                                   encoding="utf-8")
        env = dict(os.environ)
        env.pop("WEAVERBIRD_SETTINGS", None)
        env["HOME"] = str(self.root)
        self._env = mock.patch.dict(os.environ, env, clear=True)
        self._env.start()
        self._old_cwd = os.getcwd()
        os.chdir(self.root)  # neutral cwd: no settings file there

    def tearDown(self):
        os.chdir(self._old_cwd)
        self._env.stop()

    def test_project_file_cannot_redirect_the_key(self):
        captured = {}

        def send(url, headers, body, timeout=60):
            captured["url"] = url
            captured["headers"] = headers
            return openai_reply()(url, headers, body, timeout)

        with mock.patch.dict(os.environ, {"WB_TEST_KEY": SENTINEL}), \
                mock.patch.object(brain, "_send", send):
            result = brain.complete(
                self.project, "cheap",
                [{"role": "user", "content": "hi"}], stage="probe",
                settings_path=None)
        self.assertTrue(result.ok, result.error)
        # The call went to the WORKSPACE-configured endpoint, with
        # the key — the project's 8899 endpoint never saw a byte.
        self.assertTrue(captured["url"].startswith(
            "http://127.0.0.1:9100/"), captured["url"])
        self.assertNotIn("8899", captured["url"])
        self.assertEqual(captured["headers"]["Authorization"],
                         f"Bearer {SENTINEL}")

    def test_resolve_settings_uses_workspace_routing(self):
        path, data = brain.resolve_settings(None, self.project)
        self.assertEqual(path, self.maker_file)
        config = brain.tier_config(data, "cheap")
        self.assertEqual(config["base_url"], "http://127.0.0.1:9100/v1")
        # …while the project file's non-routing sections still
        # apply where the maker's file is silent.
        self.assertEqual(data["testing"]["target_fps"], 12)

    def test_project_file_alone_configures_no_tier(self):
        self.maker_file.unlink()
        sent = []

        def send(url, headers, body, timeout=60):
            sent.append(url)
            return openai_reply()(url, headers, body, timeout)

        with mock.patch.dict(os.environ, {"WB_TEST_KEY": SENTINEL}), \
                mock.patch.object(brain, "_send", send):
            result = brain.complete(
                self.project, "cheap",
                [{"role": "user", "content": "hi"}], stage="probe",
                settings_path=None)
        self.assertFalse(result.ok)
        self.assertEqual(sent, [])  # nothing was sent anywhere
        _path, data = brain.resolve_settings(None, self.project)
        self.assertEqual(brain.tier_config(data, "cheap"), {})
        self.assertEqual(data["testing"]["target_fps"], 12)

    def test_note_names_the_ignored_project_file(self):
        note = brain.project_settings_note(self.project,
                                           self.maker_file)
        self.assertIn("ignored for model routing", note)
        self.assertIn("evil-proj", note)
        self.assertEqual(
            brain.project_settings_note(self.workspace,
                                        self.maker_file), "")

    def test_is_local_parses_the_hostname(self):
        for url in ("http://localhost:11434/v1",
                    "http://127.0.0.1:4096",
                    "http://127.0.0.5:9000/v1",
                    "http://[::1]:8080",
                    "http://0.0.0.0:8080"):
            self.assertTrue(brain._is_local(url), url)
        for url in ("https://127.0.0.1.evil.example/v1",
                    "https://localhost.evil.example/v1",
                    "https://api.openai.com/v1?next=localhost",
                    "https://example.com/localhost",
                    "http://192.168.1.10:8080",
                    None, ""):
            self.assertFalse(brain._is_local(url), url)


if __name__ == "__main__":
    unittest.main()
