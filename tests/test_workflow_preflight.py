"""Early compatibility failures must never spend an implementation call."""
import contextlib
import hashlib
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from auto_coding import simple_run as runner
from auto_coding import workflow_preflight as preflight
from auto_coding.reviewer_permissions import ReviewerEnvironmentError
from auto_coding.repository_ownership import execution_lock
import test_review_recovery as recovery
from backend_fixtures import approved_plan_fixture


def snapshot(repo):
    return {str(path.relative_to(repo)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in repo.rglob("*") if path.is_file()}


class WorkflowPreflightTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.repo = self.root / "target"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-b", "feature/preflight"], cwd=self.repo,
                       capture_output=True, check=True, timeout=15)
        self.logs = self.root / "logs"
        self.logs.mkdir()

    def git(self, repo, args):
        return subprocess.run(["git", "--no-optional-locks", "-c", "safe.directory=" + str(repo), *args],
                              cwd=repo, capture_output=True, text=True, check=True, timeout=15).stdout.strip()

    def test_regular_checkout_reuses_b07_and_preserves_target_files_and_git_state(self):
        before = snapshot(self.repo)
        checker = Mock(return_value="codex")
        probe = Mock()
        preflight.workflow_preflight(self.repo, self.logs, self.git, check=checker, probe=probe)
        checker.assert_called_once()
        probe.assert_called_once()
        self.assertEqual(probe.call_args.args[0].argv, [])  # No agent request is built for preflight.
        self.assertEqual(snapshot(self.repo), before)
        self.assertEqual(list(self.logs.iterdir()), [])

    def test_controller_checkout_is_rejected_without_cli_or_scratch(self):
        checker = Mock()
        with patch.object(preflight, "TRUSTED_CHECKOUT", self.repo), \
                self.assertRaisesRegex(preflight.WorkflowPreflightError, "trusted controller"):
            preflight.workflow_preflight(self.repo, self.logs, self.git, check=checker)
        checker.assert_not_called()
        self.assertEqual(list(self.logs.iterdir()), [])

    def test_wrong_inherited_pythonpath_is_removed_using_b06(self):
        wrong = self.root / "wrong-checkout"
        wrong.mkdir()
        def probe(spec, repo):
            paths = spec.environment["PYTHONPATH"].split(preflight.os.pathsep)
            self.assertEqual(Path(paths[0]), self.repo)
            self.assertNotIn(str(wrong), paths)
            self.assertEqual(spec.environment["TEMP"], spec.environment["TMPDIR"])
        with patch.dict(preflight.os.environ, {"PYTHONPATH": str(wrong)}):
            before = preflight.os.environ.copy()
            preflight.workflow_preflight(self.repo, self.logs, self.git, check=None, probe=probe)
            self.assertEqual(dict(preflight.os.environ), before)

    def test_linked_metadata_is_rejected_by_existing_b07_check(self):
        external = self.root / "common-git"
        external.mkdir()
        checker = Mock()
        with self.assertRaisesRegex(ReviewerEnvironmentError, "external Git metadata"):
            preflight.workflow_preflight(self.repo, self.logs, lambda repo,args:str(external),
                                         check=checker, probe=Mock())
        checker.assert_not_called()
        self.assertEqual(list(self.logs.iterdir()), [])

    def test_invalid_reviewer_configuration_is_reported_and_scratch_removed(self):
        checker = Mock(side_effect=ReviewerEnvironmentError("Legacy sandbox settings in loaded project config"))
        with self.assertRaisesRegex(ReviewerEnvironmentError, "Legacy sandbox") as caught:
            preflight.workflow_preflight(self.repo, self.logs, self.git, check=checker, probe=Mock())
        self.assertEqual(caught.exception.preflight_check, "reviewer compatibility")
        self.assertEqual(list(self.logs.iterdir()), [])

    def test_controller_temp_inside_source_is_rejected_without_writing_there(self):
        before = snapshot(self.repo)
        with patch.object(tempfile, "tempdir", str(self.repo)), \
                self.assertRaisesRegex(preflight.WorkflowPreflightError, "inside a source checkout"):
            preflight.workflow_preflight(self.repo, self.logs, self.git, check=None, probe=None)
        self.assertEqual(snapshot(self.repo), before)
        with patch.dict(preflight.os.environ, {"TEMP": str(self.repo)}), \
                self.assertRaisesRegex(preflight.WorkflowPreflightError, "TEMP points inside"):
            preflight.workflow_preflight(self.repo, self.logs, self.git, check=None, probe=None)
        self.assertEqual(snapshot(self.repo), before)

    def test_native_storage_failure_cleans_only_confirmed_scratch(self):
        with self.assertRaisesRegex(preflight.WorkflowPreflightError, "sandbox denied"):
            preflight.workflow_preflight(self.repo, self.logs, self.git, check=None,
                probe=Mock(side_effect=preflight.WorkflowPreflightError("sandbox denied temp writes")))
        self.assertEqual(list(self.logs.iterdir()), [])
        error = subprocess.TimeoutExpired(["codex", "sandbox"], 15)
        error.process_started, error.cleanup_confirmed = True, False
        with self.assertRaises(subprocess.TimeoutExpired):
            preflight.workflow_preflight(self.repo, self.logs, self.git, check=None, probe=Mock(side_effect=error))
        self.assertEqual(len(list(self.logs.glob("review-scratch-*"))), 1)

    def test_native_probe_is_isolated_and_cannot_import_project_sitecustomize(self):
        with patch.object(preflight, "bounded_run", return_value=subprocess.CompletedProcess(
                [], 0, '{"storage": true, "target_environment": true}', "")) as run:
            def probe(spec, repo):
                preflight.probe_review_storage(spec, repo)
            preflight.workflow_preflight(self.repo, self.logs, self.git, check=None, probe=probe)
        command = run.call_args.args[0]
        self.assertIn("-I", command)
        self.assertIn("-S", command)
        self.assertIn("--permission-profile", command)
        self.assertEqual(run.call_args.kwargs["cwd"], str(self.repo))


class WorkflowLaunchTests(unittest.TestCase):
    def setUp(self):
        self.fixture = recovery.ReviewRecoveryTests("test_successful_a_tests_b_commit")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def run_fake(self, execute, **kwargs):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return self.fixture.run_fake(execute, **kwargs)

    def test_valid_workflow_finishes_preflight_before_first_a_only_once(self):
        execute = recovery.FakeExecutor([recovery.REJECTED, recovery.APPROVED])
        self.assertEqual(self.run_fake(execute), 0)
        events = self.fixture.events()
        checks = [i for i,event in enumerate(events) if event["event"] == "workflow_preflight_finished"]
        first_a = next(i for i,event in enumerate(events) if event["actor"] == "Codex A")
        self.assertEqual(len(checks), 1)
        self.assertLess(checks[0], first_a)
        self.assertEqual((execute.a_calls, execute.b_calls), (2, 2))

    def test_bad_configuration_stops_before_a_and_resume_cannot_bypass_preflight(self):
        execute = recovery.FakeExecutor()
        with patch.object(runner, "workflow_preflight", side_effect=ReviewerEnvironmentError("Legacy sandbox config")) as check:
            for resume in (False, True):
                with self.assertRaisesRegex(runner.PilotError, "Workflow preflight.*Legacy sandbox"):
                    self.run_fake(execute, resume=resume)
            self.assertEqual(check.call_count, 2)
        self.assertEqual((execute.a_calls, execute.test_calls, execute.b_calls), (0, 0, 0))
        self.assertEqual(self.fixture.state()["history"], [])
        failure = next(event for event in self.fixture.events() if event["event"] == "workflow_preflight_failed")
        self.assertIn("correction", failure["metadata"])

    def test_real_executor_path_reuses_b07_configuration_check_before_a(self):
        execute = recovery.FakeExecutor()
        execute.__wrapped__ = runner.call
        with approved_plan_fixture(), patch.object(runner, "check_cli", side_effect=ReviewerEnvironmentError("Legacy loaded configuration")) as checker, \
                patch.object(runner, "probe_review_storage") as probe, \
                contextlib.redirect_stdout(io.StringIO()), \
                self.assertRaisesRegex(runner.PilotError, "reviewer compatibility.*Legacy"):
            runner.run(self.fixture.plan, self.fixture.repo, self.fixture.logs, execute=execute,
                       final_verification_config={"test_commands": [["unused"]], "minimum_tests": 1, "timeout_seconds": 30})
        checker.assert_called_once()
        probe.assert_not_called()
        self.assertEqual((execute.a_calls, execute.test_calls, execute.b_calls), (0, 0, 0))

    def test_external_metadata_fails_before_a_and_preserves_git(self):
        before = snapshot(self.fixture.repo)
        external = self.fixture.root / "external-git"
        external.mkdir()
        execute = recovery.FakeExecutor()
        original = runner.git
        def git(repo, args, *extra):
            return str(external) if args == ["rev-parse", "--git-common-dir"] else original(repo, args, *extra)
        with patch.object(runner, "git", side_effect=git), self.assertRaisesRegex(runner.PilotError, "external Git metadata"):
            self.run_fake(execute)
        self.assertEqual(execute.a_calls, 0)
        self.assertEqual(snapshot(self.fixture.repo), before)

    def test_plan_and_final_settings_are_validated_before_workflow_probe(self):
        execute = recovery.FakeExecutor()
        with patch.object(runner, "workflow_preflight") as check:
            self.fixture.plan.write_text('{"tasks": []}')
            with self.assertRaises(runner.PilotError): self.run_fake(execute)
            self.fixture.plan.write_text(json.dumps({"tasks": [{"id": "one", "prompt": "Implement"}],
                "final_verification": {"test_commands": [["test"]], "minimum_tests": 0}}))
            with self.assertRaises(runner.PilotError): self.run_fake(execute)
        check.assert_not_called()
        self.assertEqual(execute.a_calls, 0)

    def test_review_only_recovery_does_not_repeat_workflow_preflight(self):
        self.assertEqual(self.run_fake(recovery.FakeExecutor([1, 1, 1])), 1)
        with patch.object(runner, "workflow_preflight", side_effect=AssertionError("No new A")):
            self.assertEqual(self.run_fake(recovery.FakeExecutor(review_only=True),
                                           resume=True, retry_review_once=True), 0)

    def test_uncertain_probe_stops_all_execution_and_retains_workspace_protection(self):
        error = subprocess.TimeoutExpired(["codex", "sandbox"], 15)
        error.process_started, error.cleanup_confirmed = True, False
        execute = recovery.FakeExecutor()
        with patch.object(runner, "workflow_preflight", side_effect=error), self.assertRaises(runner.PilotError):
            self.run_fake(execute)
        self.assertEqual(execute.a_calls, 0)
        self.assertTrue(execution_lock(self.fixture.repo, ".job.lock").exists())
        self.assertEqual(self.fixture.state()["uncertain_process"]["actor"], "workflow preflight")


if __name__ == "__main__":
    unittest.main()
