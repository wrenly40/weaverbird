import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import *  # noqa: F401,F403,E402

from weaverbird import ledger  # noqa: E402

SETTINGS = {"budgets": {"tokens_per_run": 500, "tokens_per_game": 1000,
                         "warn_at_percent_of_cap": 80}}


class LedgerTests(unittest.TestCase):
    def test_add_and_summarise(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger.add_entry(tmp, "build", "mid", 100, 0.25)
            ledger.add_entry(tmp, "build", "cheap", 50, 0.05)
            ledger.add_entry(tmp, "playtest", "frontier", 200, 1.00)
            summary = ledger.summarise(tmp)
            self.assertEqual(summary["entries"], 3)
            self.assertEqual(summary["tokens"], 350)
            self.assertAlmostEqual(summary["cost"], 1.30)
            self.assertEqual(summary["by_stage"]["build"]["tokens"], 150)
            self.assertEqual(summary["by_tier"]["frontier"]["tokens"], 200)

    def test_bad_tier_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ledger.LedgerError):
                ledger.add_entry(tmp, "build", "huge", 10, 0.1)

    def test_empty_ledger_summarises_to_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            summary = ledger.summarise(tmp)
            self.assertEqual(summary["entries"], 0)
            self.assertEqual(summary["tokens"], 0)

    def test_budget_ok(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger.add_entry(tmp, "build", "mid", 100, 0.1)
            result = ledger.check_budgets(tmp, SETTINGS)
            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["tokens_cap"], 1000)

    def test_budget_warn_at_threshold(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger.add_entry(tmp, "build", "mid", 850, 0.1)  # 85% of 1000, warn at 80%
            result = ledger.check_budgets(tmp, SETTINGS)
            self.assertEqual(result["status"], "warn")

    def test_budget_over(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger.add_entry(tmp, "build", "mid", 1000, 0.1)
            result = ledger.check_budgets(tmp, SETTINGS)
            self.assertEqual(result["status"], "over")

    def test_budget_unknown_without_cap(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger.add_entry(tmp, "build", "mid", 10, 0.1)
            result = ledger.check_budgets(tmp, {"budgets": {}})
            self.assertEqual(result["status"], "unknown")


if __name__ == "__main__":
    unittest.main()
