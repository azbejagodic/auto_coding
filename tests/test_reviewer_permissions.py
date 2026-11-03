"""Reviewer permissions and recovery regressions; no model calls."""
import contextlib
import copy
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import tomllib
import unittest
from unittest.mock import patch

from auto_coding import simple_run as runner
from auto_coding import reviewer_permissions as permissions
from auto_coding.repository_ownership import execution_lock
import test_review_recovery as recovery


def verdict(status, *, rejected=False):
    return {"decision": "REJECTED" if rejected else "APPROVED",
            "issues": ["app.txt behavior is wrong; fix and retest"] if rejected else [],
            "summary": "Source review complete",
            "verification": {"status": status, "details": "Configured tests: " + status}}


class ReviewerPermissionsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.repo = self.root / "target worktree"
        (self.repo / ".git").mkdir(parents=True)
        self.logs = self.root / "external logs"
        self.logs.mkdir()
        self.argv = runner.codex_argv(readonly=True, last_message=self.logs / "verdict.json",
                                      schema=self.logs / "schema.json", model=None)

    def prepare(self, **kwargs):
        return permissions.prepare_review(self.argv, self.repo, self.logs,
                                          lambda repo, args: str(self.repo / ".git"), check=None, **kwargs)

    def config(self, scratch):
        return {"default": "review", "legacy": {"sandbox_mode": None, "sandbox_workspace_write": None},
                "profile": {"extends": ":read-only", "workspace_roots": None,
                    "filesystem": {self.repo.as_posix(): "read", scratch.as_posix(): "write", "glob_scan_max_depth": None},
                    "network": {"enabled": False, "proxy_url": None}},
                "layers": [{"source": {"type": "user"}, "legacy": {
                    "sandbox_mode": None, "sandbox_workspace_write": None}}], "requirements": None}

    def test_profile_command_has_only_target_read_scratch_write_and_no_legacy_flags(self):
        spec = self.prepare()
        self.addCleanup(spec.cleanup)
        self.assertNotIn("--sandbox", spec.argv)
        self.assertEqual(Path(spec.argv[spec.argv.index("--cd") + 1]), self.repo)
        self.assertEqual(spec.argv[-1], "-")
        self.assertIn("--strict-config", spec.argv)
        settings = [tomllib.loads(spec.argv[i+1]) for i,part in enumerate(spec.argv) if part == "-c"]
        profile = next(value["permissions"] for value in settings if "permissions" in value)
        self.assertEqual(len(profile), 1)
        value = next(iter(profile.values()))
        self.assertEqual(value, {"extends": ":read-only", "filesystem": {
            self.repo.as_posix(): "read", spec.scratch.as_posix(): "write"}, "network": {"enabled": False}})
        self.assertEqual(spec.environment["TEMP"], str(spec.scratch / "tmp"))
        self.assertEqual([spec.environment[key] for key in ("TEMP", "TMP", "TMPDIR")], [str(spec.scratch / "tmp")] * 3)
        self.assertFalse(spec.scratch.is_relative_to(self.repo))
        self.assertIn("environment_blocked", spec.instructions)

    def test_config_conflicts_and_extra_write_grants_fail_closed(self):
        scratch = self.logs / "scratch"
        value = self.config(scratch)
        permissions.validate_configuration(value, "review", self.repo, scratch)
        for field, item in (("sandbox_mode", "read-only"), ("sandbox_mode", "workspace-write"),
                            ("sandbox_workspace_write", {})):
            for source in ("effective", "loaded_file"):
                changed = copy.deepcopy(value)
                where = changed if source == "effective" else changed["layers"][0]
                where["legacy"][field] = item
                with self.subTest(field=field, source=source), self.assertRaisesRegex(permissions.ReviewerEnvironmentError, "Legacy"):
                    permissions.validate_configuration(changed, "review", self.repo, scratch)
        mutations = [lambda v:v.update(default=":workspace"),
                     lambda v:v["profile"]["filesystem"].update({self.repo.as_posix(): "write"}),
                     lambda v:v["profile"]["filesystem"].update({self.root.as_posix(): "write"}),
                     lambda v:v["profile"]["network"].update(enabled=True),
                     lambda v:v.update(layers=[]), lambda v:v.update(requirements={})]
        for mutate in mutations:
            changed = copy.deepcopy(value); mutate(changed)
            with self.assertRaises(permissions.ReviewerEnvironmentError):
                permissions.validate_configuration(changed, "review", self.repo, scratch)

    def test_unsupported_cli_is_rejected_before_config_query(self):
        with patch.object(permissions.shutil, "which", return_value="codex"), \
                patch.object(permissions, "bounded_run", return_value=subprocess.CompletedProcess([], 0, "codex-cli 0.162.0", "")) as execute:
            with self.assertRaisesRegex(permissions.ReviewerEnvironmentError, "0.163.0"):
                permissions.check_cli([], "review", self.repo, self.logs)
        self.assertEqual(execute.call_count, 1)

    def test_external_and_ambiguous_git_metadata_are_rejected_before_scratch_creation(self):
        external = self.root / "shared-git"; external.mkdir()
        for raw in (str(external), "", "first\nsecond"):
            for option in ("--absolute-git-dir", "--git-common-dir"):
                def git(repo, args):
                    return raw if args[-1] == option else str(self.repo / ".git")
                with self.subTest(raw=raw, option=option), self.assertRaises(permissions.ReviewerEnvironmentError):
                    permissions.prepare_review(self.argv, self.repo, self.logs, git, check=None)
        self.assertEqual(list(self.logs.iterdir()), [])

    def test_gitfile_with_external_metadata_is_rejected_using_real_git(self):
        (self.repo / ".git").rmdir()
        external = self.root / "external-git"
        for path in ("objects", "refs/heads"):
            (external / path).mkdir(parents=True)
        (external / "HEAD").write_text("ref: refs/heads/feature/test\n")
        (external / "config").write_text("[core]\nrepositoryformatversion = 0\nbare = false\n")
        (self.repo / ".git").write_text("gitdir: " + str(external) + "\n")
        def git(repo, args):
            return subprocess.run(["git", "--no-optional-locks", "-c", "safe.directory=" + str(repo), *args],
                                  cwd=repo, capture_output=True, text=True, check=True, timeout=15).stdout.strip()
        with self.assertRaisesRegex(permissions.ReviewerEnvironmentError, "external Git metadata"):
            permissions.prepare_review(self.argv, self.repo, self.logs, git, check=None)

    def test_context_preserves_target_imports_and_restores_parent_environment(self):
        package = self.repo / "src" / "review_target"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text("VALUE = 'target'\n")
        other = self.root / "wrong"; other.mkdir()
        (other / "review_target.py").write_text("VALUE = 'wrong'\n")
        with patch.dict(os.environ, {"PYTHONPATH": str(other)}):
            before = os.environ.copy()
            spec = self.prepare()
            with permissions.activated(spec.environment):
                result = runner.call([runner.sys.executable, "-B", "-c",
                    "import pathlib,tempfile,review_target; assert review_target.VALUE=='target'; "
                    "assert pathlib.Path(tempfile.gettempdir()).resolve()==pathlib.Path(__import__('os').environ['TMPDIR']).resolve(); "
                    "print('verified')"], self.repo, 15)
            self.assertEqual(dict(os.environ), before)
            self.assertEqual(permissions.command_environment(self.repo)["PYTHONPATH"], spec.environment["PYTHONPATH"])
        self.assertEqual(result[0], 0, result[1])
        spec.cleanup()
        self.assertFalse(spec.scratch.exists())

    def test_uncertain_preflight_retains_scratch(self):
        error = subprocess.TimeoutExpired(["codex", "app-server"], 40)
        error.process_started, error.cleanup_confirmed = True, False
        def fail(*args): raise error
        with self.assertRaises(subprocess.TimeoutExpired):
            permissions.prepare_review(self.argv, self.repo, self.logs,
                                       lambda repo,args:str(self.repo / ".git"), check=fail)
        self.assertEqual(len(list(self.logs.glob("review-scratch-*"))), 1)

    def test_cleanup_rejects_a_replaced_scratch_directory(self):
        spec = self.prepare()
        original = self.logs / "original-scratch"
        self.assertEqual(spec.scratch.parent, self.logs)
        self.assertEqual(original.parent, self.logs)
        spec.scratch.rename(original)
        spec.scratch.mkdir()
        marker = spec.scratch / "unowned.txt"
        marker.write_text("preserve")
        with self.assertRaises(permissions.ReviewerEnvironmentError):
            spec.cleanup()
        self.assertEqual(marker.read_text(), "preserve")

    def test_required_verification_and_invalid_reports_never_become_valid_verdicts(self):
        path = self.logs / "verdict.json"
        path.write_text(json.dumps(recovery.APPROVED))
        self.assertEqual(runner.checked_review(path), recovery.APPROVED)
        with self.assertRaises(runner.PilotError): runner.checked_review(path, require_verification=True)
        for report in ({}, {"status": "unknown", "details": "x"}, {"status": "passed", "details": ""}):
            path.write_text(json.dumps(verdict("passed") | {"verification": report}))
            with self.assertRaises(runner.PilotError): runner.checked_review(path)


class ReviewerFlowTests(unittest.TestCase):
    def setUp(self):
        self.fixture = recovery.ReviewRecoveryTests("test_successful_a_tests_b_commit")
        self.fixture.setUp(); self.addCleanup(self.fixture.doCleanups)

    def run_fake(self, execute, **kwargs):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return self.fixture.run_fake(execute, **kwargs)

    def test_blocked_tests_preserve_checkpoint_and_resume_b_only(self):
        execute = recovery.FakeExecutor([verdict("environment_blocked")])
        self.assertEqual(self.run_fake(execute), 1)
        self.assertEqual((execute.a_calls, execute.test_calls, execute.b_calls), (1, 1, 1))
        state = self.fixture.state()
        self.assertEqual(state["completed"], [])
        self.assertEqual(state["pending_review"]["review_failure_kind"], "verification_environment_blocked")
        resumed = recovery.FakeExecutor([verdict("passed")], review_only=True)
        self.assertEqual(self.run_fake(resumed, resume=True), 0)
        self.assertEqual((resumed.a_calls, resumed.test_calls, resumed.b_calls), (0, 0, 1))
        self.assertEqual(list(self.fixture.logs.glob("review-scratch-*")), [])

    def test_real_code_rejections_still_get_fresh_a_tests_and_b(self):
        execute = recovery.FakeExecutor([verdict("failed", rejected=True),
                                         verdict("environment_blocked", rejected=True), verdict("passed")])
        self.assertEqual(self.run_fake(execute), 0)
        self.assertEqual((execute.a_calls, execute.test_calls, execute.b_calls), (3, 3, 3))
        self.assertIn("behavior is wrong", execute.a_prompts[1])

    def test_failed_or_unexecuted_tests_never_authorize_a_commit(self):
        for status in ("failed", "not_run"):
            execute = recovery.FakeExecutor([verdict(status)])
            self.assertEqual(self.run_fake(execute, resume=status == "not_run"), 1)
            self.assertEqual(self.fixture.state()["completed"], [])

    def test_setup_failure_does_not_consume_review_budget(self):
        execute = recovery.FakeExecutor()
        with patch.object(runner, "prepare_review", side_effect=permissions.ReviewerEnvironmentError("incompatible configuration")):
            self.assertEqual(self.run_fake(execute), 1)
        self.assertEqual((execute.a_calls, execute.test_calls, execute.b_calls), (1, 1, 0))
        self.assertEqual(self.fixture.state()["pending_review"]["review_invocations"], 0)

    def test_uncertain_preflight_blocks_resume_without_launching_reviewer(self):
        execute = recovery.FakeExecutor()
        error = subprocess.TimeoutExpired(["codex", "app-server"], 40)
        error.process_started, error.cleanup_confirmed = True, False
        with patch.object(runner, "prepare_review", side_effect=error):
            self.assertEqual(self.run_fake(execute), 1)
        self.assertEqual(execute.b_calls, 0)
        state = self.fixture.state()
        self.assertTrue(state["pending_review"]["process_uncertain"])
        self.assertEqual(state["uncertain_process"]["actor"], "reviewer preflight")
        self.assertTrue(execution_lock(self.fixture.repo, ".job.lock").exists())
        with self.assertRaises(OSError):
            self.run_fake(recovery.FakeExecutor(review_only=True), resume=True)

    def test_unverified_reviewer_retains_scratch_and_lock(self):
        execute = recovery.FakeExecutor([runner._CallResult(0, "", False, False, True)])
        self.assertEqual(self.run_fake(execute), 1)
        self.assertTrue(execution_lock(self.fixture.repo, ".job.lock").exists())
        self.assertEqual(len(list(self.fixture.logs.glob("review-scratch-*"))), 1)
        self.assertEqual(self.fixture.state()["completed"], [])

    def test_legacy_hook_verdict_is_never_reported_as_passing_independent_tests(self):
        self.assertEqual(self.run_fake(recovery.FakeExecutor()), 0)
        event = next(item for item in self.fixture.events() if item["event"] == "review_finished")
        self.assertEqual(event["metadata"]["verification_status"], "not_reported")

    def test_scratch_io_failure_after_confirmed_exit_does_not_invent_process_uncertainty(self):
        with patch.object(permissions.ReviewPermissions, "cleanup", side_effect=PermissionError("retained")):
            self.assertEqual(self.run_fake(recovery.FakeExecutor([verdict("passed")])), 0)
        self.assertFalse(execution_lock(self.fixture.repo, ".job.lock").exists())
        self.assertTrue(any(item["event"] == "review_scratch_retained" for item in self.fixture.events()))

    def test_production_wrapper_requires_verification_and_checks_configuration(self):
        execute = recovery.FakeExecutor([recovery.APPROVED] * 3)
        execute.__wrapped__ = runner.call
        with patch.object(runner, "check_cli", return_value="codex") as checker, \
                contextlib.redirect_stdout(io.StringIO()):
            result = runner.run(self.fixture.plan, self.fixture.repo, self.fixture.logs, execute=execute,
                                controller_tests=True, final_verification_config={
                                    "test_commands": [["never-final"]], "minimum_tests": 1, "timeout_seconds": 30})
        self.assertEqual(result, 1)
        self.assertEqual(checker.call_count, 3)
        self.assertEqual((execute.a_calls, execute.test_calls, execute.b_calls), (1, 1, 3))
        self.assertEqual(self.fixture.state()["completed"], [])


if __name__ == "__main__":
    unittest.main()
