import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from auto_coding.desktop_control import DesktopController
from auto_coding.github_workspace import managed_lock
from auto_coding.launch_preflight import launch_preflight, validate_repository_root
from auto_coding.process_runner import OutputLimitExceeded


class LaunchPreflightTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.git("init", "-b", "feature/test")
        self.git("config", "user.name", "Test")
        self.git("config", "user.email", "test@example.invalid")
        (self.repo / "tracked.txt").write_text("original", encoding="utf-8")
        self.git("add", "tracked.txt")
        self.git("commit", "-m", "initial")
        self.runs = self.root / "runs"
        self.controller = DesktopController(self.runs)
        self.controller.repo = validate_repository_root(self.repo)

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.repo, capture_output=True,
                              text=True, check=True).stdout.strip()

    def report(self):
        report = launch_preflight(self.repo, self.runs,
                                  other_active_run=self.controller._other_active_run)
        self.assertEqual(self.controller.preflight(), report)
        return report

    def test_clean_feature_report_and_read_only_validation(self):
        head = self.git("rev-parse", "HEAD")
        self.assertEqual(self.report(), {"branch": "feature/test", "clean": True,
                         "errors": [], "resume_error": None, "start_error": None})
        self.assertFalse(self.runs.exists())
        self.assertEqual(self.git("rev-parse", "HEAD"), head)
        self.assertEqual(self.git("status", "--porcelain"), "")
        self.assertEqual((self.repo / "tracked.txt").read_text(), "original")

    def test_missing_selection_preserves_result_without_git_or_activity_calls(self):
        self.controller.repo = None
        with patch("auto_coding.desktop_control.bounded_run") as run, \
                patch.object(self.controller, "_other_active_run") as active:
            report = self.controller.preflight()
        self.assertEqual(report, launch_preflight(None, self.runs))
        self.assertEqual(report["errors"], ["Select a Git repository root."])
        run.assert_not_called()
        active.assert_not_called()

    def test_repository_root_selection_preserves_validation_errors(self):
        subdir = self.repo / "subdirectory"
        subdir.mkdir()
        ordinary = self.root / "not-git"
        ordinary.mkdir()
        for target in (self.root / "missing", self.repo / "tracked.txt", subdir, ordinary):
            with self.subTest(target=target):
                with self.assertRaises((OSError, ValueError)) as failure:
                    validate_repository_root(target)
                selected = self.controller.select_repo(target)
                self.assertFalse(selected["ok"])
                self.assertEqual(selected["error"], f"Repository validation failed: {failure.exception}")
                self.assertIsNone(self.controller.repo)
        self.assertTrue(self.controller.select_repo(self.repo)["ok"])

    def test_protected_branches_and_detached_head_remain_blocked(self):
        for name in ("main", "master", "MAIN"):
            with self.subTest(branch=name):
                self.git("checkout", "-b", name)
                self.assertIn("main/master", " ".join(self.report()["errors"]))
                self.git("checkout", "feature/test")
                self.git("branch", "-D", name)
        self.git("checkout", "--detach")
        report = self.report()
        self.assertEqual(report["branch"], "")
        self.assertIn("detached HEAD", " ".join(report["errors"]))

    def test_staged_unstaged_and_untracked_work_remain_visible(self):
        tracked = self.repo / "tracked.txt"
        tracked.write_text("edit")
        self.assertIs(self.report()["clean"], False)
        self.git("add", "tracked.txt")
        self.assertIs(self.report()["clean"], False)
        self.git("reset", "--hard", "HEAD")
        (self.repo / "untracked.txt").write_text("new")
        report = self.report()
        self.assertIs(report["clean"], False)
        # This is a shared report, not a blanket dirty-tree prohibition: resume
        # still lets the runner reconcile already staged implementation work.
        self.assertEqual(report["errors"], [])

    def test_storage_containment_preserves_start_only_error_without_creating_paths(self):
        for runs in (self.repo, self.repo / "missing" / "runs"):
            with self.subTest(runs=runs):
                self.controller.runs_dir = runs
                report = launch_preflight(self.repo, runs)
                self.assertEqual(self.controller.preflight(), report)
                self.assertIn("outside the project", report["start_error"])
                self.assertIsNone(report["resume_error"])
                self.assertEqual(report["errors"], [])
        self.assertFalse((self.repo / "missing").exists())
        # A sibling sharing the directory-name prefix is outside the checkout.
        self.assertIsNone(launch_preflight(self.repo, self.root / "repo-runs")["start_error"])

    def test_headless_storage_check_resolves_parent_components_before_containment(self):
        runs = self.root / "unused" / ".." / "repo" / "missing-runs"
        report = launch_preflight(self.repo, runs)
        self.assertIn("outside the project", report["start_error"])
        self.assertFalse((self.repo / "missing-runs").exists())
        self.assertFalse((self.root / "unused").exists())

    def test_managed_job_lock_is_reported_and_never_removed(self):
        repo = self.root / "workspaces" / ("a" * 64)
        repo.mkdir(parents=True)
        lock = managed_lock(repo, ".job.lock")
        lock.mkdir()
        marker = lock / "uncertain"
        marker.touch()
        self.controller.repo = repo
        def git(*args, **kwargs):
            return subprocess.CompletedProcess(args[0], 0, "feature/test\n" if args[0][1] == "branch" else "", "")
        with patch("auto_coding.desktop_control.bounded_run", side_effect=git):
            report = self.controller.preflight()
        self.assertEqual(report, launch_preflight(repo, self.runs, run_command=git))
        self.assertIn("active or uncertain", " ".join(report["errors"]))
        self.assertTrue(marker.is_file())

    def test_activity_context_and_headless_saved_records_block_unsafe_launches(self):
        for value in (True, None):
            with self.subTest(activity=value):
                report = launch_preflight(self.repo, self.runs, other_active_run=lambda: value)
                self.assertIn("may still be active", " ".join(report["errors"]))
        logs = self.runs / "run-previous"
        logs.mkdir(parents=True)
        record = logs / "desktop-run.json"
        for content in ('{"status":"launching"}', '{"status":"running","pid":999999999}', '{broken'):
            record.write_text(content)
            self.assertIn("may still be active", " ".join(launch_preflight(self.repo, self.runs)["errors"]))
        with patch.object(self.controller, "_other_active_run", return_value=True):
            self.assertIn("may still be active", " ".join(self.controller.preflight()["errors"]))

    def test_git_failures_never_report_an_unverified_branch_or_clean_tree(self):
        good_branch = subprocess.CompletedProcess([], 0, "feature/test\n", "")
        failed = subprocess.CompletedProcess([], 128, "misleading output", "failed")
        empty = subprocess.CompletedProcess([], 0, "", "")
        for results, branch, clean, message in (
                ([failed, empty], None, True, "unavailable Git branch"),
                ([good_branch, failed], "feature/test", None, "Could not check Git working tree")):
            with self.subTest(message=message):
                report = launch_preflight(self.repo, self.runs, run_command=Mock(side_effect=results))
                with patch("auto_coding.desktop_control.bounded_run", side_effect=results):
                    self.assertEqual(self.controller.preflight(), report)
                self.assertEqual(report["branch"], branch)
                self.assertIs(report["clean"], clean)
                self.assertIn(message, " ".join(report["errors"]))

    def test_git_timeout_launch_failure_and_output_limit_preserve_error_behavior(self):
        for error in (OSError("cannot launch Git"), subprocess.TimeoutExpired(["git"], 10),
                      OutputLimitExceeded("complete output unavailable")):
            for during_status in (False, True):
                with self.subTest(error=error, during_status=during_status):
                    results = [subprocess.CompletedProcess([], 0, "feature/test\n", ""), error] if during_status else [error]
                    report = launch_preflight(self.repo, self.runs, run_command=Mock(side_effect=results))
                    with patch("auto_coding.desktop_control.bounded_run", side_effect=results):
                        self.assertEqual(self.controller.preflight(), report)
                    self.assertIsNone(report["clean"])
                    self.assertEqual(report["errors"], [f"Git preflight failed: {error}"])
            with patch("auto_coding.desktop_control.bounded_run", side_effect=error):
                result = self.controller.select_repo(self.repo)
            self.assertFalse(result["ok"])
            self.assertEqual(result["error"], f"Repository validation failed: {error}")
            self.controller.repo = self.repo

    def test_controller_keeps_saved_run_identity_checks(self):
        logs = self.root / "selected-logs"
        logs.mkdir()
        self.controller.logs = logs
        (logs / "desktop-run.json").write_text(json.dumps({
            "repo": str(self.repo), "plan": "another-plan", "status": "stopped"}))
        report = self.controller.preflight()
        self.assertIn("original exact repository and plan paths", report["resume_error"])
        self.assertIsNone(launch_preflight(self.repo, self.runs)["resume_error"])

    def test_headless_import_and_validation_do_not_load_desktop_modules(self):
        code = """
import builtins, sys
original = builtins.__import__
def headless(name, *args, **kwargs):
    if name.startswith('auto_coding.desktop'):
        raise AssertionError('Unexpected desktop dependency: ' + name)
    return original(name, *args, **kwargs)
builtins.__import__ = headless
from auto_coding.launch_preflight import launch_preflight, validate_repository_root
repo = validate_repository_root(sys.argv[1])
report = launch_preflight(repo, sys.argv[2])
assert report == dict(branch='feature/test', clean=True, errors=[], resume_error=None, start_error=None), report
assert not any(name.startswith('auto_coding.desktop') for name in sys.modules)
"""
        env = os.environ.copy()
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
        result = subprocess.run([sys.executable, "-B", "-c", code, str(self.repo), str(self.runs)],
                                env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.runs.exists())


if __name__ == "__main__":
    unittest.main()
