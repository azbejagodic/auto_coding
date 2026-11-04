"""Real Python children and fake Codex roles; never invoke a model."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from auto_coding import simple_run as runner
from auto_coding.python_environment import (TARGET_PYTHONPATH, TEST_ENV_CONFIG,
                                            target_environment, test_environment)
from auto_coding.reviewer_permissions import activated
from auto_coding.run_launcher import LaunchState, launch_runner
from backend_fixtures import approved_plan_fixture


class PythonEnvironmentTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.target = self.root / "target worktree"
        self.wrong = self.root / "other worktree"
        self.target.mkdir()
        self.wrong.mkdir()
        self.package(self.wrong / "src", "wrong checkout")

    def package(self, directory, value):
        package = directory / "sample_app"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text(f"VALUE = {value!r}\n", encoding="utf-8")

    def poisoned_environment(self, extra=""):
        return patch.dict(os.environ, {
            "PYTHONPATH": str(self.wrong / "src") + (os.pathsep + extra if extra else ""),
            "PYTHONDONTWRITEBYTECODE": "1",
        })

    def assert_target_import(self, source):
        code = "import json,sample_app; print(json.dumps([sample_app.VALUE,sample_app.__file__]))"
        original_module = runner.__file__
        with self.poisoned_environment():
            before = os.environ.copy()
            result = runner.call([sys.executable, "-B", "-c", code], self.target, 15)
            self.assertEqual(dict(os.environ), before)
        self.assertEqual(result[0], 0, result[1])
        value, location = json.loads(result[1])
        self.assertEqual(value, "target checkout")
        self.assertEqual(Path(location).resolve(), source / "sample_app" / "__init__.py")
        self.assertEqual(runner.__file__, original_module)

    def test_flat_target_overrides_inherited_other_worktree(self):
        self.package(self.target, "target checkout")
        self.assert_target_import(self.target)

    def test_src_target_overrides_inherited_other_worktree(self):
        self.package(self.target / "src", "target checkout")
        self.assert_target_import(self.target / "src")

    def test_pythonhome_cannot_redirect_selected_interpreter(self):
        self.package(self.target / "src", "target checkout")
        with patch.dict(os.environ, {"PYTHONHOME": str(self.wrong / "missing-python")}):
            self.assert_target_import(self.target / "src")

    def test_src_created_by_implementer_is_visible_to_its_test_children(self):
        script = (
            "import pathlib,subprocess,sys\n"
            "package=pathlib.Path('src/sample_app'); package.mkdir(parents=True)\n"
            "(package/'__init__.py').write_text(\"VALUE = 'new target source'\\n\")\n"
            "checked=subprocess.run([sys.executable,'-B','-c',"
            "'import sample_app; print(sample_app.VALUE)'],capture_output=True,text=True)\n"
            "assert checked.returncode == 0, checked.stderr\n"
            "print(checked.stdout.strip())\n"
        )
        with self.poisoned_environment():
            result = runner.call([sys.executable, "-B", "-c", script], self.target, 15)
        self.assertEqual(result[0], 0, result[1])
        self.assertEqual(result[1].strip(), "new target source")

    def test_custom_target_local_import_roots_are_preserved(self):
        source = self.target / "lib" / "python"
        self.package(source, "custom layout")
        for entry in (str(source), str(Path("lib") / "python")):
            with self.subTest(entry=entry), self.poisoned_environment(entry):
                result = runner.call([sys.executable, "-B", "-c",
                                      "import sample_app; print(sample_app.VALUE)"], self.target, 15)
                self.assertEqual(result[0], 0, result[1])
                self.assertEqual(result[1].strip(), "custom layout")

    def test_namespace_package_cannot_fall_back_to_other_worktree(self):
        for root in (self.target / "src", self.wrong / "src"):
            (root / "namespace_app").mkdir(parents=True)
        (self.target / "src" / "namespace_app" / "local.py").write_text("VALUE = 'target'\n")
        (self.wrong / "src" / "namespace_app" / "foreign.py").write_text("VALUE = 'wrong'\n")
        script = (
            "from namespace_app.local import VALUE\n"
            "assert VALUE == 'target'\n"
            "try:\n import namespace_app.foreign\n"
            "except ModuleNotFoundError:\n print('isolated')\n"
            "else:\n raise AssertionError('Imported the other worktree')\n"
        )
        with self.poisoned_environment():
            result = runner.call([sys.executable, "-B", "-c", script], self.target, 15)
        self.assertEqual(result[0], 0, result[1])
        self.assertEqual(result[1].strip(), "isolated")

    def test_only_target_paths_change_and_non_python_environment_is_preserved(self):
        with self.poisoned_environment(os.pathsep + "."), patch.dict(os.environ, {
                "TEMP": "existing-temp", "PATH": "existing-path", "CODEX_HOME": "existing-config",
                "AUTOCODING_JOB_RESERVATION": "existing-reservation", "PYTHONHOME": "wrong-python"}):
            before = os.environ.copy()
            environment = target_environment(self.target)
            self.assertEqual(dict(os.environ), before)
        self.assertEqual(environment.pop("PYTHONPATH"),
                         os.pathsep.join(map(str, [self.target, self.target / "src"])))
        self.assertNotIn("PYTHONHOME", environment)
        expected = {key: value for key, value in before.items() if key not in {"PYTHONPATH", "PYTHONHOME"}}
        self.assertEqual(environment, expected)

    def test_controller_test_child_has_runtime_settings_but_no_parent_credentials(self):
        self.package(self.target / "src", "target checkout")
        secrets = {"OPENAI_API_KEY": "harmless-openai-canary", "GITHUB_TOKEN": "harmless-github-canary",
                   "AWS_SECRET_ACCESS_KEY": "harmless-aws-canary", "CODEX_HOME": "harmless-config-canary",
                   "GIT_ASKPASS": "harmless-askpass-canary", "SSH_AUTH_SOCK": "harmless-socket-canary"}
        with self.poisoned_environment(), patch.dict(os.environ, secrets):
            before = os.environ.copy()
            environment = test_environment(self.target)
            self.assertEqual(dict(os.environ), before)
            for name in secrets:
                self.assertNotIn(name, environment)
            for name in ("PATH", "TEMP", "TMP", "TMPDIR", "SystemRoot", "WINDIR", "ComSpec",
                         "PATHEXT", "HOME", "USERPROFILE", "VIRTUAL_ENV"):
                if name in before:
                    self.assertEqual(environment[name], before[name])
            self.assertEqual(environment["PYTHONPATH"], os.pathsep.join((str(self.target), str(self.target / "src"))))
            with activated(environment):
                result = runner.call([sys.executable, "-B", "-c",
                                      "import os,sys,sample_app; print(sample_app.VALUE); "
                                      "print(os.environ.get('OPENAI_API_KEY', 'absent')); sys.exit(7)"],
                                     self.target, 15)
            self.assertEqual(dict(os.environ), before)
        self.assertEqual(result[0], 7, result[1])
        self.assertIn("target checkout", result[1])
        self.assertIn("absent", result[1])
        self.assertFalse(any(value in result[1] for value in secrets.values()))

    def test_explicit_test_settings_are_narrow_and_never_echo_invalid_values(self):
        configured = test_environment(self.target, extra={"CI": "true", "TZ": "UTC"})
        self.assertEqual((configured["CI"], configured["TZ"]), ("true", "UTC"))
        for name in ("OPENAI_API_KEY", "GITHUB_TOKEN", "PATH", "PYTHONPATH", "CODEX_HOME"):
            with self.subTest(name=name), self.assertRaises(ValueError) as caught:
                test_environment(self.target, extra={name: "harmless-canary"})
            self.assertNotIn("harmless-canary", str(caught.exception))
        with patch.dict(os.environ, {TEST_ENV_CONFIG: json.dumps({"CI": "true",
                "AUTOCODING_CONFIG_DIR": str(self.root / "test-config")})}):
            child = test_environment(self.target)
        self.assertEqual(child["CI"], "true")
        self.assertEqual(child["AUTOCODING_CONFIG_DIR"], str(self.root / "test-config"))
        self.assertNotIn(TEST_ENV_CONFIG, child)
        with patch.dict(os.environ, {TEST_ENV_CONFIG: '{"GITHUB_TOKEN":"harmless-canary"}'}):
            with self.assertRaises(ValueError) as caught:
                test_environment(self.target)
        self.assertNotIn("harmless-canary", str(caught.exception))

    def test_non_python_command_and_project_without_src_still_work(self):
        with self.poisoned_environment():
            result = runner.call(["git", "--version"], self.target, 15)
        self.assertEqual(result[0], 0, result[1])
        self.assertTrue(result[1].startswith("git version "))

    def test_background_controller_imports_trusted_engine_not_target(self):
        from auto_coding import run_launcher
        source = Path(run_launcher.__file__).resolve().parents[1]
        shadow = self.target / "auto_coding"
        shadow.mkdir()
        (shadow / "__init__.py").write_text("raise AssertionError('Untrusted controller imported')\n")
        logs = self.root / "launch-logs"
        logs.mkdir()
        process = Mock(pid=123)
        spawn = Mock(return_value=process)
        with self.poisoned_environment(str(self.target)):
            before = os.environ.copy()
            result = launch_runner(LaunchState(), repo=self.target, plan=self.root / "plan.json",
                                   logs=logs, attempts=3, popen=spawn, reserve=lambda repo: None,
                                   persist_metadata=lambda *args, **kwargs: None)
            self.assertEqual(dict(os.environ), before)
        self.assertTrue(result["ok"], result)
        options = spawn.call_args.kwargs
        probe = subprocess.run([sys.executable, "-B", "-c",
                                "from auto_coding import simple_run; print(simple_run.__file__)"],
                               cwd=options["cwd"], env=options["env"], text=True,
                               capture_output=True, timeout=15)
        self.assertEqual(probe.returncode, 0, probe.stderr)
        self.assertEqual(Path(probe.stdout.strip()).resolve(), source / "auto_coding" / "simple_run.py")

    def test_background_launch_preserves_custom_roots_only_for_target_children(self):
        source = self.target / "lib" / "python"
        self.package(source, "custom layout")
        logs = self.root / "launch-logs"
        logs.mkdir()
        spawn = Mock(return_value=Mock(pid=123))
        with self.poisoned_environment(str(source)):
            launch_runner(LaunchState(), repo=self.target, plan=self.root / "plan.json", logs=logs,
                          attempts=3, popen=spawn, reserve=lambda repo: None,
                          persist_metadata=lambda *args, **kwargs: None)
        controller_env = spawn.call_args.kwargs["env"]
        self.assertNotIn(str(source), controller_env["PYTHONPATH"])
        with patch.dict(os.environ, controller_env, clear=True):
            environment = target_environment(self.target)
        self.assertNotIn(TARGET_PYTHONPATH, environment)
        result = subprocess.run([sys.executable, "-B", "-c", "import sample_app; print(sample_app.VALUE)"],
                                cwd=self.target, env=environment, text=True, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "custom layout")

    def test_a_b_focused_final_and_resume_share_target_environment(self):
        self.package(self.target / "src", "target checkout")
        tests = self.target / "tests"
        tests.mkdir()
        (tests / "test_target.py").write_text(
            "import unittest\nfrom sample_app import VALUE\n"
            "class ImportTests(unittest.TestCase):\n"
            " def test_target(self): self.assertEqual(VALUE, 'target checkout')\n", encoding="utf-8")
        def git(*args):
            return subprocess.run(["git", *args], cwd=self.target, capture_output=True,
                                  text=True, check=True, timeout=15)
        git("init", "-b", "feature/isolation")
        git("config", "user.name", "Test")
        git("config", "user.email", "test@example.invalid")
        git("add", "-A")
        git("commit", "-m", "target fixture")
        command = [sys.executable, "-B", "-m", "unittest", "discover", "-s", "tests", "-v"]
        plan, logs = self.root / "plan.json", self.root / "run-logs"
        plan.write_text(json.dumps({
            "tasks": [{"id": "one", "prompt": "Implement the fixture", "test_commands": [command]}],
            "final_verification": {"test_commands": [command], "minimum_tests": 1, "timeout_seconds": 30},
        }), encoding="utf-8")
        # Each fake role launches its own Python test child, like an agent tool.
        agent = (
            "import json,pathlib,subprocess,sys\n"
            "answer,role=sys.argv[1:3]\n"
            "tested=subprocess.run(json.loads(sys.argv[3]),capture_output=True,text=True)\n"
            "assert tested.returncode == 0, tested.stdout+tested.stderr\n"
            "if role == 'A':\n"
            " pathlib.Path('implementation.txt').write_text('implemented')\n"
            " pathlib.Path(answer).write_text('Focused checks passed: '+tested.stderr)\n"
            "else:\n"
            " assert pathlib.Path('implementation.txt').read_text() == 'implemented'\n"
            " pathlib.Path(answer).write_text(json.dumps(dict(decision='APPROVED',issues=[],summary='Verified')))\n"
        )
        calls = []
        final_blocked = False
        def execute(argv, cwd, timeout, *, prompt=None):
            nonlocal final_blocked
            if argv[0] == "codex":
                self.assertEqual(runner.command_environment(cwd)["OPENAI_API_KEY"], "harmless-canary")
                role = "B" if "--output-schema" in argv else "A"
                if role == "B":
                    self.assertNotIn("--sandbox", argv)
                    self.assertEqual(Path(argv[argv.index("--cd") + 1]), cwd)
                    self.assertTrue(any(part.startswith('default_permissions="auto_coding_review_') for part in argv))
                else:
                    self.assertEqual(argv[argv.index("--sandbox") + 1], "workspace-write")
                answer = argv[argv.index("--output-last-message") + 1]
                calls.append(role)
                return runner.call([sys.executable, "-B", "-c", agent, answer, role, json.dumps(command)],
                                   cwd, timeout, prompt=prompt)
            state = json.loads((logs / "state.json").read_text())
            final = bool(state["completed"])
            self.assertNotIn("OPENAI_API_KEY", runner.command_environment(cwd))
            self.assertNotIn("GITHUB_TOKEN", runner.command_environment(cwd))
            calls.append("final" if final else "focused")
            if final and not final_blocked:
                final_blocked = True
                return runner._CallResult(127, "Simulated final launch failure", False, True, False)
            return runner.call(argv, cwd, timeout, prompt=prompt)
        with approved_plan_fixture(), self.poisoned_environment(), \
                patch.dict(os.environ, {"OPENAI_API_KEY": "harmless-canary",
                                     "GITHUB_TOKEN": "harmless-github-canary"}), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(runner.run(plan, self.target, logs, execute=execute, controller_tests=True), 1)
            self.assertEqual(calls, ["A", "focused", "B", "final"])
            self.assertEqual(runner.run(plan, self.target, logs, execute=execute, resume=True), 0)
            self.assertEqual(calls, ["A", "focused", "B", "final", "final"])
            self.assertEqual(runner.run(plan, self.target, logs, execute=execute, resume=True), 0)
            self.assertEqual(calls, ["A", "focused", "B", "final", "final"])
        self.assertEqual(json.loads((logs / "state.json").read_text())["status"], "completed")


if __name__ == "__main__":
    unittest.main()
