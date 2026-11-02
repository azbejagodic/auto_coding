"""Durable A identity and interruption recovery; no real Codex processes."""
import contextlib
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from auto_coding import simple_run as runner
from backend_fixtures import run_with_verification as verified_run
from auto_coding.repository_ownership import execution_lock
from test_git_resume import FakeExecutor


class AttemptRecoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.git("init", "-b", "feature/attempts")
        self.git("config", "user.name", "Test")
        self.git("config", "user.email", "test@example.invalid")
        (self.repo / "seed.txt").write_text("seed")
        self.git("add", "-A")
        self.git("commit", "-m", "seed")
        self.initial = self.git("rev-parse", "HEAD")
        self.logs = self.root / "logs"
        self.plan = self.root / "tasks.json"
        self.plan.write_text(json.dumps({"tasks": [{"id": "one", "prompt": "FIRST",
            "test_commands": [["fake-test"]]}]}))

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.repo, capture_output=True,
                              text=True, check=True, timeout=15).stdout.strip()

    def state(self):
        return json.loads((self.logs / "state.json").read_text())

    def run_fake(self, execute, **kwargs):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return verified_run(self.plan, self.repo, self.logs, execute=execute, **kwargs)

    def stop_stream_write(self, fake):
        original = runner.write_attempt_evidence
        def write(path, text):
            if path.name.endswith("-a-events.jsonl"):
                raise OSError("injected A evidence write failure")
            return original(path, text)
        with patch.object(runner, "write_attempt_evidence", side_effect=write):
            with self.assertRaisesRegex(OSError, "evidence write"):
                self.run_fake(fake)

    def test_identity_is_persisted_before_launch_and_bound_to_record(self):
        observed = []
        fake = FakeExecutor()
        def execute(*args, **kwargs):
            if not fake.calls:
                pending = self.state()["pending_execution"]
                self.assertEqual(pending["phase"], "running")
                self.assertEqual(pending["base_head"], self.initial)
                self.assertEqual(pending["task"], "one")
                self.assertEqual(pending["attempt"], 1)
                observed.append(pending["id"])
            return fake(*args, **kwargs)
        self.assertEqual(self.run_fake(execute), 0)
        self.assertEqual(fake.calls, ["A", "test", "B"])
        self.assertEqual(self.state()["history"][0]["attempt_id"], observed[0])
        self.assertIsNone(self.state()["pending_execution"])

    def test_interruption_before_launch_resumes_same_unstarted_identity(self):
        fake = FakeExecutor()
        with patch.object(runner, "codex_argv", side_effect=runner.PilotError("before launch")):
            with self.assertRaisesRegex(runner.PilotError, "before launch"):
                self.run_fake(fake)
        pending = self.state()["pending_execution"]
        self.assertEqual(pending["phase"], "unstarted")
        prompt = (self.logs / "one-01-a-prompt.txt").read_bytes()
        self.assertEqual(fake.calls, [])
        self.assertEqual(self.run_fake(fake, resume=True), 0)
        self.assertEqual(fake.calls, ["A", "test", "B"])
        self.assertEqual(self.state()["history"][0]["attempt_id"], pending["id"])
        self.assertEqual((self.logs / "one-01-a-prompt.txt").read_bytes(), prompt)

    def test_interruption_after_launch_keeps_uncertainty_and_blocks_new_run(self):
        error = OSError("executor interrupted")
        error.cleanup_confirmed = False
        execute = Mock(side_effect=error)
        with self.assertRaisesRegex(OSError, "executor interrupted"):
            self.run_fake(execute)
        execute.assert_called_once()
        self.assertEqual(self.state()["pending_execution"]["phase"], "uncertain")
        self.assertTrue(execution_lock(self.repo, ".process-uncertain.json").is_file())
        other = Mock()
        with self.assertRaises(OSError):
            self.run_fake(other, resume=True)
        with self.assertRaises(OSError):
            verified_run(self.plan, self.repo, self.root / "other", execute=other)
        other.assert_not_called()

    def test_returned_uncertain_result_preserves_observed_metrics_and_safety(self):
        execute = Mock(return_value=runner._CallResult(0, "process returned", False, False, True))
        self.assertEqual(self.run_fake(execute), 1)
        execute.assert_called_once()
        metrics = json.loads((self.logs / "metrics.json").read_text())["run"]
        self.assertEqual(metrics["codex_a"]["invocations"], 1)
        self.assertEqual(metrics["codex_a"]["outcomes"], {"cleanup_unverified": 1})
        events = [json.loads(line) for line in (self.logs / "events.jsonl").read_text().splitlines()]
        self.assertEqual(events[-1]["event"], "stopped")
        self.assertEqual(events[-1]["metadata"]["reason"], "Codex A process cleanup unverified")
        self.assertEqual(self.state()["completed"], [])
        self.assertTrue(execution_lock(self.repo, ".process-uncertain.json").is_file())

    def test_evidence_failure_recovers_completed_a_without_reinvocation(self):
        fake = FakeExecutor()
        self.stop_stream_write(fake)
        pending = self.state()["pending_execution"]
        self.assertEqual(pending["phase"], "completed")
        self.assertTrue(pending["diff_sha256"])
        self.assertEqual(fake.calls, ["A"])
        self.assertEqual(self.git("rev-parse", "HEAD"), self.initial)
        self.assertEqual(self.run_fake(fake, resume=True), 0)
        self.assertEqual(fake.calls, ["A", "test", "B"])
        metrics = json.loads((self.logs / "metrics.json").read_text())["run"]
        self.assertEqual(metrics["codex_a"]["invocations"], 1)
        self.assertEqual(metrics["implementation_attempts"], 1)

    def test_review_checkpoint_write_failure_retains_a_and_prior_test_artifacts(self):
        fake = FakeExecutor()
        original, failed = runner.save_json, []
        def save(path, value):
            if path.name == "state.json" and value.get("pending_review") and not failed:
                failed.append(True)
                raise OSError("review checkpoint write failure")
            return original(path, value)
        with patch.object(runner, "save_json", side_effect=save):
            with self.assertRaisesRegex(OSError, "checkpoint write"):
                self.run_fake(fake)
        self.assertEqual(fake.calls, ["A", "test"])
        prior = {p.name: p.read_bytes() for p in self.logs.glob("one-01-*")}
        self.assertEqual(self.run_fake(fake, resume=True), 0)
        self.assertEqual(fake.calls, ["A", "test", "test", "B"])
        for name, data in prior.items():
            self.assertEqual((self.logs / name).read_bytes(), data)
        self.assertEqual(len(list(self.logs.glob("one-01-test-*.txt"))), 2)

    def test_completed_snapshot_tampering_never_launches_another_process(self):
        fake = FakeExecutor()
        self.stop_stream_write(fake)
        (self.repo / "first.txt").write_text("altered after checkpoint")
        self.git("add", "-A")
        execute = Mock()
        with self.assertRaisesRegex(runner.PilotError, "snapshot"):
            self.run_fake(execute, resume=True)
        execute.assert_not_called()
        self.assertEqual(self.git("rev-parse", "HEAD"), self.initial)

    def test_review_cannot_reuse_a_different_attempts_test_artifact(self):
        fake = FakeExecutor()
        with patch.object(runner, "review_ready_task", return_value="stopped"):
            self.assertEqual(self.run_fake(fake), 1)
        state = self.state()
        old = self.logs / "one-99-test-1.txt"
        old.write_bytes((self.logs / "one-01-test-1.txt").read_bytes())
        state["pending_review"]["record"]["tests"][0]["output"] = old.name
        runner.save_json(self.logs / "state.json", state)
        execute = Mock()
        with self.assertRaisesRegex(runner.PilotError, "test evidence path"):
            self.run_fake(execute, resume=True)
        execute.assert_not_called()
        self.assertEqual(self.git("rev-parse", "HEAD"), self.initial)

    def test_missing_snapshot_after_confirmed_exit_stops_without_rerunning_a(self):
        fake = FakeExecutor()
        with patch.object(runner, "staging_summary", side_effect=OSError("snapshot unavailable")):
            with self.assertRaisesRegex(OSError, "snapshot unavailable"):
                self.run_fake(fake)
        self.assertEqual(fake.calls, ["A"])
        self.assertEqual(self.state()["pending_execution"]["phase"], "completed")
        execute = Mock()
        with self.assertRaises(runner.PilotError):
            self.run_fake(execute, resume=True)
        execute.assert_not_called()

    def test_completed_checkpoint_persistence_failure_cannot_replay_a(self):
        fake = FakeExecutor()
        original, failed = runner.save_json, []
        def save(path, value):
            pending = value.get("pending_execution")
            if path.name == "state.json" and pending and pending["phase"] == "completed" and not failed:
                failed.append(True)
                raise OSError("completion persistence failure")
            return original(path, value)
        with patch.object(runner, "save_json", side_effect=save):
            with self.assertRaisesRegex(OSError, "persistence failure"):
                self.run_fake(fake)
        self.assertEqual(fake.calls, ["A"])
        execute = Mock()
        with self.assertRaisesRegex(runner.PilotError, "confirmed completion"):
            self.run_fake(execute, resume=True)
        execute.assert_not_called()
        self.assertEqual(self.git("rev-parse", "HEAD"), self.initial)

    def test_existing_orphan_artifact_is_never_overwritten(self):
        self.logs.mkdir()
        artifact = self.logs / "one-01-a-answer.txt"
        artifact.write_text("previous execution evidence")
        execute = Mock()
        with self.assertRaisesRegex(runner.PilotError, "artifacts already exist"):
            self.run_fake(execute)
        execute.assert_not_called()
        self.assertEqual(artifact.read_text(), "previous execution evidence")

    def test_unstarted_resume_refuses_answer_entries_even_when_the_target_is_missing(self):
        with patch.object(runner, "codex_argv", side_effect=runner.PilotError("before launch")):
            with self.assertRaisesRegex(runner.PilotError, "before launch"):
                self.run_fake(Mock())
        answer = self.logs / "one-01-a-answer.txt"
        self.assertFalse(answer.exists())
        original = Path.lstat
        def lstat(path, *args, **kwargs):
            # A dangling link is an existing directory entry even though exists()
            # is false. Simulate it without requiring Windows symlink privileges.
            return object() if path == answer else original(path, *args, **kwargs)
        execute = Mock()
        with patch.object(Path, "lstat", lstat), self.assertRaisesRegex(runner.PilotError, "answer already exists"):
            self.run_fake(execute, resume=True)
        execute.assert_not_called()

    def test_immutable_evidence_reuses_identical_bytes_and_rejects_changes(self):
        target = self.root / "evidence.txt"
        runner.write_attempt_evidence(target, "original\n")
        runner.write_attempt_evidence(target, "original\n")
        with self.assertRaisesRegex(runner.PilotError, "differs"):
            runner.write_attempt_evidence(target, "replacement")
        self.assertEqual(target.read_bytes(), b"original\n")

    def test_later_task_completed_execution_recovers_after_an_approved_task(self):
        self.plan.write_text(json.dumps({"tasks": [
            {"id": "one", "prompt": "FIRST", "test_commands": [["fake-test"]]},
            {"id": "two", "prompt": "SECOND", "test_commands": [["fake-test"]]}]}))
        original, fake = runner.write_attempt_evidence, FakeExecutor()
        def write(path, text):
            if path.name == "two-01-a-events.jsonl":
                raise OSError("second task evidence failure")
            return original(path, text)
        with patch.object(runner, "write_attempt_evidence", side_effect=write):
            with self.assertRaisesRegex(OSError, "second task"):
                self.run_fake(fake)
        self.assertEqual(self.state()["completed"], ["one"])
        self.assertEqual(fake.calls, ["A", "test", "B", "A"])
        # Simulate interruption during recovery, after its active state save.
        state = self.state()
        state["status"] = "running"
        runner.save_json(self.logs / "state.json", state)
        self.assertEqual(self.run_fake(fake, resume=True), 0)
        self.assertEqual(fake.calls, ["A", "test", "B", "A", "test", "B"])
        self.assertEqual(self.state()["completed"], ["one", "two"])

    def test_corrections_get_distinct_execution_ids_and_artifact_paths(self):
        fake = FakeExecutor()
        def execute(argv, *args, **kwargs):
            result = fake(argv, *args, **kwargs)
            if fake.calls == ["A", "test", "B"]:
                Path(argv[argv.index("--output-last-message") + 1]).write_text(json.dumps({
                    "decision": "REJECTED", "issues": ["Fix the focused defect"], "summary": "Incomplete"}))
            return result
        self.assertEqual(self.run_fake(execute), 0)
        records = self.state()["history"]
        self.assertEqual([record["attempt"] for record in records], [1, 2])
        self.assertNotEqual(records[0]["attempt_id"], records[1]["attempt_id"])
        self.assertTrue((self.logs / "one-01-a-prompt.txt").is_file())
        self.assertTrue((self.logs / "one-02-a-prompt.txt").is_file())


if __name__ == "__main__":
    unittest.main()
