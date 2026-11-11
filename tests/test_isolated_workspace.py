"""Real disposable Git/source operations; no MXC execution in this module."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from auto_coding import test_workspace as workspace
from auto_coding import controller_test_sandbox as sandbox


class IsolatedWorkspaceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.repo = self.root / "original"; self.repo.mkdir()
        self.copy = self.root / "workspace"; self.copy.mkdir()
        self.git(self.repo, ["init", "-q"])
        self.git(self.repo, ["config", "core.autocrlf", "false"])
        self.git(self.repo, ["config", "core.hooksPath", str(self.root / "no-hooks")])
        (self.repo / "source.py").write_bytes(b"# source\n")
        (self.repo / ".gitignore").write_bytes(b"ignored/\n")
        self.git(self.repo, ["add", "."])
        self.git(self.repo, ["-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                             "-c", "commit.gpgsign=false", "commit", "-qm", "fixture"])

    @staticmethod
    def git(repo, args):
        return subprocess.check_output(["git", *args], cwd=repo, text=True, encoding="utf-8").strip()

    def test_snapshot_tree_matches_git_and_exact_source_bytes(self):
        snapshot = workspace.capture(self.repo, self.git, self.copy)
        self.assertEqual(snapshot["head"], self.git(self.repo, ["rev-parse", "HEAD"]))
        self.assertEqual(snapshot["tree"], self.git(self.repo, ["rev-parse", "HEAD^{tree}"]))
        self.assertEqual(snapshot, workspace.capture(self.repo, self.git))
        self.assertEqual((self.copy / "source.py").read_bytes(), b"# source\n")
        self.assertFalse((self.copy / ".git").exists())

    def test_uncommitted_changes_and_new_tests_are_part_of_identity(self):
        before = workspace.capture(self.repo, self.git)
        (self.repo / "source.py").write_bytes(b"# implementation\r\n")
        (self.repo / "tests").mkdir()
        (self.repo / "tests" / "fixture.bin").write_bytes(bytes(range(256)))
        after = workspace.capture(self.repo, self.git, self.copy)
        self.assertEqual(before["head"], after["head"])
        self.assertNotEqual(before["tree"], after["tree"])
        self.assertNotEqual(before["files_sha256"], after["files_sha256"])
        self.assertEqual((self.copy / "tests" / "fixture.bin").read_bytes(), bytes(range(256)))

    def test_tracked_deletion_and_ignored_dependencies_are_not_copied(self):
        (self.repo / "source.py").unlink()
        (self.repo / "ignored").mkdir()
        (self.repo / "ignored" / "host.txt").write_text("excluded")
        snapshot = workspace.capture(self.repo, self.git, self.copy)
        self.assertEqual(snapshot["file_count"], 1)
        self.assertFalse((self.copy / "source.py").exists())
        self.assertFalse((self.copy / "ignored").exists())

    def test_original_source_is_independent_of_writable_copy(self):
        workspace.capture(self.repo, self.git, self.copy)
        (self.copy / "source.py").write_text("generated")
        self.assertEqual((self.repo / "source.py").read_bytes(), b"# source\n")

    def test_sensitive_files_fail_closed_instead_of_exporting_credentials(self):
        for name in (".env", "credentials.json", ".npmrc"):
            with self.subTest(name=name):
                path = self.repo / name
                path.write_text("fixture")
                with self.assertRaisesRegex(ValueError, "sensitive"):
                    workspace.capture(self.repo, self.git)
                path.unlink()

    def test_git_head_movement_is_rejected(self):
        calls = iter(["a" * 40, "", "source.py\0", "b" * 40])
        with self.assertRaisesRegex(ValueError, "changed"):
            workspace.capture(self.repo, lambda *args: next(calls))

    def test_unsafe_git_paths_never_escape_workspace(self):
        for path in ("../escape", "C:/escape", "folder/../escape", "NUL", "a:stream",
                     "folder\\escape", ".git/config", ".codex/config.toml", "bad. "):
            def git(repo, args):
                if args == ["ls-files", "--stage", "-v", "-z"]:
                    return ""
                return path + "\0" if args[0] == "ls-files" else "a" * 40
            with self.subTest(path=path), self.assertRaises(ValueError):
                workspace.capture(self.repo, git, self.copy)

    def test_executable_git_mode_is_bound_to_snapshot_tree(self):
        self.git(self.repo, ["update-index", "--chmod=+x", "source.py"])
        snapshot = workspace.capture(self.repo, self.git)
        self.assertEqual(snapshot["tree"], self.git(self.repo, ["write-tree"]))

    def test_hidden_working_tree_flags_cannot_misrepresent_approved_source(self):
        for flag in ("--assume-unchanged", "--skip-worktree"):
            with self.subTest(flag=flag):
                self.git(self.repo, ["update-index", flag, "source.py"])
                with self.assertRaisesRegex(ValueError, "hidden working-tree flags"):
                    workspace.capture(self.repo, self.git)
                self.git(self.repo, ["update-index", "--no-assume-unchanged", "--no-skip-worktree", "source.py"])

    def test_new_repository_binds_unborn_branch_and_exact_implementation_tree(self):
        unborn = self.root / "new-project"; unborn.mkdir()
        self.git(unborn, ["init", "-q"])
        (unborn / "implementation.py").write_bytes(b"# first implementation\n")
        snapshot = workspace.capture(unborn, self.git)
        self.assertIsNone(snapshot["head"])
        self.assertTrue(snapshot["unborn_branch"].startswith("refs/heads/"))
        self.git(unborn, ["add", "."])
        self.assertEqual(snapshot["tree"], self.git(unborn, ["write-tree"]))

    def test_reparse_file_is_rejected(self):
        original = Path.lstat
        def lstat(path, *args, **kwargs):
            if path == self.repo / "source.py":
                return Mock(st_mode=0o100644, st_file_attributes=0x400)
            return original(path, *args, **kwargs)
        with patch.object(Path, "lstat", lstat), self.assertRaisesRegex(ValueError, "reparse"):
            workspace.capture(self.repo, self.git)

    def test_environment_places_homes_caches_and_temporary_files_inside_scratch(self):
        scratch = self.root / "scratch"; scratch.mkdir()
        tool = self.root / "tools"; tool.mkdir()
        outside = self.root / "personal"; outside.mkdir()
        result = workspace.environment(self.copy, scratch, {
            "PATH": os.pathsep.join([str(outside), ".", str(tool)]),
            "HOME": str(outside), "USERPROFILE": str(outside), "VIRTUAL_ENV": str(outside)},
            {tool.as_posix(): "read", self.repo.as_posix(): "deny"})
        self.assertEqual(result["PATH"], str(tool))
        self.assertNotIn("VIRTUAL_ENV", result)
        for key in ("TEMP", "TMP", "TMPDIR", "HOME", "USERPROFILE", "APPDATA",
                    "LOCALAPPDATA", "XDG_CACHE_HOME", "PIP_CACHE_DIR", "npm_config_cache",
                    "AUTOCODING_TEST_OUTPUT_DIR"):
            self.assertTrue(Path(result[key]).is_relative_to(scratch), key)
            self.assertTrue(Path(result[key]).is_dir(), key)
        self.assertEqual(result["GIT_CONFIG_GLOBAL"], os.devnull)
        self.assertEqual(result["GIT_CONFIG_NOSYSTEM"], "1")
        self.assertNotIn(str(self.repo), result["PYTHONPATH"])

    def bootstrap(self, command, environment):
        result = subprocess.run([sys.executable, "-I", "-B", "-c", sandbox._BOOTSTRAP,
            json.dumps([command, "RESULT:", environment])], capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout.rsplit("RESULT:", 1)[1])

    def test_validated_custom_import_roots_are_relocated(self):
        scratch = self.root / "scratch"; scratch.mkdir()
        result = workspace.environment(self.copy, scratch, {"PATH": "", "PYTHONPATH": os.pathsep.join([
            str(self.repo), str(self.repo / "src"), str(self.repo / "custom-lib"), str(self.root / "personal")])},
            {}, source=self.repo)
        self.assertEqual(result["PYTHONPATH"].split(os.pathsep),
                         [str(self.copy), str(self.copy / "src"), str(self.copy / "custom-lib")])

    def test_missing_temp_leaf_is_recovered_once_before_nested_git_execution(self):
        environment = os.environ.copy()
        temporary = self.root / "temporary"
        environment.update({key: str(temporary) for key in ("TEMP", "TMP", "TMPDIR")})
        command = [sys.executable, "-I", "-c",
                   "import subprocess,tempfile; from pathlib import Path; "
                   "d=tempfile.mkdtemp(); subprocess.run(['git','init','-q',d],check=True); "
                   "assert Path(d,'.git').is_dir()"]
        evidence = self.bootstrap(command, environment)
        self.assertEqual(evidence["exit_code"], 0)
        self.assertEqual(evidence["context"]["recovery"], ["recreated_temporary_directory"])

    def test_missing_parent_is_not_recreated_and_test_is_not_launched(self):
        environment = os.environ.copy()
        environment.update({key: str(self.root / "absent" / "leaf") for key in ("TEMP", "TMP", "TMPDIR")})
        evidence = self.bootstrap([sys.executable, "-c", "raise AssertionError('must not run')"], environment)
        self.assertEqual(evidence["kind"], "environment")
        self.assertEqual(evidence["stage"], "temporary_storage")
        self.assertFalse((self.root / "absent").exists())
        self.assertEqual(sandbox._category(evidence, ""), "temporary_storage_failure")

    def test_invalid_temporary_location_is_a_structured_pre_execution_failure(self):
        environment = os.environ.copy()
        temporary = self.root / "not-a-directory"; temporary.write_text("fixture")
        environment.update({key: str(temporary) for key in ("TEMP", "TMP", "TMPDIR")})
        evidence = self.bootstrap([sys.executable, "-c", "raise AssertionError('must not run')"], environment)
        self.assertEqual(evidence["kind"], "environment")
        self.assertEqual(evidence["error_type"], "NotADirectoryError")
        self.assertEqual(evidence["stage"], "temporary_storage")
        self.assertEqual(evidence["context"]["recovery"], [])
        self.assertEqual(sandbox._category(evidence, ""), "temporary_storage_failure")

    def test_missing_node_fails_once_with_resolution_and_errno(self):
        environment = os.environ.copy(); environment["PATH"] = ""
        evidence = self.bootstrap([str(self.root / "missing-node.exe"), "--version"], environment)
        self.assertEqual(evidence["kind"], "environment")
        self.assertEqual(evidence["errno"], 2)
        self.assertIsNone(evidence["context"]["tools"]["node"])
        self.assertEqual(evidence["context"]["recovery"], [])

    def test_diagnostics_do_not_promote_output_hints_to_proven_environment_causes(self):
        self.assertEqual(sandbox._category({"kind": "environment", "errno": 13}, ""), "filesystem_permission")
        self.assertEqual(sandbox._category({"kind": "test", "exit_code": 1}, "PermissionError:"),
                         "possible_filesystem_permission")
        self.assertEqual(sandbox._category({"kind": "test", "exit_code": 1}, "CalledProcessError:"),
                         "nested_subprocess_failure")
        self.assertEqual(sandbox._category({"kind": "test", "exit_code": 1}, "AssertionError:"), "test_failure")


if __name__ == "__main__":
    unittest.main()
