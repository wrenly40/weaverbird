import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import *  # noqa: F401,F403,E402

from weaverbird import vcs  # noqa: E402


@unittest.skipUnless(shutil.which("git"), "git not available")
class VcsTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        vcs.init_repo(self.dir)
        for key, value in (("user.name", "Test"), ("user.email", "test@example.com")):
            subprocess.run(["git", "config", key, value], cwd=self.dir, check=True)

    def tearDown(self):
        self._tmp.cleanup()

    def test_is_repo(self):
        self.assertTrue(vcs.is_repo(self.dir))
        with tempfile.TemporaryDirectory() as other:
            self.assertFalse(vcs.is_repo(other))

    def test_commit_tag_and_rollback(self):
        target = self.dir / "game.txt"
        target.write_text("version one\n", encoding="utf-8")
        commit = vcs.commit_all(self.dir, "first")
        self.assertTrue(commit)
        vcs.tag_known_good(self.dir, "good-1")
        self.assertIn("good-1", vcs.list_tags(self.dir))

        target.write_text("version two\n", encoding="utf-8")
        vcs.commit_all(self.dir, "second")
        self.assertEqual(target.read_text(encoding="utf-8"), "version two\n")

        vcs.rollback_to_tag(self.dir, "good-1")
        self.assertEqual(target.read_text(encoding="utf-8"), "version one\n")

    def test_rollback_to_unknown_tag_fails(self):
        (self.dir / "a.txt").write_text("x\n", encoding="utf-8")
        vcs.commit_all(self.dir, "first")
        with self.assertRaises(vcs.VcsError):
            vcs.rollback_to_tag(self.dir, "no-such-tag")

    def test_github_status_shape_and_no_token(self):
        status = vcs.github_status()
        self.assertIn("logged_in", status)
        self.assertIn("account", status)
        self.assertNotIn("token", str(status).lower().replace("no token", ""))


if __name__ == "__main__":
    unittest.main()
