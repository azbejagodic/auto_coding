import json
import hashlib
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
import sys
from unittest.mock import patch

REPOSITORY = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY / "src"))
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

    def test_branch_inventory_tracking_and_local_only_history(self):
        repo = self.connect()
        inventory = self.manager.branches("owner/repo")
        self.assertIn("origin/main", inventory["remote"])
        self.assertNotIn("origin/HEAD", inventory["remote"])
        self.assertEqual(inventory["head"], inventory["remote_head"])
        main = self.git(repo, "rev-parse", "main")
        self.manager.checkout_branch("owner/repo", "feature/existing")
        self.assertEqual(self.git(repo, "rev-parse", "--abbrev-ref", "@{upstream}"),
                         "origin/feature/existing")
        self.manager.create_branch("owner/repo", "feature/local", base="main", confirmed_base=True)
        approved = self.checkpoint(repo)
        sha = next(iter(approved))
        self.manager.checkout_branch("owner/repo", "feature/existing")
        self.manager.checkout_branch("owner/repo", "feature/local")
        inventory = self.manager.branches("owner/repo")
        self.assertEqual(inventory["head"], sha)
        self.assertIsNone(inventory["remote_head"])
        self.assertNotIn("origin/feature/local", inventory["remote"])
        self.assertEqual(self.git(repo, "rev-parse", "main"), main)
        self.assertFalse(any("push" in argv for argv in self.calls))

    def test_main_base_requires_confirmation_without_changing_refs(self):
        repo = self.connect()
        before = self.git(repo, "show-ref")
        self.assert_error("confirmation", lambda: self.manager.create_branch(
            "owner/repo", "feature/new", base="main"))
        self.assertEqual(self.git(repo, "show-ref"), before)
        self.assertEqual(self.git(repo, "branch", "--show-current"), "main")

    def test_subprocess_imports_this_checkout_from_fixture_directory(self):
        env = os.environ.copy()
        env["PYTHONPATH"] = str(REPOSITORY / "src")
        result = subprocess.run([sys.executable, "-B", "-c",
                                 "from auto_coding import github_workspace; print(github_workspace.__file__)"],
                                cwd=self.root, env=env, capture_output=True, text=True, check=True)
        self.assertEqual(Path(result.stdout.strip()).resolve(),
                         REPOSITORY / "src" / "auto_coding" / "github_workspace.py")

    def test_fetch_failure_and_dirty_fetch_preserve_checkout(self):
        repo = self.connect()
        self.manager.checkout_branch("owner/repo", "feature/existing")
        (repo / "file.txt").write_text("staged local work")
        self.git(repo, "add", "file.txt")
        (repo / "local.txt").write_text("untracked local work")
        before = self.git(repo, "status", "--porcelain")
        staged = self.git(repo, "diff", "--cached")
        refs = self.git(repo, "show-ref")
        for diagnostic, code in (("Could not resolve host SECRET", "network"),
                                 ("Authentication failed SECRET", "authentication")):
            def fail_fetch(argv, **kwargs):
                if argv[1] == "fetch":
                    return subprocess.CompletedProcess(argv, 1, "", diagnostic)
                return self.command(argv, **kwargs)
            with patch.object(self.manager, "run_command", side_effect=fail_fetch):
                with self.assertRaises(WorkspaceError) as caught:
                    self.manager.fetch("owner/repo")
                self.assertEqual(caught.exception.code, code)
                self.assertNotIn("SECRET", str(caught.exception))
            self.assertEqual(self.git(repo, "show-ref"), refs)
        self.git(self.seed, "branch", "feature/fetched")
        self.git(self.seed, "push", str(self.remote), "feature/fetched")
        self.assertIn("fetched", self.manager.fetch("owner/repo")["message"])
        self.assertIn("origin/feature/fetched", self.manager.branches("owner/repo")["remote"])
        self.assertEqual(self.git(repo, "status", "--porcelain"), before)
        self.assertEqual(self.git(repo, "diff", "--cached"), staged)
        self.assertEqual(self.git(repo, "branch", "--show-current"), "feature/existing")

    def test_each_approved_task_publishes_and_network_retry_skips_codex(self):
        from auto_coding import simple_run
        self.assertEqual(Path(simple_run.__file__).resolve(),
                         REPOSITORY / "src" / "auto_coding" / "simple_run.py")
        repo = self.connect()
        main = self.git(self.remote, "rev-parse", "main")
        self.manager.create_branch("owner/repo", "feature/auto", base="main", confirmed_base=True)
        plan, logs = self.root / "plan.json", self.root / "logs"
        plan.write_text(json.dumps({"tasks": [{"id": "one", "prompt": "First"},
                                               {"id": "two", "prompt": "Second"}]}))
        config = {"repository": "owner/repo", "branch": "feature/auto"}
        codex_calls = []
        def execute(argv, cwd, timeout, *, prompt=None):
            reviewing = "--output-schema" in argv
            second = "Second" in prompt
            codex_calls.append((second, reviewing))
            if second and not reviewing:
                # Publication must happen before the next task starts.
                self.assertEqual(self.git(self.remote, "rev-parse", config["branch"]),
                                 self.git(repo, "rev-parse", "HEAD"))
            out = Path(argv[argv.index("--output-last-message") + 1])
            if reviewing:
                out.write_text(json.dumps({"decision": "APPROVED", "issues": [], "summary": "OK"}))
            else:
                (cwd / ("two.txt" if second else "one.txt")).write_text("approved")
                out.write_text("done")
            return 0, "", False
        with patch("auto_coding.simple_run.GitHubWorkspaceManager", return_value=self.manager):
            self.assertEqual(simple_run.run(plan, repo, self.root / "success-logs",
                             auto_push=config, execute=execute), 0)
        self.assertEqual(len([argv for argv in self.calls if "push" in argv]), 2)
        self.assertEqual(self.git(self.remote, "rev-parse", config["branch"]),
                         self.git(repo, "rev-parse", "HEAD"))
        self.assertEqual(self.git(self.remote, "rev-parse", "main"), main)
        self.manager.create_branch("owner/repo", "feature/retry", base="main", confirmed_base=True)
        config["branch"] = "feature/retry"
        codex_calls.clear()
        def fail_second_push(argv, **kwargs):
            if "push" in argv and len([c for c in codex_calls if c[1]]) == 2:
                return subprocess.CompletedProcess(argv, 1, "", "Could not resolve host SECRET")
            return self.command(argv, **kwargs)
        with patch("auto_coding.simple_run.GitHubWorkspaceManager", return_value=self.manager):
            with patch.object(self.manager, "run_command", side_effect=fail_second_push):
                self.assertEqual(simple_run.run(plan, repo, logs, auto_push=config, execute=execute), 1)
            before = json.loads((logs / "state.json").read_text())
            sha = self.git(repo, "rev-parse", "HEAD")
            self.assertEqual(before["completed"], ["one", "two"])
            self.assertEqual(before["publication"]["outcome"], "pending")
            self.assertEqual(self.git(self.remote, "rev-parse", config["branch"]),
                             self.git(repo, "rev-parse", "HEAD^"))
            self.assertEqual(simple_run.run(plan, repo, logs, resume=True, auto_push=config,
                             execute=lambda *a, **kw: self.fail("Retry must not rerun Codex")), 0)
        after = json.loads((logs / "state.json").read_text())
        self.assertEqual(after["history"], before["history"])
        self.assertEqual(after["publication"]["outcome"], "published")
        self.assertEqual(self.git(self.remote, "rev-parse", config["branch"]), sha)
        self.assertEqual(self.git(self.remote, "rev-parse", "main"), main)
        self.assertEqual(self.git(repo, "status", "--porcelain"), "")

    def test_coordinator_checkpoint_publish_reconciles_remote_and_retries_identical_sha(self):
        repo = self.connect()
        self.manager.create_branch("owner/repo", "feature/auto", confirmed_base=True)
        approved = self.checkpoint(repo)
        sha = next(iter(approved))
        self.manager.publish_checkpoint(repo, "owner/repo", "feature/auto", sha, approved)
        self.assertEqual(self.git(self.remote, "rev-parse", "feature/auto"), sha)
        pushes = len([a for a in self.calls if "push" in a])
        self.manager.publish_checkpoint(repo, "owner/repo", "feature/auto", sha, approved)
        self.assertEqual(len([a for a in self.calls if "push" in a]), pushes)
        self.assert_error("checkpoint", lambda: self.manager.publish_checkpoint(
            repo, "owner/repo", "feature/other", sha, approved))

    def test_coordinator_checkpoint_rejects_unapproved_and_diverged_remote(self):
        repo = self.connect()
        self.manager.checkout_branch("owner/repo", "feature/existing")
        approved = self.checkpoint(repo)
        sha = next(iter(approved))
        self.assert_error("approval", lambda: self.manager.publish_checkpoint(
            repo, "owner/repo", "feature/existing", sha, {}))
        self.git(self.seed, "checkout", "feature/existing")
        (self.seed / "remote.txt").write_text("remote change")
        self.git(self.seed, "add", ".")
        self.git(self.seed, "commit", "-m", "remote")
        self.git(self.seed, "push", str(self.remote), "feature/existing")
        self.assert_error("non_fast_forward", lambda: self.manager.publish_checkpoint(
            repo, "owner/repo", "feature/existing", sha, approved))
        self.assertEqual(self.git(repo, "rev-parse", "HEAD"), sha)

    def test_published_checkpoint_reconciles_dirty_tree_without_pushing(self):
        repo = self.connect()
        self.manager.create_branch("owner/repo", "feature/auto", confirmed_base=True)
        approved = self.checkpoint(repo)
        sha = next(iter(approved))
        self.manager.publish_checkpoint(repo, "owner/repo", "feature/auto", sha, approved)
        (repo / "unfinished.txt").write_text("rejected staged changes")
        self.git(repo, "add", "unfinished.txt")
        (repo / "file.txt").write_text("unfinished unstaged changes")
        (repo / "untracked.txt").write_text("unfinished untracked changes")
        status = self.git(repo, "status", "--porcelain")
        patch_text = self.git(repo, "diff", "--cached", "--binary")
        self.calls.clear()
        self.manager.publish_checkpoint(repo, "owner/repo", "feature/auto", sha, approved)
        self.assertTrue(any("ls-remote" in argv for argv in self.calls))
        self.assertFalse(any("push" in argv for argv in self.calls))
        self.assertEqual(self.git(repo, "status", "--porcelain"), status)
        self.assertEqual(self.git(repo, "diff", "--cached", "--binary"), patch_text)
        self.assert_error("approval", lambda: self.manager.publish_checkpoint(
            repo, "owner/repo", "feature/auto", sha, {}))

    def test_unpublished_checkpoint_still_requires_clean_tree_before_push(self):
        repo = self.connect()
        self.manager.create_branch("owner/repo", "feature/auto", confirmed_base=True)
        approved = self.checkpoint(repo)
        sha = next(iter(approved))
        (repo / "unfinished.txt").write_text("unreviewed")
        self.git(repo, "add", "unfinished.txt")
        self.calls.clear()
        self.assert_error("dirty", lambda: self.manager.publish_checkpoint(
            repo, "owner/repo", "feature/auto", sha, approved))
        self.assertFalse(any("push" in argv for argv in self.calls))
        self.assertEqual(self.git(repo, "rev-parse", "HEAD"), sha)
        self.assertIn("unfinished.txt", self.git(repo, "diff", "--cached", "--name-only"))

    def test_auto_push_resume_repairs_rejected_second_task_after_first_published(self):
        from auto_coding.simple_run import run
        repo = self.connect()
        self.manager.create_branch("owner/repo", "feature/auto", confirmed_base=True)
        plan, logs = self.root / "plan.json", self.root / "logs"
        plan.write_text(json.dumps({"tasks": [
            {"id": "first", "prompt": "Make first file"},
            {"id": "second", "prompt": "Make second file"}]}))
        config = {"repository": "owner/repo", "branch": "feature/auto"}
        repairing = False
        calls = []
        def fake(argv, cwd, timeout, *, prompt=None):
            second = "Make second file" in prompt
            reviewing = "--output-schema" in argv
            calls.append((second, reviewing))
            out = Path(argv[argv.index("--output-last-message") + 1])
            if reviewing:
                rejected = second and not repairing
                out.write_text(json.dumps({
                    "decision": "REJECTED" if rejected else "APPROVED",
                    "issues": ["repair second task"] if rejected else [], "summary": "review"}))
            else:
                if repairing:
                    self.assertTrue(second)
                    self.assertIn("second.txt", self.git(repo, "diff", "--cached", "--name-only"))
                    self.assertEqual((repo / "second.txt").read_text(), "needs repair")
                    state = json.loads((logs / "state.json").read_text())
                    self.assertEqual(state["publication"]["outcome"], "published")
                    self.assertEqual(state["publication"]["commit"], first_sha)
                    self.assertEqual(self.git(self.remote, "rev-parse", "feature/auto"), first_sha)
                (cwd / ("second.txt" if second else "first.txt")).write_text(
                    "repaired" if repairing else "needs repair")
                out.write_text("done")
            return 0, "stub events", False

        with patch("auto_coding.simple_run.GitHubWorkspaceManager", return_value=self.manager):
            self.assertEqual(run(plan, repo, logs, attempts=1, auto_push=config, execute=fake), 1)
            before = json.loads((logs / "state.json").read_text())
            self.assertEqual(before["completed"], ["first"])
            self.assertEqual(before["publication"]["outcome"], "published")
            first_sha = before["history"][0]["commit"]
            self.assertEqual(self.git(self.remote, "rev-parse", "feature/auto"), first_sha)
            self.assertIn("second.txt", self.git(repo, "diff", "--cached", "--name-only"))
            repairing = True
            calls.clear()
            self.calls.clear()
            self.assertEqual(run(plan, repo, logs, resume=True, attempts=2,
                                 auto_push=config, execute=fake), 0)
        after = json.loads((logs / "state.json").read_text())
        self.assertEqual(calls, [(True, False), (True, True)])
        self.assertEqual(after["completed"], ["first", "second"])
        self.assertEqual(after["history"][0]["commit"], first_sha)
        self.assertEqual(after["history"][2]["attempt"], 2)
        self.assertEqual(after["publication"]["outcome"], "published")
        second_sha = after["history"][2]["commit"]
        self.assertEqual(self.git(repo, "rev-parse", "HEAD^"), first_sha)
        self.assertEqual(self.git(self.remote, "rev-parse", "feature/auto"), second_sha)
        self.assertEqual(len([argv for argv in self.calls if "push" in argv]), 1)
        self.assertEqual(self.git(repo, "status", "--porcelain"), "")
        events = [json.loads(line) for line in (logs / "events.jsonl").read_text().splitlines()]
        pushes = [e for e in events if e["event"] == "push_finished"]
        self.assertEqual([e["metadata"]["commit"] for e in pushes], [first_sha, first_sha, second_sha])
        self.assertTrue(all(e["outcome"] == "success" and e["timestamp_utc"]
                            and e["metadata"]["remote"] == "confirmed" for e in pushes))

    def test_coordinator_transient_authentication_and_rejection_preserve_commit(self):
        repo = self.connect()
        self.manager.create_branch("owner/repo", "feature/retry", confirmed_base=True)
        approved = self.checkpoint(repo)
        sha = next(iter(approved))
        for diagnostic, code in (("permission denied SECRET_TOKEN", "authentication"),
                                 ("[rejected] fetch first SECRET_TOKEN", "non_fast_forward"),
                                 ("could not resolve host SECRET_TOKEN", "network"),
                                 (None, "network")):
            def fail_push(argv, **kwargs):
                if "push" in argv:
                    if diagnostic is None:
                        raise subprocess.TimeoutExpired(argv, 120)
                    return subprocess.CompletedProcess(argv, 1, "", diagnostic)
                return self.command(argv, **kwargs)
            self.manager.run_command = fail_push
            with self.assertRaises(WorkspaceError) as caught:
                self.manager.publish_checkpoint(repo, "owner/repo", "feature/retry", sha, approved)
            self.assertEqual(caught.exception.code, code)
            self.assertNotIn("SECRET_TOKEN", str(caught.exception))
            self.assertEqual(self.git(repo, "rev-parse", "HEAD"), sha)
        self.manager.run_command = self.command
        self.manager.publish_checkpoint(repo, "owner/repo", "feature/retry", sha, approved)
        self.assertEqual(self.git(self.remote, "rev-parse", "feature/retry"), sha)

    def assert_error(self, code, fn):
        with self.assertRaises(WorkspaceError) as caught:
            fn()
        self.assertEqual(caught.exception.code, code)

    def checkpoint(self, repo, filename="approved.txt"):
        parent = self.git(repo, "rev-parse", "HEAD")
        (repo / filename).write_text("approved change", encoding="utf-8")
        self.git(repo, "config", "user.name", "Test")
        self.git(repo, "config", "user.email", "test@example.invalid")
        self.git(repo, "add", "--", filename)
        patch_text = subprocess.run(["git", "diff", "--cached", "--binary", "--no-ext-diff"], cwd=repo,
                                    capture_output=True, text=True, encoding="utf-8", check=True).stdout
        self.git(repo, "commit", "-m", "approved checkpoint")
        commit = self.git(repo, "rev-parse", "HEAD")
        return {commit: {"commit": commit, "base_head": parent,
                         "diff_sha256": hashlib.sha256(patch_text.encode()).hexdigest(),
                         "review": {"decision": "APPROVED", "issues": []}}}

    def test_push_new_branch_and_non_force_explicit_destination(self):
        repo = self.connect()
        self.manager.create_branch("owner/repo", "feature/publish", confirmed_base=True)
        approved = self.checkpoint(repo)
        self.git(repo, "config", "push.followTags", "true")
        self.git(repo, "config", "remote.origin.mirror", "true")
        self.git(repo, "tag", "-a", "local-only", "-m", "must remain local")
        self.assertTrue(self.manager.push_eligibility("owner/repo", approved)["eligible"])
        result = self.manager.push_approved("owner/repo", approved)
        self.assertTrue(result["published"])
        self.assertEqual(result["sha"], next(iter(approved)))
        self.assertIn(result["branch"], result["message"])
        self.assertIn(result["sha"], result["message"])
        self.assertEqual(self.git(self.remote, "rev-parse", "feature/publish"), next(iter(approved)))
        pushes = [argv for argv in self.calls if "push" in argv]
        self.assertEqual(pushes, [["git", "-c", "core.hooksPath=" + os.devnull,
                                  "-c", "push.followTags=false", "-c", "push.recurseSubmodules=no",
                                  "-c", "remote.origin.mirror=false", "-c", "push.gpgSign=false",
                                  "push", "origin", "HEAD:refs/heads/feature/publish"]])
        self.assertEqual(self.git(self.remote, "for-each-ref", "refs/tags/"), "")
        self.assertEqual(self.git(self.remote, "rev-parse", "main"), self.git(self.seed, "rev-parse", "main"))
        self.assert_error("approval", lambda: self.manager.push_eligibility("owner/repo", approved))

    def test_push_blocks_dirty_active_main_origin_unapproved_and_changed_bytes(self):
        repo = self.connect()
        self.assert_error("invalid_branch", lambda: self.manager.push_eligibility("owner/repo", {}))
        self.manager.create_branch("owner/repo", "feature/publish", confirmed_base=True)
        approved = self.checkpoint(repo)
        self.assert_error("approval", lambda: self.manager.push_approved("owner/repo", {}))
        record = next(iter(approved.values()))
        original = record["diff_sha256"]
        record["diff_sha256"] = "0" * 64
        self.assert_error("approval", lambda: self.manager.push_approved("owner/repo", approved))
        record["diff_sha256"] = original
        (repo / "dirty.txt").write_text("dirty")
        self.assert_error("dirty", lambda: self.manager.push_approved("owner/repo", approved))
        self.git(repo, "add", "dirty.txt")
        self.assert_error("dirty", lambda: self.manager.push_approved("owner/repo", approved))
        self.git(repo, "reset", "--", "dirty.txt")
        (repo / "dirty.txt").unlink()
        self.manager.active_check = lambda path: None
        self.assert_error("active_job", lambda: self.manager.push_approved("owner/repo", approved))
        self.manager.active_check = None
        self.git(repo, "remote", "set-url", "--push", "origin", "https://github.com/other/repo.git")
        self.assert_error("origin_mismatch", lambda: self.manager.push_approved("owner/repo", approved))
        self.assertFalse(any("push" in argv for argv in self.calls))

    def test_push_rechecks_remote_and_rejects_divergence(self):
        repo = self.connect()
        self.manager.checkout_branch("owner/repo", "feature/existing")
        approved = self.checkpoint(repo)
        self.git(self.seed, "switch", "feature/existing")
        (self.seed / "remote.txt").write_text("remote change")
        self.git(self.seed, "add", ".")
        self.git(self.seed, "commit", "-m", "remote change")
        self.git(self.seed, "push", str(self.remote), "feature/existing")
        self.assert_error("non_fast_forward", lambda: self.manager.push_approved("owner/repo", approved))
        self.assertFalse(any("push" in argv for argv in self.calls))
        with patch.object(self.manager, "run_command", return_value=subprocess.CompletedProcess([], 1, "", "[rejected] non-fast-forward SECRET")):
            with self.assertRaises(WorkspaceError) as caught:
                self.manager._git(repo, "push", "origin", "HEAD:refs/heads/feature/existing")
            self.assertEqual(caught.exception.code, "non_fast_forward")
            self.assertNotIn("SECRET", str(caught.exception))

    def test_push_rejects_sensitive_and_generated_checkpoint_names(self):
        for filename in (".env.production", "credentials.json", "cache.pyc"):
            repo = self.connect()
            branch = "feature/" + filename.replace(".", "x")
            self.manager.create_branch("owner/repo", branch, confirmed_base=True)
            approved = self.checkpoint(repo, filename)
            self.assert_error("sensitive", lambda: self.manager.push_approved("owner/repo", approved))
        self.assertFalse(any("push" in argv for argv in self.calls))

    def test_controller_push_eligibility_uses_durable_approval_and_reopens_identity(self):
        from unittest.mock import Mock
        controller = DesktopController(self.manager.runs_dir)
        self.manager.active_check = controller.checkout_busy
        controller._workspace_manager = lambda: self.manager
        bridge = Bridge(controller=controller, dialogs=Mock())
        self.assertTrue(bridge.connect_github("owner/repo")["ok"])
        self.assertTrue(bridge.create_github_branch("owner/repo", "feature/checkpoint", confirmed_base=True)["ok"])
        approved = self.checkpoint(controller.repo)
        logs = controller.runs_dir / "run-fixture"
        logs.mkdir(parents=True)
        state = {"repo": str(controller.repo), "status": "completed", "history": list(approved.values())}
        (logs / "state.json").write_text(json.dumps(state))
        plan = self.root / "task.md"
        plan.write_text("task")
        (logs / "desktop-run.json").write_text(json.dumps({"repo": str(controller.repo), "plan": str(plan), "status": "stopped"}))
        self.assertTrue(bridge.job_status()["push"]["eligible"])
        recovered = DesktopController(controller.runs_dir)
        recovered._workspace_manager = lambda: self.manager
        self.assertTrue(recovered.reopen(logs)["ok"])
        self.assertEqual(recovered.repository, "owner/repo")
        self.assertTrue(bridge.push_approved_commits()["published"])
        self.assertFalse(bridge.job_status()["push"]["eligible"])
        legacy = self.root / "old-logs"
        legacy.mkdir()
        self.assertTrue(recovered.reopen(legacy)["ok"])
        self.assertIsNone(recovered.repository)

    def test_bridge_managed_selection_confirmation_start_and_resume_identity(self):
        from unittest.mock import Mock
        controller = DesktopController(self.root / "runs")
        self.manager.runs_dir = controller.runs_dir
        self.manager.active_check = controller.checkout_busy
        controller._workspace_manager = lambda: self.manager
        dialogs = Mock()
        bridge = Bridge(controller=controller, dialogs=dialogs)
        self.assertFalse(bridge.github_repositories()["available"])
        with patch("auto_coding.desktop_control.Popen") as popen:
            self.assertTrue(bridge.connect_github("https://github.com/Owner/Repo.git")["ok"])
            self.assertTrue(bridge.checkout_github_branch("owner/repo", "feature/existing")["ok"])
            self.assertTrue(bridge.create_github_branch("owner/repo", "feature/desktop", confirmed_base=True)["ok"])
            self.assertFalse(bridge.checkout_github_branch("other/repo", "feature/existing")["ok"])
            repo = controller.repo
            (repo / "dirty").write_text("dirty")
            self.assertFalse(bridge.checkout_github_branch("owner/repo", "feature/existing")["ok"])
            (repo / "dirty").unlink()
            plan = self.root / "task.md"
            plan.write_text("Implement one task")
            self.assertTrue(bridge.select_plan(str(plan))["ok"])
            popen.assert_not_called()
            dialogs.confirm.return_value = False
            self.assertTrue(bridge.start(4)["cancelled"])
            popen.assert_not_called()
            dialogs.confirm.return_value = True
            popen.return_value = Mock(pid=424242)
            popen.return_value.poll.return_value = None
            self.assertTrue(bridge.start(4)["ok"])
            argv = popen.call_args.args[0]
            self.assertEqual(argv[argv.index("--repo") + 1], str(repo))
            self.assertEqual(argv[-1], str(plan))
            self.assertEqual(argv[argv.index("--attempts") + 1], "4")
            confirmation = dialogs.confirm.call_args.args[0]
            for text in ("owner/repo", "feature/desktop", str(plan), "Attempts: 4", "NO automatic main merge"):
                self.assertIn(text, confirmation)
            self.assertFalse(bridge.create_github_branch("owner/repo", "feature/blocked", confirmed_base=True)["ok"])
            self.assertFalse(bridge.push_approved_commits()["ok"])

    def test_master_only_remote_confirmed_base_reaches_managed_start(self):
        from unittest.mock import Mock
        self.git(self.remote, "branch", "-m", "main", "master")
        self.git(self.remote, "branch", "-D", "feature/existing")
        controller = DesktopController(self.manager.runs_dir)
        self.manager.active_check = controller.checkout_busy
        controller._workspace_manager = lambda: self.manager
        dialogs = Mock()
        dialogs.confirm.return_value = True
        bridge = Bridge(controller=controller, dialogs=dialogs)
        with patch("auto_coding.desktop_control.Popen") as popen:
            self.assertTrue(bridge.connect_github("https://github.com/Owner/Repo.git")["ok"])
            status = bridge.job_status()
            self.assertEqual(status["github"]["remote"], ["origin/master"])
            self.assertFalse(status["start_ready"])
            rejected = bridge.create_github_branch("owner/repo", "feature/master-base", "master", False)
            self.assertEqual(rejected["code"], "confirmation")
            self.assertEqual(self.git(controller.repo, "branch", "--show-current"), "master")
            self.assertTrue(bridge.create_github_branch("owner/repo", "feature/master-base", "master", True)["ok"])
            self.assertEqual(self.git(controller.repo, "rev-parse", "HEAD"), self.git(self.remote, "rev-parse", "master"))
            plan = self.root / "master-plan.md"
            plan.write_text("Implement one task", encoding="utf-8")
            self.assertTrue(bridge.select_plan(str(plan))["ok"])
            self.assertTrue(bridge.job_status()["start_ready"])
            popen.assert_not_called()
            popen.return_value = Mock(pid=424242)
            popen.return_value.poll.return_value = None
            self.assertTrue(bridge.start(2)["ok"])
            argv = popen.call_args.args[0]
            self.assertEqual(argv[argv.index("--repo") + 1], str(controller.repo))
            self.assertEqual(argv[-1], str(plan))
            self.assertIn("Branch: feature/master-base", dialogs.confirm.call_args.args[0])

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
        result = self.manager.create_branch("owner/repo", "feature/new", confirmed_base=True)
        self.assertEqual(result, {"branch": "feature/new", "clean": True})
        self.assertEqual(self.git(repo, "rev-parse", "HEAD"), self.git(self.seed, "rev-parse", "HEAD"))
        self.assertNotEqual(self.git(repo, "rev-parse", "main"), self.git(repo, "rev-parse", "HEAD"))
        self.assert_error("branch_exists", lambda: self.manager.create_branch("owner/repo", "feature/new", confirmed_base=True))
        self.assert_error("branch_exists", lambda: self.manager.create_branch("owner/repo", "feature/existing", confirmed_base=True))

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
        self.assertEqual(self.manager.create_branch("owner/repo", created, confirmed_base=True), {"branch": created, "clean": True})
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
        self.assert_error("git_operation", lambda: self.manager.create_branch("owner/repo", "feature/new", confirmed_base=True))
        (repo / ".git" / "MERGE_HEAD").unlink()
        for active in (True, None):
            self.manager.active_check = lambda path: active
            self.assert_error("active_job", lambda: self.manager.create_branch("owner/repo", "feature/new", confirmed_base=True))
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
                self.manager.create_branch("owner/repo", name, confirmed_base=True)
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
            self.assert_error("active_job", lambda: self.manager.create_branch("owner/repo", "feature/new", confirmed_base=True))
        job(None, repo, None)
        self.manager.create_branch("owner/repo", "feature/new", confirmed_base=True)

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
        self.assert_error("dirty", lambda: self.manager.create_branch("owner/repo", "feature/new", confirmed_base=True))

    def test_deleted_base_cannot_use_stale_remote_ref(self):
        repo = self.connect()
        self.git(self.remote, "config", "receive.denyDeleteCurrent", "ignore")
        self.git(self.seed, "push", str(self.remote), ":main")
        self.assert_error("git", lambda: self.manager.create_branch("owner/repo", "feature/new", confirmed_base=True))
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
                lambda: manager.create_branch("owner/repo", "feature/new", confirmed_base=True),
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
                                operation(*args, **({"confirmed_base": True} if action == "create_branch" else {}))
                            self.assertEqual(caught.exception.code, "network")
                            self.assertNotIn("SECRET", str(caught.exception))
                        else:
                            with self.assertRaises(KeyboardInterrupt):
                                operation(*args, **({"confirmed_base": True} if action == "create_branch" else {}))
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
        self.assert_error("active_job", lambda: self.manager.create_branch("owner/repo", "feature/new", confirmed_base=True))

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
        self.assert_error("active_job", lambda: self.manager.create_branch("owner/repo", "feature/new", confirmed_base=True))
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
        self.assert_error("active_job", lambda: independent.create_branch("owner/repo", "feature/new", confirmed_base=True))
        @managed_job
        def child(plan, repo, logs):
            self.assertFalse((job_path / "reservation").exists())
            self.assert_error("active_job", lambda: independent.checkout_branch("owner/repo", "feature/existing"))
            # Even a duplicate with the same inherited token cannot claim again.
            self.assert_error("busy", lambda: child(plan, repo, logs))
        with patch.dict(os.environ, child_env):
            child(None, repo, None)
        self.assertFalse(job_path.exists())
        independent.create_branch("owner/repo", "feature/new", confirmed_base=True)

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
        self.manager.create_branch("owner/repo", "feature/new", confirmed_base=True)

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
        self.manager.create_branch("owner/repo", "feature/new", confirmed_base=True)
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
        self.manager.create_branch("owner/repo", "feature/new", confirmed_base=True)

    def test_controller_bridge_and_legacy_logs(self):
        controller = DesktopController(self.manager.runs_dir)
        self.manager.active_check = controller.checkout_busy
        bridge = Bridge(controller=controller)
        with patch.object(controller, "_workspace_manager", return_value=self.manager):
            self.assertTrue(bridge.connect_github("owner/repo")["ok"])
            self.assertTrue(bridge.create_github_branch("owner/repo", "feature/new", confirmed_base=True)["ok"])
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
