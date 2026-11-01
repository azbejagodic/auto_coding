"""Review-only recovery uses fake executors and disposable Git repositories."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from auto_coding.simple_run import (PilotError, run, checked_review, correction_context,
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

    def run_fake(self, fake, *, resume=False):
        return run(self.plan, self.repo, self.logs, resume=resume, execute=fake)

    def state(self):
        return json.loads((self.logs / "state.json").read_text(encoding="utf-8"))

    def events(self):
        return [json.loads(line) for line in
                (self.logs / "events.jsonl").read_text(encoding="utf-8").splitlines()]

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

    def test_connection_failure_retries_only_b(self):
        fake = FakeExecutor([1, APPROVED])
        self.assertEqual(self.run_fake(fake), 0)
        self.assertEqual((fake.a_calls, fake.test_calls, fake.b_calls), (1, 1, 2))
        self.assertEqual([e["event"] for e in self.events()].count("review_retry"), 1)

    def test_connection_failure_restart_resumes_only_b(self):
        original = self.stopped_at_review()
        pending = original["pending_review"]
        self.assertEqual(pending["attempt"], 1)
        self.assertIsNone(pending["base_head"])
        self.assertEqual(pending["record"]["tests"][0]["exit_code"], 0)
        resumed = FakeExecutor(review_only=True)
        self.assertEqual(self.run_fake(resumed, resume=True), 0)
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

    def test_invalid_response_cannot_approve(self):
        fake = FakeExecutor(["invalid", "invalid", "invalid"])
        self.assertEqual(self.run_fake(fake), 1)
        self.assertEqual((fake.a_calls, fake.test_calls, fake.b_calls), (1, 1, 3))
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
        self.assertIn("Fix the implementation", fake.a_prompts[1])
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
        with self.assertRaisesRegex(PilotError, "cleanup is unverified"):
            self.run_fake(FakeExecutor(review_only=True), resume=True)

    def test_confirmed_reviewer_timeout_retries_only_b(self):
        class ConfirmedTimeout(tuple):
            def __new__(cls):
                result = super().__new__(cls, (124, "timed out", True))
                result.cleanup_confirmed = True
                return result
        fake = FakeExecutor([ConfirmedTimeout(), APPROVED])
        self.assertEqual(self.run_fake(fake), 0)
        self.assertEqual((fake.a_calls, fake.test_calls, fake.b_calls), (1, 1, 2))

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


class CorrectionContextTests(unittest.TestCase):
    def test_missing_latest_feedback_does_not_erase_earlier_issues(self):
        prior = [{"attempt": 1, "review": {"decision": "REJECTED", "issues": ["Unresolved reset"]}},
                 {"attempt": 2, "feedback": ""}]
        self.assertIn("Unresolved reset", correction_context(prior))

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


if __name__ == "__main__":
    unittest.main()
