import io
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import filled_scope_text  # noqa: E402

from weaverbird import cli  # noqa: E402


def run_cli(*argv):
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        code = cli.main(list(argv))
    return code, buffer.getvalue()


class CliTests(unittest.TestCase):
    def test_init_then_scope_check_fails_incomplete(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = str(Path(tmp) / "demo")
            code, out = run_cli("init", project)
            self.assertEqual(code, 0)
            self.assertTrue((Path(project) / "SCOPE.md").exists())
            self.assertTrue((Path(project) / ".weaverbird" / "state.json").exists())
            code, out = run_cli("scope", "check", project)
            self.assertEqual(code, 1)
            self.assertIn("Still missing", out)

    def test_init_refuses_non_empty_dir_without_force(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "file.txt").write_text("x", encoding="utf-8")
            code, out = run_cli("init", tmp)
            self.assertEqual(code, 1)
            self.assertIn("not empty", out)

    def test_fill_approve_and_pipeline_advance(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "demo"
            run_cli("init", str(project))
            (project / "SCOPE.md").write_text(filled_scope_text(), encoding="utf-8")
            code, _ = run_cli("scope", "check", str(project))
            self.assertEqual(code, 0)
            code, out = run_cli("scope", "approve", str(project), "--by", "Tester")
            self.assertEqual(code, 0)
            code, out = run_cli("pipeline", "advance", str(project))
            self.assertEqual(code, 0)
            self.assertIn("skeleton", out)

    def test_settings_check_flags_template(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "demo"
            run_cli("init", str(project), "--with-settings")
            code, out = run_cli("settings", "check", str(project / "global-settings.yaml"))
            self.assertEqual(code, 1)
            self.assertIn("tokens_per_game", out)

    def test_ledger_and_provenance_commands(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = str(Path(tmp) / "demo")
            run_cli("init", project)
            code, _ = run_cli("ledger", "add", project, "--stage", "build",
                              "--tier", "mid", "--tokens", "120", "--cost", "0.5")
            self.assertEqual(code, 0)
            code, out = run_cli("ledger", "show", project)
            self.assertEqual(code, 0)
            self.assertIn("120", out)
            code, _ = run_cli("provenance", "add", project, "--asset", "assets/a.png",
                              "--source", "ai", "--note", "test asset", "--provider", "TestModel")
            self.assertEqual(code, 0)
            code, out = run_cli("provenance", "disclosure", project)
            self.assertEqual(code, 0)
            self.assertIn("assets/a.png", out)

    def test_connectors_check_unknown_action_fails_closed(self):
        code, out = run_cli("connectors", "check", "github", "delete_everything")
        self.assertEqual(code, 0)
        self.assertIn("fails closed", out)

    def test_doctor_runs_and_exits_zero(self):
        code, out = run_cli("doctor")
        self.assertEqual(code, 0)
        self.assertIn("Python", out)
        self.assertIn("Godot", out)


class DeleteCliTests(unittest.TestCase):
    """0.12.0 (2/4): `weaverbird delete` — typed confirmation, or
    --yes for scripts; every other guardrail always applies."""

    def _make_project(self, tmp) -> Path:
        project = Path(tmp) / "bee-game"
        (project / ".weaverbird").mkdir(parents=True)
        (project / "SCOPE.md").write_text("# scope\n",
                                         encoding="utf-8")
        return project

    def test_delete_yes_removes_the_project(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = self._make_project(tmp)
            code, out = run_cli("delete", str(project), "--yes")
            self.assertEqual(code, 0, out)
            self.assertIn("permanently", out)
            self.assertFalse(project.exists())

    def test_delete_typed_confirmation_over_stdin(self):
        import io as _io
        from unittest import mock
        with tempfile.TemporaryDirectory() as tmp:
            project = self._make_project(tmp)
            with mock.patch("sys.stdin", _io.StringIO("bee-game\n")):
                code, out = run_cli("delete", str(project))
            self.assertEqual(code, 0, out)
            self.assertFalse(project.exists())

    def test_delete_wrong_typed_name_refuses(self):
        import io as _io
        from unittest import mock
        with tempfile.TemporaryDirectory() as tmp:
            project = self._make_project(tmp)
            with mock.patch("sys.stdin", _io.StringIO("nope\n")):
                code, out = run_cli("delete", str(project))
            self.assertEqual(code, 1)
            self.assertIn("does not match", out)
            self.assertTrue((project / "SCOPE.md").exists())

    def test_delete_yes_still_refuses_a_running_job(self):
        from weaverbird import jobs as jobs_module
        with tempfile.TemporaryDirectory() as tmp:
            project = self._make_project(tmp)
            jobs_module.JobStore(tmp).save(
                {"id": "j1", "project": "bee-game",
                 "status": "running", "created": "2026-10-24"})
            code, out = run_cli("delete", str(project), "--yes")
            self.assertEqual(code, 1)
            self.assertIn("job is currently running", out)
            self.assertTrue(project.exists())

    def test_delete_missing_folder_fails_plainly(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, out = run_cli("delete", str(Path(tmp) / "ghost"),
                                "--yes")
            self.assertEqual(code, 1)
            self.assertIn("nothing was deleted", out)


if __name__ == "__main__":
    unittest.main()
