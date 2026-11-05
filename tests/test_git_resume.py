"""Branch binding and completed-checkpoint recovery never invoke real agents."""
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


class FakeExecutor:
    def __init__(self, *, fail_second=False, hook=None):
        self.calls = []
        self.fail_second = fail_second
        self.hook = hook

    def __call__(self, argv, cwd, timeout, *, prompt=None):
        if argv[0] == "fake-test":
            role = "test"
        elif "--output-schema" in argv:
            role = "B"
            Path(argv[argv.index("--output-last-message") + 1]).write_text(
                json.dumps({"decision": "APPROVED", "issues": [], "summary": "OK"}))
        else:
            role = "A"
            if self.fail_second and "TASK:\nSECOND" in prompt:
                self.calls.append(role)
                return runner._CallResult(1, "confirmed process failure", False, True, True)
            name = "second.txt" if "TASK:\nSECOND" in prompt else "first.txt"
            (cwd / name).write_text("implementation")
            Path(argv[argv.index("--output-last-message") + 1]).write_text("done")
        self.calls.append(role)
        if self.hook:
            self.hook(role)
        return runner._CallResult(0, "completed", False, True, True)


class GitResumeTests(unittest.TestCase):
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
        self.write_plan([{"id": "one", "prompt": "FIRST", "test_commands": [["fake-test"]]}])

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.repo, check=True,
                              capture_output=True, text=True).stdout.strip()

    def write_plan(self, tasks):
        self.plan.write_text(json.dumps({"tasks": tasks}))

    def run_fake(self, fake, **options):
        with contextlib.redirect_stdout(io.StringIO()):
            return verified_run(self.plan, self.repo, self.logs, execute=fake, **options)

    def state(self):
        return json.loads((self.logs / "state.json").read_text())

    def metadata(self):
        return {str(p.relative_to(self.repo / ".git")): p.read_bytes()
                for p in (self.repo / ".git").rglob("*") if p.is_file()}

    def assert_invalid_without_execution_or_git_mutation(self, **options):
        before = self.metadata()
        executor = Mock(side_effect=AssertionError("An invalid Git state must never launch an agent"))
        with self.assertRaises(runner.PilotError):
            self.run_fake(executor, **options)
        executor.assert_not_called()
        self.assertEqual(self.metadata(), before)

    def test_direct_main_and_master_are_rejected_without_git_mutation(self):
        for branch in ("main", "master"):
            with self.subTest(branch=branch):
                self.git("switch", "-c", branch)
                self.assert_invalid_without_execution_or_git_mutation()
                self.git("switch", "feature/test")

    def test_cli_main_rejects_before_constructing_a_codex_command(self):
        self.git("switch", "-c", "main")
        before = self.metadata()
        with patch.object(runner.shutil, "which", return_value="fake-codex"), \
                patch.object(runner, "codex_argv", side_effect=AssertionError("No Codex calls")) as codex, \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(runner.main([str(self.plan), "--repo", str(self.repo), "--logs", str(self.logs)]), 1)
        codex.assert_not_called()
        self.assertEqual(self.metadata(), before)

    def test_detached_head_is_rejected_without_git_mutation(self):
        self.git("switch", "--detach")
        self.assert_invalid_without_execution_or_git_mutation()

    def test_feature_branch_is_bound_to_manifest_state_and_approval(self):
        fake = FakeExecutor()
        self.assertEqual(self.run_fake(fake), 0)
        self.assertEqual(fake.calls, ["A", "test", "B"])
        state = self.state()
        manifest = json.loads((self.logs / "run-manifest.json").read_text())
        self.assertEqual(state["branch"], "feature/test")
        self.assertEqual(manifest["branch"], state["branch"])
        self.assertEqual(state["history"][-1]["branch"], state["branch"])
        self.assertEqual(state["initial_git_head"], self.initial_head)

    def test_new_disposable_repo_uses_feature_branch(self):
        target = self.root / "new"
        fake = FakeExecutor()
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(verified_run(self.plan, target, self.logs, create_repo=True, execute=fake), 0)
        self.assertEqual(runner.git(target, ["branch", "--show-current"]), "feature/auto-coding")

    def test_same_sha_branch_switch_on_resume_is_rejected(self):
        fake = FakeExecutor(fail_second=True)
        self.write_plan([
            {"id": "one", "prompt": "FIRST", "test_commands": [["fixture-focused-check"]]},
            {"id": "two", "prompt": "SECOND", "test_commands": [["fixture-focused-check"]]}])
        self.assertEqual(self.run_fake(fake), 1)
        saved_head = self.git("rev-parse", "HEAD")
        self.git("switch", "-c", "feature/other")
        self.assertEqual(self.git("rev-parse", "HEAD"), saved_head)
        self.assert_invalid_without_execution_or_git_mutation(resume=True)

    def test_a_branch_switch_at_identical_sha_stops_before_staging_or_review(self):
        self.git("branch", "feature/other")
        fake = FakeExecutor(hook=lambda role: self.git("switch", "feature/other") if role == "A" else None)
        self.assertEqual(self.run_fake(fake), 1)
        self.assertEqual(fake.calls, ["A"])
        self.assertEqual(self.git("diff", "--cached", "--name-only"), "")
        self.assertEqual(self.git("rev-parse", "HEAD"), self.initial_head)
        self.assertEqual(self.state()["completed"], [])

    def test_test_branch_switch_stops_before_another_test_or_b(self):
        self.git("branch", "feature/other")
        self.write_plan([{"id": "one", "prompt": "FIRST", "test_commands": [["fake-test"], ["fake-test"]]}])
        fake = FakeExecutor(hook=lambda role: self.git("switch", "feature/other") if role == "test" else None)
        with self.assertRaisesRegex(runner.PilotError, "branch changed"):
            self.run_fake(fake)
        self.assertEqual(fake.calls, ["A", "test"])
        self.assertEqual(self.git("rev-parse", "HEAD"), self.initial_head)

    def test_reviewer_branch_switch_cannot_approve_or_commit(self):
        self.git("branch", "feature/other")
        fake = FakeExecutor(hook=lambda role: self.git("switch", "feature/other") if role == "B" else None)
        with self.assertRaisesRegex(runner.PilotError, "branch changed"):
            self.run_fake(fake)
        self.assertEqual(fake.calls, ["A", "test", "B"])
        self.assertEqual(self.git("rev-parse", "HEAD"), self.initial_head)
        self.assertIsNone(self.state()["pending_checkpoint"])
        self.assertEqual(self.state()["completed"], [])

    def test_clean_reset_cannot_skip_a_completed_task(self):
        self.write_plan([
            {"id": "one", "prompt": "FIRST", "test_commands": [["fixture-focused-check"]]},
            {"id": "two", "prompt": "SECOND", "test_commands": [["fixture-focused-check"]]}])
        self.assertEqual(self.run_fake(FakeExecutor(fail_second=True)), 1)
        self.assertEqual(self.state()["completed"], ["one"])
        self.git("reset", "--hard", self.initial_head)  # This disposable fixture only.
        self.assertEqual(self.git("status", "--porcelain"), "")
        self.assert_invalid_without_execution_or_git_mutation(resume=True)
        self.assertEqual(self.state()["completed"], ["one"])

    def test_completed_run_is_revalidated_instead_of_returning_early(self):
        self.assertEqual(self.run_fake(FakeExecutor()), 0)
        self.git("reset", "--hard", self.initial_head)
        self.assert_invalid_without_execution_or_git_mutation(resume=True)

    def test_valid_completed_resume_does_not_repeat_agents_tests_or_commit(self):
        self.assertEqual(self.run_fake(FakeExecutor()), 0)
        history = self.state()["history"]
        expected = self.git("rev-parse", "HEAD")
        fake = Mock(side_effect=AssertionError("Completed work must not repeat"))
        self.assertEqual(self.run_fake(fake, resume=True), 0)
        fake.assert_not_called()
        self.assertEqual(self.state()["history"], history)
        self.assertEqual(self.git("rev-parse", "HEAD"), expected)

    def test_valid_pending_approved_checkpoint_recovers_without_agent_calls(self):
        with patch.object(runner, "local_commit", side_effect=runner.PilotError("before commit")):
            self.assertEqual(self.run_fake(FakeExecutor()), 1)
        self.assertEqual(self.state()["status"], "checkpointing")
        fake = Mock(side_effect=AssertionError("Approval recovery must not repeat an agent"))
        self.assertEqual(self.run_fake(fake, resume=True), 0)
        fake.assert_not_called()
        self.assertEqual(self.state()["completed"], ["one"])

    def test_verified_legacy_commit_history_can_bind_branch_without_inventing_progress(self):
        self.assertEqual(self.run_fake(FakeExecutor()), 0)
        state, manifest_path = self.state(), self.logs / "run-manifest.json"
        state.pop("branch")
        state.pop("initial_git_head")
        state.pop("process_ownership")
        for record in state["history"]:
            record.pop("branch")
        expected_history = state["history"]
        (self.logs / "state.json").write_text(json.dumps(state))
        manifest = json.loads(manifest_path.read_text())
        manifest.pop("branch")
        manifest_path.write_text(json.dumps(manifest))
        fake = Mock()
        self.assertEqual(self.run_fake(fake, resume=True), 0)
        fake.assert_not_called()
        self.assertEqual(self.state()["history"], expected_history)
        self.assertEqual(self.state()["branch"], "feature/test")

    def test_legacy_completed_task_without_commit_evidence_is_rejected(self):
        self.assertEqual(self.run_fake(FakeExecutor()), 0)
        state = self.state()
        state.pop("branch")
        state["history"][0].pop("commit")
        (self.logs / "state.json").write_text(json.dumps(state))
        self.assert_invalid_without_execution_or_git_mutation(resume=True)
        self.assertNotIn("commit", self.state()["history"][0])

    def test_branch_mismatch_in_manifest_is_not_overwritten(self):
        self.assertEqual(self.run_fake(FakeExecutor()), 0)
        path = self.logs / "run-manifest.json"
        manifest = json.loads(path.read_text())
        manifest["branch"] = "feature/other"
        path.write_text(json.dumps(manifest))
        original = path.read_bytes()
        self.assert_invalid_without_execution_or_git_mutation(resume=True)
        self.assertEqual(path.read_bytes(), original)

    def test_legacy_a_timeout_requires_cleanup_proof_and_cannot_escape_via_new_logs(self):
        failed = Mock(return_value=runner._CallResult(124, "timeout", True, True, True))
        self.assertEqual(self.run_fake(failed), 1)
        state = self.state()
        state.pop("process_ownership")
        state["history"][0].pop("a_cleanup_confirmed")
        state["history"][0].pop("a_process_started")
        path = self.logs / "state.json"
        path.write_text(json.dumps(state))
        self.assert_invalid_without_execution_or_git_mutation(resume=True)
        executor = Mock()
        with self.assertRaisesRegex(OSError, "cleanup remains unverified"):
            verified_run(self.plan, self.repo, self.root / "fresh-logs", execute=executor)
        executor.assert_not_called()

    def test_legacy_timeout_does_not_override_saved_confirmed_cleanup(self):
        failed = Mock(return_value=runner._CallResult(124, "timeout", True, True, True))
        self.assertEqual(self.run_fake(failed), 1)
        state = self.state()
        state.pop("process_ownership")
        (self.logs / "state.json").write_text(json.dumps(state))
        self.assertEqual(self.run_fake(FakeExecutor(), resume=True), 0)

    def test_legacy_reserved_b_call_cannot_reuse_an_earlier_failure_kind_as_cleanup_proof(self):
        class Interrupted(Exception):
            cleanup_confirmed = True
        def interrupt(role):
            if role == "B":
                raise Interrupted("confirmed fake interruption")
        with self.assertRaises(Interrupted):
            self.run_fake(FakeExecutor(hook=interrupt))
        state = self.state()
        state.pop("process_ownership")
        state["pending_review"]["review_failure_kind"] = "process_failure"
        (self.logs / "state.json").write_text(json.dumps(state))
        self.assert_invalid_without_execution_or_git_mutation(resume=True)

    def test_malformed_history_does_not_become_completed_progress(self):
        self.assertEqual(self.run_fake(FakeExecutor()), 0)
        state = self.state()
        state["history"] = [None]
        (self.logs / "state.json").write_text(json.dumps(state))
        self.assert_invalid_without_execution_or_git_mutation(resume=True)

    def test_explicit_uncertain_cleanup_is_not_overridden_by_an_ownership_version(self):
        self.assertEqual(self.run_fake(FakeExecutor()), 0)
        state = self.state()
        state["history"][0]["a_cleanup_confirmed"] = False
        (self.logs / "state.json").write_text(json.dumps(state))
        self.assert_invalid_without_execution_or_git_mutation(resume=True)

    def test_completed_checkpoint_patch_identity_is_verified_on_resume(self):
        self.assertEqual(self.run_fake(FakeExecutor()), 0)
        state = self.state()
        state["history"][0]["diff_sha256"] = "0" * 64
        (self.logs / "state.json").write_text(json.dumps(state))
        self.assert_invalid_without_execution_or_git_mutation(resume=True)

    def test_pending_checkpoint_branch_mismatch_prevents_recovery_commit(self):
        with patch.object(runner, "local_commit", side_effect=runner.PilotError("before commit")):
            self.assertEqual(self.run_fake(FakeExecutor()), 1)
        state = self.state()
        state["pending_checkpoint"]["branch"] = "feature/other"
        (self.logs / "state.json").write_text(json.dumps(state))
        self.assert_invalid_without_execution_or_git_mutation(resume=True)
        self.assertEqual(self.git("rev-parse", "HEAD"), self.initial_head)

    def test_pending_approval_cannot_be_reassigned_to_a_later_task(self):
        self.write_plan([
            {"id": "one", "prompt": "FIRST", "test_commands": [["fixture-focused-check"]]},
            {"id": "two", "prompt": "SECOND", "test_commands": [["fixture-focused-check"]]}])
        with patch.object(runner, "local_commit", side_effect=runner.PilotError("before commit")):
            self.assertEqual(self.run_fake(FakeExecutor()), 1)
        state = self.state()
        state["pending_checkpoint"]["task"] = "two"
        state["pending_checkpoint"]["record"]["task"] = "two"
        (self.logs / "state.json").write_text(json.dumps(state))
        self.assert_invalid_without_execution_or_git_mutation(resume=True)


if __name__ == "__main__":
    unittest.main()
