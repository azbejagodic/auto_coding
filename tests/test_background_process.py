"""Headless launch regressions; simulated Windows never starts a real child."""
from contextlib import ExitStack, contextmanager
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from auto_coding.background_process import background_options
from auto_coding.desktop_control import DesktopController
from auto_coding.github_workspace import GitHubWorkspaceManager, WorkspaceError
from auto_coding.simple_run import call, codex_argv


NO_WINDOW = 0x08000000
NEW_GROUP = 0x00000200


@contextmanager
def platform(name):
    with ExitStack() as stack:
        stack.enter_context(patch("auto_coding.background_process.sys.platform", name))
        stack.enter_context(patch.object(subprocess, "CREATE_NO_WINDOW", NO_WINDOW, create=True))
        stack.enter_context(patch.object(subprocess, "CREATE_NEW_PROCESS_GROUP", NEW_GROUP, create=True))
        yield


class BackgroundLaunchTests(unittest.TestCase):
    def assert_options(self, options, host, *, runner=False):
        self.assertIs(options["shell"], False)
        self.assertNotIn("startupinfo", options)
        if host == "win32":
            self.assertEqual(options["creationflags"], NO_WINDOW | (NEW_GROUP if runner else 0))
        else:
            self.assertNotIn("creationflags", options)

    def test_portable_helper_never_reads_windows_constants(self):
        for host in ("linux", "darwin"):
            with patch("auto_coding.background_process.sys.platform", host), patch(
                    "auto_coding.background_process.subprocess", new=object()):
                self.assertEqual(background_options(), {})
                self.assertEqual(background_options(new_process_group=True), {})

    def test_windows_flags_do_not_detach_or_request_a_console(self):
        for runner in (False, True):
            with self.subTest(runner=runner), platform("win32"):
                options = background_options(new_process_group=runner)
                self.assertEqual(options, {"creationflags": NO_WINDOW | (NEW_GROUP if runner else 0)})
                self.assertFalse(options["creationflags"] & 0x00000008)  # DETACHED_PROCESS
                self.assertFalse(options["creationflags"] & 0x00000010)  # CREATE_NEW_CONSOLE

    def test_codex_a_b_git_and_test_calls_keep_output_exit_timeout_and_prompt(self):
        commands = [
            ["git", "status", "--porcelain"],
            ["python", "-m", "unittest", "a test with spaces"],
            *[codex_argv(readonly=review, last_message=Path("last message.txt"),
                         schema=Path("schema.json") if review else None, model=None)
              for review in (False, True)],
        ]
        for host in ("win32", "linux", "darwin"):
            for argv in commands:
                with self.subTest(host=host, argv=argv), platform(host), patch(
                        "auto_coding.simple_run.subprocess.run") as run:
                    run.return_value = subprocess.CompletedProcess(argv, 17, "stdout and stderr")
                    self.assertEqual(call(argv, Path("project with spaces"), 42, prompt="prompt"),
                                     (17, "stdout and stderr", False))
                    self.assertEqual(run.call_args.args, (argv,))
                    options = run.call_args.kwargs
                    self.assert_options(options, host)
                    self.assertEqual(options["cwd"], "project with spaces")
                    self.assertEqual(options["input"], "prompt")
                    self.assertEqual(options["timeout"], 42)
                    self.assertEqual(options["stdout"], subprocess.PIPE)
                    self.assertEqual(options["stderr"], subprocess.STDOUT)
                    self.assertNotIn("env", options)  # Inherit CLI authentication/environment.
                    run.side_effect = subprocess.TimeoutExpired(argv, 42, output=b"partial stderr")
                    with patch("auto_coding.simple_run.mark_process_uncertain") as uncertain:
                        self.assertEqual(call(argv, Path("project with spaces"), 42),
                                         (124, "partial stderr\nPROCESS TIMED OUT", True))
                        uncertain.assert_called_once_with()
                    run.side_effect = FileNotFoundError()
                    self.assertEqual(call(argv, Path("."), 42)[0], 127)

    def test_managed_git_and_gh_keep_capture_environment_and_failures(self):
        with tempfile.TemporaryDirectory() as root:
            command = Mock()
            manager = GitHubWorkspaceManager(root, run_command=command)
            for host in ("win32", "linux", "darwin"):
                for argv in (["git", "fetch", "--prune", "origin"],
                             ["git", "clone", "--", "https://github.com/owner/repo.git", "project with spaces"],
                             ["git", "switch", "feature/test"],
                             ["git", "switch", "-c", "feature/new", "origin/main"],
                             ["git", "push", "origin", "HEAD:refs/heads/feature/test"],
                             ["gh", "repo", "list"]):
                    with self.subTest(host=host, argv=argv), platform(host), patch.dict(
                            os.environ, {"AUTOCODING_LAUNCH_TEST": "retained"}):
                        command.side_effect = None
                        command.return_value = subprocess.CompletedProcess(argv, 0, "result\n", "diagnostic")
                        self.assertEqual(manager._command(argv, cwd=root), "result")
                        self.assertEqual(command.call_args.args, (argv,))
                        options = command.call_args.kwargs
                        self.assert_options(options, host)
                        self.assertEqual(options["cwd"], root)
                        self.assertTrue(options["capture_output"])
                        self.assertEqual(options["timeout"], 120)
                        self.assertEqual(options["env"]["AUTOCODING_LAUNCH_TEST"], "retained")
                        self.assertEqual(options["env"]["GIT_TERMINAL_PROMPT"], "0")
                        self.assertEqual(options["env"]["GCM_INTERACTIVE"], "never")
                        command.return_value = subprocess.CompletedProcess(argv, 1, "expected nonzero\n", "diagnostic")
                        self.assertEqual(manager._command(argv, cwd=root, allowed=(0, 1)), "expected nonzero")
                        command.return_value = subprocess.CompletedProcess(argv, 1, "", "authentication failed")
                        with self.assertRaises(WorkspaceError) as error:
                            manager._command(argv, cwd=root)
                        self.assertEqual(error.exception.code, "authentication")
                        command.side_effect = subprocess.TimeoutExpired(argv, 120)
                        with self.assertRaises(WorkspaceError) as error:
                            manager._command(argv, cwd=root)
                        self.assertEqual(error.exception.code, "network")

    def test_codex_authentication_and_usage_failures_retain_diagnostics(self):
        for host in ("win32", "linux", "darwin"):
            for diagnostic in ("Authentication failed: sign in again", "Usage limit reached"):
                with self.subTest(host=host, diagnostic=diagnostic), platform(host), patch(
                        "auto_coding.simple_run.subprocess.run") as run:
                    argv = codex_argv(readonly=False, last_message=Path("answer.txt"),
                                      schema=None, model=None)
                    run.return_value = subprocess.CompletedProcess(argv, 1, diagnostic)
                    self.assertEqual(call(argv, Path("project"), 30), (1, diagnostic, False))
                    self.assert_options(run.call_args.kwargs, host)
                    self.assertEqual(run.call_args.kwargs["stdout"], subprocess.PIPE)
                    self.assertEqual(run.call_args.kwargs["stderr"], subprocess.STDOUT)

    def test_controller_git_selection_and_preflight_capture_both_streams(self):
        with tempfile.TemporaryDirectory() as root:
            repo = Path(root).resolve()
            controller = DesktopController(repo / "runs")
            for host in ("win32", "linux", "darwin"):
                with self.subTest(host=host), platform(host), patch(
                        "auto_coding.desktop_control.subprocess.run") as run:
                    run.side_effect = [subprocess.CompletedProcess([], 0, str(repo), ""),
                                       subprocess.CompletedProcess([], 0, "feature/test", ""),
                                       subprocess.CompletedProcess([], 0, "", "")]
                    self.assertTrue(controller.select_repo(repo)["ok"])
                    self.assertEqual(controller.preflight()["errors"], [])
                    self.assertEqual(run.call_count, 3)
                    for index, invocation in enumerate(run.call_args_list):
                        options = invocation.kwargs
                        self.assert_options(options, host)
                        self.assertEqual(options["cwd"], repo)
                        self.assertTrue(options["capture_output"])
                        self.assertNotIn("env", options)
                        if index:
                            self.assertEqual(options["timeout"], 10)
                    run.side_effect = subprocess.TimeoutExpired(["git"], 10)
                    self.assertIn("Git preflight failed", controller.preflight()["errors"][0])
                    run.side_effect = None
                    run.return_value = subprocess.CompletedProcess([], 1, "", "git error")
                    self.assertFalse(controller.select_repo(repo)["ok"])

    def test_start_resume_spawn_keep_group_logs_environment_and_exit_status(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            for host in ("win32", "linux", "darwin"):
                for resume in (False, True):
                    with self.subTest(host=host, resume=resume):
                        controller = DesktopController(root / "runs")
                        controller.repo = root / "project with spaces"
                        controller.plan = root / "task plan.txt"
                        controller.logs = root / f"logs-{host}-{resume}"
                        controller.logs.mkdir()
                        (controller.logs / "state.json").write_text('{"status":"stopped"}')

                        def spawn(argv, **options):
                            options["stdout"].write(b"runner stdout and stderr\n")
                            return Mock(pid=424242, poll=Mock(return_value=23))

                        with platform(host), patch("auto_coding.desktop_control.Popen", side_effect=spawn) as popen, patch.dict(
                                os.environ, {"PYTHONPATH": "existing-import-path", "AUTOCODING_LAUNCH_TEST": "retained"}):
                            self.assertTrue(controller._launch(3, resume)["ok"])
                        argv = popen.call_args.args[0]
                        options = popen.call_args.kwargs
                        self.assert_options(options, host, runner=True)
                        self.assertEqual("--resume" in argv, resume)
                        self.assertEqual(argv[argv.index("--repo") + 1], str(controller.repo))
                        self.assertEqual(argv[-1], str(controller.plan))
                        src = str(Path(__file__).resolve().parents[1] / "src")
                        self.assertEqual(options["cwd"], src)
                        self.assertEqual(options["env"]["PYTHONPATH"], src + os.pathsep + "existing-import-path")
                        self.assertEqual(options["env"]["AUTOCODING_LAUNCH_TEST"], "retained")
                        self.assertEqual(options["stderr"], subprocess.STDOUT)
                        self.assertEqual(options["stdin"], subprocess.DEVNULL)
                        self.assertEqual(Path(options["stdout"].name), controller.logs / "runner-output.txt")
                        self.assertEqual((controller.logs / "runner-output.txt").read_bytes(), b"runner stdout and stderr\n")
                        self.assertEqual(controller.poll()["exit_code"], 23)
                        self.assertIn("exit code 23", controller.error)

    def test_runner_spawn_failure_stays_visible_with_background_options(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            for host in ("win32", "linux", "darwin"):
                with self.subTest(host=host):
                    controller = DesktopController(root / "runs")
                    controller.repo = root / "project"
                    controller.plan = root / "plan.txt"
                    controller.logs = root / host
                    controller.logs.mkdir()
                    with platform(host), patch("auto_coding.desktop_control.Popen",
                                               side_effect=OSError("spawn failed")) as popen:
                        result = controller._launch(3, False)
                    self.assertFalse(result["ok"])
                    self.assertIn("spawn failed", result["error"])
                    self.assert_options(popen.call_args.kwargs, host, runner=True)
                    self.assertEqual(popen.call_args.kwargs["stderr"], subprocess.STDOUT)
                    self.assertEqual(Path(popen.call_args.kwargs["stdout"].name),
                                     controller.logs / "runner-output.txt")
