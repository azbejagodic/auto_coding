import ctypes
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

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

    def test_excessive_output_fails_with_incomplete_diagnostic_excerpt(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(process_runner, "MAX_CAPTURE_BYTES", 4096):
            code, output, timed = call([sys.executable, "-c", "print('x' * 100000)"],
                                       Path(directory), 10)
        self.assertEqual(code, 125)
        self.assertFalse(timed)
        message, diagnostic = output.split("\n", 1)
        self.assertEqual(message, "PROCESS OUTPUT EXCEEDED SAFE LIMIT")
        diagnostic = json.loads(diagnostic)
        self.assertTrue(diagnostic["incomplete"])
        self.assertTrue(diagnostic["truncated"])
        self.assertIn("x" * 100, diagnostic["stdout"])
        self.assertIsNone(diagnostic["stderr"])
        self.assertLessEqual(len(output.encode("utf-8")), process_runner.MAX_FAILURE_OUTPUT_BYTES)

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
            result = call(["missing-autocoding-executable-xyz"], Path(directory), 1)
            code, output, timed = result
        self.assertEqual(code, 127)
        self.assertIn("Process could not start", output)
        self.assertFalse(timed)
        self.assertIs(result.process_started, False)

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
                      "pathlib.Path(sys.argv[1]).write_text(str(child.pid)); "
                      "print('foreground started child', flush=True); time.sleep(60)")

            @managed_job
            def job(plan, repo, logs):
                return call([sys.executable, "-c", script, str(pid_file)], repo, 1)

            result = job(None, repo, None)
            code, output, timed = result
            self.assertEqual(code, 124)
            self.assertTrue(timed)
            self.assertTrue(result.cleanup_confirmed)
            self.assertIs(result.process_started, True)
            self.assertIn("foreground started child", json.loads(output.split("\n", 1)[1])["stdout"])
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

    def test_output_limit_respects_platform_cleanup_confirmation(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "workspaces" / ("c" * 64)
            repo.mkdir(parents=True)

            @managed_job
            def job(plan, repo, logs):
                return call([sys.executable, "-c", "print('x' * 100000)"], repo, 5)

            with patch.object(process_runner, "MAX_CAPTURE_BYTES", 4096):
                result = job(None, repo, None)
            self.assertEqual(result[0], 125)
            if sys.platform == "win32":
                self.assertTrue(result.cleanup_confirmed)
                self.assertFalse(managed_lock(repo, ".job.lock").exists())
            else:
                self.assertFalse(result.cleanup_confirmed)
                self.assertTrue((managed_lock(repo, ".job.lock") / "uncertain").exists())

    def test_workspace_timeout_respects_platform_cleanup_confirmation(self):
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
            if sys.platform == "win32":
                self.assertFalse(lock.exists())
            else:
                self.assertTrue((lock / "uncertain").exists())

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


class PartialOutputTests(unittest.TestCase):
    def fake_run(self, output, error_output=None, *, confirmed=True, running=True, **kwargs):
        def stream(value):
            return io.BytesIO(value) if isinstance(value, bytes) else value
        process = Mock(stdout=stream(output), stderr=None if error_output is None else stream(error_output))
        process.poll.return_value = None if running else 0
        process.returncode = 0
        def stop(process, readers, job):
            for reader in readers:
                reader.join(timeout=1)
                self.assertFalse(reader.is_alive())
            return confirmed
        with patch.object(process_runner, "sys", SimpleNamespace(platform="fixture")), \
                patch.object(process_runner.subprocess, "Popen", return_value=process), \
                patch.object(process_runner, "_stop_tree", side_effect=stop):
            options = {"capture_output": True} if error_output is not None else {
                "stdout": subprocess.PIPE, "stderr": subprocess.STDOUT}
            return process_runner.bounded_run(["fixture"], timeout=0.03, **options, **kwargs)

    def test_timeout_preserves_combined_and_separate_streams(self):
        for separate in (False, True):
            with self.subTest(separate=separate), self.assertRaises(subprocess.TimeoutExpired) as caught:
                self.fake_run(b"stdout prefix\n", b"stderr prefix\n" if separate else None)
            exc = caught.exception
            self.assertEqual(exc.output, "stdout prefix\n")
            self.assertEqual(exc.stderr, "stderr prefix\n" if separate else None)
            self.assertTrue(exc.output_incomplete)
            self.assertFalse(exc.output_truncated)
            self.assertTrue(exc.cleanup_confirmed)
            self.assertTrue(exc.process_started)

    def test_capture_limit_retains_prefix_without_returning_success(self):
        with patch.object(process_runner, "MAX_CAPTURE_BYTES", 4096), \
                self.assertRaises(process_runner.OutputLimitExceeded) as caught:
            self.fake_run(b"prefix before limit\n" + b"x" * 100000)
        exc = caught.exception
        self.assertTrue(exc.output.startswith("prefix before limit\n"))
        self.assertEqual(len(exc.output), 4096)
        self.assertTrue(exc.output_incomplete)
        self.assertTrue(exc.output_truncated)
        self.assertTrue(exc.cleanup_confirmed)

    def test_failure_excerpts_share_byte_limit_and_replace_invalid_text(self):
        for separate in (False, True):
            with self.subTest(separate=separate), self.assertRaises(subprocess.TimeoutExpired) as caught:
                self.fake_run(b"stdout\n" + b"\xff" * 40000,
                              b"stderr\n" + b"z" * 40000 if separate else None)
            exc = caught.exception
            self.assertTrue(exc.output.startswith("stdout\n"))
            if separate:
                self.assertTrue(exc.stderr.startswith("stderr\n"))
            self.assertLessEqual(len(exc.output.encode("utf-8")) + len((exc.stderr or "").encode("utf-8")),
                                 process_runner.MAX_FAILURE_OUTPUT_BYTES)
            self.assertTrue(exc.output_incomplete)
            self.assertTrue(exc.output_truncated)

    def test_read_error_retains_streams_and_cleanup_uncertainty(self):
        class BrokenStream(io.BytesIO):
            def read(self, size):
                if self.tell():
                    raise OSError("fixture read failure")
                return super().read(size)
        for separate in (False, True):
            with self.subTest(separate=separate), \
                    patch("auto_coding.github_workspace.mark_process_uncertain") as mark, \
                    self.assertRaisesRegex(OSError, "Subprocess output could not be read") as caught:
                self.fake_run(BrokenStream(b"before read error"), b"stderr evidence" if separate else None,
                              confirmed=False)
            exc = caught.exception
            self.assertEqual(exc.output, "before read error")
            self.assertEqual(exc.stderr, "stderr evidence" if separate else None)
            self.assertIsInstance(exc.__cause__, OSError)
            self.assertFalse(exc.cleanup_confirmed)
            self.assertTrue(exc.process_started)
            self.assertTrue(exc.output_incomplete)
            mark.assert_called_once_with()

    def test_call_preserves_failure_semantics_and_quotes_incomplete_cli_events(self):
        from auto_coding.metrics import codex_usage
        from auto_coding.simple_run import review_process_failure_category
        cli_events = '\n'.join(json.dumps(event) for event in (
            {"type": "thread.started"}, {"type": "turn.started"},
            {"type": "turn.completed", "usage": {"input_tokens": 1, "cached_input_tokens": 0, "output_tokens": 1}}))
        for kind, code, timed in ((subprocess.TimeoutExpired, 124, True),
                                  (process_runner.OutputLimitExceeded, 125, False), (OSError, 127, False)):
            for confirmed in (False, True):
                with self.subTest(kind=kind, confirmed=confirmed):
                    exc = kind(["fixture"], 1) if kind is subprocess.TimeoutExpired else kind("fixture failure")
                    exc.output, exc.stderr = cli_events + '\n' + '\x00' * 16000, None
                    exc.output_incomplete, exc.output_truncated = True, False
                    exc.cleanup_confirmed, exc.process_started = confirmed, True
                    with patch("auto_coding.simple_run.bounded_run", side_effect=exc), \
                            patch("auto_coding.simple_run.mark_process_uncertain") as mark, \
                            patch("sys.stdout", new_callable=io.StringIO) as console:
                        result = call(["fixture"], Path.cwd(), 1)
                    self.assertEqual((result[0], result[2]), (code, timed))
                    self.assertIs(result.cleanup_confirmed, confirmed)
                    self.assertTrue(result.process_started)
                    self.assertEqual(mark.call_count, int(not confirmed))
                    self.assertEqual(console.getvalue(), "")
                    self.assertLessEqual(len(result[1].encode("utf-8")), process_runner.MAX_FAILURE_OUTPUT_BYTES)
                    diagnostic = json.loads(result[1].split('\n', 1)[1])
                    self.assertTrue(diagnostic["incomplete"])
                    self.assertTrue(diagnostic["truncated"])
                    self.assertIn('turn.completed', diagnostic["stdout"])
                    self.assertIsNone(codex_usage(result[1]))
                    self.assertEqual(review_process_failure_category(result[1]), "unknown")

    def test_normal_combined_and_separate_output_is_unchanged(self):
        for separate in (False, True):
            with self.subTest(separate=separate):
                options = {"capture_output": True} if separate else {
                    "stdout": subprocess.PIPE, "stderr": subprocess.STDOUT}
                result = process_runner.bounded_run(
                    [sys.executable, "-c", "import sys; print('out', flush=True); print('err', file=sys.stderr)"],
                    timeout=5, **options)
                self.assertEqual(result.returncode, 0)
                self.assertEqual(result.stdout.splitlines(), ["out"] if separate else ["out", "err"])
                self.assertEqual(result.stderr.splitlines() if separate else result.stderr, ["err"] if separate else None)
