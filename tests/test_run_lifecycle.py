import ctypes
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from auto_coding import run_lifecycle
from auto_coding.desktop_control import DesktopController
from auto_coding.desktop_reader import read_evidence


class PidLifecycleTests(unittest.TestCase):
    def test_pid_validation_and_controller_delegate_reject_invalid_identifiers(self):
        for pid in (None, False, True, 0, -1, "42", 1.5, [], {}):
            with self.subTest(pid=pid), patch("os.kill") as kill:
                self.assertFalse(run_lifecycle.valid_pid(pid))
                self.assertFalse(DesktopController._valid_pid(pid))
                self.assertFalse(run_lifecycle.pid_active(pid))
                self.assertFalse(DesktopController._pid_active(pid))
                kill.assert_not_called()
        for pid in (1, 42, os.getpid()):
            self.assertTrue(run_lifecycle.valid_pid(pid))
            self.assertTrue(DesktopController._valid_pid(pid))

    def test_existing_valid_pid_mocking_hook_still_controls_controller_probe(self):
        with patch.object(DesktopController, "_valid_pid", return_value=False), \
                patch.object(run_lifecycle, "pid_active") as query:
            self.assertFalse(DesktopController._pid_active(42))
            query.assert_not_called()

    def test_posix_queries_keep_permission_and_other_failures_conservatively_active(self):
        for failure, expected in ((None, True), (ProcessLookupError("absent"), False),
                                  (PermissionError("denied"), True), (OSError("unavailable"), True)):
            with self.subTest(failure=failure), patch.object(run_lifecycle.os, "name", "posix"), \
                    patch("os.kill", side_effect=failure) as kill:
                self.assertIs(run_lifecycle.pid_active(42), expected)
                self.assertIs(DesktopController._pid_active(42), expected)
                self.assertEqual(kill.call_count, 2)
                kill.assert_called_with(42, 0)

    def test_windows_queries_never_kill_and_close_opened_handles(self):
        # Simulate the Windows API on every CI platform; the existing required
        # Windows PID regression and the real self-PID test also run on Windows.
        kernel = Mock()
        kernel.OpenProcess.return_value = 123
        for code, query_succeeds, expected in ((259, True, True), (0, True, False), (0, False, True)):
            def query(handle, pointer):
                pointer._obj.value = code
                return query_succeeds
            kernel.reset_mock()
            kernel.GetExitCodeProcess.side_effect = query
            with self.subTest(code=code, query_succeeds=query_succeeds), \
                    patch.object(run_lifecycle.os, "name", "nt"), \
                    patch("ctypes.WinDLL", return_value=kernel, create=True), patch("os.kill") as kill:
                self.assertIs(run_lifecycle.pid_active(42), expected)
                kernel.OpenProcess.assert_called_once_with(0x1000, False, 42)
                kernel.CloseHandle.assert_called_once_with(123)
                kill.assert_not_called()

    def test_windows_absent_and_inaccessible_process_queries(self):
        kernel = Mock()
        kernel.OpenProcess.return_value = None
        for error, expected in ((87, False), (5, True), (999, True)):
            kernel.reset_mock()
            with self.subTest(error=error), patch.object(run_lifecycle.os, "name", "nt"), \
                    patch("ctypes.WinDLL", return_value=kernel, create=True), \
                    patch("ctypes.get_last_error", return_value=error, create=True), patch("os.kill") as kill:
                self.assertIs(run_lifecycle.pid_active(42), expected)
                kernel.GetExitCodeProcess.assert_not_called()
                kernel.CloseHandle.assert_not_called()
                kill.assert_not_called()

    def test_windows_query_exception_closes_handle_and_preserves_uncertainty(self):
        kernel = Mock()
        kernel.OpenProcess.return_value = 123
        kernel.GetExitCodeProcess.side_effect = OSError("query failed")
        with patch.object(run_lifecycle.os, "name", "nt"), \
                patch("ctypes.WinDLL", return_value=kernel, create=True), patch("os.kill") as kill:
            self.assertTrue(run_lifecycle.pid_active(42))
            kernel.CloseHandle.assert_called_once_with(123)
            kill.assert_not_called()

    def test_real_current_process_query_is_non_destructive(self):
        self.assertTrue(run_lifecycle.pid_active(os.getpid()))
        self.assertTrue(DesktopController._pid_active(os.getpid()))


class LaunchMetadataTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.logs = self.root / "external-logs"
        self.logs.mkdir()
        self.path = self.logs / "desktop-run.json"
        self.controller = DesktopController(self.root / "runs")
        self.controller.logs = self.logs
        self.controller.repo = self.root / "repo"
        self.controller.plan = self.root / "plan.txt"

    def read(self):
        value, error = run_lifecycle.read_launch_metadata(self.logs)
        self.assertEqual(self.controller._read_metadata(), value)
        self.assertEqual(self.controller._metadata_error, error)
        return value, error

    def test_missing_and_legacy_metadata_reset_previous_errors_without_writes(self):
        self.assertEqual(run_lifecycle.read_launch_metadata(None), (None, None))
        self.controller._metadata_error = "earlier error"
        self.assertEqual(self.read(), (None, None))
        state = self.logs / "state.json"
        state.write_text('{"status":"stopped"}')
        self.assertEqual(self.read(), (None, None))
        self.assertFalse(self.controller._selected_run_unknown())
        self.assertEqual(state.read_text(), '{"status":"stopped"}')
        self.assertFalse(self.path.exists())

    def test_legacy_fields_and_history_are_preserved_exactly(self):
        value = {"repo": str(self.controller.repo), "plan": str(self.controller.plan),
                 "legacy_field": "retained", "process_history": [
                     {"exit_code": 1, "launch_warning": "disk warning", "message": "interrupted"}],
                 "status": "stopped"}
        self.path.write_text(json.dumps(value))
        self.assertEqual(self.read(), (value, None))
        self.assertFalse(self.controller._selected_run_unknown())

    def test_corrupted_nonobject_and_oversized_metadata_remain_uncertain(self):
        for text in ("{broken", "[]", "null", "true", "x" * (1024 * 1024 + 1)):
            with self.subTest(length=len(text)):
                self.path.write_text(text)
                value, error = self.read()
                self.assertIsNone(value)
                self.assertIn("desktop-run.json is corrupt or unreadable", error)
                self.assertTrue(self.controller._selected_run_unknown())
                status = self.controller.poll()
                self.assertIsNone(status["active"])
                self.assertTrue(status["uncertain"])
                self.assertEqual(self.path.read_text(), text)
                self.controller.error = None

    def test_metadata_permission_failure_is_not_mistaken_for_a_legacy_run(self):
        with patch.object(Path, "lstat", side_effect=PermissionError("cannot inspect")):
            value, error = self.read()
            self.assertIsNone(value)
            self.assertIn("cannot inspect", error)
            self.assertTrue(self.controller._selected_run_unknown())

    def test_bounded_confined_reader_preserves_existing_evidence_diagnostics(self):
        self.path.write_text("x" * (1024 * 1024 + 1))
        value, error = self.read()
        self.assertIsNone(value)
        self.assertEqual(error, "desktop-run.json is corrupt or unreadable: " +
                         read_evidence(self.logs, "desktop-run.json")["text"])
        self.path.unlink()
        self.path.mkdir()
        value, error = self.read()
        self.assertIsNone(value)
        self.assertEqual(error, "desktop-run.json is corrupt or unreadable: " +
                         read_evidence(self.logs, "desktop-run.json")["text"])

    def test_metadata_resolution_cannot_escape_logs_or_hide_a_dangling_link(self):
        self.path.write_text('{"status":"stopped"}')
        original = Path.resolve
        def outside(path, *args, **kwargs):
            return self.root / "outside.json" if path == self.path else original(path, *args, **kwargs)
        with patch.object(Path, "resolve", outside):
            value, error = self.read()
            self.assertIsNone(value)
            self.assertIn("escapes the selected log directory", error)
        # Mock the link lookup so this regression also runs on Windows hosts
        # without permission to create symlinks.
        with patch.object(Path, "lstat", return_value=Mock()), \
                patch.object(Path, "resolve", side_effect=FileNotFoundError("dangling link")):
            value, error = self.read()
            self.assertIsNone(value)
            self.assertIsNotNone(error)

    def test_atomic_replace_publishes_complete_metadata_and_preserves_history(self):
        self.path.write_text('{"status":"launching"}')
        previous = self.path.read_bytes()
        history = [{"exit_code": 7, "launch_warning": "save failed", "message": "inspect logs"}]
        self.controller.process_history = history
        expected = {"repo": str(self.controller.repo), "plan": str(self.controller.plan),
                    "logs": str(self.logs), "process_history": history, "pid": 42, "status": "running"}
        replace = Path.replace
        observed = []
        def before_replace(source, target):
            self.assertEqual(target, self.path)
            self.assertEqual(self.path.read_bytes(), previous)
            self.assertEqual(json.loads(source.read_text()), expected)
            observed.append(source)
            return replace(source, target)
        with patch.object(Path, "replace", before_replace):
            self.controller._metadata(self.logs, pid=42, status="running")
        self.assertEqual(len(observed), 1)
        self.assertEqual(self.path.read_text(), json.dumps(expected, indent=2))
        self.assertFalse(observed[0].exists())
        self.assertEqual(self.read(), (expected, None))
        self.controller._metadata(self.logs, status="stopped")
        value, error = self.read()
        self.assertIsNone(error)
        self.assertNotIn("pid", value)
        self.assertEqual(value["process_history"], history)

    def test_failed_write_or_replace_preserves_previous_metadata_and_propagates_error(self):
        original_write = Path.write_text
        for failure in ("partial write", "replace"):
            self.path.write_text('{"status":"launching"}')
            previous = self.path.read_bytes()
            def partial_write(path, text, *args, **kwargs):
                original_write(path, text[:8], *args, **kwargs)
                raise OSError("partial write")
            mutation = patch.object(Path, "write_text", partial_write) if failure == "partial write" else \
                patch.object(Path, "replace", side_effect=OSError("replace"))
            with self.subTest(failure=failure), mutation:
                with self.assertRaisesRegex(OSError, failure):
                    self.controller._metadata(self.logs, pid=42, status="running")
            self.assertEqual(self.path.read_bytes(), previous)
            self.assertEqual(self.read(), ({"status": "launching"}, None))

    def test_active_stopped_and_unknown_states_keep_existing_controller_hooks(self):
        for status, pid, unknown in (("running", 42, False), ("launching", None, True),
                                      ("running", True, True), ("stopped", 42, False)):
            with self.subTest(status=status, pid=pid):
                self.controller._metadata(self.logs, pid=pid, status=status)
                self.assertIs(self.controller._selected_run_unknown(), unknown)
                with patch.object(self.controller, "_pid_active", return_value=True) as query:
                    self.assertIs(self.controller._selected_run_active(), status == "running")
                    if status == "stopped":
                        query.assert_not_called()

    def test_headless_module_import_and_metadata_round_trip_without_desktop(self):
        code = """
import builtins, os, sys
original = builtins.__import__
def headless(name, *args, **kwargs):
    if name.startswith('auto_coding.desktop') or name == 'webview':
        raise AssertionError('Unexpected presentation dependency: ' + name)
    return original(name, *args, **kwargs)
builtins.__import__ = headless
from auto_coding.run_lifecycle import pid_active, read_launch_metadata, write_launch_metadata
assert pid_active(os.getpid())
write_launch_metadata(sys.argv[1], repo='repo', plan='plan', pid=os.getpid(), status='running')
value, error = read_launch_metadata(sys.argv[1])
assert error is None and value['pid'] == os.getpid() and value['status'] == 'running'
assert not any(name.startswith('auto_coding.desktop') or name == 'webview' for name in sys.modules)
"""
        env = os.environ.copy()
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
        result = subprocess.run([sys.executable, "-B", "-c", code, str(self.logs)], env=env,
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
