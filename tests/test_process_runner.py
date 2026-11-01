import ctypes
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from auto_coding import process_runner
from auto_coding.github_workspace import (GitHubWorkspaceManager, WorkspaceError,
                                          checkout_lock, managed_job, managed_lock)
from auto_coding.simple_run import call, git_patch, PilotError


class ProcessRunnerTests(unittest.TestCase):
    def assert_windows_pid_stopped(self, child_pid):
        kernel = ctypes.windll.kernel32
        kernel.OpenProcess.argtypes = [ctypes.c_uint, ctypes.c_int, ctypes.c_uint]
        kernel.OpenProcess.restype = ctypes.c_void_p
        kernel.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint)]
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        deadline = time.monotonic() + 5
        while True:
            handle = kernel.OpenProcess(0x1000, 0, child_pid)
            if not handle:
                return
            exit_code = ctypes.c_uint()
            self.assertTrue(kernel.GetExitCodeProcess(handle, ctypes.byref(exit_code)))
            kernel.CloseHandle(handle)
            if exit_code.value != 259:
                return
            if time.monotonic() >= deadline:
                self.fail("Descendant is still running")
            time.sleep(0.05)

    def test_excessive_output_fails_without_returning_partial_output(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(process_runner, "MAX_CAPTURE_BYTES", 4096):
            code, output, timed = call([sys.executable, "-c", "print('x' * 100000)"],
                                       Path(directory), 10)
        self.assertEqual(code, 125)
        self.assertFalse(timed)
        self.assertEqual(output, "PROCESS OUTPUT EXCEEDED SAFE LIMIT")

    def test_large_git_patch_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
            (repo / "large.txt").write_text("x" * 20000)
            subprocess.run(["git", "add", "large.txt"], cwd=repo, check=True, capture_output=True)
            with patch.object(process_runner, "MAX_CAPTURE_BYTES", 4096):
                with self.assertRaises(PilotError):
                    git_patch(repo, ["diff", "--cached", "--binary", "--no-ext-diff"])

    def test_launch_failure_has_explicit_result(self):
        with tempfile.TemporaryDirectory() as directory:
            code, output, timed = call(["missing-autocoding-executable-xyz"], Path(directory), 1)
        self.assertEqual(code, 127)
        self.assertIn("Process could not start", output)
        self.assertFalse(timed)

    def test_injected_workspace_output_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = GitHubWorkspaceManager(
                Path(directory), run_command=lambda argv, **kwargs:
                subprocess.CompletedProcess(argv, 0, "x" * (process_runner.MAX_CAPTURE_BYTES + 1), ""))
            with self.assertRaises(WorkspaceError) as caught:
                manager._command(["git", "status"])
        self.assertEqual(caught.exception.code, "output_limit")

    @unittest.skipUnless(sys.platform == "win32", "Windows process-tree check")
    def test_timeout_kills_windows_descendant_and_releases_gate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / "workspaces" / ("a" * 64)
            repo.mkdir(parents=True)
            pid_file = root / "child.pid"
            script = ("import subprocess,sys,time,pathlib; "
                      "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'], "
                      "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL, "
                      "creationflags=subprocess.CREATE_NEW_PROCESS_GROUP); "
                      "pathlib.Path(sys.argv[1]).write_text(str(child.pid)); time.sleep(60)")

            @managed_job
            def job(plan, repo, logs):
                return call([sys.executable, "-c", script, str(pid_file)], repo, 1)

            result = job(None, repo, None)
            code, _, timed = result
            self.assertEqual(code, 124)
            self.assertTrue(timed)
            self.assertTrue(result.cleanup_confirmed)
            self.assertFalse(managed_lock(repo, ".job.lock").exists())
            self.assertTrue(pid_file.exists())
            self.assert_windows_pid_stopped(int(pid_file.read_text()))

    @unittest.skipUnless(sys.platform == "win32", "Windows process-tree check")
    def test_parent_exit_does_not_hide_running_descendant(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / "workspaces" / ("e" * 64)
            repo.mkdir(parents=True)
            pid_file = root / "child.pid"
            script = ("import subprocess,sys,pathlib; "
                      "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'], "
                      "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL, "
                      "creationflags=subprocess.CREATE_NEW_PROCESS_GROUP); "
                      "pathlib.Path(sys.argv[1]).write_text(str(child.pid))")

            @managed_job
            def job(plan, repo, logs):
                return call([sys.executable, "-c", script, str(pid_file)], repo, 0.5)

            result = job(None, repo, None)
            self.assertEqual(result[0], 124)
            self.assertTrue(result.cleanup_confirmed)
            self.assertFalse(managed_lock(repo, ".job.lock").exists())
            self.assert_windows_pid_stopped(int(pid_file.read_text()))

    def test_failed_cleanup_confirmation_keeps_gate(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "workspaces" / ("b" * 64)
            repo.mkdir(parents=True)
            actual_stop = process_runner._stop_tree

            def unconfirmed(*args, **kwargs):
                actual_stop(*args, **kwargs)
                return False

            @managed_job
            def job(plan, repo, logs):
                return call([sys.executable, "-c", "import time;time.sleep(10)"], repo, 0.1)

            with patch.object(process_runner, "_stop_tree", side_effect=unconfirmed):
                result = job(None, repo, None)
            self.assertEqual(result[0], 124)
            self.assertFalse(result.cleanup_confirmed)
            self.assertTrue((managed_lock(repo, ".job.lock") / "uncertain").exists())

    def test_explicit_unverified_output_result_keeps_gate(self):
        class TaggedResult(tuple):
            cleanup_confirmed = False

        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "workspaces" / ("1" * 64)
            repo.mkdir(parents=True)

            @managed_job
            def job(plan, repo, logs, *, execute=lambda: TaggedResult((125, "too much", False))):
                return execute()

            self.assertEqual(job(None, repo, None)[0], 125)
            self.assertTrue((managed_lock(repo, ".job.lock") / "uncertain").exists())

    def test_confirmed_output_limit_releases_gate(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "workspaces" / ("c" * 64)
            repo.mkdir(parents=True)

            @managed_job
            def job(plan, repo, logs):
                return call([sys.executable, "-c", "print('x' * 100000)"], repo, 5)

            with patch.object(process_runner, "MAX_CAPTURE_BYTES", 4096):
                result = job(None, repo, None)
            self.assertEqual(result[0], 125)
            self.assertTrue(result.cleanup_confirmed)
            self.assertFalse(managed_lock(repo, ".job.lock").exists())

    def test_workspace_command_verified_timeout_releases_operation_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "workspaces" / ("d" * 64)
            repo.mkdir(parents=True)

            def slow_command(argv, **kwargs):
                kwargs["timeout"] = 0.1
                return process_runner.bounded_run(
                    [sys.executable, "-c", "import time;time.sleep(10)"], **kwargs)

            manager = GitHubWorkspaceManager(Path(directory), run_command=slow_command)
            lock = managed_lock(repo)
            with checkout_lock(lock):
                with self.assertRaises(WorkspaceError) as caught:
                    manager._command(["git", "status"], repo)
            self.assertEqual(caught.exception.code, "network")
            self.assertFalse(lock.exists())

    @unittest.skipUnless(sys.platform == "win32", "Windows job assignment check")
    def test_failed_windows_job_assignment_keeps_gate(self):
        from auto_coding.windows_job import WindowsJob

        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "workspaces" / ("f" * 64)
            repo.mkdir(parents=True)

            @managed_job
            def job(plan, repo, logs):
                return call([sys.executable, "-c", "print('should not run')"], repo, 1)

            with patch.object(WindowsJob, "assign_and_resume", side_effect=OSError("assignment failed")):
                result = job(None, repo, None)
            self.assertEqual(result[0], 127)
            self.assertTrue((managed_lock(repo, ".job.lock") / "uncertain").exists())
