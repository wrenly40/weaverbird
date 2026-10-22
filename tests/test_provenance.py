import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import *  # noqa: F401,F403,E402

from weaverbird import provenance  # noqa: E402


class ProvenanceTests(unittest.TestCase):
    def test_add_and_list_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            provenance.add_record(tmp, "assets/hero.png", "ai", note="hero sprite",
                                  provider="TestModel 1")
            provenance.add_record(tmp, "assets/wall.png", "code", note="drawn in code")
            records = provenance.list_records(tmp)
            self.assertEqual(len(records), 2)
            self.assertEqual(records[0]["asset"], "assets/hero.png")

    def test_bad_source_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(provenance.ProvenanceError):
                provenance.add_record(tmp, "assets/x.png", "borrowed")

    def test_disclosure_includes_ai_assets_and_provider(self):
        with tempfile.TemporaryDirectory() as tmp:
            provenance.add_record(tmp, "assets/hero.png", "ai", note="hero sprite",
                                  provider="TestModel 1")
            provenance.add_record(tmp, "assets/theme.ogg", "library",
                                  note="CC-BY, by A. Composer")
            text = provenance.disclosure_text(tmp)
            self.assertIn("assets/hero.png", text)
            self.assertIn("TestModel 1", text)
            self.assertIn("assets/theme.ogg", text)
            self.assertIn("CC-BY", text)

    def test_disclosure_with_no_ai_assets_says_so(self):
        with tempfile.TemporaryDirectory() as tmp:
            provenance.add_record(tmp, "assets/wall.png", "code")
            text = provenance.disclosure_text(tmp)
            self.assertIn("No AI-generated assets are recorded", text)

    def test_unrecorded_assets_are_listed(self):
        with tempfile.TemporaryDirectory() as tmp:
            provenance.add_record(tmp, "assets/hero.png", "ai")
            missing = provenance.unrecorded_assets(
                tmp, ["assets/hero.png", "assets/mystery.png", "assets/ghost.png"]
            )
            self.assertEqual(missing, ["assets/mystery.png", "assets/ghost.png"])

    def test_replacing_a_record_for_same_asset(self):
        with tempfile.TemporaryDirectory() as tmp:
            provenance.add_record(tmp, "assets/hero.png", "ai")
            provenance.add_record(tmp, "assets/hero.png", "code", note="redrawn in code")
            records = provenance.list_records(tmp)
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["source"], "code")


if __name__ == "__main__":
    unittest.main()
