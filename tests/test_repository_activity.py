import builtins
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from auto_coding import repository_activity
from auto_coding.desktop_control import DesktopController
from auto_coding.github_workspace import GitHubWorkspaceManager, WorkspaceError, managed_lock


class RepositoryActivityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.runs = self.root / "runs"
        self.controller = DesktopController(self.runs)

    def record(self, value, logs=None):
        logs = logs or self.runs / "run-one"
        logs.mkdir(parents=True, exist_ok=True)
        path = logs / "desktop-run.json"
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def assert_activity(self, expected):
        context = {"selected_repo": self.controller.repo, "selected_logs": self.controller.logs,
                   "selected_activity": self.controller._selected_checkout_busy}
        self.assertIs(repository_activity.repository_busy(self.repo, self.runs, **context), expected)
        self.assertIs(self.controller.checkout_busy(self.repo), expected)

    def test_saved_metadata_contract_matches_controller_for_legacy_and_current_runs(self):
        cases = [
            ({"repo": str(self.repo), "status": "stopped", "pid": 999999999}, False),
            ({"repo": str(self.repo), "status": "completed"}, False),
            ({"repo": str(self.repo), "status": "running"}, True),
            ({"repo": str(self.repo), "status": "running", "pid": 999999999}, True),
            ({"repo": str(self.repo), "status": "launching", "pid": None}, True),
            ({"status": "running"}, True),
            ({"status": "stopped"}, False),
            ({"repo": str(self.root / "other"), "status": "running"}, False),
        ]
        with patch.object(DesktopController, "_pid_active", return_value=False) as probe:
            for metadata, expected in cases:
                with self.subTest(metadata=metadata):
                    self.record(metadata)
                    self.assert_activity(expected)
            probe.assert_not_called()

    def test_ambiguous_or_unreadable_records_fail_closed(self):
        for metadata in ([], None, {}, {"status": "unknown"}, {"repo": 123, "status": "stopped"}):
            with self.subTest(metadata=metadata):
                self.record(metadata)
                self.assert_activity(True)
        path = self.record({"status": "stopped"})
        path.write_text("{broken", encoding="utf-8")
        self.assert_activity(True)
        with patch.object(Path, "read_text", side_effect=PermissionError("cannot inspect")):
            self.assert_activity(True)

    def test_missing_storage_and_legacy_logs_without_launch_metadata_are_idle(self):
        self.assert_activity(False)
        logs = self.root / "external-legacy"
        logs.mkdir()
        self.controller.repo = self.repo
        self.controller.logs = logs
        self.assert_activity(False)
        (logs / "state.json").write_text(json.dumps({
            "repo": str(self.repo), "status": "stopped"}), encoding="utf-8")
        self.assert_activity(False)

    def test_external_selected_logs_are_checked_even_outside_run_storage(self):
        logs = self.root / "external-selected"
        self.record({"repo": str(self.repo), "status": "running"}, logs)
        self.controller.logs = logs
        self.controller.repo = self.root / "different-selected-repo"
        self.assert_activity(True)
        self.record({"repo": str(self.root / "unrelated"), "status": "running"}, logs)
        self.assert_activity(False)

    def test_selected_live_state_and_nonterminal_runner_state_are_preserved(self):
        self.controller.repo = self.repo
        logs = self.root / "external"
        logs.mkdir()
        self.controller.logs = logs
        state = logs / "state.json"
        for status, expected in (("running", True), ("checkpointing", True),
                                 ("stopped", False), ("completed", False)):
            with self.subTest(status=status):
                state.write_text(json.dumps({"repo": str(self.repo), "status": status}), encoding="utf-8")
                self.assert_activity(expected)
        self.controller.process = Mock()
        self.controller.process.poll.return_value = None
        self.assert_activity(True)  # Recorded completion never overrides a live handle.
        self.controller.process = None
        self.controller._launch_uncertain = True
        self.assert_activity(True)

    def test_unknown_selected_metadata_blocks_even_when_pid_probe_reports_absent(self):
        self.controller.repo = self.repo
        self.controller.logs = self.root / "external"
        self.record({"repo": str(self.repo), "status": "running", "pid": 12345}, self.controller.logs)
        with patch.object(self.controller, "_pid_active", return_value=False):
            self.assert_activity(True)
        self.record({"repo": str(self.repo), "status": "launching"}, self.controller.logs)
        self.assert_activity(True)

    def test_enumeration_errors_and_disappearing_observed_records_are_busy(self):
        path = self.record({"repo": str(self.repo), "status": "stopped"})
        with patch.object(repository_activity.os, "scandir", side_effect=PermissionError("unreadable")):
            self.assert_activity(True)
        self.controller.logs = path.parent
        with patch.object(repository_activity, "_run_records", return_value=[path]), \
                patch.object(Path, "read_text", side_effect=FileNotFoundError("vanished")):
            self.assert_activity(True)
        self.controller.logs = self.root / "deleted-selected-logs"
        self.assert_activity(True)

    def test_live_context_failures_or_missing_context_do_not_establish_inactivity(self):
        for callback in (None, Mock(return_value=None), Mock(side_effect=OSError("unknown"))):
            with self.subTest(callback=callback):
                self.assertTrue(repository_activity.repository_busy(
                    self.repo, self.runs, selected_repo=self.repo, selected_activity=callback))
        self.assertFalse(repository_activity.repository_busy(
            self.repo, self.runs, selected_repo=self.root / "other",
            selected_activity=Mock(side_effect=AssertionError("unrelated context must not be called"))))

    def test_controller_checkout_busy_is_a_compatible_delegate(self):
        with patch("auto_coding.desktop_control.repository_busy", return_value=True) as check:
            self.assertTrue(self.controller.checkout_busy(self.repo))
        check.assert_called_once_with(self.repo, self.runs, selected_repo=None, selected_logs=None,
                                      selected_activity=self.controller._selected_checkout_busy)


class HeadlessWorkspaceActivityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.manager = GitHubWorkspaceManager(self.root / "config")
        self.repo = self.manager.workspaces / ("a" * 64)
        self.repo.mkdir(parents=True)
        self.home = self.root / "home"
        self.home_patch = patch.object(Path, "home", return_value=self.home)
        self.home_patch.start()
        self.addCleanup(self.home_patch.stop)

    def assert_busy(self):
        with self.assertRaises(WorkspaceError) as failure:
            self.manager._idle(self.repo)
        self.assertEqual(failure.exception.code, "active_job")

    def test_workspace_idle_has_no_desktop_import_and_still_checks_publication_tree(self):
        original = builtins.__import__
        def headless_import(name, *args, **kwargs):
            if name.startswith("auto_coding.desktop"):
                raise AssertionError("Workspace activity must not import desktop modules")
            return original(name, *args, **kwargs)
        with patch("builtins.__import__", side_effect=headless_import), \
                patch.object(self.manager, "_publication_tree") as publication:
            self.manager._idle(self.repo)
            publication.assert_called_once_with(self.repo)

    def test_workspace_checks_current_and_legacy_storage_without_a_controller(self):
        for root in (self.manager.runs_dir, self.home / "AutoCoding-Runs"):
            with self.subTest(root=root):
                logs = root / "run-legacy"
                logs.mkdir(parents=True)
                record = logs / "desktop-run.json"
                record.write_text(json.dumps({"repo": str(self.repo), "status": "running"}), encoding="utf-8")
                self.assert_busy()
                record.unlink()

    def test_external_activity_callback_and_uncertain_job_lock_are_preserved(self):
        for result in (True, None):
            self.manager.active_check = Mock(return_value=result)
            self.assert_busy()
            self.manager.active_check.assert_called_once_with(self.repo)
        self.manager.active_check = Mock(return_value=False)
        lock = managed_lock(self.repo, ".job.lock")
        lock.mkdir()
        marker = lock / "uncertain"
        marker.touch()
        with patch("auto_coding.github_workspace.repository_busy", return_value=False):
            self.assert_busy()
        self.assertTrue(marker.is_file())
        self.assertTrue(lock.is_dir())
        self.manager.active_check.assert_not_called()


if __name__ == "__main__":
    unittest.main()
