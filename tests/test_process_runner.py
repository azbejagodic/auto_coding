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
from auto_coding.github_workspace import GitHubWorkspaceManager, WorkspaceError, managed_job, managed_lock
from auto_coding.simple_run import call, git_patch, PilotError


class ProcessRunnerTests(unittest.TestCase):
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
    def test_timeout_kills_windows_child_and_retains_gate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / "workspaces" / ("a" * 64)
            repo.mkdir(parents=True)
            pid_file = root / "child.pid"
            script = ("import subprocess,sys,time,pathlib; "
                      "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
                      "pathlib.Path(sys.argv[1]).write_text(str(child.pid)); time.sleep(60)")

            @managed_job
            def job(plan, repo, logs):
                return call([sys.executable, "-c", script, str(pid_file)], repo, 1)

            code, _, timed = job(None, repo, None)
            self.assertEqual(code, 124)
            self.assertTrue(timed)
            self.assertTrue((managed_lock(repo, ".job.lock") / "uncertain").exists())
            self.assertTrue(pid_file.exists())
            child_pid = int(pid_file.read_text())
            kernel = ctypes.windll.kernel32
            kernel.OpenProcess.argtypes = [ctypes.c_uint, ctypes.c_int, ctypes.c_uint]
            kernel.OpenProcess.restype = ctypes.c_void_p
            kernel.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint)]
            kernel.CloseHandle.argtypes = [ctypes.c_void_p]
            deadline = time.monotonic() + 5
            while True:
                handle = kernel.OpenProcess(0x1000, 0, child_pid)
                if not handle:
                    break
                exit_code = ctypes.c_uint()
                self.assertTrue(kernel.GetExitCodeProcess(handle, ctypes.byref(exit_code)))
                kernel.CloseHandle(handle)
                if exit_code.value != 259:
                    break
                if time.monotonic() >= deadline:
                    self.fail("Timed-out descendant is still running")
                time.sleep(0.05)
