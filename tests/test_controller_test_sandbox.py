"""Deterministic MXC launcher checks; never launch Codex or require Windows."""
import copy
import json
import os
import stat
from pathlib import Path
import subprocess
import sys
import tempfile
import tomllib
import unittest
from unittest.mock import Mock, patch

from auto_coding import controller_test_sandbox as sandbox
from auto_coding import reviewer_permissions as permissions
from auto_coding import simple_run as runner


class ControllerTestSandboxTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.repo = self.root / "checkout"
        (self.repo / ".git").mkdir(parents=True)
        self.logs = self.root / "logs"; self.logs.mkdir()
        self.windows = self.root / "Windows"; self.windows.mkdir()
        self.toolchain = self.root / "tools" / "runtime"
        self.toolchain.mkdir(parents=True)
        (self.toolchain / "tool.exe").write_text("fixture")
        self.git = lambda repo, args: str(self.repo / ".git")
        self.command = ["python", "-m", "unittest", "test_target"]
        for context in (patch.object(sandbox.sys, "platform", "win32"),
                        patch.dict(os.environ, {"SYSTEMROOT": str(self.windows),
                            "USERPROFILE": str(self.root / "users" / "profile"),
                            "HOME": str(self.root / "users" / "profile"),
                            "PROGRAMDATA": str(self.root / "programdata")}),
                        patch.object(sandbox.Path, "home", return_value=self.root / "users" / "profile"),
                        patch.object(sandbox.shutil, "which", return_value=str(self.root / "codex.exe"))):
            context.start(); self.addCleanup(context.stop)
        os.environ.pop(sandbox.TOOLCHAIN_DIRS_ENV, None)
        self.check = patch.object(sandbox, "check_cli", return_value=str(self.root / "codex.exe"))
        self.check_mock = self.check.start(); self.addCleanup(self.check.stop)

    def prepare(self):
        return sandbox.prepare(self.command, self.repo, self.logs, self.git)

    def run_test(self, launch):
        return sandbox.run(self.command, self.repo, self.logs, 17, git=self.git, launch=launch)

    def scratch(self):
        return list(self.logs.glob("review-scratch-*"))

    def test_command_and_exact_root_deny_policy(self):
        spec, marker = self.prepare(); self.addCleanup(spec.cleanup)
        arguments = spec.argv
        self.assertEqual(arguments[arguments.index("sandbox") + 1:arguments.index("--")],
                         ["-P", self.check_mock.call_args.args[1], "-C", str(self.repo)])
        self.assertEqual(json.loads(arguments[-1])[:2], [self.command, marker])
        self.assertNotIn("CODEX_HOME", json.loads(arguments[-1])[2])
        self.assertEqual(arguments[arguments.index("--") + 2:arguments.index("--") + 4], ["-I", "-B"])
        self.assertNotIn("exec", arguments)
        settings = self.check_mock.call_args.args[0]
        parsed = {}
        for item in settings:
            parsed.update(tomllib.loads(item.replace("windows.sandbox=mxc", 'windows.sandbox="mxc"')))
        self.assertEqual(parsed["windows"], {"sandbox": "mxc"})
        profile = parsed["permissions"][parsed["default_permissions"]]
        self.assertEqual(profile["extends"], ":read-only")
        self.assertEqual(profile["network"], {"enabled": False})
        self.assertEqual(profile["filesystem"], self.check_mock.call_args.kwargs["filesystem"])
        self.assertEqual(profile["filesystem"][":root"], "deny")
        self.assertEqual(profile["filesystem"][self.repo.as_posix()], "read")
        self.assertEqual([key for key,value in profile["filesystem"].items() if value == "write"],
                         [spec.scratch.as_posix()])

    def test_sanitized_target_environment_and_isolated_home(self):
        with patch.dict(os.environ, {"GITHUB_TOKEN": "canary", "CODEX_API_KEY": "canary",
                                     "PYTHONHOME": "wrong", "PYTHONPATH": str(self.root / "wrong")}):
            before = os.environ.copy()
            spec, _ = self.prepare()
            self.assertEqual(dict(os.environ), before)
        self.addCleanup(spec.cleanup)
        for key in ("GITHUB_TOKEN", "CODEX_API_KEY", "PYTHONHOME"):
            self.assertNotIn(key, spec.environment)
        self.assertEqual(spec.environment["PYTHONPATH"].split(os.pathsep),
                         [str(self.repo), str(self.repo / "src")])
        self.assertEqual([spec.environment[key] for key in ("TEMP", "TMP", "TMPDIR")],
                         [str(spec.scratch / "tmp")] * 3)
        self.assertEqual(Path(spec.environment["CODEX_HOME"]).parent, spec.scratch)
        self.assertIn('os.environ.clear()', sandbox._BOOTSTRAP)

    def test_opt_in_toolchain_is_read_only_without_other_permission_changes(self):
        with patch.dict(os.environ, {sandbox.TOOLCHAIN_DIRS_ENV: json.dumps([str(self.toolchain)])}):
            spec, _ = self.prepare()
        self.addCleanup(spec.cleanup)
        filesystem = self.check_mock.call_args.kwargs["filesystem"]
        self.assertEqual(filesystem[self.toolchain.as_posix()], "read")
        self.assertEqual(filesystem[":root"], "deny")
        self.assertEqual(filesystem[self.repo.as_posix()], "read")
        self.assertEqual([key for key, value in filesystem.items() if value == "write"], [spec.scratch.as_posix()])
        settings = self.check_mock.call_args.args[0]
        policy = tomllib.loads(settings[1])["permissions"][self.check_mock.call_args.args[1]]
        self.assertEqual(policy["filesystem"], filesystem)
        self.assertEqual(policy["network"], {"enabled": False})
        self.assertNotIn(sandbox.TOOLCHAIN_DIRS_ENV, spec.environment)
        self.assertNotIn(sandbox.TOOLCHAIN_DIRS_ENV, json.loads(spec.argv[-1])[2])

    def test_missing_configuration_keeps_default_roots(self):
        spec, _ = self.prepare(); self.addCleanup(spec.cleanup)
        filesystem = self.check_mock.call_args.kwargs["filesystem"]
        self.assertEqual(len(filesystem), 5)
        self.assertNotIn(self.toolchain.as_posix(), filesystem)

    def test_invalid_toolchain_roots_and_configuration_fail_before_launch(self):
        profile = self.root / "users" / "profile"; profile.mkdir(parents=True)
        for value in (str(self.root.anchor), str(self.root.parent), str(self.repo), str(self.logs),
                      str(profile), str(profile.parent), str(self.windows),
                      str(self.toolchain / "tool.exe"), str(self.root / "missing"), "relative/tools",
                      str(self.toolchain / ".." / "runtime"), str(self.toolchain) + ".",
                      "\\\\server\\share\\tools", str(self.toolchain) + ":stream"):
            with self.subTest(value=value), patch.dict(os.environ, {sandbox.TOOLCHAIN_DIRS_ENV: json.dumps([value])}):
                with self.assertRaises(sandbox.ControllerTestEnvironmentError) as caught:
                    self.prepare()
                self.assertTrue(caught.exception.cleanup_confirmed)
                self.assertFalse(caught.exception.process_started)
                self.assertEqual(caught.exception.classification, "confirmed_environment_blocker")
        for raw in ("invalid JSON", '{}', '[1]', json.dumps([str(self.toolchain)] * 9)):
            with self.subTest(raw=raw), patch.dict(os.environ, {sandbox.TOOLCHAIN_DIRS_ENV: raw}):
                with self.assertRaises(sandbox.ControllerTestEnvironmentError): self.prepare()
        self.check_mock.assert_not_called()
        self.assertEqual(self.scratch(), [])

    def test_reparse_ancestors_and_nested_links_are_rejected(self):
        original = Path.lstat
        nested = self.toolchain / "linked"; nested.mkdir()
        for dangerous in (self.toolchain, self.toolchain.parent, nested, self.toolchain / "tool.exe"):
            def lstat(path, *args, **kwargs):
                if path == dangerous:
                    return Mock(st_mode=stat.S_IFDIR, st_file_attributes=0x400)
                return original(path, *args, **kwargs)
            with self.subTest(path=dangerous), patch.dict(os.environ, {
                    sandbox.TOOLCHAIN_DIRS_ENV: json.dumps([str(self.toolchain)])}), patch.object(Path, "lstat", lstat):
                with self.assertRaises(sandbox.ControllerTestEnvironmentError): self.prepare()
        self.check_mock.assert_not_called()
        self.assertEqual(self.scratch(), [])

    def test_git_and_credentials_inside_installations_remain_denied(self):
        for name in (".git", ".codex", ".env", ".npmrc", "credentials.json"):
            sensitive = self.toolchain / name
            sensitive.write_text("harmless fixture")
            with self.subTest(name=name), patch.dict(os.environ, {
                    sandbox.TOOLCHAIN_DIRS_ENV: json.dumps([str(self.toolchain)])}):
                spec, _ = self.prepare()
                filesystem = self.check_mock.call_args.kwargs["filesystem"]
                self.assertEqual(filesystem[self.toolchain.as_posix()], "read")
                self.assertEqual(filesystem[sensitive.as_posix()], "deny")
                spec.cleanup()
            sensitive.unlink()

    def test_effective_permissions_reject_conflicting_grants_and_backend(self):
        spec, _ = self.prepare(); self.addCleanup(spec.cleanup)
        fs = self.check_mock.call_args.kwargs["filesystem"]
        legacy = {"sandbox_mode": None, "sandbox_workspace_write": None}
        value = {"default": "tests", "legacy": legacy, "windows_sandbox": "mxc",
                 "profile": {"extends": ":read-only", "filesystem": fs, "network": {"enabled": False}},
                 "layers": [{"legacy": legacy}], "requirements": None}
        def validate(v):
            permissions.validate_configuration(v, "tests", self.repo, spec.scratch, filesystem=fs, backend="mxc")
        validate(value)
        for mutate in (lambda v:v.update(windows_sandbox="elevated"),
                       lambda v:v["profile"]["filesystem"].update({":root": "read"}),
                       lambda v:v["profile"]["filesystem"].update({self.repo.as_posix(): "write"}),
                       lambda v:v["profile"]["network"].update(enabled=True),
                       lambda v:v.update(requirements={}),
                       lambda v:v["layers"][0]["legacy"].update(sandbox_mode="read-only")):
            changed = copy.deepcopy(value); mutate(changed)
            with self.assertRaises(permissions.ReviewerEnvironmentError): validate(changed)

    def test_actual_test_exit_and_output_preserved_then_scratch_removed(self):
        def launch(argv, repo, timeout):
            self.assertEqual(timeout, 17)
            self.assertEqual(repo, self.repo)
            self.assertEqual(Path(permissions.command_environment(repo)["TMPDIR"]).name, "tmp")
            marker = json.loads(argv[-1])[1]
            return runner._CallResult(0, 'test failure\n' + marker + '{"kind":"test","exit_code":3}\n', False)
        result = self.run_test(launch)
        self.assertEqual(result, (3, "test failure", False))
        self.assertTrue(result.cleanup_confirmed)
        self.assertEqual(self.scratch(), [])

    def test_startup_and_command_environment_failures_stop_without_host_fallback(self):
        for output, code in (("MXC startup failed", 1), ("not a footer", 0), ("", 127)):
            with self.subTest(code=code, output=output):
                launch = Mock(return_value=runner._CallResult(code, output, False, True, True))
                with self.assertRaises(sandbox.ControllerTestEnvironmentError) as caught:
                    self.run_test(launch)
                self.assertTrue(caught.exception.cleanup_confirmed)
                self.assertEqual(launch.call_count, 1)
                self.assertEqual(self.scratch(), [])
        def permission_failure(argv, *args):
            marker = json.loads(argv[-1])[1]
            return runner._CallResult(0, '\n' + marker + json.dumps({"kind":"environment",
                "error_type":"PermissionError", "error_text":"denied", "errno":13,
                "winerror":5, "executable":"compiler"}), False)
        with self.assertRaises(sandbox.ControllerTestEnvironmentError) as caught:
            self.run_test(permission_failure)
        self.assertEqual(caught.exception.classification, "confirmed_environment_blocker")
        self.assertEqual(caught.exception.details["executable"], "compiler")
        self.assertEqual(caught.exception.details["errno"], 13)
        self.assertFalse(caught.exception.process_started)
        self.assertTrue(caught.exception.cleanup_confirmed)
        self.assertTrue(list(self.logs.glob("controller-test-sandbox-*.txt")))

    def test_missing_executable_is_a_structured_launch_blocker(self):
        marker = "RESULT:"
        environment = os.environ.copy()
        missing = str(self.root / "missing-test-tool")
        result = subprocess.run([sys.executable, "-I", "-B", "-c", sandbox._BOOTSTRAP,
                                 json.dumps([[missing], marker, environment])],
                               capture_output=True, text=True, timeout=10)
        evidence = json.loads(result.stdout.split(marker, 1)[1])
        self.assertEqual(evidence["kind"], "environment")
        self.assertEqual(evidence["error_type"], "FileNotFoundError")
        self.assertEqual(evidence["errno"], 2)
        self.assertEqual(evidence["executable"], missing)

    def test_ambiguous_sandbox_output_is_not_called_a_permission_failure(self):
        with self.assertRaises(sandbox.ControllerTestEnvironmentError) as caught:
            self.run_test(Mock(return_value=runner._CallResult(1, "browser crashed", False, True, True)))
        self.assertEqual(caught.exception.classification, "unclassified_execution_problem")
        self.assertNotIn("permission", caught.exception.classification)
        self.assertTrue(caught.exception.output_artifact)

    def test_timeout_cleanup_proof_and_uncertainty_are_preserved(self):
        for confirmed in (True, False):
            result = runner._CallResult(124, "PROCESS TIMED OUT", True, confirmed, True)
            normalized = self.run_test(Mock(return_value=result))
            self.assertEqual(normalized, result)
            self.assertIs(normalized.cleanup_confirmed, confirmed)
            self.assertEqual(bool(self.scratch()), not confirmed)
        for path in self.scratch():
            # Test fixture owns these; no actual subprocess ran.
            import shutil
            shutil.rmtree(path)

    def test_unknown_launch_cleanup_cannot_release_the_gate(self):
        result = self.run_test(Mock(return_value=runner._CallResult(127, "launch error", False)))
        self.assertFalse(result.cleanup_confirmed)
        self.assertEqual(len(self.scratch()), 1)

    def test_bootstrap_restores_environment_and_reports_real_exit_or_launch_failure(self):
        environment = os.environ.copy()
        environment.pop("CODEX_HOME", None)
        environment["TEMP"] = environment["TMP"] = environment["TMPDIR"] = str(self.logs)
        command = [sys.executable, "-I", "-B", "-c",
                   "import os,sys; assert 'CODEX_HOME' not in os.environ; "
                   "assert os.environ['TEMP']==os.environ['TMP']==os.environ['TMPDIR']; sys.exit(3)"]
        with patch.dict(os.environ, {"CODEX_HOME": "unwanted", "TEMP": "wrong"}):
            result = subprocess.run([sys.executable, "-I", "-B", "-c", sandbox._BOOTSTRAP,
                                     json.dumps([command, "RESULT:", environment])],
                                    capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout.split("RESULT:")[1]), {"kind": "test", "exit_code": 3})
        missing = [str(self.root / "missing-executable")]
        result = subprocess.run([sys.executable, "-I", "-B", "-c", sandbox._BOOTSTRAP,
                                 json.dumps([missing, "RESULT:", environment])],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(json.loads(result.stdout.split("RESULT:")[1])["kind"], "environment")

    def test_unverified_exception_retains_scratch_and_propagates_protection(self):
        for position, started in enumerate((True, False), 1):
            error = OSError("ownership uncertain")
            error.process_started = started; error.cleanup_confirmed = False
            with self.assertRaises(OSError): self.run_test(Mock(side_effect=error))
            self.assertFalse(error.cleanup_confirmed)
            self.assertEqual(len(self.scratch()), position)

    def test_preflight_failure_never_launches_and_retains_only_uncertain_scratch(self):
        for confirmed in (True, False):
            error = subprocess.TimeoutExpired(["config"], 40)
            error.cleanup_confirmed = confirmed; error.process_started = True
            self.check_mock.side_effect = error
            launch = Mock()
            with self.assertRaises(sandbox.ControllerTestEnvironmentError) as caught: self.run_test(launch)
            self.assertEqual(caught.exception.cleanup_confirmed, confirmed)
            self.assertEqual(bool(self.scratch()), not confirmed)
            launch.assert_not_called()

    def test_unvalidated_platform_never_launches(self):
        with patch.object(sandbox.sys, "platform", "linux"):
            launch = Mock()
            with self.assertRaisesRegex(sandbox.ControllerTestEnvironmentError, "Windows MXC"):
                self.run_test(launch)
            launch.assert_not_called()
        self.assertEqual(self.scratch(), [])

    def test_production_dispatch_and_fake_executor_environment(self):
        with patch.object(sandbox, "run", return_value=(0, "sandboxed", False)) as isolated:
            self.assertEqual(runner.execute_controller_test(runner.call, self.command, self.repo,
                             self.logs, 17, {}), (0, "sandboxed", False))
            isolated.assert_called_once()
        fake = Mock(side_effect=lambda *a: (0, permissions.command_environment(self.repo)["CI"], False))
        with patch.object(sandbox, "run") as isolated:
            self.assertEqual(runner.execute_controller_test(fake, self.command, self.repo,
                             self.logs, 17, {"CI": "mock"}), (0, "mock", False))
            isolated.assert_not_called()
