import io
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import *  # noqa: F401,F403,E402

from weaverbird import cli, identity  # noqa: E402


def run_cli(*argv):
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        code = cli.main(list(argv))
    return code, buffer.getvalue()


class IdentityTests(unittest.TestCase):
    def test_tokens_load_from_packaged_assets(self):
        tokens = identity.load_tokens()
        self.assertEqual(tokens["colors"]["accent"], "#6C4CF1")
        self.assertEqual(tokens["colors"]["background"], "#F7F6FA")
        self.assertEqual(tokens["colors"]["text"], "#232936")
        self.assertIn(16, tokens["spacing"])
        self.assertEqual(tokens["radii"]["medium"], 12)

    def test_mark_is_an_svg_in_the_accent(self):
        svg = identity.mark_svg()
        self.assertIn("<svg", svg)
        self.assertIn("#6C4CF1", svg)
        self.assertIn("weaverbird", svg)

    def test_identity_command_prints_the_palette(self):
        code, out = run_cli("identity")
        self.assertEqual(code, 0)
        self.assertIn("#6C4CF1", out)
        self.assertIn("accent", out)

    def test_identity_export_writes_tokens_and_mark(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, _ = run_cli("identity", "--export", tmp)
            self.assertEqual(code, 0)
            self.assertTrue((Path(tmp) / "design_tokens.json").exists())
            self.assertTrue((Path(tmp) / "weaverbird.svg").exists())

    def test_bare_cli_launches_the_app(self):
        # App-first entry model (v0.3): bare `weaverbird` starts the
        # app for the current folder instead of printing an overview.
        # The launch itself is mocked — no server starts in a test.
        from unittest import mock

        from weaverbird import app as app_module
        with mock.patch.object(app_module, "run_app",
                               return_value=0) as run_app:
            code, _ = run_cli()
        self.assertEqual(code, 0)
        args, kwargs = run_app.call_args
        self.assertEqual(Path(args[0]), Path.cwd())
        self.assertEqual(kwargs.get("open_browser"), True)

    def test_help_prints_the_overview_and_the_scripting_group(self):
        code, out = run_cli("help")
        self.assertEqual(code, 0)
        self.assertIn("The app is the product", out)
        self.assertIn("scope", out)
        self.assertIn("playtest", out)
        self.assertIn("Project commands (scripting)", out)
        self.assertIn("weaverbird update", out)

    def test_doctor_uses_consistent_markers(self):
        code, out = run_cli("doctor")
        self.assertEqual(code, 0)
        self.assertIn("✓", out)


if __name__ == "__main__":
    unittest.main()
