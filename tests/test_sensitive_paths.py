"""Sensitive changes are preserved and rejected before review/commit; no real agents."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from auto_coding import simple_run as runner
from backend_fixtures import run_with_verification as verified_run
from auto_coding.sensitive_paths import is_sensitive_path


DUMMY = "DUMMY-FIXTURE-CONTENT-NOT-A-CREDENTIAL"


class SensitivePathTests(unittest.TestCase):
    def test_root_sensitive_filenames(self):
        for name in (".env", ".env.local", ".env.production", ".env.example", "credentials",
                     "credentials.json", "id_rsa", "id_ed25519", "private.pem", "cert.p12",
                     "cert.pfx", "private.key", "safe:private.key", "nested/public:private.PEM"):
            with self.subTest(name=name):
                self.assertTrue(is_sensitive_path(name))

    def test_nested_paths_mixed_separators_and_case(self):
        for name in (".aws/credentials", "src/.ssh/config", "src/.git/config", "nested/.env.production",
                     "nested/credentials.json", "src\\.AWS\\config", "src/.SSH\\config",
                     "src/./nested//PRIVATE.KEY", "nested\\credentials"):
            with self.subTest(name=name):
                self.assertTrue(is_sensitive_path(name))

    def test_windows_filename_aliases_are_conservatively_blocked_on_both_hosts(self):
        for name in ("credentials.", "credentials ", "credentials:stream", "private.KEY:stream",
                     "src/.aws. /config", "src\\.SSH.\\config"):
            with self.subTest(name=name):
                self.assertTrue(is_sensitive_path(name))

    def test_ordinary_files_and_public_key_filenames_remain_allowed(self):
        for name in ("README.md", ".gitignore", "src/app.py", "src\\app.py", "package-lock.json",
                     "docs/environment.md", "credentials-example.json", "id_rsa.pub", "public.key.txt"):
            with self.subTest(name=name):
                self.assertFalse(is_sensitive_path(name))

    def test_classifier_import_has_no_desktop_dependency(self):
        code = """
import builtins
original = builtins.__import__
def headless(name, *args, **kwargs):
    if name.startswith('auto_coding.desktop') or name == 'webview':
        raise AssertionError('Unexpected desktop dependency: ' + name)
    return original(name, *args, **kwargs)
builtins.__import__ = headless
from auto_coding.sensitive_paths import is_sensitive_path
assert is_sensitive_path('.aws/credentials')
assert not is_sensitive_path('src/app.py')
"""
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
        result = subprocess.run([sys.executable, "-B", "-c", code], env=environment,
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)


class FakeExecutor:
    def __init__(self, filename):
        self.filename = filename
        self.calls = []
        self.b_prompts = []

    def __call__(self, argv, cwd, timeout, *, prompt=None):
        if argv[0] == "fake-test":
            self.calls.append("test")
        elif "--output-schema" in argv:
            self.calls.append("B")
            self.b_prompts.append(prompt)
            Path(argv[argv.index("--output-last-message") + 1]).write_text(
                json.dumps({"decision": "APPROVED", "issues": [], "summary": "OK"}))
        else:
            self.calls.append("A")
            file = cwd / self.filename
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text(DUMMY)
            Path(argv[argv.index("--output-last-message") + 1]).write_text("done")
        return runner._CallResult(0, "finished", False, True, True)


class SensitiveChangeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.repo = self.make_repo("repo")
        self.logs = self.root / "logs"
        self.plan = self.root / "plan.json"
        self.plan.write_text(json.dumps({"tasks": [{"id": "one", "prompt": "Implement ONE",
                                                   "test_commands": [["fake-test"]]}]}))

    def make_repo(self, name):
        repo = self.root / name
        repo.mkdir()
        for args in (("init", "-b", "feature/test"), ("config", "user.name", "Test"),
                     ("config", "user.email", "test@example.invalid")):
            subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
        (repo / "seed.txt").write_text("seed")
        subprocess.run(["git", "add", "seed.txt"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "initial"], cwd=repo, check=True, capture_output=True)
        return repo

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.repo, check=True,
                              capture_output=True, text=True).stdout.strip()

    def run_fake(self, fake, **options):
        console = io.StringIO()
        try:
            with contextlib.redirect_stdout(console):
                return verified_run(self.plan, self.repo, self.logs, execute=fake, **options)
        finally:
            self.console = console.getvalue()

    def state(self):
        return json.loads((self.logs / "state.json").read_text())

    def test_sensitive_root_and_nested_changes_stop_before_tests_and_b(self):
        for index, filename in enumerate((".env.production", "credentials", ".aws/credentials", "nested/private.KEY")):
            with self.subTest(filename=filename):
                self.repo = self.make_repo(f"case-{index}")
                self.logs = self.root / f"logs-{index}"
                before = self.git("rev-parse", "HEAD")
                fake = FakeExecutor(filename)
                self.assertEqual(self.run_fake(fake), 1)
                self.assertEqual(fake.calls, ["A"])
                self.assertEqual(fake.b_prompts, [])
                self.assertEqual(self.git("rev-parse", "HEAD"), before)
                self.assertEqual((self.repo / filename).read_text(), DUMMY)
                self.assertIn(filename, self.git("diff", "--cached", "--name-only"))
                self.assertIn(filename, self.state()["history"][-1]["feedback"])
                self.assertNotIn(DUMMY, self.state()["history"][-1]["feedback"])
                self.assertFalse(list(self.logs.glob("*-b*-prompt.txt")))
                self.assertEqual(self.state()["completed"], [])

    def test_ordinary_change_completes_a_tests_b_commit(self):
        fake = FakeExecutor("src/app.py")
        self.assertEqual(self.run_fake(fake), 0)
        self.assertEqual(fake.calls, ["A", "test", "B"])
        self.assertEqual(self.state()["completed"], ["one"])
        self.assertEqual(self.git("log", "-1", "--format=%s"), "auto-coding: one")

    def test_direct_local_commit_rejects_sensitive_staged_files_without_discarding_them(self):
        filename = ".aws/credentials"
        file = self.repo / filename
        file.parent.mkdir()
        file.write_text(DUMMY)
        self.git("add", filename)
        before = self.git("rev-parse", "HEAD")
        with self.assertRaises(runner.PilotError) as caught:
            runner.local_commit(self.repo, "one")
        self.assertIn(filename, str(caught.exception))
        self.assertNotIn(DUMMY, str(caught.exception))
        self.assertEqual(self.git("rev-parse", "HEAD"), before)
        self.assertEqual(file.read_text(), DUMMY)
        self.assertIn(filename, self.git("diff", "--cached", "--name-only"))

    def test_sensitive_generated_path_is_not_silently_unstaged(self):
        filename = "node_modules/.env.production"
        file = self.repo / filename
        file.parent.mkdir()
        file.write_text(DUMMY)
        self.git("add", "-f", filename)
        with self.assertRaisesRegex(runner.PilotError, "Sensitive file"):
            runner.staging_summary(self.repo)
        self.assertEqual(file.read_text(), DUMMY)
        self.assertIn(filename, self.git("diff", "--cached", "--name-only"))

    def test_sensitive_file_added_after_review_cannot_enter_a_commit(self):
        original = runner.local_commit
        filename = "nested/credentials.json"
        def inject(repo, ident):
            file = repo / filename
            file.parent.mkdir()
            file.write_text(DUMMY)
            self.git("add", filename)
            return original(repo, ident)
        before = self.git("rev-parse", "HEAD")
        fake = FakeExecutor("src/app.py")
        with patch.object(runner, "local_commit", side_effect=inject):
            self.assertEqual(self.run_fake(fake), 1)
        self.assertEqual(fake.calls, ["A", "test", "B"])
        self.assertEqual(self.git("rev-parse", "HEAD"), before)
        self.assertEqual(self.state()["completed"], [])
        self.assertIsNotNone(self.state()["pending_checkpoint"])
        self.assertEqual((self.repo / filename).read_text(), DUMMY)
        self.assertIn(filename, self.console)

    def test_old_review_ready_checkpoint_rejects_sensitive_paths_before_b(self):
        filename = ".env.production"
        fake = FakeExecutor(filename)
        with patch.object(runner, "is_sensitive_path", return_value=False), \
                patch.object(runner, "review_ready_task", return_value="stopped"):
            self.assertEqual(self.run_fake(fake), 1)
        self.assertEqual(fake.calls, ["A", "test"])
        before = self.git("rev-parse", "HEAD")
        executor = Mock()
        with self.assertRaises(runner.PilotError) as caught:
            self.run_fake(executor, resume=True)
        executor.assert_not_called()
        self.assertIn(filename, str(caught.exception))
        self.assertNotIn(DUMMY, str(caught.exception))
        self.assertEqual(self.git("rev-parse", "HEAD"), before)
        self.assertEqual((self.repo / filename).read_text(), DUMMY)

    def test_old_approved_checkpoint_rejects_sensitive_paths_before_recovery_commit(self):
        filename = ".aws/credentials"
        with patch.object(runner, "is_sensitive_path", return_value=False), \
                patch.object(runner, "local_commit", side_effect=runner.PilotError("before commit")):
            self.assertEqual(self.run_fake(FakeExecutor(filename)), 1)
        before = self.git("rev-parse", "HEAD")
        executor = Mock()
        with self.assertRaises(runner.PilotError) as caught:
            self.run_fake(executor, resume=True)
        executor.assert_not_called()
        self.assertIn(filename, str(caught.exception))
        self.assertEqual(self.git("rev-parse", "HEAD"), before)
        self.assertEqual((self.repo / filename).read_text(), DUMMY)

    def test_already_committed_old_sensitive_checkpoint_is_not_recertified(self):
        with patch.object(runner, "is_sensitive_path", return_value=False):
            self.assertEqual(self.run_fake(FakeExecutor(".env.production")), 0)
        before = self.git("rev-parse", "HEAD")
        executor = Mock()
        with self.assertRaisesRegex(runner.PilotError, "Sensitive file"):
            self.run_fake(executor, resume=True)
        executor.assert_not_called()
        self.assertEqual(self.git("rev-parse", "HEAD"), before)
        self.assertEqual((self.repo / ".env.production").read_text(), DUMMY)


if __name__ == "__main__":
    unittest.main()
