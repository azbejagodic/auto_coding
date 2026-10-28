import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from auto_coding.desktop_control import DesktopController


class FakeProcess:
    def __init__(self, argv, **kwargs):
        self.argv, self.kwargs, self.code, self.pid = argv, kwargs, None, 424242
    def poll(self): return self.code


class DesktopControlTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-b", "feature/test"], cwd=self.repo, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=self.repo, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=self.repo, check=True)
        (self.repo / "tracked.txt").write_text("clean")
        subprocess.run(["git", "add", "tracked.txt"], cwd=self.repo, check=True)
        subprocess.run(["git", "commit", "-m", "initial"], cwd=self.repo, check=True, capture_output=True)
        self.plan = self.root / "plan.txt"
        self.plan.write_text("Do a harmless test task")
        self.runs = self.root / "app-runs"
        self.controller = DesktopController(self.runs)
        self.assertTrue(self.controller.select_repo(self.repo)["ok"])
        self.assertTrue(self.controller.select_plan(self.plan)["ok"])

    def tearDown(self): self.temp.cleanup()

    @patch("auto_coding.desktop_control.Popen", side_effect=FakeProcess)
    def test_explicit_auto_push_arguments_and_locked_resume_settings(self, popen):
        self.controller.repository = "owner/repo"
        self.assertTrue(self.controller.start(auto_push=True)["ok"])
        argv = popen.call_args.args[0]
        self.assertEqual(argv[argv.index('--auto-push-repository') + 1], 'owner/repo')
        self.assertEqual(argv[argv.index('--auto-push-branch') + 1], 'feature/test')
        self.assertFalse(self.controller.start(auto_push=False)["ok"])
        from auto_coding.simple_run import load_tasks
        config = {'repository':'owner/repo', 'branch':'feature/test'}
        (self.controller.logs / 'state.json').write_text(json.dumps({
            'status':'stopped', 'repo':str(self.repo), 'plan_sha256':load_tasks(self.plan)[1],
            'auto_push':config, 'completed':[], 'history':[]}))
        self.controller.process.code = 1
        self.controller.job_status()
        self.assertTrue(self.controller.resume()["ok"])
        resumed = popen.call_args.args[0]
        self.assertIn('--resume', resumed)
        self.assertEqual(resumed[resumed.index('--auto-push-branch') + 1], 'feature/test')

    @patch("auto_coding.desktop_control.Popen")
    def test_failed_child_then_durable_recovery_survives_poll_and_reopen(self, popen):
        popen.side_effect = lambda argv, **kw: FakeProcess(argv, **kw)
        self.assertTrue(self.controller.start()["ok"])
        logs = self.controller.logs
        raw = logs / "runner-output.txt"
        raw.write_text("original index.lock interruption; exit 1")
        state_path = logs / "state.json"
        state_path.write_text(json.dumps({"status":"checkpointing", "completed":[]}))
        self.controller.error = "Earlier launch metadata warning"
        self.controller.process.code = 1
        status = self.controller.job_status()
        self.assertEqual(status["outcome"], "checkpointing")
        self.assertEqual(status["exit_code"], 1)
        self.assertIn("exit code 1", status["error"])
        self.assertFalse(self.controller.resume()["ok"])
        state = {"status":"completed", "completed":["task-1"], "checkpoint":"d836d75"}
        state_path.write_text(json.dumps(state))
        status = self.controller.job_status()
        self.assertEqual(status["outcome"], "completed")
        self.assertEqual(status["exit_code"], 1)
        self.assertEqual(status["process_history"][0]["exit_code"], 1)
        self.assertEqual(status["process_history"][0]["launch_warning"], "Earlier launch metadata warning")
        reopened = DesktopController(self.runs)
        self.assertTrue(reopened.reopen(logs)["ok"])
        self.assertEqual(reopened.job_status()["outcome"], "completed")
        self.assertEqual(reopened.job_status()["exit_code"], 1)
        self.assertEqual(reopened.job_status()["process_history"], status["process_history"])
        self.assertEqual(json.loads(state_path.read_text()), state)
        self.assertEqual(raw.read_text(), "original index.lock interruption; exit 1")
        self.assertEqual(popen.call_count, 1)

    @patch("auto_coding.desktop_control.Popen")
    def test_completed_state_cannot_override_live_or_unknown_process(self, popen):
        popen.side_effect = lambda argv, **kw: FakeProcess(argv, **kw)
        self.assertTrue(self.controller.start()["ok"])
        logs = self.controller.logs
        (logs / "state.json").write_text('{"status":"completed"}')
        self.assertEqual(self.controller.job_status()["outcome"], "active")
        self.controller.process = None
        (logs / "desktop-run.json").write_text('{"status":"launching"}')
        self.assertEqual(self.controller.job_status()["outcome"], "unknown")
        self.assertFalse(self.controller.start()["ok"])
        self.assertEqual(popen.call_count, 1)

    @patch("auto_coding.desktop_control.Popen")
    def test_exact_argv_cwd_env_and_unique_external_logs(self, popen):
        popen.side_effect = lambda argv, **kw: FakeProcess(argv, **kw)
        result = self.controller.start()
        self.assertTrue(result["ok"], result)
        args, kwargs = popen.call_args
        src = str(Path(__file__).resolve().parents[1] / "src")
        self.assertEqual(args[0], [__import__("sys").executable, "-B", "-m", "auto_coding.simple_run",
                                   "--repo", str(self.repo), "--logs", str(self.controller.logs),
                                   "--attempts", "3", str(self.plan)])
        self.assertFalse(kwargs["shell"])
        self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
        self.assertEqual(kwargs["stderr"], subprocess.STDOUT)
        self.assertEqual(Path(kwargs["stdout"].name), self.controller.logs / "runner-output.txt")
        if __import__("os").name == "nt":
            self.assertEqual(kwargs["creationflags"], subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP)
        self.assertEqual(kwargs["cwd"], src)
        self.assertIn(src, kwargs["env"]["PYTHONPATH"])
        self.assertFalse(self.controller.logs.is_relative_to(self.repo))
        self.assertTrue((self.controller.logs / "desktop-run.json").is_file())
        self.controller.process.code = 0
        self.controller.poll()
        self.controller.process = None
        self.assertTrue(self.controller.start()["ok"])
        self.assertNotEqual(self.controller.logs, json.loads((self.runs / next(p.name for p in self.runs.iterdir() if p != self.controller.logs) / "desktop-run.json").read_text())["logs"])

    @patch("auto_coding.desktop_control.Popen")
    def test_dirty_and_default_branch_are_blocked(self, popen):
        (self.repo / "new.txt").write_text("dirty")
        self.assertIn("staged or unstaged", self.controller.start()["error"])
        self.assertFalse(popen.called)
        (self.repo / "new.txt").unlink()
        subprocess.run(["git", "checkout", "-b", "main"], cwd=self.repo, check=True, capture_output=True)
        self.assertIn("main/master", self.controller.start()["error"])
        self.assertFalse(popen.called)

    @patch("auto_coding.desktop_control.Popen")
    def test_detached_head_is_blocked_and_preflight_is_read_only(self, popen):
        subprocess.run(["git", "checkout", "--detach"], cwd=self.repo, check=True, capture_output=True)
        check = self.controller.preflight()
        self.assertFalse(check["branch"])
        self.assertIn("detached HEAD", " ".join(check["errors"]))
        self.assertFalse(self.controller.start()["ok"])
        self.assertFalse(self.runs.exists())
        popen.assert_not_called()

    @patch("auto_coding.desktop_control.Popen")
    def test_resume_rejects_different_exact_plan_even_if_hash_matches(self, popen):
        popen.side_effect = lambda argv, **kw: FakeProcess(argv, **kw)
        self.assertTrue(self.controller.start()["ok"])
        logs = self.controller.logs
        self.controller.process.code = 1
        self.controller.poll()
        import hashlib
        (logs / "state.json").write_text(json.dumps({"status":"stopped", "repo":str(self.repo),
            "plan_sha256":hashlib.sha256(self.plan.read_bytes()).hexdigest()}))
        copy = self.root / "same-content.txt"
        copy.write_bytes(self.plan.read_bytes())
        self.controller.select_plan(copy)
        self.assertIn("original exact", self.controller.resume()["error"])
        self.assertEqual(popen.call_count, 1)
        self.assertEqual(json.loads((logs / "desktop-run.json").read_text())["plan"], str(self.plan))

    @unittest.skipUnless(__import__("os").name == "nt", "Windows PID query")
    def test_windows_pid_probe_queries_without_os_kill(self):
        import ctypes
        from unittest.mock import Mock
        kernel = Mock()
        kernel.OpenProcess.return_value = 123
        def alive(handle, code):
            code._obj.value = 259
            return 1
        kernel.GetExitCodeProcess.side_effect = alive
        with patch("ctypes.WinDLL", return_value=kernel), patch("os.kill") as kill:
            self.assertTrue(self.controller._pid_active(424242))
            kernel.OpenProcess.assert_called_once_with(0x1000, False, 424242)
            kernel.CloseHandle.assert_called_once_with(123)
            kill.assert_not_called()
            kernel.OpenProcess.return_value = None
            with patch("ctypes.get_last_error", return_value=87):
                self.assertFalse(self.controller._pid_active(424242))
            with patch("ctypes.get_last_error", return_value=5):
                self.assertTrue(self.controller._pid_active(424242))
    @patch("auto_coding.desktop_control.Popen")
    def test_nested_missing_run_storage_is_rejected_without_creating_it(self, popen):
        nested = self.repo / "not-created" / "runs"
        controller = DesktopController(nested)
        controller.select_repo(self.repo)
        controller.select_plan(self.plan)
        result = controller.start()
        self.assertFalse(result["ok"])
        self.assertIn("outside the target repository", result["error"])
        self.assertFalse(nested.exists())
        self.assertFalse(popen.called)

    @patch("auto_coding.desktop_control.Popen")
    def test_stopped_run_pid_is_not_treated_as_active(self, popen):
        popen.side_effect = lambda argv, **kw: FakeProcess(argv, **kw)
        self.assertTrue(self.controller.start()["ok"])
        logs = self.controller.logs
        self.controller.process.code = 0
        status = self.controller.poll()
        self.assertFalse(status["active"])
        metadata = json.loads((logs / "desktop-run.json").read_text())
        self.assertEqual(metadata["status"], "stopped")
        self.assertNotIn("pid", metadata)
        # A reused OS PID cannot cause this stopped record to block another run.
        with patch.object(self.controller, "_pid_active", return_value=True):
            self.assertTrue(self.controller.start()["ok"])

    @patch("auto_coding.desktop_control.Popen")
    def test_single_active_job_and_resume_preserves_paths(self, popen):
        popen.side_effect = lambda argv, **kw: FakeProcess(argv, **kw)
        self.controller.start()
        logs = self.controller.logs
        self.assertIn("already active", self.controller.start()["error"])
        # Existing staged changes are deliberately allowed through to runner reconciliation.
        (self.repo / "tracked.txt").write_text("staged edit")
        subprocess.run(["git", "add", "tracked.txt"], cwd=self.repo, check=True)
        (logs / "state.json").write_text(json.dumps({"status": "stopped", "repo": str(self.repo), "plan_sha256": __import__("hashlib").sha256(self.plan.read_bytes()).hexdigest()}))
        self.controller.process.code = 1
        self.controller.poll()
        self.assertTrue(self.controller.resume()["ok"])
        argv = popen.call_args[0][0]
        self.assertIn("--resume", argv)
        self.assertEqual(argv[argv.index("--logs") + 1], str(logs))
        self.assertEqual(argv[-1], str(self.plan))

    @patch("auto_coding.desktop_control.Popen")
    def test_resume_mismatch_stopped_truth_and_missing_paths(self, popen):
        logs = self.root / "oldrun"
        logs.mkdir()
        self.controller.logs = logs
        (logs / "state.json").write_text(json.dumps({"status": "stopped", "repo": str(self.repo), "plan_sha256": "wrong"}))
        self.assertIn("mismatch", self.controller.resume()["error"])
        self.assertFalse(popen.called)
        self.assertFalse(self.controller.select_plan(self.root / "missing.txt")["ok"])
        self.assertFalse(self.controller.select_repo(self.root / "missing")["ok"])
        self.assertFalse(self.controller.select_logs(self.root / "missing-logs")["ok"])

    @patch("auto_coding.desktop_control.Popen", side_effect=OSError("no process"))
    def test_subprocess_failure_is_reported(self, popen):
        result = self.controller.start()
        self.assertFalse(result["ok"])
        self.assertIn("Could not start", result["error"])

    @patch("auto_coding.desktop_control.Popen")
    def test_zero_child_exit_preserves_running_state_and_selected_logs(self, popen):
        popen.side_effect = lambda argv, **kw: FakeProcess(argv, **kw)
        self.assertTrue(self.controller.start()["ok"])
        logs = self.controller.logs
        (logs / "state.json").write_text('{"status":"running","current_task":"one","completed":[]}')
        self.controller.process.code = 0
        data = self.controller.snapshot()
        self.assertFalse(data["controller"]["active"])
        self.assertEqual(data["controller"]["exit_code"], 0)
        self.assertEqual(data["state"]["status"], "running")
        self.assertEqual(data["state"]["completed"], [])
        self.assertEqual(self.controller.logs, logs)

    @patch("auto_coding.desktop_control.Popen")
    def test_reopen_metadata_and_exit_status(self, popen):
        popen.side_effect = lambda argv, **kw: FakeProcess(argv, **kw)
        self.controller.start()
        logs = self.controller.logs
        reopened = DesktopController(self.runs)
        self.assertTrue(reopened.reopen(logs)["ok"])
        self.assertEqual(reopened.repo, self.repo)
        self.assertEqual(reopened.plan, self.plan)
        self.controller.process.code = 7
        status = self.controller.poll()
        self.assertEqual(status["exit_code"], 7)
        self.assertIn("exit code 7", status["error"])

    @patch("auto_coding.desktop_control.Popen")
    def test_resume_rejects_active_child_even_when_reopened_outside_runs_dir(self, popen):
        popen.side_effect = lambda argv, **kw: FakeProcess(argv, **kw)
        self.assertTrue(self.controller.start()["ok"])
        logs = self.controller.logs
        reopened = DesktopController(self.root / "other-app-runs")
        self.assertTrue(reopened.reopen(logs)["ok"])
        # Even a misleading stopped state cannot override the persisted live PID.
        (logs / "state.json").write_text(json.dumps({
            "status": "stopped", "repo": str(self.repo),
            "plan_sha256": __import__("hashlib").sha256(self.plan.read_bytes()).hexdigest(),
        }))
        with patch.object(reopened, "_pid_active", return_value=True):
            result = reopened.resume()
        self.assertFalse(result["ok"])
        self.assertIn("child process is still active", result["error"])
        self.assertEqual(popen.call_count, 1)

    @patch("auto_coding.desktop_control.Popen")
    def test_pid_persistence_failure_keeps_live_job_and_blocks_reopened_resume(self, popen):
        popen.side_effect = lambda argv, **kw: FakeProcess(argv, **kw)
        original_metadata = self.controller._metadata

        def fail_pid_write(logs, pid=None, status="stopped"):
            if pid is not None:
                raise OSError("metadata disk failure")
            return original_metadata(logs, pid=pid, status=status)

        with patch.object(self.controller, "_metadata", side_effect=fail_pid_write):
            result = self.controller.start()
        self.assertTrue(result["ok"])
        self.assertIsNotNone(self.controller.process)
        self.assertTrue(self.controller.job_status()["active"])
        self.assertIn("PID could not be saved", self.controller.job_status()["error"])
        self.assertEqual(json.loads((self.controller.logs / "desktop-run.json").read_text())["status"], "launching")

        reopened = DesktopController(self.root / "elsewhere")
        self.assertTrue(reopened.reopen(self.controller.logs)["ok"])
        # START on a second controller must treat the unresolved launch intent
        # as blocking even though no PID was persisted.
        starter = DesktopController(self.runs)
        starter.select_repo(self.repo)
        starter.select_plan(self.plan)
        result = starter.start()
        self.assertFalse(result["ok"])
        self.assertIn("still active", result["error"])
        self.assertEqual(popen.call_count, 1)
        (self.controller.logs / "state.json").write_text(json.dumps({
            "status": "stopped", "repo": str(self.repo),
            "plan_sha256": __import__("hashlib").sha256(self.plan.read_bytes()).hexdigest(),
        }))
        result = reopened.resume()
        self.assertFalse(result["ok"])
        self.assertIn("could not persist its child PID", result["error"])
        self.assertEqual(popen.call_count, 1)

    @patch("auto_coding.desktop_control.Popen")
    def test_reopen_unknown_pid_reports_uncertainty_and_blocks_new_start(self, popen):
        logs = self.root / "external-run"
        logs.mkdir()
        (logs / "desktop-run.json").write_text(json.dumps({
            "repo": str(self.repo), "plan": str(self.plan),
            "logs": str(logs), "status": "running",
        }))
        reopened = DesktopController(self.root / "different-storage")
        result = reopened.reopen(logs)
        self.assertTrue(result["ok"])
        self.assertIn("unknown", result["warning"].lower())
        status = reopened.job_status()
        self.assertIsNone(status["active"], status)
        self.assertTrue(status["uncertain"])
        self.assertIn("unknown", status["error"].lower())
        self.assertIn("runner may still be active", status["error"].lower())
        self.assertFalse(reopened.start()["ok"])
        self.assertIn("unresolved launch", reopened.error)
        self.assertFalse(popen.called)

    @patch("auto_coding.desktop_control.Popen")
    def test_select_unknown_pid_without_reopen_blocks_start(self, popen):
        logs = self.root / "external-run"
        logs.mkdir()
        (logs / "desktop-run.json").write_text(json.dumps({
            "repo": str(self.repo), "plan": str(self.plan),
            "logs": str(logs), "status": "launching", "pid": "invalid",
        }))
        controller = DesktopController(self.root / "different-storage")
        self.assertTrue(controller.select_repo(self.repo)["ok"])
        self.assertTrue(controller.select_plan(self.plan)["ok"])
        self.assertTrue(controller.select_logs(logs)["ok"])
        self.assertFalse(controller.start()["ok"])
        self.assertFalse(popen.called)
        status = controller.job_status()
        self.assertIsNone(status["active"])
        self.assertTrue(status["uncertain"])

    def test_reopen_stopped_metadata_ignores_stale_pid(self):
        logs = self.root / "old-run"
        logs.mkdir()
        (logs / "desktop-run.json").write_text(json.dumps({
            "repo": str(self.repo), "plan": str(self.plan),
            "logs": str(logs), "status": "stopped", "pid": 424242,
        }))
        reopened = DesktopController(self.runs)
        self.assertTrue(reopened.reopen(logs)["ok"])
        with patch.object(reopened, "_pid_active", return_value=True):
            status = reopened.job_status()
        self.assertIs(status["active"], False)
        self.assertFalse(status["uncertain"])
        self.assertIsNone(status["error"])

    def test_invalid_pids_remain_uncertain(self):
        logs = self.root / "invalid-pid-run"
        logs.mkdir()
        controller = DesktopController(self.root / "elsewhere")
        for bad_pid in [None, 0, -1, "424242", True]:
            with self.subTest(pid=bad_pid):
                (logs / "desktop-run.json").write_text(json.dumps({
                    "repo": str(self.repo), "plan": str(self.plan),
                    "logs": str(logs), "status": "running", "pid": bad_pid,
                }))
                self.assertTrue(controller.reopen(logs)["ok"])
                result = controller.job_status()
                self.assertIsNone(result["active"], result)
                self.assertTrue(result["uncertain"])

    @patch("auto_coding.desktop_control.Popen")
    def test_missing_reopened_child_reports_stopped_without_changing_runner_state(self, popen):
        popen.side_effect = lambda argv, **kw: FakeProcess(argv, **kw)
        self.assertTrue(self.controller.start()["ok"])
        logs = self.controller.logs
        runner_state = {"status": "stopped", "reason": "interrupted"}
        (logs / "state.json").write_text(json.dumps(runner_state))
        reopened = DesktopController(self.runs)
        self.assertTrue(reopened.reopen(logs)["ok"])
        with patch.object(reopened, "_pid_active", return_value=False):
            status = reopened.job_status()
        self.assertFalse(status["active"])
        self.assertIn("no longer running", status["error"])
        self.assertEqual(json.loads((logs / "state.json").read_text()), runner_state)
        self.assertEqual(json.loads((logs / "desktop-run.json").read_text())["status"], "stopped")


if __name__ == "__main__": unittest.main()
