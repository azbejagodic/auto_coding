"""Run library tests use temporary evidence and mocked processes, never Codex/GUI."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, Mock

from auto_coding.desktop_app import Bridge
from auto_coding.desktop_control import DesktopController
from auto_coding.desktop_reader import read_evidence
from auto_coding.run_history import RunHistory, LIMIT


class RunHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        app_data = patch.dict(os.environ, {"LOCALAPPDATA": self.temp.name})
        app_data.start()
        self.addCleanup(app_data.stop)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.runs = self.root / "app" / "runs"
        self.runs.mkdir(parents=True)

    def run_folder(self, path, state=None):
        path.mkdir(parents=True)
        if state is not None:
            (path / "state.json").write_text(json.dumps(state), encoding="utf-8")
        (path / "raw.txt").write_text("<script>private rejection evidence</script>", encoding="utf-8")
        return path

    def bridge(self):
        return Bridge(controller=DesktopController(self.runs),
                      controller_factory=lambda: DesktopController(self.runs))

    def test_restart_discovery_external_import_and_no_git_or_log_changes(self):
        managed = self.run_folder(self.runs / "run-one", {"status": "completed", "completed": ["a"], "repo": "saved/repo"})
        external = self.run_folder(self.root / "old-runs" / "legacy", {"status": "stopped"})
        self.run_folder(self.root / "unregistered", {"status": "completed"})
        before = {p: p.read_bytes() for folder in (managed, external) for p in folder.iterdir()}
        with patch("auto_coding.desktop_control.Popen") as spawn, patch("auto_coding.desktop_control.subprocess.run") as git:
            bridge = self.bridge()
            entries = bridge.recent_runs()["runs"]
            self.assertEqual([e["path"] for e in entries], [str(managed)])
            self.assertEqual(entries[0]["completed"], 1)
            self.assertTrue(bridge.open_run(str(external))["ok"])
            fresh = self.bridge()
            self.assertEqual({e["path"] for e in fresh.recent_runs()["runs"]}, {str(managed), str(external)})
            self.assertTrue(fresh.open_recent_run(str(external))["ok"])
            snapshot = fresh.snapshot()
            self.assertEqual(snapshot["events"], [])
            self.assertEqual(snapshot["state"]["status"], "stopped")
            self.assertTrue(any(f["path"] == "raw.txt" for f in snapshot["files"]))
            self.assertIn("private rejection", fresh.evidence("raw.txt")["text"])
            spawn.assert_not_called()
            git.assert_not_called()
        self.assertEqual(before, {p: p.read_bytes() for p in before})
        index = (self.runs.parent / "recent-runs.json").read_text()
        self.assertNotIn("private rejection", index)
        self.assertNotIn("saved/repo", index)

    def test_missing_corrupt_and_forget_never_deletes_data(self):
        managed = self.run_folder(self.runs / "run-discovered")
        external = self.run_folder(self.root / "old")
        history = RunHistory(self.runs)
        history.list()  # Discovered managed paths survive disappearance too.
        history.register(external)
        renamed = external.with_name("renamed")
        external.rename(renamed)
        rows = {e["path"]: e for e in RunHistory(self.runs).list()["runs"]}
        self.assertEqual(rows[str(external)]["status"], "missing")
        (managed / "state.json").write_text("broken")
        bridge = self.bridge()
        rows = {e["path"]: e for e in bridge.recent_runs()["runs"]}
        self.assertEqual(rows[str(managed)]["status"], "unavailable")
        self.assertTrue(bridge.open_recent_run(str(managed))["ok"])
        self.assertIn("corrupt", bridge.snapshot()["state_error"])
        self.assertTrue(bridge.forget_run(str(managed))["ok"])
        self.assertTrue(bridge.forget_run(str(external))["ok"])
        self.assertEqual(self.bridge().recent_runs()["runs"], [])
        self.assertTrue((managed / "raw.txt").is_file())
        self.assertTrue((renamed / "raw.txt").is_file())
        self.assertTrue(self.bridge().open_run(str(managed))["ok"])
        self.assertEqual(len(self.bridge().recent_runs()["runs"]), 1)

    def test_history_totals_use_recorded_manifest_without_changing_logs(self):
        run = self.run_folder(self.runs / "with-manifest", {"status": "stopped", "completed": ["one"]})
        manifest = run / "run-manifest.json"
        manifest.write_text(json.dumps({"task_ids": ["one", "two", "three"]}), encoding="utf-8")
        before = {p: p.read_bytes() for p in run.iterdir()}
        row = RunHistory.summary(str(run))
        self.assertEqual((row["completed"], row["total"]), (1, 3))
        self.assertEqual(before, {p: p.read_bytes() for p in run.iterdir()})
        legacy = self.run_folder(self.runs / "legacy", {"status": "completed", "completed": ["one"]})
        self.assertIsNone(RunHistory.summary(str(legacy))["total"])

    def test_corrupt_index_and_atomic_write_failure_preserve_originals(self):
        history = RunHistory(self.runs)
        run = self.run_folder(self.runs / "run-ok")
        history.path.write_text("broken")
        self.assertFalse(history.register(run))
        self.assertIn("unavailable", history.list()["error"])
        self.assertEqual(history.path.read_text(), "broken")
        history.path.write_text('{"opened":[],"forgotten":[]}')
        original = history.path.read_bytes()
        with patch("auto_coding.run_history.os.replace", side_effect=OSError("disk failure")):
            self.assertFalse(history.register(run))
        self.assertEqual(history.path.read_bytes(), original)
        self.assertEqual(list(history.path.parent.glob("recent-runs-*.tmp")), [])
        self.assertTrue((run / "raw.txt").exists())

    def test_listing_is_bounded_and_never_reads_raw_or_timeline(self):
        for number in range(LIMIT + 3):
            self.run_folder(self.runs / f"run-{number}")
        original = Path.open
        def guarded(path, *args, **kwargs):
            self.assertNotIn(path.name, {"raw.txt", "events.jsonl"})
            return original(path, *args, **kwargs)
        with patch.object(Path, "open", guarded):
            self.assertEqual(len(RunHistory(self.runs).list()["runs"]), LIMIT)

    def test_full_index_discovers_newer_managed_run_and_retains_history_on_restart(self):
        history = RunHistory(self.runs)
        indexed = []
        for number in range(LIMIT):
            run = self.run_folder(self.root / "imported" / f"old-{number}")
            os.utime(run, ns=(1_000_000_000, 1_000_000_000))
            indexed.append(str(run))
        self.assertTrue(history._save({"opened": indexed, "forgotten": []}))
        new_run = self.run_folder(self.runs / "new-managed", {"status": "completed"})
        os.utime(new_run, ns=(2_000_000_000, 2_000_000_000))
        original_files = {p: p.read_bytes() for p in new_run.iterdir()}

        for instance in (history, RunHistory(self.runs)):
            rows = instance.list()["runs"]
            self.assertEqual(len(rows), LIMIT)
            self.assertEqual(rows[0]["path"], str(new_run))
            self.assertEqual(rows[0]["status"], "completed")
            saved = json.loads(history.path.read_text())
            self.assertEqual(set(saved["opened"]), {*indexed, str(new_run)})
            self.assertEqual(saved["forgotten"], [])

        self.assertEqual(original_files, {p: p.read_bytes() for p in original_files})
        self.assertTrue(all(Path(path).is_dir() for path in indexed))

    def test_invalid_indexed_path_does_not_block_new_managed_runs(self):
        history = RunHistory(self.runs)
        invalid = str(self.root / "invalid\0folder")
        self.assertTrue(history._save({"opened": [invalid], "forgotten": []}))
        run = self.run_folder(self.runs / "valid")
        rows = {row["path"]: row for row in history.list()["runs"]}
        self.assertIn(rows[invalid]["status"], {"missing", "unavailable"})
        self.assertEqual(rows[str(run)]["status"], "unknown")

    @patch.object(DesktopController, "_pid_active", return_value=True)
    def test_active_inspection_and_uncertain_switch_safeguards(self, pid_active):
        active = self.run_folder(self.root / "active", {"status": "running"})
        (active / "desktop-run.json").write_text(json.dumps({"repo": "saved/repo", "plan": "saved/plan", "status": "running", "pid": 456789}))
        other = self.run_folder(self.root / "other")
        with patch("auto_coding.desktop_control.Popen") as spawn:
            bridge = self.bridge()
            self.assertTrue(bridge.open_run(str(active))["ok"])
            self.assertTrue(bridge.job_status()["active"])
            self.assertTrue(bridge.evidence("raw.txt")["ok"])
            self.assertFalse(bridge.open_run(str(other))["ok"])
            self.assertFalse(bridge.start()["ok"])
            spawn.assert_not_called()
        (active / "desktop-run.json").write_text("broken")
        bridge = self.bridge()
        self.assertTrue(bridge.open_run(str(active))["ok"])
        self.assertTrue(bridge.job_status()["uncertain"])
        self.assertFalse(bridge.open_run(str(other))["ok"])

    def test_stale_desktop_status_preserves_terminal_outcome_after_restart(self):
        with patch("auto_coding.desktop_control.Popen") as spawn, patch("auto_coding.desktop_control.subprocess.run") as git:
            git.return_value = Mock(returncode=1, stdout="", stderr="fixture checkout unavailable")
            for terminal in ("completed", "stopped"):
                for desktop_status in ("running", "launching"):
                    with self.subTest(terminal=terminal, desktop_status=desktop_status):
                        run = self.run_folder(self.runs / f"{terminal}-{desktop_status}",
                                              {"status": terminal, "completed": ["task-one"]})
                        (run / "desktop-run.json").write_text(json.dumps({
                            "status": desktop_status, "pid": 456789,
                            "repo": str(self.root / "project"), "plan": str(self.root / "plan.md")
                        }), encoding="utf-8")
                        before = {p: p.read_bytes() for p in run.iterdir()}
                        with patch.object(DesktopController, "_pid_active", return_value=False):
                            self.bridge().recent_runs()
                            fresh = self.bridge()
                            row = next(row for row in fresh.recent_runs()["runs"] if row["path"] == str(run))
                            self.assertEqual(row["status"], terminal)
                            self.assertEqual(row["completed"], 1)
                            self.assertEqual(before, {p: p.read_bytes() for p in before})
                            self.assertTrue(fresh.open_recent_run(str(run))["ok"])
                            self.assertFalse(fresh.job_status()["active"])
                            self.assertFalse(fresh.job_status()["uncertain"])
                            self.assertEqual(fresh.job_status()["outcome"], terminal)
                        # Existing controller polling reconciles desktop process
                        # metadata; durable runner state and raw evidence stay intact.
                        for path in before:
                            if path.name != "desktop-run.json":
                                self.assertEqual(before[path], path.read_bytes())
            spawn.assert_not_called()
            for call in git.call_args_list:
                self.assertIn(call.args[0], (
                    ["git", "branch", "--show-current"],
                    ["git", "status", "--porcelain", "--untracked-files=all"],
                ))

    def test_history_status_keeps_active_and_uncertain_pid_precedence(self):
        run = self.run_folder(self.runs / "status-matrix")
        for durable in ("completed", "stopped", "running", None):
            for desktop_status in ("running", "launching"):
                for pid, active, expected in (
                    (456789, True, "running"),
                    (456789, False, durable if durable in {"completed", "stopped"} else "unknown"),
                    (None, False, "unknown"),
                    (True, False, "unknown"),
                    (-1, False, "unknown"),
                ):
                    with self.subTest(durable=durable, desktop_status=desktop_status, pid=pid, active=active):
                        (run / "state.json").write_text(json.dumps({"status": durable}), encoding="utf-8")
                        (run / "desktop-run.json").write_text(json.dumps({
                            "status": desktop_status, "pid": pid
                        }), encoding="utf-8")
                        with patch.object(DesktopController, "_pid_active", return_value=active) as probe:
                            self.assertEqual(RunHistory.summary(str(run))["status"], expected)
                            if type(pid) is int and pid > 0:
                                probe.assert_called_once_with(pid)
                            else:
                                probe.assert_not_called()

    @patch("auto_coding.desktop_control.Popen")
    def test_new_run_indexed_across_fresh_controller(self, spawn):
        controller = DesktopController(self.runs)
        controller.repo = self.root / "project"
        controller.plan = self.root / "plan.md"
        controller.plan.write_text("Implement useful logs")
        spawn.return_value = Mock(pid=1234)
        spawn.return_value.poll.return_value = None
        with patch.object(controller, "_validate"), patch("auto_coding.desktop_control.reserve_job", return_value=None):
            self.assertTrue(controller.start()["ok"])
        self.assertEqual(self.bridge().recent_runs()["runs"][0]["path"], str(controller.logs))

    def test_evidence_paths_and_symlink_escape(self):
        run = self.run_folder(self.runs / "run-safe")
        outside = self.run_folder(self.root / "outside")
        for path in (str(outside / "raw.txt"), "../outside/raw.txt", "C:raw.txt", "..\\outside\\raw.txt"):
            self.assertFalse(read_evidence(run, path)["ok"])
        try:
            (run / "escape.txt").symlink_to(outside / "raw.txt")
            (self.runs / "run-link").symlink_to(outside, target_is_directory=True)
        except OSError:
            self.skipTest("Symlink creation requires Windows developer mode or privilege")
        self.assertFalse(read_evidence(run, "escape.txt")["ok"])
        self.assertNotIn(str(self.runs / "run-link"), [e["path"] for e in self.bridge().recent_runs()["runs"]])

    def test_redirected_files_and_registered_roots_are_rejected_without_symlink_privileges(self):
        run = self.run_folder(self.runs / "run-safe")
        outside = self.run_folder(self.root / "outside")
        bridge = self.bridge()
        bridge.recent_runs()
        original = Path.resolve
        def redirected(path, *args, **kwargs):
            if path == run / "raw.txt":
                return outside / "raw.txt"
            return original(path, *args, **kwargs)
        with patch.object(Path, "resolve", redirected):
            self.assertFalse(read_evidence(run, "raw.txt")["ok"])
        def replaced_root(path, *args, **kwargs):
            return outside if path == run else original(path, *args, **kwargs)
        with patch.object(Path, "resolve", replaced_root):
            self.assertFalse(read_evidence(run, "raw.txt")["ok"])
            self.assertEqual(bridge.recent_runs()["runs"][0]["status"], "unavailable")
            self.assertFalse(bridge.open_recent_run(str(run))["ok"])

