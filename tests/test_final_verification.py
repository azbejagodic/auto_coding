"""Final-suite policy, snapshot and recovery tests, using fake A/B executors."""
import contextlib
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from auto_coding import simple_run as runner
from auto_coding.final_verification import unittest_evidence, verification_policy
from auto_coding.repository_ownership import execution_lock
from backend_fixtures import approved_plan_fixture


PASSED = "Ran 2 tests in 0.002s\n\nOK\n"
FAILED = "Ran 2 tests in 0.002s\n\nFAILED (failures=1)\n"
POLICY = {"test_commands": [["fake-final"]], "minimum_tests": 2, "timeout_seconds": 2400}
CODE_REVIEW_APPROVED = {"decision": "APPROVED", "issues": [],
                        "summary": "Requirements met; optional extra coverage is not a blocker",
                        "verification": {"status": "not_run", "details": "Code review only; B did not execute tests"}}


class Executor:
    def __init__(self, final=(), reviews=(), hook=None, implementation=None, focused=()):
        self.final = list(final)
        self.reviews = list(reviews)
        self.focused = list(focused)
        self.hook, self.implementation = hook, implementation
        self.calls, self.timeouts, self.a_prompts, self.b_prompts = [], [], [], []

    def __call__(self, argv, cwd, timeout, *, prompt=None):
        if argv == ["fake-final"]:
            role = "final"
            result = self.final.pop(0) if self.final else runner._CallResult(0, PASSED, False, True, True)
        elif argv == ["fake-focused"]:
            role = "focused"
            result = self.focused.pop(0) if self.focused else runner._CallResult(0, "focused check passed", False, True, True)
        else:
            answer = Path(argv[argv.index("--output-last-message") + 1])
            if "--output-schema" in argv:
                role = "B"
                self.b_prompts.append(prompt)
                verdict = self.reviews.pop(0) if self.reviews else {"decision": "APPROVED", "issues": [], "summary": "OK"}
                if isinstance(verdict, tuple):
                    result = verdict
                else:
                    answer.write_text(json.dumps(verdict) if isinstance(verdict, dict) else verdict)
                    result = runner._CallResult(0, "review complete", False, True, True)
            else:
                role = "A"
                self.a_prompts.append(prompt)
                number = self.calls.count("A") + 1
                (cwd / "app.py").write_text(self.implementation or f"VERSION = {number}\n")
                answer.write_text(f"Implemented app.py; focused checks passed for implementation {number}")
                result = runner._CallResult(0, "implementation complete", False, True, True)
        self.calls.append(role)
        self.timeouts.append((role, timeout))
        if self.hook:
            self.hook(role)
        if isinstance(result, BaseException):
            raise result
        return result


class VerificationPolicyTests(unittest.TestCase):
    def test_bounded_timeout_and_explicit_coverage_are_required(self):
        for seconds in (301, 1200, 1800, 2400, 86400):
            self.assertEqual(verification_policy(POLICY | {"timeout_seconds": seconds})["timeout_seconds"], seconds)
        for value in (None, {}, POLICY | {"timeout_seconds": True}, POLICY | {"timeout_seconds": 29},
                      POLICY | {"timeout_seconds": 86401}, POLICY | {"minimum_tests": 0},
                      POLICY | {"test_commands": ["echo OK"]}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                verification_policy(value)

    def test_successful_exit_or_incidental_ok_is_not_test_evidence(self):
        self.assertIsNone(unittest_evidence("OK\n"))
        self.assertIsNone(unittest_evidence("all tests passed"))
        self.assertTrue(unittest_evidence(PASSED)["successful"])
        self.assertFalse(unittest_evidence(PASSED + FAILED)["successful"])
        self.assertEqual(unittest_evidence("Ran 2 tests in 0.1s\n\nOK (skipped=1)\n")["skipped"], 1)


class FinalVerificationTests(unittest.TestCase):
    def setUp(self):
        authorization = approved_plan_fixture()
        authorization.__enter__()
        self.addCleanup(authorization.__exit__, None, None, None)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.git("init", "-b", "feature/verification")
        self.git("config", "user.name", "Test")
        self.git("config", "user.email", "test@example.invalid")
        (self.repo / "seed.txt").write_text("seed")
        self.git("add", "-A")
        self.git("commit", "-m", "seed")
        self.initial = self.git("rev-parse", "HEAD")
        self.plan, self.logs = self.root / "plan.json", self.root / "logs"
        self.document = {"tasks": [{"id": "one", "prompt": "Implement only app.py; run the focused checks",
                                    "test_commands": [["fake-focused"]]}], "final_verification": POLICY}
        self.write_plan()
        self.now = 1000.0
        clock = patch.object(runner, "_review_clock", side_effect=lambda: self.now)
        sleep = patch.object(runner, "_review_sleep", side_effect=self.advance)
        clock.start(); sleep.start()
        self.addCleanup(clock.stop); self.addCleanup(sleep.stop)

    def advance(self, seconds):
        self.now += seconds

    def write_plan(self):
        self.plan.write_text(json.dumps(self.document))

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.repo, capture_output=True,
                              text=True, check=True, timeout=15).stdout.strip()

    def state(self):
        return json.loads((self.logs / "state.json").read_text())

    def events(self):
        return [json.loads(line) for line in (self.logs / "events.jsonl").read_text().splitlines()]

    def run_fake(self, fake, **kwargs):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return runner.run(self.plan, self.repo, self.logs, execute=fake, **kwargs)

    def start_controller_workflow(self, fake, **kwargs):
        # Construct a historical saved workflow; resume uses production binding.
        with patch.object(runner, "new_workflow_test_mode", return_value=True):
            return self.run_fake(fake, controller_tests=True, **kwargs)

    def test_saved_controller_workflow_uses_checks_b_commit_then_one_final_suite(self):
        fake = Executor(reviews=[runner._CallResult(127, "unavailable", False, True, False)])
        self.assertEqual(self.start_controller_workflow(fake, timeout=45), 1)
        self.assertEqual(fake.calls, ["A", "focused", "B"])
        self.assertNotIn("fake-focused", fake.a_prompts[0])
        self.assertIn("Do not execute tests", fake.a_prompts[0])
        self.assertIn("passed focused controller verification", fake.b_prompts[0])
        self.assertTrue(self.state()["controller_tests"])
        self.assertEqual(self.run_fake(fake, resume=True, timeout=45), 0)
        self.assertEqual(fake.calls, ["A", "focused", "B", "B", "final"])
        self.assertEqual(dict(fake.timeouts)["focused"], 2400)
        self.assertTrue(self.state()["controller_tests"])
        self.assertEqual(self.state()["history"][0]["tests"][0]["exit_code"], 0)

    def test_new_json_uses_a_focused_checks_b_commit_then_one_final_suite(self):
        fake = Executor()
        with patch.object(runner, "execute_controller_test", wraps=runner.execute_controller_test) as checks:
            self.assertEqual(self.run_fake(fake, timeout=30), 0)
        self.assertEqual(fake.calls, ["A", "B", "final"])
        self.assertEqual(fake.timeouts, [("A", 30), ("B", 30), ("final", 2400)])
        self.assertEqual(checks.call_count, 1)
        self.assertEqual(checks.call_args.args[1], ["fake-final"])
        self.assertIn(json.dumps([["fake-focused"]]), fake.a_prompts[0])
        self.assertIn("Run focused tests", fake.a_prompts[0])
        self.assertIn("including failures or unavailable checks", fake.a_prompts[0])
        self.assertIn("IMPLEMENTER TEST REPORT (claims, not controller verification", fake.b_prompts[0])
        self.assertIn("focused checks passed for implementation 1", fake.b_prompts[0])
        self.assertIn("TEST RESULTS:\n[]", fake.b_prompts[0])
        self.assertNotIn("Approval requires passed focused controller verification", fake.b_prompts[0])
        state = self.state()
        manifest = json.loads((self.logs / "run-manifest.json").read_text())
        self.assertFalse(state["controller_tests"])
        self.assertFalse(manifest["controller_tests"])
        self.assertEqual(state["history"][0]["tests"], [])
        self.assertIn("a_report", state["history"][0])
        self.assertEqual(state["status"], "completed")
        self.assertEqual(state["final_verification_result"]["head"], state["history"][0]["commit"])
        self.assertEqual(state["final_verification_result"]["test_count"], 2)
        names = [event["event"] for event in self.events()]
        self.assertLess(names.index("checkpoint_committed"), names.index("final_verification_started"))

    def test_production_code_review_not_run_approval_checkpoints_then_final_suite(self):
        fake = Executor(reviews=[CODE_REVIEW_APPROVED])
        def execute(argv, cwd, timeout, *, prompt=None):
            if "--output-schema" in argv:
                self.assertNotIn("--sandbox", argv)
                self.assertIn('--strict-config', argv)
                self.assertTrue(any('extends=":read-only"' in part for part in argv))
                self.assertTrue(any('network={enabled=false}' in part for part in argv))
            return fake(argv, cwd, timeout, prompt=prompt)
        execute.__wrapped__ = runner.call
        with patch.object(runner, "check_cli", return_value="fake-codex"), \
                patch.object(runner, "probe_review_storage"), \
                patch.object(runner.controller_test_sandbox, "run", side_effect=lambda command, repo, logs, timeout, **kw:
                             kw["launch"](command, repo, timeout)) as sandbox:
            self.assertEqual(self.run_fake(execute), 0)
        self.assertEqual(fake.calls, ["A", "B", "final"])
        self.assertEqual(sandbox.call_count, 1)
        self.assertEqual(sandbox.call_args.args[0], ["fake-final"])
        prompt = fake.b_prompts[0]
        for text in ("READ-ONLY code reviewer", "Do not execute tests, linting, builds or verification commands",
                     "CODE REVIEW ENVIRONMENT", "status=not_run", "extra test modules or optional coverage",
                     "implementer claims, not independently verified evidence"):
            self.assertIn(text, prompt)
        for text in ("Run configured tests", "minimal relevant checks", "significant missing test coverage",
                     "blocked/unexecuted testing cannot authorize", "independently check):"):
            self.assertNotIn(text, prompt)
        state = self.state()
        self.assertEqual(state["history"][0]["review"], CODE_REVIEW_APPROVED)
        self.assertEqual(state["history"][0]["tests"], [])
        self.assertEqual(state["status"], "completed")
        self.assertEqual(state["final_verification_result"]["head"], state["history"][0]["commit"])
        self.assertNotEqual(self.git("rev-parse", "HEAD"), self.initial)

    def test_new_mode_failed_or_blocked_verification_cannot_authorize_commit(self):
        for index, status in enumerate(("failed", "environment_blocked")):
            with self.subTest(status=status):
                answer = CODE_REVIEW_APPROVED | {"verification": {"status": status, "details": "Reported failure"}}
                fake = Executor(reviews=[answer])
                self.assertEqual(self.run_fake(fake, resume=bool(index)), 1)
                self.assertEqual(fake.calls, ["B"] if index else ["A", "B"])
                self.assertEqual(self.state()["completed"], [])
                self.assertEqual(self.state()["pending_review"]["review_failure_kind"], "verification_" + status)
                self.assertEqual(self.git("rev-parse", "HEAD"), self.initial)

    def test_saved_not_run_verdict_integrity_and_resume_without_new_review(self):
        fake = Executor(reviews=[CODE_REVIEW_APPROVED])
        with patch.object(runner, "finish_review_verdict", side_effect=RuntimeError("interrupted before disposition")):
            with self.assertRaisesRegex(RuntimeError, "interrupted before disposition"):
                self.run_fake(fake)
        verdict = self.logs / "one-01-b-verdict.json"
        original = verdict.read_text()
        verdict.write_text(json.dumps(CODE_REVIEW_APPROVED | {"summary": "altered approval"}))
        execute = Mock()
        with self.assertRaisesRegex(runner.PilotError, "Saved review response changed"):
            self.run_fake(execute, resume=True)
        execute.assert_not_called()
        self.assertEqual(self.git("rev-parse", "HEAD"), self.initial)
        verdict.write_text(original)
        self.assertEqual(self.run_fake(fake, resume=True), 0)
        self.assertEqual(fake.calls, ["A", "B", "final"])
        self.assertEqual(self.state()["history"][0]["review"], CODE_REVIEW_APPROVED)

    def test_new_mode_reviewer_mutation_still_blocks_not_run_approval(self):
        def hook(role):
            if role == "B":
                (self.repo / "app.py").write_text("unauthorized reviewer mutation")
        fake = Executor(reviews=[CODE_REVIEW_APPROVED], hook=hook)
        with self.assertRaisesRegex(runner.PilotError, "unstaged or untracked"):
            self.run_fake(fake)
        self.assertEqual(fake.calls, ["A", "B"])
        self.assertEqual(self.git("rev-parse", "HEAD"), self.initial)

    def test_saved_controller_failed_checks_return_to_a_before_b(self):
        fake = Executor(focused=[runner._CallResult(1, "focused regression failed", False, True, True)])
        self.assertEqual(self.start_controller_workflow(fake), 0)
        self.assertEqual(fake.calls, ["A", "focused", "A", "focused", "B", "final"])
        self.assertIn("focused regression failed", fake.a_prompts[1])
        self.assertEqual(len(self.state()["completed"]), 1)

    def test_a_reported_failure_reaches_b_as_claims_without_controller_rerun(self):
        report = "Executed fake-focused: FAILED (assertion error); remaining limitation: regression unresolved"
        def hook(role):
            if role == "A":
                (self.logs / "one-01-a-answer.txt").write_text(report)
        fake = Executor(hook=hook, reviews=[{
            "decision": "REJECTED", "issues": ["Fix the reported regression"], "summary": "Focused check failed"}])
        self.assertEqual(self.run_fake(fake, attempts=1), 1)
        self.assertEqual(fake.calls, ["A", "B"])
        self.assertIn(report, fake.b_prompts[0])
        self.assertIn("claims, not controller verification", fake.b_prompts[0])
        self.assertEqual(self.state()["history"][0]["tests"], [])
        self.assertEqual(self.git("rev-parse", "HEAD"), self.initial)

    def test_saved_controller_failed_checks_cannot_commit_or_launch_b_at_attempt_limit(self):
        fake = Executor(focused=[runner._CallResult(1, "failed", False, True, True)])
        self.assertEqual(self.start_controller_workflow(fake, attempts=1), 1)
        self.assertEqual(fake.calls, ["A", "focused"])
        self.assertEqual(self.git("rev-parse", "HEAD"), self.initial)
        self.assertEqual(self.state()["completed"], [])

    def test_missing_final_designation_blocks_before_a(self):
        del self.document["final_verification"]
        self.write_plan()
        execute = Mock()
        with self.assertRaisesRegex(runner.PilotError, "Missing designated final"):
            self.run_fake(execute)
        execute.assert_not_called()
        self.assertEqual(self.git("rev-parse", "HEAD"), self.initial)

    def test_txt_plan_can_use_explicit_policy_and_resume_from_saved_settings(self):
        self.plan = self.root / "task.txt"
        self.plan.write_text("Implement app.py and run focused checks")
        fake = Executor(final=[runner._CallResult(124, "timeout", True, True, True)])
        self.assertEqual(self.run_fake(fake, final_verification_config=POLICY), 1)
        self.assertEqual(self.run_fake(fake, resume=True), 0)
        self.assertEqual(fake.calls, ["A", "B", "final", "final"])
        self.assertEqual(fake.timeouts[-1], ("final", 2400))

    def test_cli_reads_external_policy_without_launching_a_real_executor(self):
        config = self.root / "verification.json"
        config.write_text(json.dumps(POLICY))
        with patch.object(runner.shutil, "which", return_value="fake-codex"), \
                patch.object(runner, "run", return_value=0) as run:
            self.assertEqual(runner.main([str(self.plan), "--repo", str(self.repo), "--logs", str(self.logs),
                "--final-verification-config", str(config), "--controller-tests"]), 0)
        self.assertEqual(run.call_args.kwargs["final_verification_config"], POLICY)
        self.assertTrue(run.call_args.kwargs["controller_tests"])

    def test_plain_success_failed_summary_and_skips_never_complete_batch(self):
        for index, output in enumerate(("echo succeeded", FAILED, "Ran 2 tests in 0.1s\n\nOK (skipped=1)\n")):
            with self.subTest(output=output):
                self.logs = self.root / f"logs-{index}"
                fake = Executor(final=[runner._CallResult(0, output, False, True, True)], implementation=f"CASE = {index}")
                self.assertEqual(self.run_fake(fake), 1)
                self.assertEqual(self.state()["status"], "stopped")
                self.assertNotIn("run_completed", [event["event"] for event in self.events()])

    def test_failed_final_suite_preserves_commits_and_real_failure_evidence(self):
        fake = Executor(final=[runner._CallResult(1, FAILED, False, True, True)])
        self.assertEqual(self.run_fake(fake), 1)
        state = self.state()
        self.assertEqual(state["final_verification_result"]["outcome"], "test_failure")
        self.assertEqual(fake.calls, ["A", "B", "final"])
        self.assertEqual(state["completed"], ["one"])
        self.assertEqual(self.git("rev-parse", "HEAD"), state["history"][0]["commit"])
        self.assertEqual((self.logs / "final-verification-0001-test-1.txt").read_text(), FAILED)

    def test_verified_timeout_resume_uses_saved_long_timeout_without_a_or_b(self):
        fake = Executor(final=[runner._CallResult(124, "PROCESS TIMED OUT", True, True, True)])
        self.assertEqual(self.run_fake(fake), 1)
        self.assertEqual(self.state()["final_verification_result"]["outcome"], "infrastructure_timeout")
        self.assertFalse(execution_lock(self.repo, ".job.lock").exists())
        self.assertEqual(self.run_fake(fake, resume=True, timeout=90), 0)
        self.assertEqual(fake.calls, ["A", "B", "final", "final"])
        self.assertEqual(fake.timeouts[-1], ("final", 2400))
        self.assertEqual(self.state()["final_verification_invocations"], 2)

    def test_launch_failure_is_infrastructure_failure_not_a_correction(self):
        fake = Executor(final=[runner._CallResult(127, "unavailable", False, True, False)])
        self.assertEqual(self.run_fake(fake), 1)
        self.assertEqual(self.state()["final_verification_result"]["outcome"], "infrastructure_process_failure")
        self.assertEqual(fake.calls, ["A", "B", "final"])

    def test_cleanup_uncertainty_blocks_resume_new_runs_and_later_commands(self):
        self.document["final_verification"] = POLICY | {"test_commands": [["fake-final"], ["fake-final"]]}
        self.write_plan()
        fake = Executor(final=[runner._CallResult(0, PASSED, False, False, True)])
        self.assertEqual(self.run_fake(fake), 1)
        self.assertEqual(fake.calls, ["A", "B", "final"])
        self.assertIn("uncertain_test", self.state())
        self.assertTrue(execution_lock(self.repo, ".process-uncertain.json").exists())
        execute = Mock()
        with self.assertRaises(OSError):
            self.run_fake(execute, resume=True)
        with self.assertRaises(OSError):
            runner.run(self.plan, self.repo, self.root / "other", execute=execute)
        execute.assert_not_called()

    def test_confirmed_exception_preserves_resumable_infrastructure_failure(self):
        error = subprocess.TimeoutExpired(["fake-final"], 2400)
        error.cleanup_confirmed = True
        fake = Executor(final=[error])
        with self.assertRaises(subprocess.TimeoutExpired):
            self.run_fake(fake)
        self.assertFalse(execution_lock(self.repo, ".job.lock").exists())
        self.assertEqual(self.run_fake(fake, resume=True), 0)
        self.assertEqual(fake.calls, ["A", "B", "final", "final"])

    def test_unverified_exception_keeps_workspace_protection(self):
        error = OSError("child cleanup unavailable")
        error.cleanup_confirmed = False
        fake = Executor(final=[error])
        with self.assertRaisesRegex(OSError, "child cleanup"):
            self.run_fake(fake)
        self.assertIn("uncertain_test", self.state())
        execute = Mock()
        with self.assertRaises(OSError):
            self.run_fake(execute, resume=True)
        execute.assert_not_called()

    def test_legacy_completed_state_needs_final_evidence_without_replaying_tasks(self):
        fake = Executor()
        self.assertEqual(self.start_controller_workflow(fake), 0)
        original_head, state = self.git("rev-parse", "HEAD"), self.state()
        state.pop("final_verification_result")
        state.pop("final_verification")
        runner.save_json(self.logs / "state.json", state)
        manifest_path = self.logs / "run-manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest.pop("verification_policy_sha256")
        runner.save_json(manifest_path, manifest)
        self.assertEqual(self.run_fake(fake, resume=True), 0)
        self.assertEqual(fake.calls, ["A", "focused", "B", "final", "final"])
        self.assertEqual(self.git("rev-parse", "HEAD"), original_head)

    def test_modified_source_invalidates_final_results_and_blocks_resume(self):
        def hook(role):
            if role == "final":
                (self.repo / "app.py").write_text("changed during verification")
        fake = Executor(hook=hook)
        self.assertEqual(self.run_fake(fake), 1)
        self.assertEqual(self.state()["final_verification_result"]["outcome"], "snapshot_changed")
        execute = Mock()
        with self.assertRaises(runner.PilotError):
            self.run_fake(execute, resume=True)
        execute.assert_not_called()

    def test_successful_completed_resume_reuses_valid_final_evidence(self):
        fake = Executor()
        self.assertEqual(self.run_fake(fake), 0)
        execute = Mock()
        self.assertEqual(self.run_fake(execute, resume=True), 0)
        execute.assert_not_called()
        self.assertEqual(self.state()["final_verification_invocations"], 1)

    def test_missing_final_output_requires_fresh_suite_without_a_or_b(self):
        fake = Executor()
        self.assertEqual(self.run_fake(fake), 0)
        (self.logs / "final-verification-0001-test-1.txt").unlink()
        self.assertEqual(self.run_fake(fake, resume=True), 0)
        self.assertEqual(fake.calls, ["A", "B", "final", "final"])
        self.assertTrue((self.logs / "final-verification-0002-test-1.txt").is_file())

    def test_orphan_final_artifact_blocks_execution_without_overwrite(self):
        self.assertEqual(self.run_fake(Executor()), 0)
        artifact = self.logs / "final-verification-0001-test-1.txt"
        before, state = artifact.read_bytes(), self.state()
        state.pop("final_verification_result")
        state["final_verification_invocations"] = 0
        runner.save_json(self.logs / "state.json", state)
        execute = Mock()
        with self.assertRaisesRegex(runner.PilotError, "invocation ownership"):
            self.run_fake(execute, resume=True)
        execute.assert_not_called()
        self.assertEqual(artifact.read_bytes(), before)

    def test_configured_minimum_prevents_a_partial_suite_from_completing(self):
        self.document["final_verification"] = POLICY | {"minimum_tests": 3}
        self.write_plan()
        self.assertEqual(self.run_fake(Executor()), 1)
        self.assertEqual(self.state()["final_verification_result"]["outcome"], "insufficient_test_coverage")

    def test_resume_cannot_change_verification_timeout_or_focused_mode(self):
        fake = Executor(final=[runner._CallResult(124, "timeout", True, True, True)])
        self.assertEqual(self.run_fake(fake), 1)
        execute = Mock()
        with self.assertRaises(runner.PilotError):
            self.run_fake(execute, resume=True, final_verification_config=POLICY | {"timeout_seconds": 3600})
        with self.assertRaisesRegex(runner.PilotError, "fixed"):
            self.run_fake(execute, resume=True, controller_tests=True)
        execute.assert_not_called()
        self.assertEqual(self.state()["final_verification"]["timeout_seconds"], 2400)

    def test_b_rejection_requires_fresh_a_report_and_independent_review(self):
        self.document["tasks"][0]["prompt"] = "Set app.py VERSION = 2"
        self.write_plan()
        issue = "app.py:1 VERSION is 1; the task requires 2. Set VERSION = 2 to fix version behavior."
        fake = Executor(reviews=[{"decision": "REJECTED", "issues": [issue], "summary": "Incorrect",
                                 "verification": CODE_REVIEW_APPROVED["verification"]}, CODE_REVIEW_APPROVED])
        self.assertEqual(self.run_fake(fake), 0)
        self.assertEqual(fake.calls, ["A", "B", "A", "B", "final"])
        self.assertIn(issue, fake.a_prompts[1])
        self.assertIn("+VERSION = 1", fake.b_prompts[0])
        self.assertIn("+VERSION = 2", fake.b_prompts[1])
        self.assertEqual((self.repo / "app.py").read_text(), "VERSION = 2\n")
        self.assertIn("implementation 2", fake.b_prompts[1])
        self.assertNotEqual(self.state()["history"][0]["a_report"], self.state()["history"][1]["a_report"])

    def test_invalid_b_verdict_cannot_reach_final_testing_or_commit(self):
        fake = Executor(reviews=["bad", "bad", "bad"])
        self.assertEqual(self.run_fake(fake), 1)
        self.assertEqual(fake.calls, ["A", "B", "B", "B"])
        self.assertEqual(self.git("rev-parse", "HEAD"), self.initial)
        self.assertNotEqual(self.state()["status"], "completed")

    def test_review_only_recovery_preserves_mode_timeout_and_implementation(self):
        fake = Executor(reviews=[runner._CallResult(127, "connection unavailable", False, True, False), CODE_REVIEW_APPROVED])
        self.assertEqual(self.run_fake(fake), 1)
        self.assertTrue(self.state()["pending_review"])
        self.assertEqual(self.run_fake(fake, resume=True), 0)
        self.assertEqual(fake.calls, ["A", "B", "B", "final"])
        self.assertFalse(self.state()["controller_tests"])

    def test_review_ready_requires_saved_implementer_report_identity(self):
        fake = Executor(reviews=[runner._CallResult(127, "unavailable", False, True, False)])
        self.assertEqual(self.run_fake(fake), 1)
        state = self.state()
        state["pending_review"]["record"].pop("a_report")
        runner.save_json(self.logs / "state.json", state)
        (self.logs / "one-01-a-answer.txt").write_text("unverified replacement testing claim")
        execute = Mock()
        with self.assertRaisesRegex(runner.PilotError, "report identity"):
            self.run_fake(execute, resume=True)
        execute.assert_not_called()
        self.assertEqual(self.state()["pending_review"]["review_invocations"], 1)
        self.assertEqual(self.git("rev-parse", "HEAD"), self.initial)

    def test_saved_controller_results_cannot_be_used_as_implementer_evidence(self):
        fake = Executor(reviews=[runner._CallResult(127, "unavailable", False, True, False)])
        self.assertEqual(self.start_controller_workflow(fake), 1)
        state = self.state()
        task = self.document["tasks"][0]
        # Even checkpoints predating an explicit mode retain controller evidence.
        state.pop("controller_tests")
        runner.verify_review_ready(self.repo, self.logs, state, task)
        state["controller_tests"] = False
        with self.assertRaisesRegex(runner.PilotError, "verified tests"):
            runner.verify_review_ready(self.repo, self.logs, state, task)
        self.assertEqual(fake.calls, ["A", "focused", "B"])
        self.assertEqual(self.git("rev-parse", "HEAD"), self.initial)

    def test_modified_implementer_report_cannot_be_approved(self):
        def hook(role):
            if role == "B":
                (self.logs / "one-01-a-answer.txt").write_text("altered test claim")
        fake = Executor(hook=hook)
        with self.assertRaisesRegex(runner.PilotError, "report"):
            self.run_fake(fake)
        self.assertEqual(fake.calls, ["A", "B"])
        self.assertEqual(self.git("rev-parse", "HEAD"), self.initial)

    def test_missing_a_report_stops_without_b_or_silent_a_replay(self):
        def hook(role):
            if role == "A":
                (self.logs / "one-01-a-answer.txt").unlink()
        fake = Executor(hook=hook)
        self.assertEqual(self.run_fake(fake), 1)
        self.assertEqual(fake.calls, ["A"])
        execute = Mock()
        with self.assertRaises(runner.PilotError):
            self.run_fake(execute, resume=True)
        execute.assert_not_called()
        self.assertEqual(self.git("rev-parse", "HEAD"), self.initial)

    def test_authorized_correction_plan_requires_new_a_b_and_final_evidence(self):
        fake = Executor(final=[runner._CallResult(1, FAILED, False, True, True)])
        self.assertEqual(self.run_fake(fake), 1)
        previous_logs, previous_head = self.logs, self.git("rev-parse", "HEAD")
        # This is an externally prepared correction, never generated by the controller.
        self.document["tasks"] = [{"id": "repair", "prompt": "Correct only app.py for the reported failing assertion",
                                   "test_commands": [["fake-focused"]]}]
        self.write_plan()
        self.logs = self.root / "authorized-correction"
        correction = Executor(implementation="VERSION = 99\n")
        self.assertEqual(self.run_fake(correction), 0)
        self.assertEqual(correction.calls, ["A", "B", "final"])
        self.assertNotEqual(self.git("rev-parse", "HEAD"), previous_head)
        self.assertEqual(json.loads((previous_logs / "state.json").read_text())["status"], "stopped")
        self.assertEqual(self.state()["final_verification_result"]["head"], self.git("rev-parse", "HEAD"))


if __name__ == "__main__":
    unittest.main()
