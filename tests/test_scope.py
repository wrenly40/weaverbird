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


class TemplateWordingTests(unittest.TestCase):
    """0.12.1: two template fields carry their hint inside the
    line — "Allowed sources" with its options list and the long
    Telemetry label — and a maker answering in the template's
    own words (writing the answer after the line's trailing
    colon, where the template invites it) must read as filled.
    Before the fix both parsed as empty forever, so a scope
    filled in entirely from the CLI template could never pass
    the required-fields gate."""

    def _answered_template(self) -> str:
        text = template_scope_text()
        text = text.replace(
            "- REQUIRED Allowed sources: code-drawn / AI-generated "
            "/ licensed library (pick any, in preference order):",
            "- REQUIRED Allowed sources: code-drawn / AI-generated "
            "/ licensed library (pick any, in preference order): "
            "code-drawn")
        text = text.replace(
            "- Telemetry: off by default — REQUIRED explicit yes/no:",
            "- Telemetry: off by default — REQUIRED explicit yes/no: no")
        return text

    def test_template_worded_answers_parse_as_filled(self):
        parsed = scope.parse_scope_text(self._answered_template())
        by_name = {f.name: f for f in parsed.fields}
        self.assertEqual(by_name["Allowed sources"].value, "code-drawn")
        self.assertTrue(by_name["Allowed sources"].filled)
        self.assertEqual(by_name["Telemetry"].value, "no")
        self.assertTrue(by_name["Telemetry"].filled)

    def test_template_filled_in_its_own_words_passes_the_gate(self):
        # Every other field filled the plain way, the two
        # template-worded lines kept exactly as a maker editing
        # the template leaves them: the scope reads complete,
        # with zero questions left to ask.
        lines = []
        for line in self._answered_template().splitlines():
            stripped = line.strip()
            if stripped.startswith("- REQUIRED"):
                name = stripped[len("- REQUIRED"):].partition(":")[0].strip()
                if name == "Allowed sources":
                    lines.append(line)
                elif name == "Dimension":
                    lines.append("- REQUIRED Dimension: 2D")
                elif name == "Scope class":
                    lines.append("- REQUIRED Scope class: Micro")
                else:
                    lines.append(
                        f"- REQUIRED {name}: Filled in for the test")
            else:
                lines.append(line)
        parsed = scope.parse_scope_text("\n".join(lines) + "\n")
        self.assertTrue(parsed.is_complete,
                        [f.name for f in parsed.missing_fields])
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
