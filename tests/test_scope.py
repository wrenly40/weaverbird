import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import filled_scope_text, template_scope_text  # noqa: E402

from weaverbird import scope  # noqa: E402


def _write(tmp, text):
    path = Path(tmp) / "SCOPE.md"
    path.write_text(text, encoding="utf-8")
    return path


class ScopeParsingTests(unittest.TestCase):
    def test_template_is_incomplete(self):
        parsed = scope.parse_scope_text(template_scope_text())
        self.assertFalse(parsed.is_complete)
        self.assertGreater(len(parsed.missing_fields), 10)
        names = {f.name for f in parsed.missing_fields}
        self.assertIn("Dimension", names)
        self.assertIn("Palette", names)
        self.assertIn("Telemetry", names)

    def test_template_hints_are_not_filled_values(self):
        parsed = scope.parse_scope_text(template_scope_text())
        by_name = {f.name: f for f in parsed.fields}
        # "2D / 3D" and "Micro / Small / Medium" are options, not answers.
        self.assertFalse(by_name["Dimension"].filled)
        self.assertFalse(by_name["Scope class"].filled)
        self.assertFalse(by_name["Allowed sources"].filled)

    def test_filled_fixture_is_complete(self):
        parsed = scope.parse_scope_text(filled_scope_text())
        self.assertTrue(parsed.is_complete, parsed.missing_fields)
        self.assertEqual(parsed.missing_fields, [])

    def test_sections_are_tracked(self):
        parsed = scope.parse_scope_text(template_scope_text())
        palette = next(f for f in parsed.fields if f.name == "Palette")
        self.assertIn("Style lock", palette.section)

    def test_questions_cover_exactly_the_missing_fields(self):
        parsed = scope.parse_scope_text(template_scope_text())
        groups = scope.interview_questions(parsed)
        asked = sum(len(qs) for _, qs in groups)
        self.assertEqual(asked, len(parsed.missing_fields))
        flat = " ".join(q for _, qs in groups for q in qs)
        self.assertIn("[Palette]", flat)

    def test_complete_scope_has_no_questions(self):
        parsed = scope.parse_scope_text(filled_scope_text())
        self.assertEqual(scope.interview_questions(parsed), [])


class ScopeApprovalTests(unittest.TestCase):
    def test_approve_refuses_incomplete_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(tmp, template_scope_text())
            with self.assertRaises(scope.ScopeIncompleteError) as ctx:
                scope.approve_scope(path, by="Tester")
            self.assertTrue(ctx.exception.missing)

    def test_approve_complete_scope_writes_approval(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(tmp, filled_scope_text())
            approved = scope.approve_scope(path, by="Ada")
            self.assertTrue(approved.is_approved)
            self.assertIn("Ada", approved.approval)
            reloaded = scope.load_scope(path)
            self.assertTrue(reloaded.is_approved)

    def test_approve_requires_a_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(tmp, filled_scope_text())
            with self.assertRaises(scope.ScopeError):
                scope.approve_scope(path, by="  ")


if __name__ == "__main__":
    unittest.main()
