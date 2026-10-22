"""Tests for self-update (update.py) and the app-first entry surface.

Every network and subprocess call is mocked at update.py's seams —
no test performs a real network call, runs git, or replaces a real
executable. The zipapp swap is exercised against a fake executable
file in a temp directory.
"""

import io
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import helpers  # noqa: E402,F401  (puts src/ on sys.path)

from weaverbird import __version__, cli, update  # noqa: E402


def release(tag, assets=None):
    return {"tag": tag, "version": tag.lstrip("vV"), "assets": assets or {}}


def run_captured(**kwargs):
    out = io.StringIO()
    with redirect_stdout(out):
        code = update.run_update(**kwargs)
    return code, out.getvalue()


class TestCheckMode(unittest.TestCase):
    def check(self, tag):
        with mock.patch.object(update, "latest_release",
                               return_value=release(tag)):
            return run_captured(check_only=True)

    def test_newer_release_reports_an_update(self):
        code, text = self.check("v99.0.0")
        self.assertEqual(code, 0)
        self.assertIn(__version__, text)
        self.assertIn("99.0.0", text)
        self.assertIn("An update is available", text)

    def test_same_version_is_up_to_date(self):
        code, text = self.check("v" + __version__)
        self.assertEqual(code, 0)
        self.assertIn("up to date", text)

    def test_older_release_is_up_to_date(self):
        code, text = self.check("v0.0.1")
        self.assertEqual(code, 0)
        self.assertIn("up to date", text)

    def test_network_failure_is_plain_and_nonzero(self):
        with mock.patch.object(update, "latest_release",
                               side_effect=update.UpdateError("no route")):
            code, text = run_captured(check_only=True)
        self.assertEqual(code, 1)
        self.assertIn("Update check failed", text)

    def test_parse_version(self):
        self.assertEqual(update.parse_version("v1.2.3"), (1, 2, 3))
        self.assertEqual(update.parse_version("0.3.0"), (0, 3, 0))
        self.assertGreater(update.parse_version("v0.10.0"),
                           update.parse_version("v0.9.9"))


class ZipappCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.target = self.tmp / "weaverbird"
        self.target.write_bytes(b"old-executable")
        self.target.chmod(0o755)
        self.info = release("v99.0.0", {
            "weaverbird": "https://example.invalid/weaverbird"})

    def drive(self, download=None, run=None):
        with mock.patch.object(update, "latest_release",
                               return_value=self.info), \
                mock.patch.object(update, "detect_install",
                                  return_value=("zipapp", self.target)), \
                mock.patch.object(update, "_download",
                                  download or self._write_new), \
                mock.patch.object(update, "_run",
                                  run or (lambda cmd, cwd=None:
                                          (0, "weaverbird 99.0.0"))):
            return run_captured()

    @staticmethod
    def _write_new(url, dest):
        Path(dest).write_bytes(b"new-executable")


class TestZipappSwap(ZipappCase):
    def test_happy_path_swaps_and_keeps_a_backup(self):
        code, text = self.drive()
        self.assertEqual(code, 0, text)
        self.assertEqual(self.target.read_bytes(), b"new-executable")
        backup = self.tmp / "weaverbird.bak"
        self.assertTrue(backup.exists())
        self.assertEqual(backup.read_bytes(), b"old-executable")
        self.assertIn("99.0.0", text)

    def test_verification_failure_keeps_the_original(self):
        def bad_run(cmd, cwd=None):
            return 0, "weaverbird 0.0.1"  # wrong version reported
        code, text = self.drive(run=bad_run)
        self.assertEqual(code, 1)
        self.assertEqual(self.target.read_bytes(), b"old-executable")
        self.assertFalse((self.tmp / "weaverbird.bak").exists())
        self.assertIn("did not verify", text)
        leftovers = [p for p in self.tmp.iterdir()
                     if p.name.startswith("weaverbird.new-")]
        self.assertEqual(leftovers, [])

    def test_release_without_an_executable_asset(self):
        self.info = release("v99.0.0", {})
        code, text = self.drive()
        self.assertEqual(code, 1)
        self.assertIn("no 'weaverbird' executable asset", text)
        self.assertEqual(self.target.read_bytes(), b"old-executable")


class TestSourceCheckout(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / ".git").mkdir()
        self.info = release("v99.0.0", {
            "weaverbird": "https://example.invalid/weaverbird"})

    def drive(self, run):
        with mock.patch.object(update, "latest_release",
                               return_value=self.info), \
                mock.patch.object(update, "detect_install",
                                  return_value=("source", self.tmp)), \
                mock.patch.object(update, "_run", run):
            return run_captured()

    def test_dirty_tree_is_refused_before_any_pull(self):
        calls = []

        def run(cmd, cwd=None):
            calls.append(cmd)
            return 0, " M src/weaverbird/cli.py\n"

        code, text = self.drive(run)
        self.assertEqual(code, 1)
        self.assertIn("uncommitted changes", text)
        self.assertEqual(len(calls), 1)  # status only — no pull, no build

    def test_clean_tree_pulls_builds_and_verifies(self):
        calls = []

        def run(cmd, cwd=None):
            calls.append(cmd[0])
            if cmd[0].endswith("weaverbird"):
                return 0, "weaverbird 99.0.0"
            return 0, ""

        code, text = self.drive(run)
        self.assertEqual(code, 0, text)
        self.assertIn("git", calls[0])
        self.assertTrue(any(c == "sh" for c in calls))


class TestOtherInstalls(unittest.TestCase):
    def test_pip_install_gets_instructions_and_no_mutation(self):
        info = release("v99.0.0", {
            "weaverbird": "https://example.invalid/weaverbird"})
        with mock.patch.object(update, "latest_release", return_value=info), \
                mock.patch.object(update, "detect_install",
                                  return_value=("other", None)), \
                mock.patch.object(update, "_download") as download, \
                mock.patch.object(update, "_run") as run:
            code, text = run_captured()
        self.assertEqual(code, 0)
        self.assertIn("pip install --upgrade", text)
        download.assert_not_called()
        run.assert_not_called()


class TestDetectInstall(unittest.TestCase):
    def test_zipapp_detection(self):
        tmp = Path(tempfile.mkdtemp())
        exe = tmp / "weaverbird"
        exe.write_bytes(b"fake")
        kind, where = update.detect_install(
            argv0=str(exe),
            package_file=exe / "weaverbird" / "update.py")
        self.assertEqual(kind, "zipapp")
        self.assertEqual(where, exe)

    def test_source_detection(self):
        tmp = Path(tempfile.mkdtemp())
        (tmp / ".git").mkdir()
        package = tmp / "src" / "weaverbird" / "update.py"
        kind, where = update.detect_install(
            argv0="/usr/bin/false", package_file=package)
        self.assertEqual(kind, "source")
        self.assertEqual(where, tmp)


class TestEntrySurface(unittest.TestCase):
    def test_version_command(self):
        out = io.StringIO()
        with redirect_stdout(out):
            code = cli.main(["version"])
        self.assertEqual(code, 0)
        self.assertEqual(out.getvalue().strip(), f"weaverbird {__version__}")

    def test_help_leads_with_the_app(self):
        out = io.StringIO()
        with redirect_stdout(out):
            code = cli.main(["help"])
        self.assertEqual(code, 0)
        text = out.getvalue()
        self.assertIn("The app is the product", text)
        self.assertIn("Project commands (scripting)", text)
        self.assertIn("weaverbird update", text)

    def test_bare_launch_opens_the_app_for_the_cwd(self):
        with mock.patch.object(cli.app_module, "run_app",
                               return_value=0) as run_app:
            code = cli.main([])
        self.assertEqual(code, 0)
        args, kwargs = run_app.call_args
        self.assertEqual(Path(args[0]), Path.cwd())
        self.assertEqual(kwargs.get("open_browser"), True)

    def test_no_open_flag_is_passed_through(self):
        with mock.patch.object(cli.app_module, "run_app",
                               return_value=0) as run_app:
            code = cli.main(["--no-open", "--port", "9999"])
        self.assertEqual(code, 0)
        _, kwargs = run_app.call_args
        self.assertEqual(kwargs.get("open_browser"), False)
        self.assertEqual(kwargs.get("port"), 9999)


if __name__ == "__main__":
    unittest.main()
