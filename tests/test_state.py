import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import filled_scope_text, template_scope_text  # noqa: E402

from weaverbird import scope, state  # noqa: E402


def _project(tmp, scope_text=None):
    project = Path(tmp)
    if scope_text is not None:
        (project / "SCOPE.md").write_text(scope_text, encoding="utf-8")
    state.init_state(project)
    return project


class PipelineTests(unittest.TestCase):
    def tearDown(self):
        state.clear_handlers()

    def test_new_project_starts_at_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            _project(tmp)
            self.assertEqual(state.current_stage(tmp), "scope")

    def test_advance_blocked_by_incomplete_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            _project(tmp, template_scope_text())
            with self.assertRaises(state.PipelineError) as ctx:
                state.advance(tmp)
            self.assertIn("incomplete", str(ctx.exception))

    def test_advance_blocked_by_unapproved_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            _project(tmp, filled_scope_text())
            with self.assertRaises(state.PipelineError) as ctx:
                state.advance(tmp)
            self.assertIn("not approved", str(ctx.exception))

    def test_advance_through_scope_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            _project(tmp, filled_scope_text())
            scope.approve_scope(Path(tmp) / "SCOPE.md", by="Tester")
            result = state.advance(tmp)
            self.assertEqual(result["stage"], "skeleton")
            # One stage at a time: the next advance moves exactly one more.
            result = state.advance(tmp)
            self.assertEqual(result["stage"], "assets")

    def test_out_of_order_advance_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            _project(tmp, filled_scope_text())
            with self.assertRaises(state.PipelineError) as ctx:
                state.advance_to(tmp, "publish")
            self.assertIn("Out-of-order", str(ctx.exception))

    def test_finished_gate_needs_definition_of_done(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = _project(tmp, filled_scope_text())
            scope.approve_scope(project / "SCOPE.md", by="Tester")
            for _ in range(5):  # scope -> ... -> playtest
                state.advance(tmp)
            self.assertEqual(state.current_stage(tmp), "playtest")
            with self.assertRaises(state.PipelineError) as ctx:
                state.advance(tmp)
            self.assertIn("Definition of done", str(ctx.exception))
            for key in state.DEFINITION_OF_DONE:
                state.set_definition_of_done(tmp, key, True)
            result = state.advance(tmp)
            self.assertEqual(result["stage"], "finished")

    def test_handler_can_block_and_allow(self):
        with tempfile.TemporaryDirectory() as tmp:
            _project(tmp, filled_scope_text())
            scope.approve_scope(Path(tmp) / "SCOPE.md", by="Tester")
            state.register_handler("scope", lambda project, st: False)
            with self.assertRaises(state.PipelineError):
                state.advance(tmp)
            state.register_handler("scope", lambda project, st: True)
            self.assertEqual(state.advance(tmp)["stage"], "skeleton")

    def test_handler_failure_blocks_advance(self):
        with tempfile.TemporaryDirectory() as tmp:
            _project(tmp, filled_scope_text())
            scope.approve_scope(Path(tmp) / "SCOPE.md", by="Tester")

            def broken(project, st):
                raise RuntimeError("boom")

            state.register_handler("scope", broken)
            with self.assertRaises(state.PipelineError):
                state.advance(tmp)
            self.assertEqual(state.current_stage(tmp), "scope")


if __name__ == "__main__":
    unittest.main()
