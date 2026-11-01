import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from auto_coding.desktop_control import DesktopController
from auto_coding.github_workspace import (WorkspaceError, cancel_reservation, checkout_lock,
                                         managed_job, managed_lock, reserve_job)
from auto_coding.repository_activity import repository_busy
from auto_coding.run_launcher import LaunchState, launch_runner
from auto_coding.run_lifecycle import read_launch_metadata, write_launch_metadata


class FakeProcess:
    pid = 424242

    def poll(self):
        return None


class RunLauncherTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.repo = self.root / "workspaces" / ("a" * 64)
        self.repo.mkdir(parents=True)
        subprocess.run(["git", "init", "-b", "feature/test"], cwd=self.repo,
                       check=True, capture_output=True)
        self.plan = self.root / "plan.txt"
        self.plan.write_text("Implement the prepared example task", encoding="utf-8")
        self.runs = self.root / "runs"
        self.logs = self.runs / "run-one"
        self.logs.mkdir(parents=True)
        self.gate = managed_lock(self.repo, ".job.lock")
        self.state = LaunchState()
        self.spawn = Mock(return_value=FakeProcess())

    def launch(self, **options):
        with checkout_lock(managed_lock(self.repo)):
            return launch_runner(self.state, repo=self.repo, plan=self.plan, logs=self.logs,
                                 attempts=4, popen=self.spawn, **options)

    def persist(self, logs, pid=None, status="stopped"):
        write_launch_metadata(logs, repo=self.repo, plan=self.plan, pid=pid, status=status,
                              process_history=self.state.process_history)

    def metadata(self):
        value, error = read_launch_metadata(self.logs)
        self.assertIsNone(error)
        return value

    def test_success_reserves_and_writes_intent_before_spawning_then_records_pid(self):
        original_head = (self.repo / ".git" / "HEAD").read_bytes()
        self.state.exit_code, self.state.error, self.state._launch_uncertain = 7, "earlier", True
        self.state.process_history = [{"exit_code": 7, "launch_warning": "old warning"}]
        output = self.logs / "runner-output.txt"
        output.write_bytes(b"previous diagnostics\n")
        def spawn(argv, **kwargs):
            self.assertEqual(self.metadata()["status"], "launching")
            self.assertNotIn("pid", self.metadata())
            self.assertIsNone(self.state.process)
            self.assertTrue(managed_lock(self.repo).is_dir())
            token = (self.gate / "reservation").read_text(encoding="ascii")
            self.assertEqual(kwargs["env"]["AUTOCODING_JOB_RESERVATION"], token)
            self.assertNotEqual(token, "parent-reservation")
            self.assertFalse(kwargs["stdout"].closed)
            kwargs["stdout"].write(b"new diagnostics\n")
            return FakeProcess()
        self.spawn.side_effect = spawn
        with patch.dict(os.environ, {"AUTOCODING_JOB_RESERVATION": "parent-reservation",
                                     "PYTHONPATH": "existing-path"}):
            environment = os.environ.copy()
            result = self.launch()
            self.assertEqual(dict(os.environ), environment)
        argv, options = self.spawn.call_args.args[0], self.spawn.call_args.kwargs
        src = str(Path(__file__).resolve().parents[1] / "src")
        self.assertEqual(argv, [sys.executable, "-B", "-m", "auto_coding.simple_run", "--repo", str(self.repo),
                               "--logs", str(self.logs), "--attempts", "4", str(self.plan)])
        self.assertEqual(result, {"ok": True, "argv": argv, "error": None})
        self.assertEqual(options["cwd"], src)
        self.assertEqual(options["env"]["PYTHONPATH"], src + os.pathsep + "existing-path")
        self.assertFalse(options["shell"])
        self.assertEqual(options["stdin"], subprocess.DEVNULL)
        self.assertEqual(options["stderr"], subprocess.STDOUT)
        self.assertTrue(options["stdout"].closed)
        if os.name == "nt":
            self.assertEqual(options["creationflags"], subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP)
        else:
            self.assertNotIn("creationflags", options)
        self.assertEqual(output.read_bytes(), b"previous diagnostics\nnew diagnostics\n")
        self.assertEqual(self.metadata()["pid"], self.state.process.pid)
        self.assertEqual(self.metadata()["status"], "running")
        self.assertEqual(self.metadata()["process_history"], self.state.process_history)
        self.assertIsNone(self.state.exit_code)
        self.assertIsNone(self.state.error)
        self.assertFalse(self.state._launch_uncertain)
        self.assertEqual((self.repo / ".git" / "HEAD").read_bytes(), original_head)
        self.assertTrue(repository_busy(self.repo, self.runs))
        self.assertTrue(self.gate.is_dir())
        # Exercise the real one-time handoff with a fake child body, never Codex.
        @managed_job
        def child(plan, repo, logs):
            self.assertFalse((self.gate / "reservation").exists())
            with self.assertRaises(WorkspaceError):
                child(plan, repo, logs)
        with patch.dict(os.environ, options["env"]):
            child(self.plan, self.repo, self.logs)
        self.assertFalse(self.gate.exists())

    def test_resume_and_publication_arguments_preserve_locked_configuration(self):
        config = {"repository": "owner/repo", "branch": "feature/original"}
        for resume in (False, True):
            for publication in (None, config):
                with self.subTest(resume=resume, publication=publication):
                    result = self.launch(resume=resume, publication=publication, reserve=lambda repo: None)
                    argv = result["argv"]
                    self.assertEqual("--resume" in argv, resume)
                    self.assertEqual("--auto-push-repository" in argv, publication is not None)
                    if publication:
                        self.assertEqual(argv[argv.index("--auto-push-repository") + 1], "owner/repo")
                        self.assertEqual(argv[argv.index("--auto-push-branch") + 1], "feature/original")
                    self.assertEqual(argv[-1], str(self.plan))

    def test_unmanaged_launch_cannot_inherit_another_jobs_reservation(self):
        repo = self.root / "plain-repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-b", "feature/test"], cwd=repo, check=True, capture_output=True)
        with patch.dict(os.environ, {"AUTOCODING_JOB_RESERVATION": "parent-token", "PYTHONPATH": ""}):
            result = launch_runner(self.state, repo=repo, plan=self.plan, logs=self.logs,
                                   attempts=2, popen=self.spawn)
        self.assertTrue(result["ok"])
        environment = self.spawn.call_args.kwargs["env"]
        self.assertNotIn("AUTOCODING_JOB_RESERVATION", environment)
        self.assertEqual(environment["PYTHONPATH"], str(Path(__file__).resolve().parents[1] / "src"))

    def test_spawn_failure_releases_only_its_reservation_and_records_stopped(self):
        self.spawn.side_effect = OSError("spawn failed")
        result = self.launch()
        self.assertEqual(result, {"ok": False, "error": "Could not start the local runner: OSError: spawn failed"})
        self.assertEqual(self.state.error, result["error"])
        self.assertIsNone(self.state.process)
        self.assertFalse(self.gate.exists())
        self.assertEqual(self.metadata()["status"], "stopped")

    def test_failed_spawn_never_releases_a_reservation_with_different_ownership(self):
        def fail(argv, **kwargs):
            (self.gate / "reservation").write_text("different-owner", encoding="ascii")
            raise OSError("spawn failed")
        self.spawn.side_effect = fail
        self.assertFalse(self.launch()["ok"])
        self.assertEqual((self.gate / "reservation").read_text(), "different-owner")

    def test_intent_failure_prevents_spawning_and_cancels_safe_reservation(self):
        def persist(logs, pid=None, status="stopped"):
            if status == "launching":
                raise OSError("intent write failed")
            self.persist(logs, pid=pid, status=status)
        result = self.launch(persist_metadata=persist)
        self.assertFalse(result["ok"])
        self.spawn.assert_not_called()
        self.assertFalse(self.gate.exists())
        self.assertEqual(self.metadata()["status"], "stopped")

    def test_failed_stop_metadata_retains_launch_intent_and_uncertainty(self):
        def persist(logs, pid=None, status="stopped"):
            if status == "launching":
                self.persist(logs, status=status)
            raise OSError("metadata unavailable")
        result = self.launch(persist_metadata=persist)
        self.assertFalse(result["ok"])
        self.assertTrue(self.state._launch_uncertain)
        self.spawn.assert_not_called()
        self.assertFalse(self.gate.exists())  # No child was created.
        self.assertEqual(self.metadata()["status"], "launching")
        self.assertTrue(repository_busy(self.repo, self.runs))

    def test_failed_reservation_ownership_write_preserves_uncertain_gate(self):
        original = Path.write_text
        def fail(path, text, *args, **kwargs):
            if path == self.gate / "reservation":
                raise OSError("ownership write failed")
            return original(path, text, *args, **kwargs)
        with patch.object(Path, "write_text", fail):
            result = self.launch()
        self.assertFalse(result["ok"])
        self.spawn.assert_not_called()
        self.assertTrue(self.gate.is_dir())
        self.assertFalse((self.gate / "reservation").exists())

    def test_existing_job_gate_blocks_spawn_without_touching_its_owner(self):
        with checkout_lock(managed_lock(self.repo)):
            token = reserve_job(self.repo)
        cancel = Mock(wraps=cancel_reservation)
        with self.assertRaises(WorkspaceError):
            self.launch(cancel=cancel)
        self.spawn.assert_not_called()
        cancel.assert_not_called()
        self.assertEqual((self.gate / "reservation").read_text(), token)
        self.assertIsNone(self.metadata())

    def test_pid_write_failure_keeps_live_handle_gate_and_reopened_run_blocking(self):
        def persist(logs, pid=None, status="stopped"):
            if status == "running":
                self.assertIs(self.state.process, self.spawn.return_value)
                raise OSError("PID write failed")
            self.persist(logs, pid=pid, status=status)
        cancel = Mock(wraps=cancel_reservation)
        result = self.launch(persist_metadata=persist, cancel=cancel)
        self.assertTrue(result["ok"])
        self.assertIn("PID could not be saved", result["error"])
        self.assertIs(self.state.process, self.spawn.return_value)
        self.assertTrue(self.state._launch_uncertain)
        self.assertTrue(self.gate.is_dir())
        cancel.assert_not_called()
        self.assertEqual(self.metadata()["status"], "launching")
        self.assertNotIn("pid", self.metadata())
        reopened = DesktopController(self.runs)
        self.assertTrue(reopened.reopen(self.logs)["ok"])
        self.assertTrue(reopened.job_status()["uncertain"])
        with patch("auto_coding.desktop_control.Popen") as spawn:
            self.assertFalse(reopened.start()["ok"])
            self.assertFalse(reopened.resume()["ok"])
            spawn.assert_not_called()

    def test_output_path_escape_is_rejected_before_opening_or_spawning(self):
        outside = self.root / "outside.txt"
        outside.write_bytes(b"unchanged")
        original = Path.resolve
        def resolve(path, *args, **kwargs):
            return outside if path == self.logs / "runner-output.txt" else original(path, *args, **kwargs)
        with patch.object(Path, "resolve", resolve):
            result = self.launch()
        self.assertFalse(result["ok"])
        self.assertIn("escapes the selected logs directory", result["error"])
        self.spawn.assert_not_called()
        self.assertEqual(outside.read_bytes(), b"unchanged")
        self.assertFalse(self.gate.exists())

    def test_output_close_failure_after_spawn_cannot_release_gate_or_lose_handle(self):
        original = Path.open
        class Output:
            def __enter__(stream):
                stream.file = original(self.logs / "runner-output.txt", "ab")
                return stream.file
            def __exit__(stream, *args):
                stream.file.close()
                raise OSError("output close failed")
        def open_file(path, *args, **kwargs):
            return Output() if path == self.logs / "runner-output.txt" else original(path, *args, **kwargs)
        cancel = Mock(wraps=cancel_reservation)
        with patch.object(Path, "open", open_file):
            result = self.launch(cancel=cancel)
        self.assertFalse(result["ok"])
        self.assertIs(self.state.process, self.spawn.return_value)
        self.assertTrue(self.gate.is_dir())
        cancel.assert_not_called()
        self.assertEqual(self.metadata()["status"], "launching")
        self.assertTrue(repository_busy(self.repo, self.runs))

    def test_controller_delegate_preserves_spawn_metadata_reservation_and_error_hooks(self):
        controller = DesktopController(self.runs)
        controller.repo, controller.plan, controller.logs = self.repo, self.plan, self.logs
        controller.repository = "owner/repo"
        with patch("auto_coding.desktop_control.Popen", self.spawn), \
                patch.object(controller, "_metadata", wraps=controller._metadata) as persist, \
                patch("auto_coding.desktop_control.reserve_job", wraps=reserve_job) as reserve, \
                patch.object(controller, "preflight", return_value={"branch": "feature/test"}):
            with checkout_lock(managed_lock(self.repo)):
                result = controller._launch(4, False, auto_push=True)
        self.assertTrue(result["ok"])
        self.assertIs(controller.process, self.spawn.return_value)
        reserve.assert_called_once_with(self.repo)
        self.assertEqual([call.kwargs["status"] for call in persist.call_args_list], ["launching", "running"])
        self.assertEqual(result["argv"][result["argv"].index("--auto-push-branch") + 1], "feature/test")
        # Release only this test's unclaimed fake-child reservation.
        cancel_reservation(self.repo, self.spawn.call_args.kwargs["env"]["AUTOCODING_JOB_RESERVATION"])
        controller.process = None
        self.spawn.side_effect = OSError("failed again")
        with patch("auto_coding.desktop_control.Popen", self.spawn), \
                patch("auto_coding.desktop_control.cancel_reservation", wraps=cancel_reservation) as cancel, \
                patch.object(controller, "_fail", wraps=controller._fail) as fail:
            with checkout_lock(managed_lock(self.repo)):
                result = controller._launch(4, False)
        self.assertFalse(result["ok"])
        cancel.assert_called_once()
        fail.assert_called_once_with(result["error"])
        self.assertEqual(controller.error, result["error"])

    def test_controller_resume_uses_saved_publication_without_new_preflight_config(self):
        controller = DesktopController(self.runs)
        controller.repo, controller.plan, controller.logs = self.repo, self.plan, self.logs
        config = {"repository": "owner/original", "branch": "feature/original"}
        with patch("auto_coding.desktop_control.Popen", self.spawn), \
                patch("auto_coding.desktop_control.read_run", return_value={"state": {"auto_push": config}}), \
                patch.object(controller, "preflight", side_effect=AssertionError("must retain saved config")):
            with checkout_lock(managed_lock(self.repo)):
                result = controller._launch(5, True)
        argv = result["argv"]
        self.assertIn("--resume", argv)
        self.assertEqual(argv[argv.index("--attempts") + 1], "5")
        self.assertEqual(argv[argv.index("--auto-push-repository") + 1], "owner/original")
        self.assertEqual(argv[argv.index("--auto-push-branch") + 1], "feature/original")

    def test_headless_launch_with_fake_process_has_no_dashboard_imports(self):
        code = """
import builtins, sys
from pathlib import Path
original = builtins.__import__
def headless(name, *args, **kwargs):
    if name.startswith('auto_coding.desktop') or name == 'webview':
        raise AssertionError('Unexpected presentation dependency: ' + name)
    return original(name, *args, **kwargs)
builtins.__import__ = headless
from auto_coding.run_launcher import LaunchState, launch_runner
from auto_coding.run_lifecycle import read_launch_metadata
from auto_coding.github_workspace import checkout_lock, managed_lock
repo, plan, logs = map(Path, sys.argv[1:])
class FakeProcess:
    pid = 424242
def spawn(argv, **kwargs):
    value, error = read_launch_metadata(logs)
    assert error is None and value['status'] == 'launching'
    assert (managed_lock(repo, '.job.lock') / 'reservation').read_text() == kwargs['env']['AUTOCODING_JOB_RESERVATION']
    return FakeProcess()
state = LaunchState()
with checkout_lock(managed_lock(repo)):
    result = launch_runner(state, repo=repo, plan=plan, logs=logs, attempts=3, popen=spawn)
assert result['ok'] and state.process.pid == 424242
value, error = read_launch_metadata(logs)
assert error is None and value['pid'] == 424242 and value['status'] == 'running'
assert not any(name.startswith('auto_coding.desktop') or name == 'webview' for name in sys.modules)
"""
        env = os.environ.copy()
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
        result = subprocess.run([sys.executable, "-B", "-c", code, str(self.repo), str(self.plan), str(self.logs)],
                                env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
