"""Tests for the feedback store, fetch sources and triage
(feedback.py): dedupe, the deterministic classifier, clustering
and ranking, the brain refinement seam, and status changes.
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import filled_scope_text  # noqa: E402,F401  (sets sys.path)

from weaverbird import brain, feedback  # noqa: E402


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.project = Path(self.tmp)

    def test_add_and_dedupe(self):
        item, created = feedback.add_item(
            self.project, source="manual", text="Great game!")
        self.assertTrue(created)
        again, created_again = feedback.add_item(
            self.project, source="manual", text="Great game!")
        self.assertFalse(created_again)
        self.assertEqual(item["id"], again["id"])
        store = feedback.load_store(self.project)
        self.assertEqual(len(store["items"]), 1)
        self.assertEqual(store["items"][0]["status"], "new")
        self.assertEqual(store["items"][0]["category"], "unknown")

    def test_inbox_and_fetch_report(self):
        inbox = self.project / "feedback-inbox"
        inbox.mkdir()
        (inbox / "one.txt").write_text(
            "The door never opens\nI stood at the exit and nothing "
            "happened.\n", encoding="utf-8")
        report = feedback.fetch_feedback(self.project)
        self.assertEqual(report["inbox"]["added"], 1)
        self.assertEqual(report["itch"]["added"], 0)  # no key: skipped
        self.assertEqual(report["github"]["added"], 0)  # no remote
        store = feedback.load_store(self.project)
        self.assertEqual(len(store["items"]), 1)
        self.assertIn("door never opens", store["items"][0]["text"])
        # A second fetch adds nothing new (dedupe by filename).
        report2 = feedback.fetch_feedback(self.project)
        self.assertEqual(report2["inbox"]["added"], 0)


class TriageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.project = Path(self.tmp)

    def _add(self, text):
        item, _ = feedback.add_item(self.project, source="manual", text=text)
        return item

    def test_deterministic_categories(self):
        cases = {
            "It crashes every time I reach level 2": "bug",
            "The hazards are way too fast, it feels unfair": "balance",
            "How do I open the door? I am so confused": "confusion",
            "Please add a hardcore mode, would love it": "request",
            "Love it, great fun!": "praise",
        }
        for text in cases:
            self._add(text)
        result = feedback.triage(self.project, use_brain=False)
        by_text = {i["text"]: i["category"] for i in result["items"]}
        for text, expected in cases.items():
            self.assertEqual(by_text[text], expected, text)
        for item in result["items"]:
            self.assertEqual(item["status"], "triaged")
            self.assertTrue(item["cluster"])

    def test_natural_balance_language(self):
        # Dogfood finding D7: the parent's real words. Players
        # write "wall", "ran out", "much harder" — not "too hard".
        fixture = ("Round 2 is a wall. I finished round 1 in "
                   "seconds, then in round 2 I could not find the "
                   "last pups and the timer ran out. Feels much "
                   "harder than round 1.")
        category, _severity = feedback.classify_text(fixture)
        self.assertEqual(category, "balance")

    def test_balance_corpus_and_negative_controls(self):
        balance_texts = [
            "Honestly the difficulty spike in level 4 feels unfair.",
            "I keep running out of time on the clock in round 3.",
            "The second boss got way tougher than the first one.",
        ]
        for text in balance_texts:
            self.assertEqual(feedback.classify_text(text)[0],
                             "balance", text)
        controls = {
            # A crash at a spike trap is a bug, not a "spike".
            "The game crashes on the spike trap in round 2 and "
            "freezes.": "bug",
            # A comparative inside praise stays praise.
            "The harder levels were great fun — loved every "
            "minute of it.": "praise",
            # A comparative inside a request stays a request.
            "Could you add an easier mode for kids?": "request",
            # A plain where-is question stays confusion.
            "Where do I find the last pup? The map is not "
            "clear.": "confusion",
        }
        for text, expected in controls.items():
            self.assertEqual(feedback.classify_text(text)[0],
                             expected, text)

    def test_ranking_mentions_times_severity(self):
        self._add("The timer is too fast")
        self._add("Timer too fast, please slow it")
        self._add("Timer runs out too fast, unfair")
        self._add("Nice music")
        result = feedback.triage(self.project, use_brain=False)
        top = result["items"][0]
        self.assertIn("timer", top["cluster"])
        self.assertGreaterEqual(top["score"], 6)  # 3 mentions x sev 2
        praise = result["items"][-1]
        self.assertEqual(praise["category"], "praise")

    def test_brain_refinement_validated(self):
        self._add("Game is broken on the second screen")
        settings = Path(self.tmp) / "global-settings.yaml"
        settings.write_text(
            "budgets:\n  tokens_per_run: 1000\n  tokens_per_game: 100000\n"
            "models:\n  cheap: { provider: openai, model: cheap-x, "
            "base_url: null, api_key_env: WB_FB_KEY, "
            "cost_per_1k_tokens: null }\n", encoding="utf-8")

        def send(url, headers, body, timeout=60):
            payload = {"items": {"fb-001": {"category": "bug",
                                            "cluster": "second screen"}}}
            return 200, json.dumps({
                "choices": [{"message": {"content": json.dumps(payload)}}],
                "usage": {"prompt_tokens": 9, "completion_tokens": 4}})

        with mock.patch.dict("os.environ", {"WB_FB_KEY": "test-key"}), \
                mock.patch.object(brain, "_send", send):
            result = feedback.triage(self.project, settings_path=settings)
        item = result["items"][0]
        self.assertEqual(item["category"], "bug")
        self.assertEqual(item["cluster"], "second screen")
        self.assertIn("refined", result["note"])

    def test_brain_garbage_keeps_deterministic(self):
        self._add("It crashes on start")
        settings = Path(self.tmp) / "global-settings.yaml"
        settings.write_text(
            "budgets:\n  tokens_per_run: 1000\n  tokens_per_game: 100000\n"
            "models:\n  cheap: { provider: openai, model: cheap-x, "
            "base_url: null, api_key_env: WB_FB_KEY, "
            "cost_per_1k_tokens: null }\n", encoding="utf-8")

        def send(url, headers, body, timeout=60):
            return 200, json.dumps({
                "choices": [{"message": {"content": "not json at all"}}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 2}})

        with mock.patch.dict("os.environ", {"WB_FB_KEY": "test-key"}), \
                mock.patch.object(brain, "_send", send):
            result = feedback.triage(self.project, settings_path=settings)
        self.assertEqual(result["items"][0]["category"], "bug")

    def test_set_status_validation(self):
        item = self._add("Something")
        updated = feedback.set_status(self.project, item["id"], "approved")
        self.assertEqual(updated["status"], "approved")
        with self.assertRaises(feedback.FeedbackError):
            feedback.set_status(self.project, item["id"], "shipped")
        with self.assertRaises(feedback.FeedbackError):
            feedback.set_status(self.project, "fb-999", "approved")


if __name__ == "__main__":
    unittest.main()
