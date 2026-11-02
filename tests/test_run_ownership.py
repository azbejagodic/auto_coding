"""Whole-run ownership uses fake agents and disposable repositories only."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from auto_coding import simple_run as runner
from backend_fixtures import run_with_verification as verified_run
from auto_coding.github_workspace import WorkspaceError, managed_job, reserve_job, cancel_reservation
from auto_coding.repository_activity import repository_busy
from auto_coding.repository_ownership import execution_lock, managed_lock


class FakeExecutor:
    def __init__(self, *, a=None, b=()):
        self.a = a
        self.b = iter(b)
        self.calls = []

    def __call__(self, argv, cwd, timeout, *, prompt=None):
        if argv[0] == "fake-test":
            self.calls.append("test")
            return runner._CallResult(0, "passed", False, True, True)
        answer = Path(argv[argv.index("--output-last-message") + 1])
        if "--output-schema" in argv:
            self.calls.append("B")
            result = next(self.b, runner._CallResult(0, "reviewed", False, True, True))
            answer.write_text(json.dumps({"decision": "APPROVED", "issues": [], "summary": "OK"}))
            return result
        self.calls.append("A")
        (cwd / "app.txt").write_text("implementation")
        answer.write_text("done")
        return self.a if self.a is not None else runner._CallResult(0, "implemented", False, True, True)


class RunOwnershipTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.git("init", "-b", "feature/test")
        self.git("config", "user.name", "Test")
        self.git("config", "user.email", "test@example.invalid")
        (self.repo / "seed.txt").write_text("seed")
        self.git("add", "seed.txt")
        self.git("commit", "-m", "initial")
        self.initial_head = self.git("rev-parse", "HEAD")
        self.logs = self.root / "logs"
        self.plan = self.root / "plan.json"
        self.plan.write_text(json.dumps({"tasks": [{"id": "one", "prompt": "Implement one",
                                                   "test_commands": [["fake-test"]]}]}))

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.repo, check=True,
                              capture_output=True, text=True).stdout.strip()

    def run_fake(self, fake, **options):
        with contextlib.redirect_stdout(io.StringIO()):
            return verified_run(self.plan, self.repo, self.logs, execute=fake, **options)

    def state(self):
        return json.loads((self.logs / "state.json").read_text())

    def assert_blocked(self):
        self.assertTrue(execution_lock(self.repo, ".job.lock").is_dir())
        self.assertTrue(execution_lock(self.repo, ".process-uncertain.json").is_file())
        self.assertTrue(repository_busy(self.repo, self.root / "other-storage"))
        for resume, logs in ((True, self.logs), (False, self.root / "new-logs")):
            executor = Mock()
            with self.assertRaisesRegex(OSError, "cleanup remains unverified"):
                verified_run(self.plan, self.repo, logs, resume=resume, execute=executor)
            executor.assert_not_called()
        self.assertEqual(self.git("rev-parse", "HEAD"), self.initial_head)
        self.assertEqual(self.state()["completed"], [])

    def test_independent_unmanaged_processes_cannot_both_enter(self):
        code = """
from pathlib import Path
import sys, time
from auto_coding.github_workspace import managed_job, WorkspaceError
repo, ready, release = map(Path, sys.argv[1:])
@managed_job
def body(plan, repo, logs):
    ready.write_text('entered')
    deadline = time.monotonic() + 15
    while not release.exists() and time.monotonic() < deadline:
        time.sleep(.02)
    if not release.exists():
        raise RuntimeError('test owner release deadline')
try:
    body(None, repo, None)
except WorkspaceError:
    sys.exit(42)
"""
        ready, release = self.root / "ready", self.root / "release"
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
        owner = subprocess.Popen([sys.executable, "-B", "-c", code, str(self.repo), str(ready), str(release)],
                                 env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.monotonic() + 8
            while not ready.exists() and owner.poll() is None and time.monotonic() < deadline:
                time.sleep(.02)
            self.assertTrue(ready.exists(), "owner never acquired its gate")
            contender_ready = self.root / "contender-entered"
            contender = subprocess.run([sys.executable, "-B", "-c", code, str(self.repo),
                                        str(contender_ready), str(release)], env=environment,
                                       capture_output=True, text=True, timeout=10)
            self.assertEqual(contender.returncode, 42, contender.stderr)
            self.assertFalse(contender_ready.exists())
            self.assertTrue(execution_lock(self.repo, ".job.lock").is_dir())
        finally:
            release.touch()
            stdout, stderr = owner.communicate(timeout=20)
        self.assertEqual(owner.returncode, 0, stderr)
        self.assertFalse(execution_lock(self.repo, ".job.lock").exists())

    def test_uncertain_a_timeout_blocks_resume_and_fresh_run(self):
        fake = FakeExecutor(a=runner._CallResult(124, "timeout", True, False, True))
        self.assertEqual(self.run_fake(fake), 1)
        self.assertEqual(fake.calls, ["A"])
        self.assertEqual(self.state()["uncertain_process"]["actor"], "Codex A")
        self.assertEqual(self.state()["history"], [])
        self.assert_blocked()

    def test_uncertain_success_cannot_reach_tests_review_or_commit(self):
        fake = FakeExecutor(a=runner._CallResult(0, "parent exited", False, False, True))
        self.assertEqual(self.run_fake(fake), 1)
        self.assertEqual(fake.calls, ["A"])
        self.assert_blocked()

    def test_uncertain_a_exception_is_persisted_before_propagation(self):
        error = OSError("interrupted executor")
        error.cleanup_confirmed = False
        executor = Mock(side_effect=error)
        with self.assertRaisesRegex(OSError, "interrupted executor"):
            self.run_fake(executor)
        executor.assert_called_once()
        self.assertEqual(self.state()["uncertain_process"]["actor"], "Codex A")
        self.assert_blocked()

    def test_uncertainty_record_survives_job_directory_lost_during_fake_execution(self):
        def execute(*args, **kwargs):
            # No actual process exists in this fault injection.
            execution_lock(self.repo, ".job.lock").rmdir()
            return runner._CallResult(1, "unknown", False, False, True)
        with self.assertRaises(FileNotFoundError):
            self.run_fake(execute)
        self.assertTrue(execution_lock(self.repo, ".process-uncertain.json").is_file())
        self.assertEqual(self.state()["uncertain_process"]["actor"], "Codex A")
        executor = Mock()
        with self.assertRaisesRegex(OSError, "cleanup remains unverified"):
            verified_run(self.plan, self.repo, self.root / "new", execute=executor)
        executor.assert_not_called()

    def test_uncertain_b_is_saved_before_evidence_write_failure(self):
        original = Path.write_text
        def write(path, *args, **kwargs):
            if path.name.endswith("-b-events.jsonl"):
                raise OSError("simulated evidence storage failure")
            return original(path, *args, **kwargs)
        fake = FakeExecutor(b=[runner._CallResult(124, "timeout", True, False, True)])
        with patch.object(Path, "write_text", write):
            with self.assertRaisesRegex(OSError, "evidence storage failure"):
                self.run_fake(fake)
        self.assertEqual(fake.calls, ["A", "test", "B"])
        self.assertTrue(self.state()["pending_review"]["process_uncertain"])
        self.assertEqual(self.state()["uncertain_process"]["actor"], "Codex B")
        self.assert_blocked()

    def test_missing_job_lock_is_not_cleanup_evidence(self):
        self.run_fake(FakeExecutor(a=runner._CallResult(1, "uncertain", False, False, True)))
        job = execution_lock(self.repo, ".job.lock")
        # Fake execution created no child: simulate loss, never release a real unknown process.
        (job / "uncertain").unlink()
        job.rmdir()
        executor = Mock()
        with self.assertRaisesRegex(OSError, "cleanup remains unverified"):
            verified_run(self.plan, self.repo, self.root / "new", execute=executor)
        executor.assert_not_called()

    def test_saved_uncertainty_survives_loss_of_fake_repository_records(self):
        self.run_fake(FakeExecutor(a=runner._CallResult(1, "uncertain", False, False, True)))
        job = execution_lock(self.repo, ".job.lock")
        (job / "uncertain").unlink()
        job.rmdir()
        execution_lock(self.repo, ".process-uncertain.json").unlink()
        executor = Mock()
        with self.assertRaisesRegex(runner.PilotError, "unverified in saved state"):
            self.run_fake(executor, resume=True)
        executor.assert_not_called()

    def test_confirmed_a_failure_allows_legitimate_resume(self):
        failed = FakeExecutor(a=runner._CallResult(124, "timeout", True, True, True))
        self.assertEqual(self.run_fake(failed), 1)
        self.assertFalse(execution_lock(self.repo, ".job.lock").exists())
        self.assertFalse(execution_lock(self.repo, ".process-uncertain.json").exists())
        resumed = FakeExecutor()
        self.assertEqual(self.run_fake(resumed, resume=True), 0)
        self.assertEqual(resumed.calls, ["A", "test", "B"])

    def test_confirmed_b_timeout_retries_review_only(self):
        fake = FakeExecutor(b=[runner._CallResult(124, "timeout", True, True, True)])
        with patch.object(runner, "_review_sleep") as sleep:
            self.assertEqual(self.run_fake(fake), 0)
        self.assertEqual(fake.calls, ["A", "test", "B", "B"])
        sleep.assert_called_once()
        self.assertFalse(execution_lock(self.repo, ".job.lock").exists())
        self.assertNotIn("uncertain_process", self.state())

    def test_failed_uncertainty_record_write_never_releases_gate(self):
        fake = FakeExecutor(a=runner._CallResult(1, "unknown", False, False, True))
        with patch("auto_coding.github_workspace.preserve_process_uncertainty", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(OSError, "disk full"):
                self.run_fake(fake)
        self.assertTrue((execution_lock(self.repo, ".job.lock") / "uncertain").exists())
        self.assertEqual(self.state()["uncertain_process"]["actor"], "Codex A")
        executor = Mock()
        with self.assertRaises(WorkspaceError):
            self.run_fake(executor, resume=True)
        executor.assert_not_called()

    def test_gate_identity_is_stable_before_git_creation_and_for_alias_paths(self):
        target = self.root / "new-repo"
        before = execution_lock(target, ".job.lock")
        target.mkdir()
        subprocess.run(["git", "init", "-b", "feature/new"], cwd=target, check=True, capture_output=True)
        self.assertEqual(before, execution_lock(target / ".", ".job.lock"))
        self.assertIsNone(managed_lock(target))  # Managed identity API remains compatible.

    def test_existing_managed_gate_paths_are_unchanged(self):
        target = self.root / "workspaces" / ("a" * 64)
        self.assertEqual(execution_lock(target, ".job.lock"), managed_lock(target, ".job.lock"))

    def test_uncertain_reservation_is_not_cancelled_even_with_matching_token(self):
        token = reserve_job(self.repo)
        gate = execution_lock(self.repo, ".job.lock")
        (gate / "uncertain").touch()
        cancel_reservation(self.repo, token)
        self.assertEqual((gate / "reservation").read_text(), token)
        self.assertTrue((gate / "uncertain").exists())

    def test_cli_validation_releases_only_its_unclaimed_reservation(self):
        for extra, expected_exit in ((["--auto-push-repository", "owner/repo"], 2),
                                     (["--auto-push-repository", "invalid", "--auto-push-branch", "feature/test"], 1)):
            with self.subTest(extra=extra):
                token = reserve_job(self.repo)
                argv = [str(self.plan), "--repo", str(self.repo), "--logs", str(self.logs)] + extra
                with patch.dict(os.environ, {"AUTOCODING_JOB_RESERVATION": token}), \
                        patch.object(runner.shutil, "which", return_value="fake-codex"), \
                        patch.object(runner, "run") as execute, \
                        contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    if expected_exit == 2:
                        with self.assertRaises(SystemExit) as caught:
                            runner.main(argv)
                        self.assertEqual(caught.exception.code, 2)
                    else:
                        self.assertEqual(runner.main(argv), expected_exit)
                    execute.assert_not_called()
                self.assertFalse(execution_lock(self.repo, ".job.lock").exists())


if __name__ == "__main__":
    unittest.main()
