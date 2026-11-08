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
from auto_coding.desktop_control import DesktopController
from auto_coding.github_workspace import GitHubWorkspaceManager, WorkspaceError, managed_lock, reserve_job
from auto_coding.repository_activity import repository_busy
from auto_coding.test_process_safety import (TestProcessGate, TestProcessSafetyError,
                                            assert_test_process_safe, test_gate_path)
from auto_coding.python_environment import TEST_ENV_CONFIG


class FakeExecutor:
    def __init__(self, results=()):
        self.results = iter(results)
        self.calls = []

    def __call__(self, argv, cwd, timeout, *, prompt=None):
        if argv[0] == "fake-test":
            self.calls.append("test")
            gate = test_gate_path(cwd)
            assert gate.is_dir(), "The persistent gate must precede execution"
            assert json.loads((gate / "execution.json").read_text())["index"] >= 1
            result = next(self.results, (0, "passed", False))
            if isinstance(result, BaseException):
                raise result
            return result
        answer = Path(argv[argv.index("--output-last-message") + 1])
        if "--output-schema" in argv:
            self.calls.append("B")
            answer.write_text(json.dumps({"decision": "APPROVED", "issues": [], "summary": "OK"}))
        else:
            self.calls.append("A")
            (cwd / "app.txt").write_text(f"implementation {self.calls.count('A')}")
            answer.write_text("done")
        return 0, "completed", False


class TestCleanupSafetyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.repo = self.make_repo("repo")
        self.logs = self.root / "logs"
        self.plan = self.root / "tasks.json"
        self.plan.write_text(json.dumps({"tasks": [{"id": "one", "prompt": "Implement one",
                              "test_commands": [["fake-test", "first"], ["fake-test", "second"]]}]}))

    def make_repo(self, name):
        repo = self.root / name
        repo.mkdir(parents=True)
        subprocess.run(["git", "init", "-b", "feature/test"], cwd=repo, check=True, capture_output=True)
        (repo / "seed.txt").write_text("original")
        subprocess.run(["git", "add", "seed.txt"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                        "commit", "-m", "initial"], cwd=repo, check=True, capture_output=True)
        return repo

    def run_fake(self, fake, **kwargs):
        with contextlib.redirect_stdout(io.StringIO()):
            return verified_run(self.plan, self.repo, self.logs, execute=fake, **kwargs)

    def state(self):
        return json.loads((self.logs / "state.json").read_text())

    def events(self):
        return [json.loads(line) for line in (self.logs / "events.jsonl").read_text().splitlines()]

    def assert_stopped_uncertain(self, fake):
        self.assertEqual(fake.calls, ["A", "test"])
        state = self.state()
        self.assertEqual(state["status"], "stopped")
        self.assertEqual(state["history"], [])  # Never a code-correction record.
        self.assertEqual(state["completed"], [])
        self.assertIsNone(state["pending_review"])
        self.assertIsNone(state["pending_checkpoint"])
        context = state["uncertain_test"]
        self.assertEqual((context["task"], context["attempt"], context["index"]), ("one", 1, 1))
        self.assertIs(context["cleanup_confirmed"], False)
        gate = test_gate_path(self.repo)
        self.assertTrue(gate.is_dir())
        self.assertTrue((gate.parent / "auto-coding-test-process-uncertain.json").is_file())
        self.assertTrue(repository_busy(self.repo, self.root / "unrelated-run-storage"))
        self.assertFalse(any(event["event"] in {"correction_requested", "review_started", "checkpoint_started",
                                               "checkpoint_committed", "push_started"} for event in self.events()))

    def test_invalid_explicit_test_environment_stops_before_process_and_gate(self):
        fake = FakeExecutor()
        with patch.dict(os.environ, {TEST_ENV_CONFIG: '{"GITHUB_TOKEN":"harmless-canary"}'}):
            with self.assertRaises(ValueError) as caught:
                self.run_fake(fake)
        self.assertNotIn("harmless-canary", str(caught.exception))
        self.assertEqual(fake.calls, ["A"])
        self.assertFalse(test_gate_path(self.repo).exists())
        self.assertEqual(self.state()["status"], "stopped")
        self.assertEqual(self.state()["completed"], [])
        self.assertNotIn("harmless-canary", (self.logs / "events.jsonl").read_text())

    def test_environment_blocker_stops_immediately_and_latches_resume(self):
        from auto_coding.controller_test_sandbox import ControllerTestEnvironmentError
        error = ControllerTestEnvironmentError("missing compiler", process_started=False,
            classification="confirmed_environment_blocker",
            details={"stage": "command_launch", "executable": "compiler", "error_type": "FileNotFoundError",
                     "error_text": "compiler was not found", "errno": 2})
        fake = FakeExecutor([error])
        self.assertEqual(self.run_fake(fake), 1)
        self.assertEqual(fake.calls, ["A", "test"])
        self.assertFalse(test_gate_path(self.repo).exists())
        self.assertEqual(self.state()["completed"], [])
        latch = self.state()["test_environment_blocker"]
        diagnostic_path = self.logs / latch["diagnostic"]
        diagnostic = json.loads(diagnostic_path.read_text())
        self.assertEqual(diagnostic["classification"], "confirmed_environment_blocker")
        self.assertEqual(diagnostic["task_id"], "one")
        self.assertEqual(diagnostic["a_attempt"], 1)
        self.assertEqual(diagnostic["test_index"], 1)
        self.assertEqual(diagnostic["executable"], "compiler")
        self.assertEqual(diagnostic["errno"], 2)
        self.assertEqual(diagnostic["supporting_evidence"]["error_text"], "compiler was not found")
        self.assertEqual(diagnostic["process_started"], False)
        self.assertEqual(diagnostic["cleanup_confirmed"], True)
        self.assertEqual(diagnostic["output_artifacts"], [])
        self.assertFalse(any(event["event"] in {"review_started", "checkpoint_started", "checkpoint_committed"}
                             for event in self.events()))
        resumed = FakeExecutor()
        with self.assertRaisesRegex(runner.PilotError, "requires inspection before resume"):
            self.run_fake(resumed, resume=True)
        self.assertEqual(resumed.calls, [])

    def test_ambiguous_execution_problem_stops_without_claiming_permission_failure(self):
        error = RuntimeError("browser process crashed")
        error.cleanup_confirmed = True
        error.process_started = True
        fake = FakeExecutor([error])
        self.assertEqual(self.run_fake(fake), 1)
        self.assertEqual(fake.calls, ["A", "test"])
        diagnostic = json.loads((self.logs / self.state()["test_environment_blocker"]["diagnostic"]).read_text())
        self.assertEqual(diagnostic["classification"], "unclassified_execution_problem")
        self.assertIn("does not establish why execution failed", diagnostic["unknown"])
        self.assertNotIn("permission", diagnostic["classification"])
        with self.assertRaisesRegex(runner.PilotError, "requires inspection before resume"):
            self.run_fake(FakeExecutor(), resume=True)

    def test_sandbox_uncertain_cleanup_blocks_recovery(self):
        from auto_coding.controller_test_sandbox import ControllerTestEnvironmentError
        error = ControllerTestEnvironmentError("MXC cleanup uncertain", cleanup_confirmed=False, process_started=True)
        fake = FakeExecutor([error])
        self.assertEqual(self.run_fake(fake), 1)
        self.assert_stopped_uncertain(fake)
        with self.assertRaises(TestProcessSafetyError): self.run_fake(FakeExecutor(), resume=True)
        diagnostic = json.loads((self.logs / self.state()["test_environment_blocker"]["diagnostic"]).read_text())
        self.assertFalse(diagnostic["cleanup_confirmed"])

    def test_legacy_and_explicitly_confirmed_success_permit_normal_continuation(self):
        for position, results in enumerate(([], [runner._CallResult(0, "OK", False, True, True)])):
            with self.subTest(results=results):
                self.repo = self.make_repo(f"success-{position}")
                self.logs = self.root / f"success-logs-{position}"
                fake = FakeExecutor(results)
                self.assertEqual(self.run_fake(fake), 0)
                self.assertEqual(fake.calls, ["A", "test", "test", "B"])
                self.assertFalse(test_gate_path(self.repo).exists())
                assert_test_process_safe(self.repo)

    def test_failed_test_with_confirmed_cleanup_keeps_normal_correction_behavior(self):
        fake = FakeExecutor([runner._CallResult(1, "assertion failed", False, True, True)])
        self.assertEqual(self.run_fake(fake), 0)
        self.assertEqual(fake.calls, ["A", "test", "test", "A", "test", "test", "B"])
        self.assertTrue(any(event["event"] == "correction_requested" for event in self.events()))
        self.assertNotIn("uncertain_test", self.state())
        assert_test_process_safe(self.repo)

    def test_unverified_results_stop_for_every_exit_status_without_commit_or_publication(self):
        for position, (code, timed) in enumerate(((0, False), (1, False), (124, True), (125, False), (127, False))):
            with self.subTest(code=code, timed=timed):
                self.repo = self.make_repo(f"uncertain-{position}")
                self.logs = self.root / f"uncertain-logs-{position}"
                before = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=self.repo)
                fake = FakeExecutor([runner._CallResult(code, "output", timed, False, True)])
                with patch.object(runner, "local_commit") as commit, \
                        patch.object(GitHubWorkspaceManager, "publish_checkpoint") as publish:
                    self.assertEqual(self.run_fake(fake, auto_push={"repository": "owner/repo",
                                                                   "branch": "feature/test"}), 1)
                    commit.assert_not_called()
                    publish.assert_not_called()
                self.assert_stopped_uncertain(fake)
                self.assertEqual(subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=self.repo), before)
                metrics = json.loads((self.logs / "metrics.json").read_text())["run"]
                self.assertEqual(metrics["codex_a"]["invocations"], 1)
                self.assertEqual(metrics["tests"]["invocations"], 1)
                self.assertEqual(metrics["tests"]["outcomes"], {"cleanup_unverified": 1})
                self.assertEqual(metrics["correction_attempts"], 0)
                self.assertEqual(self.events()[-1]["metadata"]["reason"], "test process cleanup unverified")

    def test_unmanaged_restart_resume_new_logs_and_background_launch_are_blocked(self):
        fake = FakeExecutor([runner._CallResult(125, "limit", False, False, True)])
        self.assertEqual(self.run_fake(fake), 1)
        self.assert_stopped_uncertain(fake)
        execute = Mock(side_effect=AssertionError("Execution must remain blocked"))
        for resume in (False, True):
            with self.subTest(resume=resume), self.assertRaises(TestProcessSafetyError):
                verified_run(self.plan, self.repo, self.logs if resume else self.root / "new-logs",
                           resume=resume, execute=execute)
        with self.assertRaises(TestProcessSafetyError):
            reserve_job(self.repo)
        controller = DesktopController(self.root / "fresh-controller-runs")
        controller.repo, controller.plan = self.repo, self.plan
        with patch("auto_coding.desktop_control.Popen") as spawn, \
                patch("auto_coding.desktop_control.bounded_run") as git:
            self.assertIn("Unresolved test-process cleanup", " ".join(controller.preflight()["errors"]))
            self.assertFalse(controller.start()["ok"])
            spawn.assert_not_called()
            git.assert_not_called()
        execute.assert_not_called()
        env = os.environ.copy()
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
        script = """
from pathlib import Path
import sys
from auto_coding.simple_run import run
from auto_coding.test_process_safety import TestProcessSafetyError
def execute(*a, **kw):
    raise AssertionError('A restarted process must not execute')
try:
    run(Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3]), execute=execute)
except TestProcessSafetyError:
    print('blocked across process restart')
else:
    raise AssertionError('Restart bypassed uncertainty')
"""
        result = subprocess.run([sys.executable, "-B", "-c", script, str(self.plan), str(self.repo),
                                 str(self.root / "restart-logs")], env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("blocked across process restart", result.stdout)

    def test_managed_workspace_retains_job_lock_and_missing_locks_are_not_cleanup_proof(self):
        self.repo = self.make_repo("workspaces/" + "a" * 64)
        fake = FakeExecutor([runner._CallResult(125, "limit", False, False, True)])
        self.assertEqual(self.run_fake(fake), 1)
        self.assert_stopped_uncertain(fake)
        job = managed_lock(self.repo, ".job.lock")
        self.assertTrue((job / "uncertain").is_file())
        manager = GitHubWorkspaceManager(self.root / "config")
        with self.assertRaises(WorkspaceError):
            manager._idle(self.repo)
        # Simulate loss of both disposable test locks; independent uncertainty
        # evidence must still block a new run. No real process was spawned.
        (job / "uncertain").unlink()
        job.rmdir()
        gate = test_gate_path(self.repo)
        (gate / "execution.json").unlink()
        gate.rmdir()
        execute = Mock()
        with self.assertRaises(TestProcessSafetyError):
            verified_run(self.plan, self.repo, self.root / "new-logs", execute=execute)
        execute.assert_not_called()
        with self.assertRaises(WorkspaceError):
            manager._idle(self.repo)
        with patch.object(manager, "_git") as git:
            with self.assertRaises(TestProcessSafetyError):
                manager.publish_checkpoint(self.repo, "owner/repo", "feature/test", "sha", {})
            git.assert_not_called()

    def test_saved_run_uncertainty_also_blocks_resume_when_repository_records_are_lost(self):
        fake = FakeExecutor([runner._CallResult(1, "failed", False, False, True)])
        self.assertEqual(self.run_fake(fake), 1)
        gate = test_gate_path(self.repo)
        (gate / "execution.json").unlink()
        gate.rmdir()
        (gate.parent / "auto-coding-test-process-uncertain.json").unlink()
        from auto_coding.repository_ownership import execution_lock
        # The whole-run guard now also protects this fake test execution.
        execution_lock(self.repo, ".process-uncertain.json").unlink()
        job = execution_lock(self.repo, ".job.lock")
        (job / "uncertain").unlink()
        job.rmdir()
        execute = Mock()
        for status in ("stopped", "completed"):
            state = self.state()
            state["status"] = status
            (self.logs / "state.json").write_text(json.dumps(state))
            with self.subTest(status=status), self.assertRaisesRegex(runner.PilotError, "saved state"):
                self.run_fake(execute, resume=True)
        execute.assert_not_called()

    def test_uncertainty_survives_failure_to_save_run_state(self):
        fake = FakeExecutor([runner._CallResult(0, "exited", False, False, True)])
        original = runner.save_json
        def save(path, value):
            if "uncertain_test" in value:
                raise OSError("state store unavailable")
            return original(path, value)
        with patch.object(runner, "save_json", side_effect=save), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaisesRegex(OSError, "state store unavailable"):
                self.run_fake(fake)
        self.assertEqual(fake.calls, ["A", "test"])
        with self.assertRaises(TestProcessSafetyError):
            verified_run(self.plan, self.repo, self.root / "different-logs", execute=Mock())

    def test_uncertainty_is_saved_before_test_output_io(self):
        fake = FakeExecutor([runner._CallResult(125, "limit", False, False, True)])
        original = Path.write_text
        def write(path, *args, **kwargs):
            if path.name == "one-01-test-1.txt":
                raise OSError("output store unavailable")
            return original(path, *args, **kwargs)
        with patch.object(Path, "write_text", write):
            with self.assertRaisesRegex(OSError, "output store unavailable"):
                self.run_fake(fake)
        self.assert_stopped_uncertain(fake)

    def test_safety_record_io_is_not_counted_as_test_execution_duration(self):
        now = [100.0]
        fake = FakeExecutor([runner._CallResult(0, "exited", False, False, True)])
        original = TestProcessGate.keep_uncertain
        def execute(*args, **kwargs):
            result = fake(*args, **kwargs)
            if args[0][0] == "fake-test":
                now[0] += 2.0
            return result
        def keep(gate):
            original(gate)
            now[0] += 50.0  # Simulated storage latency, not process execution.
        with patch.object(runner, "_execution_clock", side_effect=lambda: now[0]), \
                patch.object(TestProcessGate, "keep_uncertain", keep):
            self.assertEqual(self.run_fake(execute), 1)
        metrics = json.loads((self.logs / "metrics.json").read_text())["run"]
        self.assertEqual(metrics["tests"]["duration_seconds"], 2.0)

    def test_repository_record_write_failure_keeps_the_gate_and_saved_uncertainty(self):
        fake = FakeExecutor([runner._CallResult(125, "limit", False, False, True)])
        with patch.object(TestProcessGate, "keep_uncertain", side_effect=OSError("Git metadata store failed")):
            with self.assertRaisesRegex(OSError, "Git metadata store failed"):
                self.run_fake(fake)
        self.assertEqual(fake.calls, ["A", "test"])
        self.assertIn("uncertain_test", self.state())
        with self.assertRaises(TestProcessSafetyError):
            verified_run(self.plan, self.repo, self.root / "new-logs", execute=Mock())

    def test_proven_clean_result_does_not_leave_uncertainty_after_output_write_failure(self):
        fake = FakeExecutor([runner._CallResult(0, "passed", False, True, True)])
        original = Path.write_text
        def write(path, *args, **kwargs):
            if path.name == "one-01-test-1.txt":
                raise OSError("output store failed")
            return original(path, *args, **kwargs)
        with patch.object(Path, "write_text", write):
            with self.assertRaisesRegex(OSError, "output store failed"):
                self.run_fake(fake)
        self.assertEqual(fake.calls, ["A", "test"])
        self.assertNotIn("uncertain_test", self.state())
        assert_test_process_safe(self.repo)

    def test_gate_initialization_failure_never_executes_a_test_or_marks_a_clean_process_unsafe(self):
        fake = FakeExecutor()
        original = Path.open
        def open_file(path, *args, **kwargs):
            if path.name == "execution.json":
                raise OSError("gate record unavailable")
            return original(path, *args, **kwargs)
        with patch.object(Path, "open", open_file):
            with self.assertRaisesRegex(OSError, "gate record unavailable"):
                self.run_fake(fake)
        self.assertEqual(fake.calls, ["A"])
        self.assertNotIn("uncertain_test", self.state())
        assert_test_process_safe(self.repo)

    def test_gate_initialization_collision_preserves_unknown_ownership(self):
        fake = FakeExecutor()
        original = Path.open
        def open_file(path, *args, **kwargs):
            if path.name == "execution.json" and args[:1] == ("x",):
                with original(path, "w", encoding="utf-8") as stream:
                    stream.write('{"token":"other owner"}')
            return original(path, *args, **kwargs)
        with patch.object(Path, "open", open_file):
            with self.assertRaises(FileExistsError):
                self.run_fake(fake)
        self.assertEqual(fake.calls, ["A"])
        record = test_gate_path(self.repo) / "execution.json"
        self.assertEqual(json.loads(record.read_text())["token"], "other owner")
        with self.assertRaises(TestProcessSafetyError):
            assert_test_process_safe(self.repo)

    def test_test_exceptions_with_unverified_cleanup_persist_uncertainty(self):
        for position, error in enumerate((OSError("unknown child status"), KeyboardInterrupt())):
            with self.subTest(error=type(error).__name__):
                self.repo = self.make_repo(f"exception-{position}")
                self.logs = self.root / f"exception-logs-{position}"
                error.cleanup_confirmed = False
                fake = FakeExecutor([error])
                if isinstance(error, KeyboardInterrupt):
                    with self.assertRaises(KeyboardInterrupt):
                        self.run_fake(fake)
                else:
                    self.assertEqual(self.run_fake(fake), 1)
                self.assert_stopped_uncertain(fake)

    def test_confirmed_exception_and_timeout_do_not_leave_false_uncertainty(self):
        error = OSError("test interrupted after confirmed cleanup")
        error.cleanup_confirmed = True
        self.assertEqual(self.run_fake(FakeExecutor([error])), 1)
        assert_test_process_safe(self.repo)
        self.assertNotIn("uncertain_test", self.state())
        self.assertEqual(self.state()["test_environment_blocker"]["classification"],
                         "unclassified_execution_problem")
        self.repo = self.make_repo("confirmed-timeout")
        self.logs = self.root / "confirmed-timeout-logs"
        fake = FakeExecutor([runner._CallResult(124, "timeout", True, True, True)])
        self.assertEqual(self.run_fake(fake), 1)
        self.assertEqual(fake.calls, ["A", "test"])
        assert_test_process_safe(self.repo)
        self.assertNotIn("uncertain_test", self.state())

    def test_legacy_timeout_without_cleanup_proof_remains_blocking(self):
        fake = FakeExecutor([(124, "timeout", True)])
        self.assertEqual(self.run_fake(fake), 1)
        self.assert_stopped_uncertain(fake)

    def test_inflight_gate_and_changed_owner_never_auto_expire(self):
        gate = TestProcessGate(self.repo, {"task": "one", "index": 1, "pid": None})
        os.utime(gate.path, (1, 1))
        with self.assertRaises(TestProcessSafetyError):
            assert_test_process_safe(self.repo)
        record = gate.path / "execution.json"
        value = json.loads(record.read_text())
        value["token"] = "different owner"
        record.write_text(json.dumps(value))
        with self.assertRaises(TestProcessSafetyError):
            gate.finish_confirmed()
        self.assertTrue(gate.path.is_dir())

    def test_malformed_uncertainty_evidence_still_blocks_execution(self):
        marker = test_gate_path(self.repo).parent / "auto-coding-test-process-uncertain.json"
        marker.write_text("{incomplete")
        with self.assertRaises(TestProcessSafetyError):
            assert_test_process_safe(self.repo)
        with self.assertRaises(TestProcessSafetyError):
            verified_run(self.plan, self.repo, self.logs, execute=Mock())

    def test_gate_supports_git_worktree_pointers(self):
        worktree = self.root / "linked"
        subprocess.run(["git", "worktree", "add", "--detach", str(worktree), "HEAD"], cwd=self.repo,
                       check=True, capture_output=True)
        expected = Path(subprocess.check_output(["git", "rev-parse", "--absolute-git-dir"],
                                                cwd=worktree, text=True).strip())
        gate = TestProcessGate(worktree, {"task": "one", "index": 1})
        self.assertEqual(gate.path.parent, expected.resolve())
        with self.assertRaises(TestProcessSafetyError):
            assert_test_process_safe(worktree)
        gate.finish_confirmed()
        assert_test_process_safe(worktree)


if __name__ == "__main__":
    unittest.main()
