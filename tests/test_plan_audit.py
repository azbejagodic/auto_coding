"""B08 authorization and one-shot audit regressions; no real model calls."""
import contextlib
import copy
import hashlib
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from auto_coding import simple_run as runner
from auto_coding import plan_audit as audit
from auto_coding.repository_ownership import execution_lock
from auto_coding.reviewer_permissions import command_environment, ReviewerEnvironmentError
from test_final_verification import CODE_REVIEW_APPROVED, Executor, POLICY


class PlanAuditTests(unittest.TestCase):
    def test_audit_model_authentication_and_high_capability_are_fail_closed(self):
        from types import SimpleNamespace
        from auto_coding import reviewer_permissions as permissions
        cases = [({'authenticated': False, 'supported': True}, 'authentication'),
                 ({'authenticated': True, 'supported': False}, 'High reasoning'),
                 ({'authenticated': True, 'supported': True}, None)]
        for response, error in cases:
            with self.subTest(response=response), patch.object(permissions.shutil, 'which', return_value='codex'), patch.object(
                    permissions, 'bounded_run', return_value=SimpleNamespace(returncode=0, stdout=json.dumps(response))) as command:
                if error:
                    with self.assertRaisesRegex(ReviewerEnvironmentError, error):
                        permissions.check_audit_model('fake-audit-model', self.repo)
                else:
                    permissions.check_audit_model('fake-audit-model', self.repo)
                argv = command.call_args.args[0]
                self.assertIn('account/read', argv[3])
                self.assertIn('model/list', argv[3])
                self.assertNotIn('thread/start', argv[3])
                self.assertEqual(command.call_args.kwargs['timeout'], 40)
        for result in (SimpleNamespace(returncode=1, stdout=''),
                       SimpleNamespace(returncode=0, stdout='malformed response')):
            with patch.object(permissions.shutil, 'which', return_value='codex'), patch.object(
                    permissions, 'bounded_run', return_value=result):
                with self.assertRaisesRegex(ReviewerEnvironmentError, 'configuration'):
                    permissions.check_audit_model('fake-audit-model', self.repo)
        self.assertEqual(self.calls, [])

    def test_production_auth_failure_precedes_reservation_and_agent_call(self):
        with patch.object(audit, 'unwrap', return_value=runner.call), patch.object(
                runner, 'workflow_preflight'), patch(
                'auto_coding.reviewer_permissions.check_audit_model',
                side_effect=ReviewerEnvironmentError('Local audit authentication missing')):
            with self.assertRaisesRegex(runner.PilotError, 'authentication missing'):
                self.do_audit()
        self.assertEqual(self.calls, [])
        self.assertFalse(audit.ledger_path(self.repo, self.identity()).exists())
        self.assertFalse((self.logs / 'audit-evidence.json').exists())

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.git("init", "-b", "feature/audit")
        self.git("config", "user.name", "Test")
        self.git("config", "user.email", "test@example.invalid")
        (self.repo / "seed.txt").write_text("seed")
        self.git("add", "-A")
        self.git("commit", "-m", "seed")
        self.plan = self.root / "plan.json"
        self.document = {"tasks": [{"id": "one", "prompt": "Implement app.py", "test_commands": [["fake-focused"]]},
                                   {"id": "two", "prompt": "Complete app.py", "test_commands": [["fake-focused"]]}],
                         "final_verification": POLICY}
        self.plan.write_text(json.dumps(self.document))
        self.logs, self.run_logs = self.root / "audit", self.root / "run"
        self.calls = []
        self.report = {"decision": "APPROVE", "summary": "Feasible ordered plan", "overall_risk": "low",
                       "tasks": [{"task_id": key, "findings": ["Task requirements are specific"],
                                  "recommended_changes": [], "blocking_issues": []}
                                 for key in ("one", "two")], "recommended_changes": [], "blocking_issues": []}
        self.result = runner._CallResult(0, "audit completed", False, True, True)

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.repo, capture_output=True,
                              text=True, check=True, timeout=15).stdout.strip()

    def files(self):
        return {str(p.relative_to(self.repo)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in self.repo.rglob("*") if p.is_file()}

    def execute(self, argv, cwd, timeout, *, prompt):
        self.calls.append((argv, cwd, timeout, prompt))
        self.assertEqual(cwd, self.repo)
        self.assertNotIn("--sandbox", argv)
        self.assertIn('model_reasoning_effort="high"', argv)
        self.assertEqual(argv[argv.index("--model") + 1], "fake-audit-model")
        environment = command_environment(cwd)
        self.assertEqual(environment["TEMP"], environment["TMP"])
        self.assertEqual(environment["TMP"], environment["TMPDIR"])
        self.assertEqual(Path(environment["PYTHONPATH"].split(__import__("os").pathsep)[0]), cwd)
        ledger = audit.ledger_path(cwd, self.identity())
        self.assertEqual(audit.read_json(ledger)["status"], "invoking")
        answer = Path(argv[argv.index("--output-last-message") + 1])
        answer.write_text(json.dumps(self.report) if isinstance(self.report, dict) else self.report)
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result

    def identity(self):
        _, fingerprint = runner.load_tasks(self.plan)
        return {"plan_sha256": fingerprint, "target": audit.snapshot(runner, self.repo),
                "verification_policy": runner.requested_verification(self.plan, fingerprint, None)}

    def do_audit(self, **kwargs):
        with contextlib.redirect_stdout(io.StringIO()):
            return runner.audit(self.plan, self.repo, self.logs, model="fake-audit-model",
                                execute=self.execute, **kwargs)

    def start(self, execute=None, **kwargs):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return runner.run(self.plan, self.repo, self.run_logs, execute=execute or Executor(), **kwargs)

    def test_approve_durable_evidence_all_tasks_one_call_read_only(self):
        before = self.files()
        self.assertEqual(self.do_audit(), 0)
        self.assertEqual(self.files(), before)
        self.assertEqual(len(self.calls), 1)
        self.assertIn(self.document["tasks"][0]["prompt"], self.calls[0][3])
        self.assertIn(self.document["tasks"][1]["prompt"], self.calls[0][3])
        evidence, report = audit.load_evidence(runner, self.logs, self.repo,
                                              runner.load_tasks(self.plan)[1], ["one", "two"])
        self.assertEqual(evidence["configuration"]["reasoning"], "high")
        self.assertIs(evidence["configuration"]["controller_tests"], False)
        self.assertIn("Controller-managed focused tests: disabled", self.calls[0][3])
        self.assertEqual(report["decision"], "APPROVE")
        self.assertFalse((self.logs / "state.json").exists())
        self.assertFalse(list(self.logs.glob("review-scratch-*")))

    def test_unchanged_audit_reuses_report_without_call(self):
        self.do_audit()
        self.assertEqual(self.do_audit(), 0)
        self.assertEqual(len(self.calls), 1)
        with self.assertRaisesRegex(runner.PilotError, "different settings"):
            self.do_audit(goal="changed goal")
        self.assertEqual(len(self.calls), 1)

    def test_start_requires_explicit_approval_and_cannot_resume_failed_start(self):
        fake = Executor()
        with self.assertRaisesRegex(runner.PilotError, "require an approved"):
            self.start(fake)
        with self.assertRaisesRegex(runner.PilotError, "No existing state"):
            self.start(fake, resume=True)
        self.assertEqual(fake.calls, [])

    def test_approved_start_and_completed_resume_have_no_audit_call(self):
        self.do_audit()
        fake = Executor()
        self.assertEqual(self.start(fake, approved_audit=self.logs), 0)
        self.assertEqual(fake.calls, ["A", "B", "A", "B", "final"])
        self.assertFalse(audit.read_json(self.run_logs / "state.json")["controller_tests"])
        self.assertFalse(audit.read_json(self.run_logs / "run-manifest.json")["controller_tests"])
        self.assertEqual(self.start(fake, resume=True), 0)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(len(fake.calls), 5)

    def test_three_dependent_tasks_audit_checkpoints_real_final_suite_and_reopen(self):
        source = self.repo / "pricing.py"
        source.write_text("# Dependent batch fixture.\n")
        (self.repo / "test_pricing.py").write_text(
            "import unittest\nimport pricing\n\n"
            "class PricingTests(unittest.TestCase):\n"
            "    def test_subtotal(self):\n"
            "        self.assertEqual(pricing.subtotal([3, 4]), 7)\n"
            "        self.assertEqual(pricing.subtotal([]), 0)\n"
            "    def test_total(self):\n"
            "        self.assertEqual(pricing.total([3, 4], 2), 5)\n"
            "        self.assertEqual(pricing.total([3, 4], 99), 0)\n"
            "    def test_receipt(self):\n"
            "        self.assertEqual(pricing.receipt([3, 4], 2), 'Total: 5.00')\n")
        self.git("add", "-A")
        self.git("commit", "-m", "seed dependent batch tests")
        initial = self.git("rev-parse", "HEAD")
        names = ("subtotal", "total", "receipt")
        changes = ("\ndef subtotal(prices):\n    return sum(prices)\n",
                   "\ndef total(prices, discount=0):\n    return max(0, subtotal(prices) - discount)\n",
                   "\ndef receipt(prices, discount=0):\n    return f'Total: {total(prices, discount):.2f}'\n")
        focused = [[runner.sys.executable, "-B", "-m", "unittest", f"test_pricing.PricingTests.test_{name}"]
                   for name in names]
        final = [runner.sys.executable, "-B", "-m", "unittest", "-v", "test_pricing"]
        policy = {"test_commands": [final], "minimum_tests": 3, "timeout_seconds": 30}
        self.document = {"tasks": [
            {"id": "one", "prompt": "Implement subtotal(prices) as the sum, including empty inputs", "test_commands": [focused[0]]},
            {"id": "two", "prompt": "Implement total(prices, discount=0) using subtotal; clamp to zero", "test_commands": [focused[1]]},
            {"id": "three", "prompt": "Implement receipt(prices, discount=0) using total, formatted as Total: 5.00", "test_commands": [focused[2]]}],
            "final_verification": policy}
        self.plan.write_text(json.dumps(self.document))
        self.report["tasks"].append({"task_id": "three", "findings": ["Depends on total and subtotal"],
                                     "recommended_changes": [], "blocking_issues": []})
        self.assertEqual(self.do_audit(), 0)
        self.assertEqual(self.do_audit(), 0)
        tasks, fingerprint = runner.load_tasks(self.plan)
        ids = [task["id"] for task in tasks]
        evidence, _ = audit.load_evidence(runner, self.logs, self.repo, fingerprint, ids)
        calls, focused_results, final_results = [], [], []

        def execute(argv, cwd, timeout, *, prompt=None):
            self.assertEqual(cwd, self.repo)
            state = audit.read_json(self.run_logs / "state.json")
            if argv == final:
                self.assertIsNone(prompt)
                self.assertEqual(calls, ["A", "B"] * 3)
                self.assertEqual(state["completed"], ids)
                self.assertEqual(len({record["commit"] for record in state["history"]}), 3)
                self.assertEqual(self.git("status", "--porcelain"), "")
                calls.append("final")
                result = runner.call(argv, cwd, timeout)
                final_results.append(result)
                return result
            # Any controller-focused test dispatch would fail here, rather than
            # silently masquerading as part of the mock implementer's testing.
            self.assertIn("--output-last-message", argv)
            answer = Path(argv[argv.index("--output-last-message") + 1])
            index = len(focused_results) - ("--output-schema" in argv)
            self.assertIn(tasks[index]["prompt"], prompt)
            if "--output-schema" in argv:
                calls.append("B")
                self.assertEqual(source.read_text(), "# Dependent batch fixture.\n" + "".join(changes[:index + 1]))
                self.assertIn("+def " + names[index], self.git("diff", "--cached", "--", "pricing.py"))
                self.assertIn("Do not execute tests, linting, builds or verification commands", prompt)
                self.assertIn("status=not_run", prompt)
                self.assertIn("claims, not controller verification; context only", prompt)
                self.assertNotIn("Run configured tests", prompt)
                self.assertNotIn("minimal relevant checks", prompt)
                self.assertNotIn("--sandbox", argv)
                self.assertTrue(any('extends=":read-only"' in part for part in argv))
                answer.write_text(json.dumps(CODE_REVIEW_APPROVED))
            else:
                calls.append("A")
                self.assertEqual(state["completed"], ids[:index])
                self.assertEqual(self.git("rev-parse", "HEAD"), state["history"][-1]["commit"] if index else initial)
                self.assertEqual(source.read_text(), "# Dependent batch fixture.\n" + "".join(changes[:index]))
                self.assertIn(json.dumps([focused[index]]), prompt)
                source.write_text(source.read_text() + changes[index])
                result = runner.call(focused[index], cwd, timeout)
                self.assertEqual(result[0], 0, result[1])
                self.assertEqual(runner.unittest_evidence(result[1])["tests"], 1)
                focused_results.append(result)
                answer.write_text("Executed " + json.dumps(focused[index]) + "\n" + result[1])
            return runner._CallResult(0, "Mock agent completed", False, True, True)

        with patch.object(runner, "execute_controller_test", wraps=runner.execute_controller_test) as controller:
            self.assertEqual(self.start(execute, approved_audit=self.logs, max_agent_calls=6), 0)
            state = audit.read_json(self.run_logs / "state.json")
            manifest = audit.read_json(self.run_logs / "run-manifest.json")
            events = [json.loads(line) for line in (self.run_logs / "events.jsonl").read_text().splitlines()]
            saved = state["final_verification_result"]
            commits = [record["commit"] for record in state["history"]]
            self.assertEqual(calls, ["A", "B"] * 3 + ["final"])
            self.assertEqual(len(self.calls), 1)
            self.assertEqual(len(focused_results), 3)
            self.assertEqual(len(final_results), 1)
            self.assertEqual(controller.call_count, 1)
            self.assertEqual(controller.call_args.args[1], final)
            self.assertFalse(state["controller_tests"])
            self.assertEqual(state["status"], "completed")
            self.assertEqual(state["completed"], ids)
            self.assertEqual(state["plan_audit"], manifest["plan_audit"])
            self.assertEqual(state["plan_audit"]["evidence_sha256"], audit.digest(evidence))
            self.assertEqual(manifest["initial_git_head"], initial)
            self.assertEqual(self.git("rev-list", "--reverse", f"{initial}..HEAD").splitlines(), commits)
            self.assertEqual(saved["head"], commits[-1])
            self.assertEqual(saved["branch"], self.git("branch", "--show-current"))
            self.assertEqual(saved["plan_sha256"], fingerprint)
            self.assertEqual(saved["policy_sha256"], runner.policy_sha256(policy))
            self.assertEqual(saved["test_count"], 3)
            self.assertEqual(state["final_verification_invocations"], 1)
            self.assertTrue(runner.final_evidence_valid(self.repo, self.run_logs, state))
            for record in state["history"]:
                self.assertEqual(record["tests"], [])
                self.assertEqual(record["review"]["verification"]["status"], "not_run")
                report = (self.run_logs / record["a_report"]["output"]).read_bytes()
                self.assertEqual(record["a_report"]["sha256"], hashlib.sha256(report).hexdigest())
            test_events = [event for event in events if event["event"] == "test_started"]
            self.assertEqual(len(test_events), 1)
            checkpoints = [event for event in events if event["event"] == "checkpoint_committed"]
            self.assertEqual(len(checkpoints), 3)
            self.assertLess(checkpoints[-1]["seq"], test_events[0]["seq"])
            output = self.run_logs / saved["tests"][0]["output"]
            original = output.read_bytes()
            self.assertEqual(original.decode("utf-8"), final_results[0][1])
            self.assertEqual(saved["tests"][0]["output_sha256"], hashlib.sha256(original).hexdigest())
            audit.verify_resume(runner, state, manifest, self.repo, fingerprint, tasks)
            unexpected = unittest.mock.Mock(side_effect=AssertionError("Completed batch must not execute again"))
            self.assertEqual(self.start(unexpected, resume=True), 0)
            unexpected.assert_not_called()
            self.assertEqual(controller.call_count, 1)
        self.assertEqual(self.git("rev-parse", "HEAD"), commits[-1])
        self.assertEqual(self.git("status", "--porcelain"), "")
        self.assertEqual(source.read_text(), "# Dependent batch fixture.\n" + "".join(changes))
        self.assertEqual(output.read_bytes(), original)
        self.assertEqual(audit.read_json(self.run_logs / "state.json")["final_verification_result"], saved)
        self.assertEqual(len(self.calls), 1)

    def test_review_only_resume_preserves_binding_and_does_not_audit(self):
        self.do_audit()
        first = Executor(reviews=[(1, json.dumps({"type": "turn.failed", "error": {
            "message": "unexpected status 503 Service Unavailable: upstream unavailable"}}), False)] * 3)
        with patch.object(runner, "_review_sleep"), patch.object(runner, "_review_clock", side_effect=range(1000, 1200, 20)):
            self.assertEqual(self.start(first, approved_audit=self.logs), 1)
        self.assertEqual(first.calls, ["A", "B", "B", "B"])
        second = Executor(implementation="VERSION = 'second task'\n")
        self.assertEqual(self.start(second, resume=True, retry_review_once=True), 0)
        self.assertEqual(second.calls, ["B", "A", "B", "final"])
        self.assertEqual(len(self.calls), 1)

    def test_revise_blocks_start(self):
        self.report.update(decision="REVISE", recommended_changes=["Specify observable acceptance criteria for task one"])
        self.assertEqual(self.do_audit(), 1)
        fake = Executor()
        with self.assertRaisesRegex(runner.PilotError, "not APPROVE"):
            self.start(fake, approved_audit=self.logs)
        self.assertEqual(fake.calls, [])

    def test_block_blocks_start(self):
        self.report.update(decision="BLOCK", blocking_issues=["Task two depends on a missing external service"])
        self.assertEqual(self.do_audit(), 1)
        with self.assertRaises(runner.PilotError):
            self.start(approved_audit=self.logs)

    def test_malformed_output_consumes_single_invocation_and_never_approves(self):
        self.report = "not JSON"
        with self.assertRaises(runner.PilotError):
            self.do_audit()
        with self.assertRaisesRegex(runner.PilotError, "already reserved"):
            self.do_audit()
        with self.assertRaises(runner.PilotError):
            self.start(approved_audit=self.logs)
        self.assertEqual(len(self.calls), 1)

    def test_contradictory_or_incomplete_verdicts_are_invalid(self):
        invalid = []
        value = copy.deepcopy(self.report); value["blocking_issues"] = ["real blocker"]; invalid.append(value)
        value = copy.deepcopy(self.report); value["tasks"][1]["task_id"] = "one"; invalid.append(value)
        value = copy.deepcopy(self.report); value["decision"] = "APPROVED"; invalid.append(value)
        value = copy.deepcopy(self.report); value["decision"] = "REVISE"; invalid.append(value)
        value = copy.deepcopy(self.report); value["tasks"][0]["findings"] = []; invalid.append(value)
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError):
                audit.validate_report(value, ["one", "two"])

    def test_verified_timeout_is_not_approval_and_releases_job_lock(self):
        self.result = runner._CallResult(124, "timeout diagnostics", True, True, True)
        with self.assertRaisesRegex(runner.PilotError, "process failed"):
            self.do_audit()
        self.assertFalse(execution_lock(self.repo, ".job.lock").exists())
        self.assertFalse((self.logs / "audit-evidence.json").exists())
        self.assertIn("timeout diagnostics", (self.logs / "audit-output.txt").read_text())

    def test_unverified_cleanup_preserves_workspace_protection(self):
        self.result = runner._CallResult(0, "parent exited", False, False, True)
        with self.assertRaisesRegex(runner.PilotError, "cleanup unverified"):
            self.do_audit()
        self.assertTrue(execution_lock(self.repo, ".job.lock").exists())
        self.assertTrue(execution_lock(self.repo, ".process-uncertain.json").exists())
        self.assertTrue(list(self.logs.glob("review-scratch-*")))
        with self.assertRaisesRegex(OSError, "unverified"):
            self.start(approved_audit=self.logs)

    def test_changed_plan_invalidates_approval(self):
        self.do_audit()
        self.plan.write_text(self.plan.read_text() + "\n")
        with self.assertRaisesRegex(runner.PilotError, "mismatch"):
            self.start(approved_audit=self.logs)

    def test_changed_head_invalidates_approval(self):
        self.do_audit()
        self.git("commit", "--allow-empty", "-m", "changed")
        with self.assertRaisesRegex(runner.PilotError, "initial target"):
            self.start(approved_audit=self.logs)

    def test_unstaged_and_untracked_changes_invalidate_approval(self):
        self.do_audit()
        for path in ("seed.txt", "unexpected.txt"):
            before = self.repo / path
            before.write_text("changed")
            with self.subTest(path=path), self.assertRaisesRegex(runner.PilotError, "clean target"):
                self.start(approved_audit=self.logs)
            if path == "seed.txt":
                before.write_text("seed")
            else:
                before.unlink()

    def test_report_tampering_is_detected(self):
        self.do_audit()
        value = audit.read_json(self.logs / "audit-report.json")
        value["summary"] = "tampered"
        runner.save_json(self.logs / "audit-report.json", value)
        with self.assertRaisesRegex(runner.PilotError, "report changed"):
            self.start(approved_audit=self.logs)

    def test_resume_cannot_drop_one_saved_audit_binding(self):
        self.do_audit()
        self.assertEqual(self.start(approved_audit=self.logs), 0)
        path = self.run_logs / "state.json"
        state = audit.read_json(path)
        del state["plan_audit"]
        runner.save_json(path, state)
        with self.assertRaisesRegex(runner.PilotError, "Saved plan audit"):
            self.start(resume=True)

    def test_preflight_failure_spends_no_model_call(self):
        with patch.object(runner, "workflow_preflight", side_effect=ReviewerEnvironmentError("legacy sandbox conflict")):
            with self.assertRaisesRegex(runner.PilotError, "legacy sandbox"):
                self.do_audit()
        self.assertEqual(self.calls, [])

    def test_confirmed_launch_failure_is_diagnostic_not_approval(self):
        failure = OSError("audit executable unavailable")
        failure.process_started = False
        self.result = failure
        with self.assertRaisesRegex(OSError, "executable unavailable"):
            self.do_audit()
        self.assertFalse(execution_lock(self.repo, ".job.lock").exists())
        self.assertFalse((self.logs / "audit-evidence.json").exists())
        with self.assertRaisesRegex(runner.PilotError, "already reserved"):
            self.do_audit()
        self.assertEqual(len(self.calls), 1)

    def test_uncertain_preflight_retains_workspace_protection_without_audit_call(self):
        failure = OSError("native probe cleanup uncertain")
        failure.process_started = True
        failure.cleanup_confirmed = False
        with patch.object(runner, "workflow_preflight", side_effect=failure):
            with self.assertRaisesRegex(OSError, "native probe"):
                self.do_audit()
        self.assertEqual(self.calls, [])
        self.assertTrue(execution_lock(self.repo, ".process-uncertain.json").exists())

    def test_scratch_removal_failure_does_not_claim_uncertain_process_cleanup(self):
        from auto_coding.reviewer_permissions import ReviewPermissions
        original = ReviewPermissions.cleanup
        def cleanup(spec):
            if self.calls:
                raise PermissionError("scratch ACL prevents removal")
            return original(spec)
        with patch.object(ReviewPermissions, "cleanup", cleanup):
            self.assertEqual(self.do_audit(), 0)
        self.assertFalse(execution_lock(self.repo, ".job.lock").exists())
        self.assertFalse(execution_lock(self.repo, ".process-uncertain.json").exists())
        events = [json.loads(line) for line in (self.logs / "events.jsonl").read_text().splitlines()]
        self.assertTrue(any(event["event"] == "plan_audit_scratch_retained" for event in events))

    def test_legacy_completed_run_recovers_without_audit_invocation(self):
        self.do_audit()
        self.start(approved_audit=self.logs)
        # A pre-B08 completed state has neither of the new optional bindings.
        for name in ("state.json", "run-manifest.json"):
            path = self.run_logs / name
            record = audit.read_json(path)
            del record["plan_audit"]
            runner.save_json(path, record)
        fake = Executor()
        with patch.object(runner, "audit", side_effect=AssertionError("No resume audit")):
            self.assertEqual(self.start(fake, resume=True), 0)
        self.assertEqual(fake.calls, [])

    def test_changed_verification_policy_and_audit_evidence_are_rejected(self):
        self.do_audit()
        evidence_path = self.logs / "audit-evidence.json"
        value = audit.read_json(evidence_path)
        value["configuration"]["model"] = "another-model"
        runner.save_json(evidence_path, value)
        with self.assertRaisesRegex(runner.PilotError, "evidence was altered"):
            self.start(approved_audit=self.logs)

    def test_changed_controller_testing_mode_invalidates_approval(self):
        # Historical controller-tested approval cannot authorize the new mode.
        with patch.object(runner, "new_workflow_test_mode", return_value=True):
            self.do_audit()
        fake = Executor()
        with self.assertRaisesRegex(runner.PilotError, "configuration"):
            self.start(fake, approved_audit=self.logs)
        self.assertEqual(fake.calls, [])
        with self.assertRaisesRegex(runner.PilotError, "different settings"):
            self.do_audit()
        self.assertEqual(len(self.calls), 1)

    def test_explicit_implementer_testing_mode_starts(self):
        self.assertEqual(self.do_audit(controller_tests=False), 0)
        fake = Executor()
        self.assertEqual(self.start(fake, approved_audit=self.logs, controller_tests=False), 0)
        self.assertEqual(fake.calls, ["A", "B", "A", "B", "final"])

    def test_missing_or_invalid_task_checks_rejected_before_audit_invocation(self):
        for index in range(2):
            for commands in (None, [], ["python -m unittest"], [[]], [[""]], [["python", True]]):
                document = copy.deepcopy(self.document)
                if commands is None:
                    document["tasks"][index].pop("test_commands")
                else:
                    document["tasks"][index]["test_commands"] = commands
                self.plan.write_text(json.dumps(document))
                with self.subTest(index=index, commands=commands), self.assertRaises(runner.PilotError):
                    self.do_audit()
                self.assertEqual(self.calls, [])
                self.assertFalse(self.logs.exists())

    def test_new_workflows_cannot_enable_controller_tests_before_audit_or_a(self):
        fake = Executor()
        for suffix in (".json", ".txt"):
            if suffix == ".txt":
                self.plan = self.root / "task.txt"
                self.plan.write_text("Implement app.py")
            with self.subTest(suffix=suffix):
                audit_error = "require Codex A-focused testing" if suffix == ".json" else "require a JSON plan"
                with self.assertRaisesRegex(runner.PilotError, audit_error):
                    self.do_audit(controller_tests=True, final_verification_config=POLICY)
                with self.assertRaisesRegex(runner.PilotError, "require Codex A-focused testing"):
                    self.start(fake, controller_tests=True, final_verification_config=POLICY)
        self.assertEqual(self.calls, [])
        self.assertEqual(fake.calls, [])

    def test_missing_task_checks_rejected_before_a_even_with_supplied_approval(self):
        self.do_audit()
        self.document["tasks"][1].pop("test_commands")
        self.plan.write_text(json.dumps(self.document))
        fake = Executor()
        with self.assertRaisesRegex(runner.PilotError, "requires at least one focused"):
            self.start(fake, approved_audit=self.logs)
        self.assertEqual(fake.calls, [])

    def test_legacy_review_only_resume_preserves_controller_mode_and_audit(self):
        fake = Executor(reviews=[runner._CallResult(127, "unavailable", False, True, False)])
        with patch.object(runner, "new_workflow_test_mode", return_value=True):
            self.do_audit()
            self.assertEqual(self.start(fake, approved_audit=self.logs), 1)
        before = (self.logs / "audit-evidence.json").read_bytes()
        self.assertEqual(fake.calls, ["A", "focused", "B"])
        resumed = Executor(implementation="VERSION = 'legacy second task'\n")
        self.assertEqual(self.start(resumed, resume=True), 0)
        self.assertEqual(resumed.calls, ["B", "A", "focused", "B", "final"])
        self.assertTrue(audit.read_json(self.run_logs / "state.json")["controller_tests"])
        self.assertEqual((self.logs / "audit-evidence.json").read_bytes(), before)
        self.assertEqual(len(self.calls), 1)

    def test_interrupted_controller_initialization_keeps_saved_mode_without_new_audit(self):
        self.assert_interrupted_initialization(controller_tests=True)

    def test_interrupted_implementer_initialization_keeps_saved_mode_without_new_audit(self):
        self.assert_interrupted_initialization(controller_tests=False)

    def assert_interrupted_initialization(self, *, controller_tests):
        if not controller_tests:
            # Older implementer workflows may have no configured commands.
            for task in self.document["tasks"]:
                task.pop("test_commands")
            self.plan.write_text(json.dumps(self.document))
        original = runner.save_json
        def fail_state(path, value):
            if path == self.run_logs / "state.json":
                raise OSError("interrupted before state persistence")
            return original(path, value)
        first = Executor()
        with patch.object(runner, "new_workflow_test_mode", return_value=controller_tests):
            self.do_audit()
            with patch.object(runner, "save_json", side_effect=fail_state):
                with self.assertRaisesRegex(OSError, "interrupted"):
                    self.start(first, approved_audit=self.logs)
        self.assertEqual(first.calls, [])
        self.assertFalse((self.run_logs / "state.json").exists())
        self.assertIs(audit.read_json(self.run_logs / "run-manifest.json")["controller_tests"], controller_tests)
        resumed = Executor()
        self.assertEqual(self.start(resumed, approved_audit=self.logs), 0)
        expected = (["A", "focused", "B", "A", "focused", "B", "final"] if controller_tests
                    else ["A", "B", "A", "B", "final"])
        self.assertEqual(resumed.calls, expected)
        self.assertIs(audit.read_json(self.run_logs / "state.json")["controller_tests"], controller_tests)
        self.assertEqual(len(self.calls), 1)

    def test_change_during_preflight_is_rechecked_before_first_a(self):
        self.do_audit()
        original = runner.workflow_preflight
        def changed(*args, **kwargs):
            original(*args, **kwargs)
            (self.repo / "unexpected.txt").write_text("external change")
        fake = Executor()
        with patch.object(runner, "workflow_preflight", side_effect=changed):
            with self.assertRaisesRegex(runner.PilotError, "clean target"):
                self.start(fake, approved_audit=self.logs)
        self.assertEqual(fake.calls, [])

    def test_different_target_cannot_reuse_approval(self):
        self.do_audit()
        other = self.root / "other"
        subprocess.run(["git", "clone", "--no-hardlinks", str(self.repo), str(other)],
                       check=True, capture_output=True, timeout=20)
        with self.assertRaisesRegex(runner.PilotError, "repository.*mismatch"):
            runner.run(self.plan, other, self.run_logs, execute=Executor(), approved_audit=self.logs)

    def test_large_plan_context_is_rejected_not_truncated(self):
        self.document["tasks"][0]["prompt"] = "x" * (audit.MAX_CONTEXT + 1)
        self.plan.write_text(json.dumps(self.document))
        with self.assertRaises(runner.PilotError):
            self.do_audit()
        self.assertEqual(self.calls, [])

    def test_cli_audit_and_start_are_separate(self):
        common = [str(self.plan), "--repo", str(self.repo), "--logs", str(self.logs)]
        with patch.object(runner.shutil, "which", return_value="codex"), \
                patch.object(runner, "audit", return_value=0) as auditor, \
                patch.object(runner, "run", return_value=0) as run:
            self.assertEqual(runner.main(["audit", *common, "--audit-model", "chosen-model"]), 0)
            run.assert_not_called()
            self.assertEqual(auditor.call_args.kwargs["model"], "chosen-model")
            self.assertEqual(runner.main(["start", *common, "--approved-audit", str(self.logs)]), 0)
            self.assertEqual(run.call_args.kwargs["approved_audit"], self.logs)


if __name__ == "__main__":
    unittest.main()
