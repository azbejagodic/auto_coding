"""Review-only recovery uses fake executors and disposable Git repositories."""
import json
import re
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import io
from contextlib import redirect_stderr

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import auto_coding.simple_run as runner
from backend_fixtures import run_with_verification as run
from auto_coding.simple_run import (PilotError, checked_review, correction_context,
                                   MAX_CORRECTION_CONTEXT, MAX_REVIEW_ISSUE_CHARS,
                                   MAX_REVIEW_ISSUES_TOTAL, MAX_VERDICT_BYTES)


APPROVED = {"decision": "APPROVED", "issues": [], "summary": "OK"}
REJECTED = {"decision": "REJECTED", "issues": ["Fix the implementation"], "summary": "Incomplete"}


class FakeExecutor:
    def __init__(self, responses=(), *, review_only=False):
        self.responses = list(responses)
        self.review_only = review_only
        self.a_calls = 0
        self.test_calls = 0
        self.b_calls = 0
        self.a_prompts = []
        self.b_prompts = []

    def __call__(self, argv, cwd, timeout, *, prompt=None):
        if argv[0] == "fake-test":
            if self.review_only:
                raise AssertionError("Verified tests must not rerun")
            self.test_calls += 1
            return 0, "tests passed", False
        answer = Path(argv[argv.index("--output-last-message") + 1])
        if "--output-schema" in argv:
            self.b_calls += 1
            self.b_prompts.append(prompt)
            response = self.responses.pop(0) if self.responses else APPROVED
            if isinstance(response, tuple):
                return response
            if isinstance(response, int):
                return response, "reviewer connection failed", False
            answer.write_text(json.dumps(response) if isinstance(response, dict) else response,
                              encoding="utf-8")
            return 0, "review completed", False
        if self.review_only:
            raise AssertionError("Codex A must not rerun")
        self.a_calls += 1
        self.a_prompts.append(prompt)
        (cwd / "app.txt").write_text(f"implementation {self.a_calls}", encoding="utf-8")
        answer.write_text("done", encoding="utf-8")
        return 0, "implementation completed", False


class ReviewRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-b", "feature/review"], cwd=self.repo,
                       check=True, capture_output=True)
        self.logs = self.root / "logs"
        self.plan = self.root / "tasks.json"
        self.plan.write_text(json.dumps({"tasks": [{"id": "one", "prompt": "Implement one",
                                                    "test_commands": [["fake-test"]]}]}),
                             encoding="utf-8")
        self.retry_now = 1000.0
        self.retry_delays = []
        clock = patch.object(runner, "_review_clock", side_effect=lambda: self.retry_now)
        sleep = patch.object(runner, "_review_sleep", side_effect=self.advance_retry_clock)
        clock.start()
        self.retry_sleep = sleep.start()
        self.addCleanup(clock.stop)
        self.addCleanup(sleep.stop)

    def advance_retry_clock(self, seconds):
        self.retry_delays.append(seconds)
        self.retry_now += seconds

    def run_fake(self, fake, *, resume=False, retry_review_once=False):
        return run(self.plan, self.repo, self.logs, resume=resume, execute=fake,
                   retry_review_once=retry_review_once)

    def state(self):
        return json.loads((self.logs / "state.json").read_text(encoding="utf-8"))

    def events(self):
        return [json.loads(line) for line in
                (self.logs / "events.jsonl").read_text(encoding="utf-8").splitlines()]

    def metrics(self):
        return json.loads((self.logs / "metrics.json").read_text(encoding="utf-8"))["run"]

    def stopped_at_review(self):
        fake = FakeExecutor([1, 1, 1])
        self.assertEqual(self.run_fake(fake), 1)
        self.assertEqual((fake.a_calls, fake.test_calls, fake.b_calls), (1, 1, 3))
        state = self.state()
        self.assertEqual(state["status"], "stopped")
        self.assertIsNotNone(state["pending_review"])
        self.assertEqual(state["history"], [])
        return state

    def test_successful_a_tests_b_commit(self):
        fake = FakeExecutor()
        self.assertEqual(self.run_fake(fake), 0)
        self.assertEqual((fake.a_calls, fake.test_calls, fake.b_calls), (1, 1, 1))
        state = self.state()
        self.assertEqual(state["completed"], ["one"])
        self.assertIsNone(state["pending_review"])
        self.assertEqual(len(state["history"]), 1)
        self.assertEqual(state["history"][0]["tests"][0]["exit_code"], 0)
        metrics = self.metrics()
        for role in ("codex_a", "codex_b", "tests"):
            self.assertEqual(metrics[role]["invocations"], 2 if role == "tests" else 1)
            self.assertEqual(metrics[role]["duration_observations"], 2 if role == "tests" else 1)
            self.assertIsNone(metrics[role]["usage"])
        self.assertEqual(metrics["correction_attempts"], 0)
        self.assertEqual(metrics["review_retries"], 0)

    def test_connection_failure_retries_only_b(self):
        fake = FakeExecutor([1, APPROVED])
        self.assertEqual(self.run_fake(fake), 0)
        self.assertEqual((fake.a_calls, fake.test_calls, fake.b_calls), (1, 1, 2))
        self.assertEqual([e["event"] for e in self.events()].count("review_retry"), 1)
        self.assertEqual(self.retry_delays, [5.0])
        self.assertEqual(self.metrics()["review_retries"], 1)
        self.assertEqual(self.metrics()["review_outcomes"], {"process_failure": 1, "approved": 1})

    def test_restart_during_delay_preserves_deadline_and_remaining_budget(self):
        first = FakeExecutor([1])
        self.retry_sleep.side_effect = RuntimeError("application restarted during wait")
        with self.assertRaisesRegex(RuntimeError, "restarted"):
            self.run_fake(first)
        saved = self.state()["pending_review"]
        self.assertEqual(saved["review_invocations"], 1)
        self.assertEqual(saved["review_retry_after"], 1005.0)
        self.assertEqual(self.state()["status"], "stopped")
        self.retry_now += 2
        self.retry_sleep.side_effect = self.advance_retry_clock
        resumed = FakeExecutor([1, APPROVED], review_only=True)
        self.assertEqual(self.run_fake(resumed, resume=True), 0)
        self.assertEqual((first.a_calls, first.test_calls, first.b_calls), (1, 1, 1))
        self.assertEqual((resumed.a_calls, resumed.test_calls, resumed.b_calls), (0, 0, 2))
        self.assertEqual(self.retry_delays, [3.0, 15.0])
        # B07 creates a fresh writable scratch per reviewer invocation. The
        # review request itself and its target context must survive restart.
        scratch = r"review-scratch-[A-Za-z0-9_]+"
        self.assertRegex(first.b_prompts[0], scratch)
        self.assertRegex(resumed.b_prompts[0], scratch)
        self.assertNotEqual(first.b_prompts[0], resumed.b_prompts[0])
        self.assertEqual(re.sub(scratch, "<scratch>", first.b_prompts[0]),
                         re.sub(scratch, "<scratch>", resumed.b_prompts[0]))
        invocations = [event["metadata"]["review_invocation"] for event in self.events()
                       if event["event"] == "review_started"]
        self.assertEqual(invocations, [1, 2, 3])

    def test_exhaustion_survives_restarts_and_explicit_grant_is_only_one_call(self):
        self.stopped_at_review()
        resumed = FakeExecutor([1, 1, 1], review_only=True)
        for _ in range(2):
            self.assertEqual(self.run_fake(resumed, resume=True), 1)
        self.assertEqual(resumed.b_calls, 0)
        self.assertEqual(self.state()["pending_review"]["review_invocations"], 3)
        before = self.metrics()
        self.assertEqual(self.retry_delays, [5.0, 15.0])
        self.assertEqual(self.run_fake(resumed, resume=True, retry_review_once=True), 1)
        self.assertEqual(resumed.b_calls, 1)
        self.assertEqual(self.state()["pending_review"]["review_invocations"], 4)
        self.assertEqual(self.state()["pending_review"]["review_invocation_limit"], 4)
        self.assertEqual(self.run_fake(resumed, resume=True), 1)
        self.assertEqual(resumed.b_calls, 1)
        self.assertEqual(self.state()["history"], [])
        self.assertEqual(self.state()["completed"], [])
        self.assertEqual(before["codex_a"]["invocations"], 1)
        self.assertEqual(before["codex_b"]["invocations"], 3)
        self.assertEqual(self.metrics()["codex_a"], before["codex_a"])
        self.assertEqual(self.metrics()["tests"], before["tests"])
        self.assertEqual(self.metrics()["codex_b"]["invocations"], 4)

    def test_unused_explicit_authorization_survives_restart_without_stacking(self):
        self.stopped_at_review()
        resumed = FakeExecutor(review_only=True)
        self.retry_sleep.side_effect = RuntimeError("restart after authorization")
        with self.assertRaisesRegex(RuntimeError, "after authorization"):
            self.run_fake(resumed, resume=True, retry_review_once=True)
        self.assertEqual(resumed.b_calls, 0)
        self.assertEqual(self.state()["pending_review"]["review_invocations"], 3)
        self.assertEqual(self.state()["pending_review"]["review_invocation_limit"], 4)
        with self.assertRaisesRegex(PilotError, "only.*after.*exhausted"):
            self.run_fake(resumed, resume=True, retry_review_once=True)
        self.retry_sleep.side_effect = self.advance_retry_clock
        self.assertEqual(self.run_fake(resumed, resume=True), 0)
        self.assertEqual(resumed.b_calls, 1)

    def test_legacy_invocations_do_not_gain_a_fresh_automatic_budget(self):
        state = self.stopped_at_review()
        pending = state["pending_review"]
        pending["review_invocations"] = 7
        for field in ("review_invocation_limit", "review_retry_after", "review_failure_kind"):
            pending.pop(field, None)
        (self.logs / "state.json").write_text(json.dumps(state), encoding="utf-8")
        resumed = FakeExecutor(review_only=True)
        self.assertEqual(self.run_fake(resumed, resume=True), 1)
        self.assertEqual(resumed.b_calls, 0)
        self.assertEqual(self.run_fake(resumed, resume=True, retry_review_once=True), 0)
        last = [event for event in self.events() if event["event"] == "review_started"][-1]
        self.assertEqual(last["metadata"]["review_invocation"], 8)
        self.assertEqual(self.retry_delays, [5.0, 15.0, 15.0])
        self.assertEqual(self.metrics()["codex_b"]["invocations"], 4)

    def test_backward_clock_cannot_create_an_unbounded_wait(self):
        state = self.stopped_at_review()
        state["pending_review"]["review_retry_after"] = self.retry_now + 1000000000
        (self.logs / "state.json").write_text(json.dumps(state), encoding="utf-8")
        self.assertEqual(self.run_fake(FakeExecutor(review_only=True), resume=True,
                                       retry_review_once=True), 0)
        self.assertEqual(self.retry_delays[-1], 15.0)

    def test_changed_worktree_during_delay_stops_before_another_call(self):
        fake = FakeExecutor([1, APPROVED])
        def mutate_while_waiting(seconds):
            (self.repo / "app.txt").write_text("unexpected edit", encoding="utf-8")
            self.advance_retry_clock(seconds)
        self.retry_sleep.side_effect = mutate_while_waiting
        with self.assertRaisesRegex(PilotError, "unstaged or untracked"):
            self.run_fake(fake)
        self.assertEqual((fake.a_calls, fake.test_calls, fake.b_calls), (1, 1, 1))
        self.assertIsNotNone(self.state()["pending_review"])

    def test_launch_failure_stops_without_automatic_retry_and_can_resume_after_fix(self):
        fake = FakeExecutor([127])
        self.assertEqual(self.run_fake(fake), 1)
        self.assertEqual(fake.b_calls, 1)
        self.assertEqual(self.retry_delays, [])
        self.assertEqual(self.state()["pending_review"]["review_failure_kind"], "launch_failure")
        self.assertEqual(self.metrics()["codex_b"]["invocations"], 0)
        self.assertEqual(self.metrics()["codex_b"]["launch_failures"], 1)
        with self.assertRaisesRegex(PilotError, "only.*after.*exhausted"):
            self.run_fake(FakeExecutor(review_only=True), resume=True, retry_review_once=True)
        self.assertEqual(self.run_fake(FakeExecutor(review_only=True), resume=True), 0)
        self.assertEqual(self.retry_delays, [])

    def test_output_limit_stops_without_repeating_the_request(self):
        fake = FakeExecutor([runner._CallResult(125, "output limit", False, True)])
        self.assertEqual(self.run_fake(fake), 1)
        self.assertEqual(fake.b_calls, 1)
        self.assertEqual(self.retry_delays, [])
        self.assertEqual(self.state()["pending_review"]["review_failure_kind"], "output_limit")

    def test_explicit_retry_requires_resume_and_a_review_checkpoint(self):
        with self.assertRaisesRegex(PilotError, "requires --resume"):
            self.run_fake(FakeExecutor(), retry_review_once=True)
        # The rejected preflight created no task state or implementation.
        self.assertFalse((self.repo / "app.txt").exists())
        self.assertEqual(self.run_fake(FakeExecutor()), 0)
        with self.assertRaisesRegex(PilotError, "requires an exhausted review-ready"):
            self.run_fake(FakeExecutor(review_only=True), resume=True, retry_review_once=True)

    def test_invalid_saved_budget_cannot_authorize_extra_calls(self):
        state = self.stopped_at_review()
        state["pending_review"]["review_invocation_limit"] = 100
        (self.logs / "state.json").write_text(json.dumps(state), encoding="utf-8")
        fake = FakeExecutor(review_only=True)
        with self.assertRaisesRegex(PilotError, "does not match"):
            self.run_fake(fake, resume=True)
        self.assertEqual(fake.b_calls, 0)

    def test_exhausted_review_requires_explicit_retry_then_resumes_only_b(self):
        original = self.stopped_at_review()
        pending = original["pending_review"]
        self.assertEqual(pending["attempt"], 1)
        self.assertIsNone(pending["base_head"])
        self.assertEqual(pending["record"]["tests"][0]["exit_code"], 0)
        resumed = FakeExecutor(review_only=True)
        self.assertEqual(self.run_fake(resumed, resume=True), 1)
        self.assertEqual(resumed.b_calls, 0)
        self.assertEqual(self.run_fake(resumed, resume=True, retry_review_once=True), 0)
        self.assertEqual((resumed.a_calls, resumed.test_calls, resumed.b_calls), (0, 0, 1))
        self.assertEqual(self.state()["history"][0]["attempt"], 1)
        streams = [e["evidence"]["stream"] for e in self.events()
                   if e["event"] == "review_started"]
        self.assertEqual(len(streams), len(set(streams)))
        self.assertEqual(len(streams), 4)
        self.assertTrue(all((self.logs / stream).exists() for stream in streams))

    def test_interruption_after_review_started_resumes_only_b(self):
        fake = FakeExecutor()
        class Interrupted(Exception):
            cleanup_confirmed = True
        def interrupted(argv, cwd, timeout, *, prompt=None):
            if "--output-schema" in argv:
                raise Interrupted("application interrupted after reviewer cleanup")
            return fake(argv, cwd, timeout, prompt=prompt)
        with self.assertRaises(Interrupted):
            run(self.plan, self.repo, self.logs, execute=interrupted)
        self.assertEqual((fake.a_calls, fake.test_calls), (1, 1))
        self.assertEqual(self.state()["status"], "stopped")
        self.assertIsNotNone(self.state()["pending_review"])
        resumed = FakeExecutor(review_only=True)
        self.assertEqual(self.run_fake(resumed, resume=True), 0)
        self.assertEqual(resumed.b_calls, 1)
        self.assertEqual(self.metrics()["codex_b"]["unconfirmed_invocations"], 1)
        self.assertEqual(self.metrics()["codex_b"]["invocations"], 1)

    def test_invalid_response_cannot_approve(self):
        fake = FakeExecutor(["invalid", "invalid", "invalid"])
        self.assertEqual(self.run_fake(fake), 1)
        self.assertEqual((fake.a_calls, fake.test_calls, fake.b_calls), (1, 1, 3))
        self.assertEqual(self.retry_delays, [5.0, 15.0])
        self.assertEqual(self.state()["pending_review"]["review_failure_kind"], "invalid_verdict")
        self.assertEqual(self.state()["completed"], [])
        self.assertIsNotNone(self.state()["pending_review"])
        self.assertFalse(any(e["event"] == "checkpoint_committed" for e in self.events()))

    def test_rejection_without_specific_issues_is_invalid(self):
        empty_rejection = {"decision": "REJECTED", "issues": [], "summary": "No evidence"}
        fake = FakeExecutor([empty_rejection, empty_rejection, empty_rejection])
        self.assertEqual(self.run_fake(fake), 1)
        self.assertEqual(fake.a_calls, 1)
        self.assertEqual(self.state()["completed"], [])

    def test_rejection_corrects_with_fresh_tests_and_review(self):
        fake = FakeExecutor([REJECTED, APPROVED])
        self.assertEqual(self.run_fake(fake), 0)
        self.assertEqual((fake.a_calls, fake.test_calls, fake.b_calls), (2, 2, 2))
        self.assertEqual(self.retry_delays, [])
        self.assertIn("Fix the implementation", fake.a_prompts[1])
        self.assertEqual(self.metrics()["correction_attempts"], 1)
        self.assertEqual(self.metrics()["codex_a"]["invocations"], 2)
        self.assertEqual(self.metrics()["tests"]["invocations"], 3)
        self.assertEqual([h["attempt"] for h in self.state()["history"]], [1, 2])
        self.assertEqual([e["outcome"] for e in self.events() if e["event"] == "review_finished"],
                         ["rejected", "approved"])

    def test_multiple_rejections_retain_prior_fixes_and_fresh_verification(self):
        first = {"decision": "REJECTED", "issues": ["app.py: reproduce missing reset",
                                                    "app.py: reproduce incorrect save"],
                 "summary": "Two blockers"}
        second = {"decision": "REJECTED", "issues": ["app.py: reproduce incorrect save",
                                                     "app.py: reproduce broken reload"],
                  "summary": "Save and reload still fail"}
        fake = FakeExecutor([first, second, APPROVED])
        self.assertEqual(self.run_fake(fake), 0)
        self.assertEqual((fake.a_calls, fake.test_calls, fake.b_calls), (3, 3, 3))
        correction = fake.a_prompts[2]
        for issue in ("missing reset", "incorrect save", "broken reload"):
            self.assertEqual(correction.count(issue), 1)
        self.assertIn("CURRENT CODEX B BLOCKERS (attempt 2)", correction)
        self.assertIn("EARLIER REVIEW ISSUES TO RECHECK", correction)
        self.assertIn("Preserve behavior that already works", correction)
        self.assertIn("do not rework an issue that is already fixed", correction)

    def test_failed_tests_do_not_erase_prior_reviewer_feedback(self):
        fake = FakeExecutor([REJECTED, APPROVED])
        def failing_correction_test(argv, cwd, timeout, *, prompt=None):
            result = fake(argv, cwd, timeout, prompt=prompt)
            if argv[0] == "fake-test" and fake.test_calls == 2:
                return 1, "correction broke reset verification", False
            return result
        self.assertEqual(run(self.plan, self.repo, self.logs, execute=failing_correction_test), 0)
        self.assertEqual((fake.a_calls, fake.test_calls, fake.b_calls), (3, 3, 2))
        self.assertIn("Fix the implementation", fake.a_prompts[2])
        self.assertIn("correction broke reset verification", fake.a_prompts[2])
        self.assertIn("Fix the implementation", fake.b_prompts[1])

    def test_all_failed_test_summaries_survive_large_output(self):
        self.plan.write_text(json.dumps({"tasks": [{"id": "one", "prompt": "Implement one",
            "test_commands": [["fake-test", "first"], ["fake-test", "second"]]}]}),
            encoding="utf-8")
        fake = FakeExecutor()
        def noisy_tests(argv, cwd, timeout, *, prompt=None):
            result = fake(argv, cwd, timeout, prompt=prompt)
            if argv[0] == "fake-test" and fake.test_calls <= 2:
                return 1, "noise" * 5000 + f"failure-tail-{fake.test_calls}", False
            return result
        self.assertEqual(run(self.plan, self.repo, self.logs, execute=noisy_tests), 0)
        self.assertEqual((fake.a_calls, fake.test_calls, fake.b_calls), (2, 4, 1))
        for text in ("TEST 1 failed", "TEST 2 failed", "failure-tail-1", "failure-tail-2"):
            self.assertIn(text, fake.a_prompts[1])

    def test_reviewer_requests_actionable_independent_blockers(self):
        fake = FakeExecutor()
        self.assertEqual(self.run_fake(fake), 0)
        prompt = fake.b_prompts[0]
        for text in ("affected path or behavior", "minimal reproduction",
                     "observed and expected results", "concrete check",
                     "Independently recheck corrected code", "Do not blindly trust"):
            self.assertIn(text, prompt)
        self.assertEqual((fake.a_calls, fake.test_calls, fake.b_calls), (1, 1, 1))

    def test_reviewer_must_continue_past_first_blocker_and_cover_affected_paths(self):
        fake = FakeExecutor()
        self.assertEqual(self.run_fake(fake), 0)
        prompt = fake.b_prompts[0]
        for text in ("entire implementation", "error paths", "state transitions",
                     "Do not stop reviewing after the first", "remaining affected functionality",
                     "EVERY distinct actionable", "no fixed finding count", "authorization/security flaws",
                     "significant missing test coverage", "task-requirement", "shared root cause",
                     "never omit independent defects", "confirmed defects from speculative concerns",
                     "code location", "impact", "Approve only if", "Stay within this task's scope"):
            self.assertIn(text, prompt)

    def test_complete_multi_issue_verdict_reaches_a_and_next_b_without_extra_calls(self):
        issues = ["app.txt:12 reset returns stale data; reproduce with reset twice; clear state",
                  "app.txt:31 save ignores errors; reproduce with unwritable file; propagate failure"]
        rejected = {"decision": "REJECTED", "issues": issues,
                    "summary": "Two independent blocking paths remain",
                    "verification": {"status": "failed", "details": "python -m unittest test_app: two failures"}}
        fake = FakeExecutor([rejected, APPROVED])
        self.assertEqual(self.run_fake(fake), 0)
        self.assertEqual((fake.a_calls, fake.test_calls, fake.b_calls), (2, 2, 2))
        correction, rereview = fake.a_prompts[1], fake.b_prompts[1]
        for issue in issues:
            self.assertEqual(correction.count(issue), 1)
            self.assertEqual(rereview.count(issue), 1)
        self.assertIn(rejected["summary"], correction)
        self.assertIn(rejected["verification"]["details"], correction)
        self.assertIn("Read the entire Codex B verdict", correction)
        self.assertIn("Address every actionable blocking finding", correction)
        self.assertIn("shared root causes and related edge cases", correction)
        self.assertIn("regression tests", correction)
        self.assertIn("Re-run relevant tests", correction)
        self.assertIn("Verify that EVERY finding below was addressed", rereview)
        self.assertIn("regressions introduced by the fixes", rereview)
        self.assertIn("Do not automatically approve", rereview)

    def test_later_rejection_supplies_its_full_finding_set_to_following_review(self):
        first = {"decision": "REJECTED", "issues": ["reset misses error path", "save omits rollback"],
                 "summary": "First review"}
        second = {"decision": "REJECTED", "issues": ["save still omits rollback", "reload now fails"],
                  "summary": "Correction introduced reload regression"}
        fake = FakeExecutor([first, second, APPROVED])
        self.assertEqual(self.run_fake(fake), 0)
        self.assertEqual((fake.a_calls, fake.test_calls, fake.b_calls), (3, 3, 3))
        for issue in second["issues"]:
            self.assertIn(issue, fake.a_prompts[2])
            self.assertIn(issue, fake.b_prompts[2])
        self.assertIn("reset misses error path", fake.a_prompts[2])
        self.assertNotIn("reset misses error path", fake.b_prompts[2])
        self.assertIn("Correction introduced reload regression", fake.a_prompts[2])

    def test_restarted_review_keeps_preceding_rejection_context(self):
        issues = ["reset still loses state", "save still hides write failures"]
        rejected = {"decision": "REJECTED", "issues": issues, "summary": "Both paths need repair"}
        initial = FakeExecutor([rejected])

        class Interrupted(Exception):
            cleanup_confirmed = True

        def interrupted(argv, cwd, timeout, *, prompt=None):
            if "--output-schema" in argv and initial.b_calls == 1:
                raise Interrupted("review interrupted after confirmed cleanup")
            return initial(argv, cwd, timeout, prompt=prompt)

        with self.assertRaises(Interrupted):
            run(self.plan, self.repo, self.logs, execute=interrupted)
        self.assertEqual((initial.a_calls, initial.test_calls, initial.b_calls), (2, 2, 1))
        resumed = FakeExecutor([APPROVED], review_only=True)
        self.assertEqual(self.run_fake(resumed, resume=True), 0)
        self.assertEqual((resumed.a_calls, resumed.test_calls, resumed.b_calls), (0, 0, 1))
        for issue in issues:
            self.assertIn(issue, resumed.b_prompts[0])
        self.assertIn("Verify that EVERY finding below was addressed", resumed.b_prompts[0])

    def test_approval_with_issues_is_invalid_and_never_committed(self):
        ambiguous = {"decision": "APPROVED", "issues": ["A blocker remains"], "summary": "Unclear"}
        fake = FakeExecutor([ambiguous, ambiguous, ambiguous])
        self.assertEqual(self.run_fake(fake), 1)
        self.assertEqual((fake.a_calls, fake.test_calls, fake.b_calls), (1, 1, 3))
        self.assertEqual(self.state()["completed"], [])
        self.assertIsNone(self.state()["pending_review"]["record"]["review"])
        self.assertFalse(any(e["event"] == "checkpoint_committed" for e in self.events()))

    def test_changed_staged_diff_rejects_resume(self):
        self.stopped_at_review()
        (self.repo / "app.txt").write_text("altered", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=self.repo, check=True, capture_output=True)
        with self.assertRaisesRegex(PilotError, "staged diff changed"):
            self.run_fake(FakeExecutor(review_only=True), resume=True)
        self.assertEqual(self.state()["completed"], [])

    def test_unstaged_change_rejects_resume(self):
        self.stopped_at_review()
        (self.repo / "app.txt").write_text("altered", encoding="utf-8")
        with self.assertRaisesRegex(PilotError, "unstaged or untracked"):
            self.run_fake(FakeExecutor(review_only=True), resume=True)

    def test_untracked_change_rejects_resume(self):
        self.stopped_at_review()
        (self.repo / "surprise.txt").write_text("unexpected", encoding="utf-8")
        with self.assertRaisesRegex(PilotError, "unstaged or untracked"):
            self.run_fake(FakeExecutor(review_only=True), resume=True)

    def test_changed_plan_and_head_reject_resume(self):
        self.stopped_at_review()
        original = self.plan.read_text(encoding="utf-8")
        self.plan.write_text(original.replace("Implement one", "Different task"), encoding="utf-8")
        with self.assertRaisesRegex(PilotError, "plan or repository mismatch"):
            self.run_fake(FakeExecutor(review_only=True), resume=True)
        self.plan.write_text(original, encoding="utf-8")
        subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                        "commit", "-m", "unexpected", "--no-gpg-sign"], cwd=self.repo,
                       check=True, capture_output=True)
        with self.assertRaisesRegex(PilotError, "Git HEAD changed"):
            self.run_fake(FakeExecutor(review_only=True), resume=True)

    def test_different_repository_rejects_resume(self):
        self.stopped_at_review()
        other = self.root / "other"
        other.mkdir()
        subprocess.run(["git", "init", "-b", "feature/review"], cwd=other,
                       check=True, capture_output=True)
        with self.assertRaisesRegex(PilotError, "plan or repository mismatch"):
            run(self.plan, other, self.logs, resume=True,
                execute=FakeExecutor(review_only=True))

    def test_changed_test_evidence_rejects_resume(self):
        self.stopped_at_review()
        (self.logs / "one-01-test-1.txt").write_text("tampered", encoding="utf-8")
        with self.assertRaisesRegex(PilotError, "test output changed"):
            self.run_fake(FakeExecutor(review_only=True), resume=True)

    def test_retry_limit_stops_without_using_correction_attempt(self):
        state = self.stopped_at_review()
        self.assertEqual(state["pending_review"]["record"]["attempt"], 1)
        self.assertFalse(any(e["event"] == "correction_requested" for e in self.events()))
        stopped = [e for e in self.events() if e["event"] == "stopped"]
        self.assertTrue(stopped[-1]["metadata"]["review_retry_limit"])

    def test_unverified_reviewer_cleanup_blocks_retry_and_resume(self):
        class UncertainResult(tuple):
            def __new__(cls):
                result = super().__new__(cls, (124, "timed out", True))
                result.cleanup_confirmed = False
                return result
        fake = FakeExecutor([UncertainResult()])
        self.assertEqual(self.run_fake(fake), 1)
        self.assertEqual(fake.b_calls, 1)
        self.assertTrue(self.state()["pending_review"]["process_uncertain"])
        self.assertEqual(self.retry_delays, [])
        with self.assertRaisesRegex(OSError, "cleanup remains unverified"):
            self.run_fake(FakeExecutor(review_only=True), resume=True)
        with self.assertRaisesRegex(OSError, "cleanup remains unverified"):
            self.run_fake(FakeExecutor(review_only=True), resume=True, retry_review_once=True)

    def test_confirmed_reviewer_timeout_retries_only_b(self):
        class ConfirmedTimeout(tuple):
            def __new__(cls):
                result = super().__new__(cls, (124, "timed out", True))
                result.cleanup_confirmed = True
                return result
        fake = FakeExecutor([ConfirmedTimeout(), APPROVED])
        self.assertEqual(self.run_fake(fake), 0)
        self.assertEqual((fake.a_calls, fake.test_calls, fake.b_calls), (1, 1, 2))
        self.assertEqual(self.retry_delays, [5.0])
        retry = next(event for event in self.events() if event["event"] == "review_retry")
        self.assertEqual(retry["metadata"]["failure_kind"], "timeout")

    def test_test_modified_implementation_never_becomes_review_ready(self):
        fake = FakeExecutor()
        def mutating_test(argv, cwd, timeout, *, prompt=None):
            if argv[0] == "fake-test":
                (cwd / "app.txt").write_text("changed during test", encoding="utf-8")
            return fake(argv, cwd, timeout, prompt=prompt)
        self.assertEqual(run(self.plan, self.repo, self.logs, execute=mutating_test), 1)
        self.assertEqual(fake.b_calls, 0)
        self.assertIsNone(self.state()["pending_review"])
        self.assertEqual(self.state()["completed"], [])

    def test_codex_a_changed_head_never_becomes_review_ready(self):
        fake = FakeExecutor()
        def committing_a(argv, cwd, timeout, *, prompt=None):
            result = fake(argv, cwd, timeout, prompt=prompt)
            if "--output-schema" not in argv and argv[0] != "fake-test":
                subprocess.run(["git", "add", "-A"], cwd=cwd, check=True, capture_output=True)
                subprocess.run(["git", "-c", "user.name=Test",
                                "-c", "user.email=test@example.invalid",
                                "commit", "-m", "unauthorized", "--no-gpg-sign"],
                               cwd=cwd, check=True, capture_output=True)
            return result
        self.assertEqual(run(self.plan, self.repo, self.logs, execute=committing_a), 1)
        self.assertEqual((fake.a_calls, fake.test_calls, fake.b_calls), (1, 0, 0))
        self.assertIsNone(self.state()["pending_review"])

    def test_reviewer_modified_worktree_cannot_approve(self):
        fake = FakeExecutor()
        def mutating_reviewer(argv, cwd, timeout, *, prompt=None):
            if "--output-schema" in argv:
                (cwd / "app.txt").write_text("changed during review", encoding="utf-8")
            return fake(argv, cwd, timeout, prompt=prompt)
        with self.assertRaisesRegex(PilotError, "unstaged or untracked"):
            run(self.plan, self.repo, self.logs, execute=mutating_reviewer)
        self.assertEqual(self.state()["completed"], [])
        self.assertIsNotNone(self.state()["pending_review"])

    def test_metrics_have_real_usage_and_durations_without_sensitive_text_or_accumulation(self):
        secret = "private-prompt-and-output-secret"
        self.plan.write_text(self.plan.read_text().replace("Implement one", secret), encoding="utf-8")
        self.logs.mkdir()
        (self.logs / "metrics.json").write_text('{"invocations":999999}', encoding="utf-8")
        fake = FakeExecutor()
        def measured(argv, cwd, timeout, *, prompt=None):
            result = fake(argv, cwd, timeout, prompt=prompt)
            if argv[0] == "fake-test":
                return result
            usage = {"input_tokens": 100, "cached_input_tokens": 40, "output_tokens": 10}
            events = [{"type": "thread.started", "thread_id": secret}, {"type": "turn.started"},
                      {"type": "item.completed", "item": {"text": secret}},
                      {"type": "turn.completed", "usage": usage}]
            return result[0], "\n".join(json.dumps(event) for event in events), result[2]
        with patch.object(runner, "_execution_clock", side_effect=[0, 1, 1, 3, 3, 6, 6, 10]):
            self.assertEqual(run(self.plan, self.repo, self.logs, execute=measured), 0)
        metrics = self.metrics()
        self.assertEqual([metrics[role]["duration_seconds"] for role in ("codex_a", "tests", "codex_b")],
                         [1, 6, 3])
        for role in ("codex_a", "codex_b"):
            self.assertEqual(metrics[role]["usage"], {"input_tokens": 100, "cached_input_tokens": 40,
                                                     "output_tokens": 10})
            self.assertEqual(metrics[role]["usage_observations"], 1)
        content = (self.logs / "metrics.json").read_text(encoding="utf-8")
        self.assertNotIn(secret, content)
        self.assertNotIn("999999", content)
        runner.write_metrics(self.logs)
        self.assertEqual(self.metrics(), metrics)
        self.assertEqual(self.run_fake(FakeExecutor(review_only=True), resume=True), 0)
        self.assertEqual(self.metrics(), metrics)

    def test_metrics_write_failure_does_not_bypass_review_or_change_valid_execution(self):
        fake = FakeExecutor(["invalid", "invalid", "invalid"])
        errors = io.StringIO()
        with patch.object(runner, "write_metrics", side_effect=OSError("sensitive diagnostic")), \
                redirect_stderr(errors):
            self.assertEqual(self.run_fake(fake), 1)
        self.assertEqual(self.state()["completed"], [])
        self.assertNotIn("sensitive diagnostic", errors.getvalue())
        self.assertIn("metrics summary unavailable", errors.getvalue())
        with patch.object(runner, "write_metrics", side_effect=OSError("unavailable")), \
                redirect_stderr(errors):
            self.assertEqual(self.run_fake(FakeExecutor(review_only=True), resume=True,
                                           retry_review_once=True), 0)
        self.assertEqual(self.state()["completed"], ["one"])


class CorrectionContextTests(unittest.TestCase):
    def test_verdict_accepts_more_than_twenty_concise_independent_findings(self):
        verdict = {"decision": "REJECTED", "issues": [f"issue {n}: concrete path and fix" for n in range(21)],
                   "summary": "Multiple independent failures"}
        self.assertNotIn("maxItems", runner.VERDICT_SCHEMA["properties"]["issues"])
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "verdict.json"
            path.write_text(json.dumps(verdict), encoding="utf-8")
            self.assertEqual(checked_review(path), verdict)

    def test_full_bounded_verdict_keeps_summary_and_verification_without_truncation(self):
        issues = [f"issue {n}: " + "x" * 850 for n in range(10)]
        review = {"decision": "REJECTED", "issues": issues,
                  "summary": "summary:" + "s" * 3900,
                  "verification": {"status": "failed", "details": "verification:" + "v" * 3900}}
        context = correction_context([{"attempt": 1, "review": review}])
        self.assertLessEqual(len(context), MAX_CORRECTION_CONTEXT)
        self.assertNotIn("Feedback shortened", context)
        for issue in issues:
            self.assertIn(issue, context)
        self.assertIn(review["summary"], context)
        self.assertIn(review["verification"]["details"], context)

    def test_oversized_complete_verdict_stops_instead_of_dropping_findings(self):
        review = {"decision": "REJECTED", "issues": ["x"] * MAX_REVIEW_ISSUES_TOTAL,
                  "summary": "s" * 3900,
                  "verification": {"status": "failed", "details": "v" * 3900}}
        with self.assertRaisesRegex(PilotError, "Complete Codex B verdict exceeds"):
            correction_context([{"attempt": 1, "review": review}])

    def test_failed_correction_keeps_complete_prior_verdict_and_current_test_failure(self):
        review = {"decision": "REJECTED", "issues": ["reset misses edge case", "save loses failure"],
                  "summary": "Two independent bugs",
                  "verification": {"status": "failed", "details": "test_reset and test_save failed"}}
        context = correction_context([{"attempt": 1, "review": review},
                                      {"attempt": 2, "feedback": "controller test broke reload"}])
        for text in (*review["issues"], review["summary"], review["verification"]["details"],
                     "controller test broke reload"):
            self.assertEqual(context.count(text), 1)
        self.assertLessEqual(len(context), MAX_CORRECTION_CONTEXT)

    def test_noisy_latest_test_failure_shortens_only_test_output(self):
        review = {"decision": "REJECTED", "issues": ["reset: fix " + "x" * 8000],
                  "summary": "s" * 3900,
                  "verification": {"status": "failed", "details": "v" * 3900}}
        context = correction_context([{"attempt": 1, "review": review},
                                      {"attempt": 2, "feedback": "head" + "noise" * 5000 + "tail"}])
        self.assertLessEqual(len(context), MAX_CORRECTION_CONTEXT)
        self.assertIn(review["issues"][0], context)
        self.assertIn(review["summary"], context)
        self.assertIn(review["verification"]["details"], context)
        for text in ("head", "tail", "Feedback shortened"):
            self.assertIn(text, context)

    def test_missing_latest_feedback_does_not_erase_earlier_issues(self):
        prior = [{"attempt": 1, "review": {"decision": "REJECTED", "issues": ["Unresolved reset"]}},
                 {"attempt": 2, "feedback": ""}]
        self.assertIn("Unresolved reset", correction_context(prior))

    def test_test_only_failure_does_not_claim_there_was_a_review_verdict(self):
        context = correction_context([{"attempt": 1, "feedback": "test_reset failed"}])
        self.assertIn("test_reset failed", context)
        self.assertIn("latest test or execution failure evidence", context)
        self.assertNotIn("Read the entire Codex B verdict", context)

    def test_context_is_bounded_with_complete_current_blockers_and_explicit_omissions(self):
        current = [f"current-{index}: " + "x" * 880 for index in range(10)]
        prior = [{"attempt": attempt, "review": {"decision": "REJECTED",
                  "issues": [f"old-{attempt}-{index}: " + "y" * 1800 for index in range(10)]}}
                 for attempt in range(1, 10)]
        prior.append({"attempt": 10, "review": {"decision": "REJECTED", "issues": current}})
        context = correction_context(prior)
        self.assertLessEqual(len(context), MAX_CORRECTION_CONTEXT)
        for issue in current:
            self.assertIn(issue, context)
        self.assertIn("older review issue(s) omitted", context)

    def test_legacy_feedback_is_bounded_and_marked(self):
        context = correction_context([{"attempt": 1, "feedback": "head" + "x" * 50000 + "tail"}])
        self.assertLessEqual(len(context), MAX_CORRECTION_CONTEXT)
        self.assertIn("Feedback shortened", context)
        self.assertIn("head", context)
        self.assertIn("tail", context)
        self.assertEqual(correction_context([]), "")

    def test_invalid_and_oversized_verdicts_fail_closed(self):
        invalid = [
            {"decision": "APPROVED", "issues": ["remaining blocker"], "summary": "Unclear"},
            {"decision": "REJECTED", "issues": [], "summary": "No detail"},
            {"decision": "REJECTED", "issues": ["   "], "summary": "Blank"},
            {"decision": "REJECTED", "issues": ["x" * (MAX_REVIEW_ISSUE_CHARS + 1)], "summary": "Long"},
            {"decision": "REJECTED", "issues": ["x" * 1000] * (MAX_REVIEW_ISSUES_TOTAL // 1000 + 1),
             "summary": "Too much feedback"},
            {"decision": "APPROVED", "issues": [], "summary": ""},
        ]
        with tempfile.TemporaryDirectory() as root:
            verdict_path = Path(root) / "verdict.json"
            for verdict in invalid:
                with self.subTest(verdict=str(verdict)[:80]):
                    verdict_path.write_text(json.dumps(verdict), encoding="utf-8")
                    with self.assertRaises(PilotError):
                        checked_review(verdict_path)
            for raw in (b"\xff", b" " * (MAX_VERDICT_BYTES + 1)):
                verdict_path.write_bytes(raw)
                with self.assertRaises(PilotError):
                    checked_review(verdict_path)
            verdict_path.write_text(json.dumps(REJECTED), encoding="utf-8")
            self.assertEqual(checked_review(verdict_path), REJECTED)
            verdict_path.write_text(json.dumps(APPROVED), encoding="utf-8")
            self.assertEqual(checked_review(verdict_path), APPROVED)


class ReviewRetryCliTests(unittest.TestCase):
    def test_explicit_retry_flag_reaches_runner_only_with_resume(self):
        argv = ["tasks.json", "--repo", "project", "--logs", "logs", "--retry-review-once"]
        with patch.object(runner, "run", return_value=0) as execute, \
                patch.object(runner.shutil, "which", return_value="codex"), \
                patch.object(runner, "cancel_reservation") as cancel:
            with self.assertRaises(SystemExit) as invalid:
                runner.main(argv)
            self.assertEqual(invalid.exception.code, 2)
            execute.assert_not_called()
            cancel.assert_called_once()
            self.assertEqual(runner.main(argv + ["--resume"]), 0)
            self.assertTrue(execute.call_args.kwargs["retry_review_once"])
            self.assertTrue(execute.call_args.kwargs["resume"])


if __name__ == "__main__":
    unittest.main()
