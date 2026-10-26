import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

from auto_coding.github_workspace import (GitHubWorkspaceManager, WorkspaceError,
    checkout_lock, managed_job, managed_lock, normalize_repository, storage_root, reserve_job)
from auto_coding.desktop_control import DesktopController
from auto_coding.desktop_app import Bridge


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.seed = self.root / "seed"
        self.seed.mkdir()
        self.git(self.seed, "init", "-b", "main")
        self.git(self.seed, "config", "user.name", "Test")
        self.git(self.seed, "config", "user.email", "test@example.invalid")
        (self.seed / "file.txt").write_text("initial")
        self.git(self.seed, "add", ".")
        self.git(self.seed, "commit", "-m", "initial")
        self.git(self.seed, "branch", "feature/existing")
        self.remote = self.root / "remote.git"
        self.git(self.root, "clone", "--bare", str(self.seed), str(self.remote))
        self.calls = []
        self.manager = GitHubWorkspaceManager(self.root / "config", run_command=self.command)

    def tearDown(self):
        self.temp.cleanup()

    def git(self, cwd, *args):
        return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                              text=True, encoding="utf-8").stdout.strip()

    def command(self, argv, **kwargs):
        self.calls.append(argv)
        self.assertFalse(kwargs["shell"])
        self.assertEqual(kwargs["encoding"], "utf-8")
        if argv[0] == "gh":
            raise FileNotFoundError()
        argv = list(argv)
        if argv[1] == "clone":
            argv[-2] = str(self.remote)
        result = subprocess.run(argv, **kwargs)
        # Only map our fixture's local transport back to the public identity.
        if argv[1:3] == ["remote", "get-url"] and result.stdout.strip() == str(self.remote):
            result.stdout = "https://github.com/owner/repo.git\n"
        return result

    def connect(self):
        return Path(self.manager.connect("Owner/Repo")["path"])

    def assert_error(self, code, fn):
        with self.assertRaises(WorkspaceError) as caught:
            fn()
        self.assertEqual(caught.exception.code, code)

    def test_clone_reopen_and_safe_metadata(self):
        repo = self.connect()
        self.assertEqual(self.manager.reopen("git@github.com:OWNER/REPO.git")["path"], str(repo))
        self.assertEqual(sum(argv[1] == "clone" for argv in self.calls), 1)
        data = json.loads(repo.with_suffix(".json").read_text())
        self.assertEqual(data, {"schema": 1, "repository": "owner/repo"})
        self.assertEqual(self.manager.branches("owner/repo")["branch"], "main")
        self.assertIn("origin/feature/existing", self.manager.branches("owner/repo")["remote"])

    def test_existing_and_new_feature_from_fresh_main(self):
        repo = self.connect()
        self.manager.checkout_branch("owner/repo", "feature/existing")
        self.manager.checkout_branch("owner/repo", "feature/existing")
        (self.seed / "file.txt").write_text("new upstream")
        self.git(self.seed, "commit", "-am", "upstream")
        self.git(self.seed, "push", str(self.remote), "main")
        result = self.manager.create_branch("owner/repo", "feature/new")
        self.assertEqual(result, {"branch": "feature/new", "clean": True})
        self.assertEqual(self.git(repo, "rev-parse", "HEAD"), self.git(self.seed, "rev-parse", "HEAD"))
        self.assertNotEqual(self.git(repo, "rev-parse", "main"), self.git(repo, "rev-parse", "HEAD"))
        self.assert_error("branch_exists", lambda: self.manager.create_branch("owner/repo", "feature/new"))
        self.assert_error("branch_exists", lambda: self.manager.create_branch("owner/repo", "feature/existing"))

    def test_non_ascii_workspace_and_branches(self):
        self.manager = GitHubWorkspaceManager(self.root / "caf\u00e9" / "配置", run_command=self.command)
        existing, created = "feature/caf\u00e9", "feature/na\u00efve"
        self.git(self.seed, "branch", existing)
        self.git(self.seed, "push", str(self.remote), existing)
        repo = self.connect()
        self.assertEqual(self.manager.reopen("owner/repo")["path"], str(repo))
        self.assertEqual(sum(argv[1] == "clone" for argv in self.calls), 1)
        self.assertIn("origin/" + existing, self.manager.branches("owner/repo")["remote"])
        self.assertEqual(self.manager.checkout_branch("owner/repo", existing), {"branch": existing, "clean": True})
        self.assertEqual(self.manager.create_branch("owner/repo", created), {"branch": created, "clean": True})
        self.assertIn(created, self.manager.branches("owner/repo")["local"])
        self.assertEqual(self.manager.reopen("owner/repo")["branch"], created)
        self.assertEqual(self.manager.checkout_branch("owner/repo", existing)["branch"], existing)
        controller = DesktopController(self.manager.runs_dir)
        self.assertTrue(controller.select_repo(repo)["ok"])
        preflight = controller.preflight()
        self.assertEqual(preflight["branch"], existing)
        self.assertTrue(preflight["clean"])
        self.assertEqual(preflight["errors"], [])

    def test_dirty_operation_active_and_uncertain(self):
        repo = self.connect()
        (repo / "untracked").write_text("dirty")
        self.assert_error("dirty", lambda: self.manager.checkout_branch("owner/repo", "feature/existing"))
        (repo / "untracked").unlink()
        (repo / ".git" / "MERGE_HEAD").write_text(self.git(repo, "rev-parse", "HEAD"))
        self.assert_error("git_operation", lambda: self.manager.create_branch("owner/repo", "feature/new"))
        (repo / ".git" / "MERGE_HEAD").unlink()
        for active in (True, None):
            self.manager.active_check = lambda path: active
            self.assert_error("active_job", lambda: self.manager.create_branch("owner/repo", "feature/new"))
        self.manager.active_check = None
        logs = self.manager.runs_dir / "run-test"
        logs.mkdir(parents=True)
        (logs / "desktop-run.json").write_text(json.dumps({"repo": str(repo), "status": "launching"}))
        self.assert_error("active_job", lambda: self.manager.checkout_branch("owner/repo", "feature/existing"))

    def test_names_and_protected_branches(self):
        self.assertEqual(normalize_repository("Owner/.github")[0], "owner/.github")
        self.assertEqual(normalize_repository("https://github.com/o/" + "r" * 100 + ".git")[0], "o/" + "r" * 100)
        for value in ("../repo", "owner/../repo", "https://evil.com/o/r", "https://github.com/o/r?token=x",
                      "https://token@github.com/o/r", "owner/r;evil", "-owner/repo", "owner/..", "owner/r\n", "file:///tmp/r"):
            self.assert_error("invalid_repository", lambda: self.manager.connect(value))
        self.connect()
        for name in ("main", "MASTER", "-x", "../escape", "feature/../../bad", "a.lock", "a..b", "@{-1}"):
            with self.assertRaises(WorkspaceError):
                self.manager.create_branch("owner/repo", name)
        self.assert_error("confirmation", lambda: self.manager.create_branch("owner/repo", "feature/new", base="feature/existing"))
        self.manager.create_branch("owner/repo", "feature/new", base="feature/existing", confirmed_base=True)

    def test_wrong_origin_and_unrelated_directory(self):
        repo = self.connect()
        self.git(repo, "remote", "set-url", "origin", "https://github.com/other/repo.git")
        self.assert_error("origin_mismatch", lambda: self.manager.reopen("owner/repo"))
        _, _, other, _, _ = self.manager._paths("owner/other")
        other.mkdir()
        (other / "keep").write_text("untouched")
        self.assert_error("origin_mismatch", lambda: self.manager.connect("owner/other"))
        self.assertEqual((other / "keep").read_text(), "untouched")

    def test_missing_gh_and_redacted_errors(self):
        self.assertFalse(self.manager.list_repositories()["available"])
        self.connect()  # gh is never required for manual entry.
        for diagnostic, code in (("authentication failed TOKEN", "authentication"), ("Could not resolve host TOKEN", "network")):
            with patch.object(self.manager, "run_command", return_value=subprocess.CompletedProcess([], 1, "", diagnostic)):
                with self.assertRaises(WorkspaceError) as caught:
                    self.manager.connect("owner/" + code)
                self.assertEqual(caught.exception.code, code)
                self.assertNotIn("TOKEN", str(caught.exception))

    def test_same_repo_race_and_job_lifetime(self):
        repo = self.connect()
        with checkout_lock(managed_lock(repo)):
            errors = []
            def concurrent():
                try: self.manager.connect("OWNER/REPO")
                except WorkspaceError as exc: errors.append(exc.code)
            thread = threading.Thread(target=concurrent)
            thread.start()
            thread.join()
            self.assertEqual(errors, ["busy"])
        @managed_job
        def job(plan, path, logs):
            self.assert_error("active_job", lambda: self.manager.create_branch("owner/repo", "feature/new"))
        job(None, repo, None)
        self.manager.create_branch("owner/repo", "feature/new")

    def test_first_clone_race(self):
        entered, release = threading.Event(), threading.Event()
        original = self.manager.run_command
        def paused(argv, **kwargs):
            if argv[1] == "clone":
                entered.set()
                release.wait(10)
            return original(argv, **kwargs)
        self.manager.run_command = paused
        failures = []
        def clone():
            try: self.manager.connect("owner/repo")
            except Exception as exc: failures.append(exc)
        thread = threading.Thread(target=clone)
        thread.start()
        try:
            self.assertTrue(entered.wait(10))
            self.assert_error("busy", lambda: self.manager.connect("owner/repo"))
        finally:
            release.set()
            thread.join(10)
        self.assertFalse(failures)
        self.assertFalse(thread.is_alive())
        self.assertTrue(self.manager.reopen("owner/repo")["clean"])

    def test_fetch_remote_branch_and_staged_dirty(self):
        repo = self.connect()
        self.git(self.seed, "branch", "feature/later")
        self.git(self.seed, "push", str(self.remote), "feature/later")
        self.manager.fetch("owner/repo")
        self.assertIn("origin/feature/later", self.manager.branches("owner/repo")["remote"])
        (repo / "file.txt").write_text("staged")
        self.git(repo, "add", "file.txt")
        self.assertFalse(self.manager.branches("owner/repo")["clean"])
        self.assert_error("dirty", lambda: self.manager.create_branch("owner/repo", "feature/new"))

    def test_deleted_base_cannot_use_stale_remote_ref(self):
        repo = self.connect()
        self.git(self.remote, "config", "receive.denyDeleteCurrent", "ignore")
        self.git(self.seed, "push", str(self.remote), ":main")
        self.assert_error("git", lambda: self.manager.create_branch("owner/repo", "feature/new"))
        self.assertEqual(self.git(repo, "branch", "--show-current"), "main")

    def test_live_external_job_and_launch_lock(self):
        repo = self.connect()
        controller = DesktopController(self.manager.runs_dir)
        controller.repo = repo
        logs = self.root / "external-logs"
        logs.mkdir()
        controller.logs = logs
        (logs / "desktop-run.json").write_text(json.dumps({"repo": str(repo), "status": "running", "pid": os.getpid()}))
        self.manager.active_check = controller.checkout_busy
        self.assert_error("active_job", lambda: self.manager.checkout_branch("owner/repo", "feature/existing"))
        with checkout_lock(managed_lock(repo)):
            result = controller.start()
        self.assertFalse(result["ok"])
        self.assertIn("lock", result["error"])

    def test_missing_git_and_timeout_are_safe(self):
        for error, code in ((FileNotFoundError("secret"), "unavailable"), (subprocess.TimeoutExpired("secret", 1), "network")):
            with patch.object(self.manager, "run_command", side_effect=error):
                self.assert_error(code, lambda: self.manager.connect("owner/" + code))

    def test_failed_first_clones_can_retry_without_touching_failed_attempts(self):
        _, _, repo, metadata, _ = self.manager._paths("owner/repo")
        failures = (
            (subprocess.CompletedProcess([], 1, "", "authentication failed SECRET"), "authentication"),
            (subprocess.CompletedProcess([], 1, "", "Could not resolve host SECRET"), "network"),
            (FileNotFoundError("SECRET"), "unavailable"),
        )
        attempts = []
        for failure, code in failures:
            def fail(argv, **kwargs):
                self.assertEqual(argv[1], "clone")
                checkout = Path(argv[-1])
                checkout.mkdir()
                (checkout / "partial").write_text("retained")
                attempts.append(checkout)
                if isinstance(failure, Exception):
                    raise failure
                return failure
            with patch.object(self.manager, "run_command", side_effect=fail):
                self.assert_error(code, lambda: self.manager.connect("owner/repo"))
            self.assertFalse(repo.exists())
            self.assertEqual(json.loads(metadata.read_text()), {"repository": "owner/repo", "schema": 1})
        self.assertEqual(self.connect(), repo)
        self.assertEqual(self.manager.reopen("owner/repo")["path"], str(repo))
        for attempt in attempts:
            self.assertEqual((attempt / "partial").read_text(), "retained")

    def assert_uncertain_checkout_blocks_operations_and_launches(self, repo):
        self.assertTrue((managed_lock(repo) / "uncertain").is_file())
        # Independent instances must see the durable gate as well.
        manager = GitHubWorkspaceManager(self.manager.root.parent, run_command=self.command)
        controller = DesktopController(self.root / "custom-runs")
        controller.repo = repo
        @managed_job
        def job(plan, repo, logs):
            self.fail("Runner must not execute with an uncertain checkout")
        with patch.object(manager, "run_command") as command, \
                patch("auto_coding.desktop_control.Popen") as popen:
            for operation in (
                lambda: manager.checkout_branch("owner/repo", "feature/existing"),
                lambda: manager.create_branch("owner/repo", "feature/new"),
                lambda: manager.fetch("owner/repo"),
                lambda: manager.reopen("owner/repo"),
                lambda: job(None, repo, None),
            ):
                self.assert_error("busy", operation)
            for launch in (controller.start, controller.resume):
                result = launch()
                self.assertFalse(result["ok"])
                self.assertIn("uncertain", result["error"])
            command.assert_not_called()
            popen.assert_not_called()
        self.assertTrue((managed_lock(repo) / "uncertain").is_file())

    def test_workspace_git_timeout_or_interruption_retains_checkout_gate(self):
        for action_index, action in enumerate(("checkout_branch", "create_branch", "fetch")):
            for error_index, exception in enumerate((subprocess.TimeoutExpired("SECRET", 120), KeyboardInterrupt())):
                with self.subTest(action=action, exception=type(exception).__name__):
                    self.manager = GitHubWorkspaceManager(
                        self.root / f"c{action_index}{error_index}", run_command=self.command)
                    repo = self.connect()
                    original = self.manager.run_command
                    def interrupted(argv, **kwargs):
                        if argv[1] == ("fetch" if action == "fetch" else "switch"):
                            raise exception
                        return original(argv, **kwargs)
                    with patch.object(self.manager, "run_command", side_effect=interrupted):
                        operation = getattr(self.manager, action)
                        args = ("owner/repo",) if action == "fetch" else (
                            "owner/repo", "feature/existing" if action == "checkout_branch" else "feature/new")
                        if isinstance(exception, subprocess.TimeoutExpired):
                            with self.assertRaises(WorkspaceError) as caught:
                                operation(*args)
                            self.assertEqual(caught.exception.code, "network")
                            self.assertNotIn("SECRET", str(caught.exception))
                        else:
                            with self.assertRaises(KeyboardInterrupt):
                                operation(*args)
                    self.assert_uncertain_checkout_blocks_operations_and_launches(repo)

    def test_clone_timeout_retains_partial_attempt_and_checkout_gate(self):
        _, _, repo, _, _ = self.manager._paths("owner/repo")
        attempts = []
        def interrupted(argv, **kwargs):
            checkout = Path(argv[-1])
            checkout.mkdir()
            (checkout / "partial").write_text("retained")
            attempts.append(checkout)
            raise subprocess.TimeoutExpired("SECRET", 120)
        with patch.object(self.manager, "run_command", side_effect=interrupted):
            self.assert_error("network", lambda: self.manager.connect("owner/repo"))
        self.assertFalse(repo.exists())
        self.assertEqual((attempts[0] / "partial").read_text(), "retained")
        self.assert_uncertain_checkout_blocks_operations_and_launches(repo)

    def test_clone_does_not_overwrite_directory_created_during_attempt(self):
        _, _, repo, _, _ = self.manager._paths("owner/repo")
        original = self.manager.run_command
        def command(argv, **kwargs):
            result = original(argv, **kwargs)
            if argv[1] == "clone":
                repo.mkdir()
                (repo / "keep").write_text("unrelated")
            return result
        self.manager.run_command = command
        self.assert_error("collision", lambda: self.manager.connect("owner/repo"))
        self.assertEqual((repo / "keep").read_text(), "unrelated")
        self.assert_error("origin_mismatch", lambda: self.manager.connect("owner/repo"))

    def test_managed_runner_duplicate_preserves_evidence(self):
        from auto_coding.simple_run import run
        repo = self.connect()
        logs = self.root / "logs"
        logs.mkdir()
        state = '{"status":"running","repo":"original"}'
        (logs / "state.json").write_text(state)
        with checkout_lock(managed_lock(repo, ".job.lock")):
            self.assert_error("busy", lambda: run(self.root / "missing-plan", repo, logs))
        self.assertEqual((logs / "state.json").read_text(), state)

    def test_timed_out_child_leaves_uncertain_job_gate(self):
        repo = self.connect()
        @managed_job
        def job(plan, repo, logs, *, execute=lambda: (124, "", True)):
            return execute()
        job(None, repo, None)
        self.assertTrue((managed_lock(repo, ".job.lock") / "uncertain").exists())
        self.assert_error("active_job", lambda: self.manager.create_branch("owner/repo", "feature/new"))

    def test_direct_checkpoint_git_timeout_retains_job_gate(self):
        from auto_coding.simple_run import local_commit, PilotError
        repo = self.connect()
        original = subprocess.run
        def command(argv, **kwargs):
            if "commit" in argv:
                raise subprocess.TimeoutExpired(argv, 120)
            return original(argv, **kwargs)
        @managed_job
        def job(plan, repo, logs):
            local_commit(repo, "example")
        with patch("auto_coding.simple_run.subprocess.run", side_effect=command):
            with self.assertRaises(PilotError):
                job(None, repo, None)
        self.assertTrue((managed_lock(repo, ".job.lock") / "uncertain").exists())
        self.assert_error("active_job", lambda: self.manager.create_branch("owner/repo", "feature/new"))
        self.assert_error("active_job", lambda: self.manager.checkout_branch("owner/repo", "feature/existing"))

    def test_direct_git_interruption_retains_job_gate(self):
        from auto_coding.simple_run import call
        repo = self.connect()
        @managed_job
        def job(plan, repo, logs):
            call(["git", "status"], repo, 15)
        with patch("auto_coding.simple_run.subprocess.run", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                job(None, repo, None)
        self.assertTrue((managed_lock(repo, ".job.lock") / "uncertain").exists())
        self.assert_error("active_job", lambda: self.manager.checkout_branch("owner/repo", "feature/existing"))

    def test_desktop_reserves_before_spawn_and_child_claims_once(self):
        repo = self.connect()
        self.manager.checkout_branch("owner/repo", "feature/existing")
        controller = DesktopController(self.root / "custom-runs")
        controller.repo = repo
        controller.plan = self.root / "plan.txt"
        controller.plan.write_text("Implement example task", encoding="utf-8")
        # This manager cannot discover custom-runs through its log directories.
        independent = GitHubWorkspaceManager(self.root / "config", run_command=self.command)
        with patch("auto_coding.desktop_control.Popen") as popen:
            popen.return_value.pid = 12345
            popen.return_value.poll.return_value = None
            result = controller.start()
            self.assertTrue(result["ok"], result)
            child_env = popen.call_args.kwargs["env"]
        job_path = managed_lock(repo, ".job.lock")
        self.assertTrue((job_path / "reservation").exists())
        self.assert_error("active_job", lambda: independent.checkout_branch("owner/repo", "feature/existing"))
        self.assert_error("active_job", lambda: independent.create_branch("owner/repo", "feature/new"))
        @managed_job
        def child(plan, repo, logs):
            self.assertFalse((job_path / "reservation").exists())
            self.assert_error("active_job", lambda: independent.checkout_branch("owner/repo", "feature/existing"))
            # Even a duplicate with the same inherited token cannot claim again.
            self.assert_error("busy", lambda: child(plan, repo, logs))
        with patch.dict(os.environ, child_env):
            child(None, repo, None)
        self.assertFalse(job_path.exists())
        independent.create_branch("owner/repo", "feature/new")

    def test_failed_desktop_spawn_releases_reservation(self):
        repo = self.connect()
        self.manager.checkout_branch("owner/repo", "feature/existing")
        controller = DesktopController(self.root / "custom-runs")
        controller.repo = repo
        controller.plan = self.root / "plan.txt"
        controller.plan.write_text("Implement example task", encoding="utf-8")
        with patch("auto_coding.desktop_control.Popen", side_effect=OSError("spawn failed")):
            self.assertFalse(controller.start()["ok"])
        self.assertFalse(managed_lock(repo, ".job.lock").exists())
        self.manager.create_branch("owner/repo", "feature/new")

    def test_plan_validation_failure_releases_job_for_retry(self):
        from auto_coding.simple_run import run
        repo = self.connect()
        plan = self.root / "missing-plan.txt"
        logs = self.root / "logs"
        for invalid_plan in (plan, self.root / "invalid.json"):
            if invalid_plan.suffix == ".json":
                invalid_plan.write_text("{invalid", encoding="utf-8")
            with self.assertRaises((ValueError, OSError)):
                run(invalid_plan, repo, logs)
            self.assertFalse(managed_lock(repo, ".job.lock").exists())
        self.manager.create_branch("owner/repo", "feature/new")
        plan.write_text("Implement example task", encoding="utf-8")
        with patch("auto_coding.simple_run._run", return_value=0) as runner:
            self.assertEqual(run(plan, repo, logs), 0)
            runner.assert_called_once()
        self.assertFalse(managed_lock(repo, ".job.lock").exists())

    def test_executor_exception_preserves_uncertain_reservation(self):
        repo = self.connect()
        def failing_execute():
            raise OSError("child status unknown")
        @managed_job
        def job(plan, repo, logs, *, execute=failing_execute):
            execute()
        with self.assertRaises(OSError):
            job(None, repo, None)
        self.assertTrue((managed_lock(repo, ".job.lock") / "uncertain").exists())
        self.assert_error("active_job", lambda: self.manager.checkout_branch("owner/repo", "feature/existing"))

    def test_reserved_child_validation_failure_releases_for_retry(self):
        from auto_coding.simple_run import run, main
        repo = self.connect()
        for failure in ("missing_plan", "missing_codex", "invalid_attempts"):
            with checkout_lock(managed_lock(repo)):
                token = reserve_job(repo)
            with patch.dict(os.environ, {"AUTOCODING_JOB_RESERVATION": token}):
                if failure == "missing_plan":
                    with self.assertRaises(OSError):
                        run(self.root / "missing.txt", repo, self.root / "logs")
                else:
                    argv = [str(self.root / "plan.txt"), "--repo", str(repo), "--logs", str(self.root / "logs")]
                    if failure == "invalid_attempts":
                        argv.extend(["--attempts", "0"])
                    with patch("auto_coding.simple_run.shutil.which", return_value=None):
                        with self.assertRaises(SystemExit) as error:
                            main(argv)
                        self.assertEqual(error.exception.code, 2)
            self.assertFalse(managed_lock(repo, ".job.lock").exists())
        self.manager.create_branch("owner/repo", "feature/new")

    def test_controller_bridge_and_legacy_logs(self):
        controller = DesktopController(self.manager.runs_dir)
        self.manager.active_check = controller.checkout_busy
        bridge = Bridge(controller=controller)
        with patch.object(controller, "_workspace_manager", return_value=self.manager):
            self.assertTrue(bridge.connect_github("owner/repo")["ok"])
            self.assertTrue(bridge.create_github_branch("owner/repo", "feature/new")["ok"])
            self.assertTrue(bridge.github_branches("owner/repo")["ok"])
        legacy = self.root / "legacy"
        legacy.mkdir()
        (legacy / "state.json").write_text('{"status":"stopped"}')
        self.assertTrue(controller.reopen(legacy)["ok"])
        self.assertEqual(controller.snapshot()["state"]["status"], "stopped")

    def test_portable_storage(self):
        with patch.dict(os.environ, {"AUTOCODING_CONFIG_DIR": str(self.root)}, clear=True):
            self.assertEqual(storage_root(), self.root / "AutoCoding")


if __name__ == "__main__":
    unittest.main()
