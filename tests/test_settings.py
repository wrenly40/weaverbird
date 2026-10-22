import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import *  # noqa: F401,F403,E402

from weaverbird import settings, template_path  # noqa: E402


class SettingsTests(unittest.TestCase):
    def test_template_flags_missing_required_budgets(self):
        data = settings.load_settings(template_path("global-settings.yaml"))
        result = settings.validate_settings(data)
        self.assertFalse(result.ok)
        self.assertIn("budgets.tokens_per_run", result.missing_required)
        self.assertIn("budgets.tokens_per_game", result.missing_required)

    def test_complete_budgets_validate_ok(self):
        data = {"budgets": {"tokens_per_run": 1000, "tokens_per_game": 5000,
                             "warn_at_percent_of_cap": 80}}
        result = settings.validate_settings(data)
        self.assertTrue(result.ok)
        self.assertEqual(result.missing_required, [])

    def test_zero_budget_is_an_error_not_missing(self):
        data = {"budgets": {"tokens_per_run": 0, "tokens_per_game": 5000}}
        result = settings.validate_settings(data)
        self.assertFalse(result.ok)
        self.assertNotIn("budgets.tokens_per_run", result.missing_required)
        self.assertTrue(any("tokens_per_run" in e for e in result.errors))

    def test_missing_budgets_section_reports_both_required(self):
        result = settings.validate_settings({})
        self.assertIn("budgets.tokens_per_run", result.missing_required)
        self.assertIn("budgets.tokens_per_game", result.missing_required)

    def test_missing_file_raises_settings_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(settings.SettingsError):
                settings.load_settings(Path(tmp) / "nope.yaml")

    def test_bad_warn_percent_is_an_error(self):
        data = {"budgets": {"tokens_per_run": 10, "tokens_per_game": 10,
                             "warn_at_percent_of_cap": 150}}
        result = settings.validate_settings(data)
        self.assertFalse(result.ok)


if __name__ == "__main__":
    unittest.main()
