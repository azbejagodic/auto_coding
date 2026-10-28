import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from auto_coding.desktop_app import Bridge, NativeDialogs, main
from auto_coding.desktop_control import DesktopController


class FakeController:
    def __init__(self):
        self.repo = self.plan = self.logs = None
        self.result = {"ok": True, "error": None, "argv": ["fake-runner"]}
        self.start = Mock(return_value=self.result)
        self.resume = Mock(return_value=self.result)
        self.job_status = Mock(return_value={"active": False, "uncertain": False,
                                           "exit_code": None, "error": None})

    def selected_paths(self):
        return {key: str(getattr(self, key)) if getattr(self, key) else None
                for key in ("repo", "plan", "logs")}

    def preflight(self):
        return {"branch": "feature/test", "clean": True, "errors": []}

    def select_repo(self, path):
        self.repo = Path(path)
        return {"ok": True, "path": path, "error": None}

    def select_plan(self, path):
        self.plan = Path(path)
        return {"ok": True, "path": path, "error": None}

    def reopen(self, path):
        self.logs = Path(path)
        return {"ok": True, "path": path, "error": None}


class DesktopBridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.logs = self.root / "logs"
        self.logs.mkdir()
        self.plan = self.root / "task.md"
        self.plan.write_text("A local test task", encoding="utf-8")
        self.controller = FakeController()
        self.dialogs = Mock()
        self.dialogs.confirm.return_value = True
        self.bridge = Bridge(controller=self.controller, dialogs=self.dialogs,
                             controller_factory=FakeController)

    @patch("auto_coding.desktop_control.Popen")
    @patch.object(DesktopController, "preflight", return_value={"branch":"feature/smoke", "clean":True, "errors":[]})
    def test_full_lifecycle_confirmation_live_completion_and_existing_run(self, preflight, popen):
        controller = DesktopController(self.root / "runs")
        bridge = Bridge(controller=controller, dialogs=self.dialogs)
        self.assertFalse(bridge.job_status()["start_ready"])
        # Repository validation is tested separately; all launch/process calls are mocked.
        controller.repo = self.root / "project"
        controller.repo.mkdir()
        bridge.select_plan(str(self.plan))
        self.assertTrue(bridge.job_status()["start_ready"])
        preflight.return_value["clean"] = False
        self.assertFalse(bridge.job_status()["start_ready"])
        preflight.return_value.update(clean=True, errors=["main/master blocked"])
        self.assertFalse(bridge.job_status()["start_ready"])
        preflight.return_value["errors"] = []
        self.dialogs.confirm.return_value = False
        self.assertTrue(bridge.start()["cancelled"])
        popen.assert_not_called()
        self.dialogs.confirm.return_value = True
        process = Mock(pid=424242)
        process.poll.return_value = None
        popen.return_value = process
        self.assertTrue(bridge.start()["ok"])
        logs = controller.logs
        self.assertEqual(bridge.snapshot()["root"], str(logs))
        self.assertFalse(bridge.start()["ok"])
        popen.assert_called_once()
        webview = Mock()
        with patch.dict(sys.modules, {"webview": webview}), patch("auto_coding.desktop_app.Bridge", return_value=bridge):
            self.assertEqual(main([]), 0)  # Mock GUI event loop returns as if the user closed it.
        process.terminate.assert_not_called()
        process.kill.assert_not_called()
        self.assertTrue(bridge.job_status()["active"])
        (logs / "state.json").write_text('{"status":"running","current_task":"task-1"}')
        self.assertEqual(bridge.snapshot()["state"]["current_task"], "task-1")
        process.poll.return_value = 1
        self.assertNotEqual(bridge.job_status()["outcome"], "completed")
        (logs / "state.json").write_text('{"status":"completed","completed":["task-1"]}')
        self.assertEqual(bridge.job_status()["outcome"], "completed")
        reopened = Bridge(logs)
        status = reopened.job_status()
        self.assertEqual(status["outcome"], "completed")
        self.assertEqual(status["exit_code"], 1)
        self.assertFalse(status["resume_ready"])
        process.terminate.assert_not_called()
        process.kill.assert_not_called()

    def test_selections_never_launch_and_cancel_preserves_paths(self):
        self.dialogs.choose.side_effect = [str(self.root), str(self.plan), None, ()]
        self.assertTrue(self.bridge.select_repo()["ok"])
        self.assertTrue(self.bridge.select_plan()["ok"])
        before = self.bridge.selected_paths()
        self.assertTrue(self.bridge.select_repo()["cancelled"])
        self.assertTrue(self.bridge.select_plan()["cancelled"])
        self.assertEqual(self.bridge.selected_paths(), before)
        self.assertTrue(self.bridge.open_run(str(self.logs))["ok"])
        before = self.bridge.selected_paths()
        self.dialogs.choose.side_effect = [None]
        self.assertTrue(self.bridge.open_run()["cancelled"])
        self.assertEqual(self.bridge.selected_paths(), before)
        self.controller.start.assert_not_called()
        self.controller.resume.assert_not_called()
        self.bridge._controller.start.assert_not_called()
        self.dialogs.confirm.assert_not_called()

    def test_start_resume_forward_exact_result_once(self):
        for name in ("start", "resume"):
            with self.subTest(name=name):
                method = getattr(self.controller, name)
                result = getattr(self.bridge, name)(attempts=4)
                self.assertIs(result, self.controller.result)
                method.assert_called_once_with(attempts=4)
        self.assertEqual(self.dialogs.confirm.call_count, 2)
        self.controller.start.return_value = {"ok": False, "error": "already active"}
        self.assertEqual(self.bridge.start(), {"ok": False, "error": "already active"})

    def test_auto_push_requires_native_confirmation_and_is_explicit_in_launch(self):
        self.dialogs.confirm.return_value = False
        self.assertTrue(self.bridge.start(auto_push=True)["cancelled"])
        self.controller.start.assert_not_called()
        self.assertIn("Automatically push approved tasks: ENABLED", self.dialogs.confirm.call_args.args[0])
        self.assertIn("origin/feature/test", self.dialogs.confirm.call_args.args[0])
        self.dialogs.confirm.return_value = True
        self.assertTrue(self.bridge.start(auto_push=True)["ok"])
        self.controller.start.assert_called_once_with(attempts=3, auto_push=True)

    def test_confirmation_required_and_invalid_attempts_never_launch(self):
        self.dialogs.confirm.return_value = False
        self.assertTrue(self.bridge.start()["cancelled"])
        self.assertTrue(self.bridge.resume()["cancelled"])
        self.bridge._dialogs = None
        self.assertIn("confirmation", self.bridge.start()["error"])
        for attempts in (True, "3", 0, 11, -1, 2.5, "", None, float('nan')):
            self.assertFalse(self.bridge.resume(attempts)["ok"])
        self.controller.start.assert_not_called()
        self.controller.resume.assert_not_called()

    def test_numeric_attempt_range_and_cancel_never_launches(self):
        for name in ("start", "resume"):
            method = getattr(self.controller, name)
            for attempts in (1, 3, 5, 10):
                with self.subTest(action=name, attempts=attempts):
                    self.dialogs.confirm.return_value = False
                    self.assertTrue(getattr(self.bridge, name)(attempts)["cancelled"])
                    method.assert_not_called()
                    self.dialogs.confirm.return_value = True
                    self.assertTrue(getattr(self.bridge, name)(attempts)["ok"])
                    method.assert_called_once_with(attempts=attempts)
                    method.reset_mock()

    def test_controller_dialog_and_poll_errors_are_json_safe(self):
        self.dialogs.choose.side_effect = RuntimeError("picker unavailable")
        self.assertIn("picker unavailable", self.bridge.select_repo()["error"])
        self.controller.start.side_effect = RuntimeError("launch failed")
        self.assertIn("launch failed", self.bridge.start()["error"])
        self.controller.job_status.side_effect = OSError("status unavailable")
        data = self.bridge.snapshot()
        self.assertIsNone(data["controller"]["active"])
        self.assertFalse(data["controller"]["resume_ready"])
        json.dumps(data)

    def test_active_or_uncertain_job_cannot_be_replaced(self):
        for active, uncertain in ((True, False), (None, True)):
            self.controller.job_status.return_value.update(active=active, uncertain=uncertain)
            self.assertFalse(self.bridge.open_run(str(self.logs))["ok"])
            self.assertFalse(self.bridge.select_repo(str(self.root))["ok"])
            self.assertFalse(self.bridge.select_plan(str(self.plan))["ok"])
            self.assertIs(self.bridge._controller, self.controller)

    def test_public_bridge_surface_contains_only_scoped_methods(self):
        self.assertEqual({name for name in dir(self.bridge) if not name.startswith("_")},
                     {"select_repo", "select_plan", "open_run", "selected_paths", "recent_runs", "open_recent_run", "forget_run",
                      "job_status", "start", "resume", "snapshot", "evidence", "export_logs", "open_export_folder",
                          "connect_github", "github_repositories", "github_branches",
                          "create_github_branch", "checkout_github_branch", "fetch_github", "push_approved_commits"})

    @patch("auto_coding.desktop_control.Popen")
    def test_real_controller_invalid_selections_and_launch_failure(self, popen):
        bridge = Bridge(dialogs=self.dialogs)
        self.assertFalse(bridge.select_repo(str(self.root / "missing"))["ok"])
        self.assertFalse(bridge.select_repo(str(self.root))["ok"])
        self.assertFalse(bridge.select_plan(str(self.root / "missing.md"))["ok"])
        invalid = self.root / "invalid.json"
        invalid.write_text("{broken")
        self.assertFalse(bridge.select_plan(str(invalid))["ok"])
        self.assertFalse(bridge.start()["ok"])
        popen.assert_not_called()
        json.dumps(bridge.snapshot())

    def test_legacy_logs_snapshot_evidence_and_missing_files(self):
        (self.logs / "state.json").write_text('{"status":"stopped"}')
        (self.logs / "events.jsonl").write_text('{"event":"stopped"}\n')
        (self.logs / "evidence.txt").write_text("evidence")
        bridge = Bridge(self.logs)
        data = bridge.snapshot()
        self.assertEqual(data["state"]["status"], "stopped")
        self.assertEqual(len(data["events"]), 1)
        self.assertFalse(data["controller"]["resume_ready"])
        self.assertEqual(bridge.evidence("evidence.txt")["text"], "evidence")
        for name in ("../task.md", str(self.plan), None, {}):
            self.assertFalse(bridge.evidence(name)["ok"])
        (self.logs / "evidence.txt").unlink()
        self.assertFalse(bridge.evidence("evidence.txt")["ok"])
        self.logs.rename(self.root / "moved")
        self.assertFalse(bridge.snapshot()["exists"])
        self.assertEqual(bridge.selected_paths()["logs"], str(self.logs))
        self.assertFalse(bridge.open_run(str(self.root / "absent"))["ok"])
        self.assertEqual(bridge.selected_paths()["logs"], str(self.logs))

    def test_legacy_open_clears_previous_run_paths(self):
        self.bridge.select_repo(str(self.root))
        self.bridge.select_plan(str(self.plan))
        self.bridge.open_run(str(self.logs))
        self.assertIsNone(self.bridge.selected_paths()["repo"])
        self.assertIsNone(self.bridge.selected_paths()["plan"])
        self.assertFalse(self.bridge.job_status()["resume_ready"])

    @patch("auto_coding.desktop_control.Popen")
    @patch.object(DesktopController, "preflight", return_value={"branch": "feature/test", "clean": True, "errors": []})
    def test_reopen_desktop_metadata_recovers_original_paths_without_launch(self, preflight, popen):
        (self.logs / "desktop-run.json").write_text(json.dumps({
            "repo": str(self.root), "plan": str(self.plan), "status": "stopped"}))
        state = {"status": "stopped", "repo": str(self.root),
                 "plan_sha256": hashlib.sha256(self.plan.read_bytes()).hexdigest()}
        (self.logs / "state.json").write_text(json.dumps(state))
        bridge = Bridge(self.logs)
        self.assertEqual(bridge.selected_paths(), {
            "repo": str(self.root), "plan": str(self.plan), "logs": str(self.logs)})
        self.assertTrue(bridge.job_status()["resume_ready"])
        state["status"] = "running"
        (self.logs / "state.json").write_text(json.dumps(state))
        self.assertFalse(bridge.job_status()["resume_ready"])
        (self.logs / "desktop-run.json").write_text('{"status":"running"}')
        self.assertFalse(bridge.open_run(str(self.logs))["ok"])
        self.assertIsNone(bridge.job_status()["active"])
        self.assertFalse(bridge.job_status()["resume_ready"])
        popen.assert_not_called()

    def test_confirmation_names_branch_paths_and_local_only_policy(self):
        self.bridge.select_repo(str(self.root))
        self.bridge.select_plan(str(self.plan))
        self.bridge.start(5)
        text = self.dialogs.confirm.call_args.args[0]
        for value in (str(self.root), str(self.plan), "feature/test", "Attempts: 5",
                      "No automatic push", "authentication is required", "leaves the runner alive"):
            self.assertIn(value, text)

    def test_changed_branch_after_confirmation_requires_new_confirmation(self):
        self.controller.preflight = Mock(side_effect=[{"branch":"feature/one"}, {"branch":"feature/two"}])
        self.assertIn("branch changed", self.bridge.start()["error"])
        self.controller.start.assert_not_called()

    def test_concurrent_launch_is_rejected_while_confirmation_is_open(self):
        import threading
        entered, release = threading.Event(), threading.Event()

        def confirm(message):
            entered.set()
            if not release.wait(5):
                raise RuntimeError("test confirmation timed out")
            return False

        self.dialogs.confirm.side_effect = confirm
        worker = threading.Thread(target=self.bridge.start)
        worker.start()
        try:
            self.assertTrue(entered.wait(3))
            self.assertIn("already in progress", self.bridge.start()["error"])
            self.assertIn("already in progress", self.bridge.resume()["error"])
        finally:
            release.set()
            worker.join(3)
        self.assertFalse(worker.is_alive())
        self.controller.start.assert_not_called()
        self.assertEqual(self.dialogs.confirm.call_count, 1)

    def test_resume_readiness_requires_matching_stopped_state_and_live_paths(self):
        self.bridge.open_run(str(self.logs))
        self.bridge.select_repo(str(self.root))
        self.bridge.select_plan(str(self.plan))
        state = {"status": "stopped", "repo": str(self.root),
                 "plan_sha256": hashlib.sha256(self.plan.read_bytes()).hexdigest()}
        (self.logs / "state.json").write_text(json.dumps(state))
        self.assertTrue(self.bridge.job_status()["resume_ready"])
        self.plan.write_text("changed task")
        self.assertFalse(self.bridge.job_status()["resume_ready"])
        self.plan.unlink()
        status = self.bridge.job_status()
        self.assertFalse(status["resume_ready"])
        self.assertIn("plan", status["validation_errors"])

    def test_empty_and_invalid_startup_logs(self):
        bridge = Bridge()
        self.assertIsNone(bridge.snapshot()["state_error"])
        self.assertFalse(bridge.evidence("file")["ok"])
        bridge = Bridge(self.root / "missing")
        self.assertFalse(bridge.snapshot()["exists"])
        self.assertIn("selection failed", bridge.job_status()["selection_error"])

    @patch("auto_coding.desktop_control.Popen")
    def test_corrupt_desktop_metadata_is_inspectable_but_cannot_launch(self, popen):
        (self.logs / "desktop-run.json").write_text("{torn")
        (self.logs / "state.json").write_text('{"status":"stopped"}')
        bridge = Bridge(self.logs, dialogs=self.dialogs)
        status = bridge.job_status()
        self.assertIsNone(status["active"])
        self.assertTrue(status["uncertain"])
        self.assertIn("corrupt", status["error"])
        self.assertFalse(status["resume_ready"])
        self.assertEqual(bridge.evidence("desktop-run.json")["text"], "{torn")
        self.assertEqual(bridge.snapshot()["state"]["status"], "stopped")
        self.assertFalse(bridge.start()["ok"])
        self.assertFalse(bridge.resume()["ok"])
        popen.assert_not_called()

    def test_native_enum_and_legacy_constants(self):
        for module in (SimpleNamespace(FileDialog=SimpleNamespace(OPEN=10, FOLDER=20)),
                       SimpleNamespace(OPEN_DIALOG=10, FOLDER_DIALOG=20)):
            window = Mock()
            window.create_file_dialog.return_value = (str(self.plan),)
            dialogs = NativeDialogs(module, window)
            self.assertEqual(dialogs.choose("plan"), str(self.plan))
            self.assertEqual(window.create_file_dialog.call_args.args, (10,))
            self.assertIn("*.json", window.create_file_dialog.call_args.kwargs["file_types"][0])
            dialogs.choose("repo")
            self.assertEqual(window.create_file_dialog.call_args.args, (20,))
            window.create_file_dialog.return_value = None
            self.assertIsNone(dialogs.choose("run"))
            dialogs.confirm("START?")
            window.create_confirmation_dialog.assert_called_once()

    def test_help_is_gui_free_and_main_wires_window_and_startup_error(self):
        with patch.dict(sys.modules, {"webview": None}):
            with self.assertRaises(SystemExit) as exc:
                main(["--help"])
            self.assertEqual(exc.exception.code, 0)
            self.assertEqual(main([]), 2)
        webview = Mock()
        with patch.dict(sys.modules, {"webview": webview}):
            self.assertEqual(main(["--logs", str(self.logs)]), 0)
            api = webview.create_window.call_args.kwargs["js_api"]
            self.assertIsInstance(api._dialogs, NativeDialogs)
            self.assertEqual(api.selected_paths()["logs"], str(self.logs))
            webview.start.assert_called_once()
            options = webview.create_window.call_args.kwargs
            self.assertTrue(options["resizable"])
            self.assertFalse(options["frameless"])
            self.assertFalse(options["fullscreen"])
            self.assertEqual(options["min_size"], (640, 480))
            window = webview.create_window.return_value
            window.maximize.assert_not_called()
            webview.start.assert_called_once_with()
            self.assertEqual((options["width"], options["height"]), (1000, 660))
            self.assertFalse(options.get("maximized", False))
            self.assertFalse(options.get("minimized", False))
            webview.create_window.assert_called_once()
            webview.create_window.side_effect = RuntimeError("window failure")
            self.assertEqual(main([]), 2)

    def test_restored_geometry_uses_work_area_and_logical_coordinates(self):
        from types import SimpleNamespace
        from auto_coding.desktop_app import _initial_window_geometry
        for x, y, width, height in ((0, 40, 1920, 1040), (-900, 0, 900, 620)):
            screen = SimpleNamespace(frame=SimpleNamespace(X=x, Y=y, Width=width, Height=height), scale=1.5)
            geometry = _initial_window_geometry(SimpleNamespace(screens=[screen]))
            self.assertEqual(geometry["width"], min(1000, width))
            self.assertEqual(geometry["height"], min(660, height))
            self.assertGreaterEqual(geometry["x"], x)
            self.assertGreaterEqual(geometry["y"], y)
            self.assertLessEqual(geometry["x"] + geometry["width"], x + width)
            self.assertLessEqual(geometry["y"] + geometry["height"], y + height)
        self.assertEqual(_initial_window_geometry(SimpleNamespace(screens=[])), {"width":1000, "height":660})

    @patch("auto_coding.desktop_control.Popen")
    def test_real_controller_duplicate_start_and_resume_without_process_launch(self, popen):
        # Use the real controller gates with a fake process; never spawn a runner.
        controller = DesktopController(self.root / "runs")
        repo = self.root / "repo"
        repo.mkdir()
        controller.repo, controller.plan = repo, self.plan
        bridge = Bridge(controller=controller, dialogs=self.dialogs)
        process = Mock(pid=424242)
        process.poll.return_value = None
        popen.return_value = process
        with patch.object(controller, "_validate"):
            self.assertTrue(bridge.start()["ok"])
            self.assertIn("already active", bridge.start()["error"])
        self.assertEqual(popen.call_count, 1)
        self.assertEqual(bridge.snapshot()["root"], str(controller.logs))
        self.assertTrue(bridge.job_status()["active"])
        process.poll.return_value = 1
        bridge.job_status()
        state = {"status": "stopped", "repo": str(repo),
                 "plan_sha256": hashlib.sha256(self.plan.read_bytes()).hexdigest()}
        (controller.logs / "state.json").write_text(json.dumps(state))
        process.poll.return_value = None
        with patch("auto_coding.desktop_control.subprocess.run",
                   return_value=SimpleNamespace(returncode=0, stdout="feature/test")):
            self.assertTrue(bridge.resume()["ok"])
        self.assertEqual(popen.call_count, 2)
        self.assertIn("--resume", popen.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
