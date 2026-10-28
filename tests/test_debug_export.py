"""GUI/network/Codex-free export acceptance and security checks."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import zipfile

from auto_coding import debug_export as de
from auto_coding.desktop_app import Bridge, NativeDialogs


class DebugExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1])
        app_data = patch.dict(os.environ, {"LOCALAPPDATA": self.temp.name})
        app_data.start()
        self.addCleanup(app_data.stop)
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        # Keep fixtures inside this workspace while modelling an external Save As
        # directory. Hide only the enclosing real checkout marker, never fixture
        # markers; production destination validation is otherwise exercised intact.
        real_lexists = os.path.lexists
        enclosing_markers = {p / ".git" for p in self.base.parents}
        marker_patch = patch.object(de.os.path, "lexists", side_effect=lambda p:
                                   False if Path(p) in enclosing_markers else real_lexists(p))
        marker_patch.start()
        self.addCleanup(marker_patch.stop)
        self.run = self.base / "run-1"
        self.run.mkdir()
        self.out = self.base / "report.zip"
        self.secret = "sk-private-DO-NOT-EXPORT"
        self.write("state.json", {"run_id": "run-1", "status": "stopped", "current_task": "task-1",
            "created_utc": "2026-01-01T00:00:00+00:00", "completed": ["task-0"],
            "repo": self.secret, "feedback": self.secret,
            "history": [{"task": "task-1", "attempt": 1, "feedback": self.secret,
                         "tests": [{"index": 1, "exit_code": 2, "timed_out": False, "output": self.secret}],
                         "review": {"decision": "REJECTED", "summary": self.secret}}]})
        self.write("run-manifest.json", {"run_id": "run-1", "task_ids": ["task-0", "task-1"],
                   "start_time_utc": "2026-01-01T00:00:00Z", "prompt": self.secret})
        self.write("events.jsonl", {"actor": "tests", "event": "test_finished", "outcome": "failed",
                   "timestamp_utc": "2026-01-01T00:01:00Z", "task_id": "task-1", "seq": 1,
                   "metadata": {"exit_code": 2, "output": self.secret}, "evidence": {"output": "../../outside"}})
        (self.run / "task-1-01-a-prompt.txt").write_text(self.secret)
        self.write("task-1-01-b-verdict.json", {"decision": "REJECTED", "summary": self.secret})

    def write(self, name, record):
        (self.run / name).write_text(json.dumps(record), encoding="utf-8")

    def snapshot(self):
        return {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.run.iterdir() if p.is_file()}

    def report(self, path=None):
        with zipfile.ZipFile(path or self.out) as archive:
            self.assertIsNone(archive.testzip())
            return json.loads(archive.read("debug-report.json"))

    def test_standard_reopens_has_useful_results_and_no_free_form_payloads_or_mutation(self):
        before = self.snapshot()
        result = de.export_debug(self.run, self.out)
        self.assertTrue(result["ok"])
        with zipfile.ZipFile(self.out) as archive:
            self.assertEqual(archive.namelist(), ["debug-report.json"])
            self.assertNotIn(self.secret.encode(), archive.read("debug-report.json"))
        report = self.report()
        state = report["summaries"]["state.json"]
        self.assertEqual(state["status"], "stopped")
        self.assertEqual(state["history"][0]["tests"][0]["exit_code"], 2)
        self.assertEqual(state["history"][0]["review"]["decision"], "REJECTED")
        self.assertEqual(report["summaries"]["events.jsonl"][0]["actor"], "tests")
        self.assertEqual(report["summaries"]["run-manifest.json"]["task_ids"], ["task-0", "task-1"])
        self.assertIn("Inspect before sharing", result["warning"])
        self.assertEqual(before, self.snapshot())

    def test_raw_only_selected_run_and_allowed_files_no_mutation(self):
        other = self.base / "run-2"
        other.mkdir()
        (other / "state.json").write_text("OTHER RUN")
        before = self.snapshot()
        de.export_debug(self.run, self.out, "raw")
        with zipfile.ZipFile(self.out) as archive:
            self.assertIn("raw/task-1-01-a-prompt.txt", archive.namelist(), self.report()["inventory"])
            self.assertEqual(archive.read("raw/task-1-01-a-prompt.txt").decode(), self.secret)
            self.assertFalse(any(b"OTHER RUN" in archive.read(n) for n in archive.namelist()))
        self.report()
        self.assertEqual(before, self.snapshot())

    def test_sensitive_paths_arbitrary_sources_and_zip_slip_names_excluded(self):
        for name in (".env", ".env.local", "credentials.json", "id_rsa", "private.pem", ".token",
                     "source.py", "C:evil.txt", "..evil", "state.json:stream"):
            if os.name == "nt" and ":" in name:
                continue
            (self.run / name).write_text(self.secret)
        (self.run / ".git").mkdir()
        (self.run / ".git" / "config").write_text(self.secret)
        (self.run / "source").mkdir()
        (self.run / "source" / "state.json").write_text(self.secret)
        if os.name != "nt":
            (self.run / "..\\evil.txt").write_text(self.secret)
        for mode in ("standard", "raw"):
            output = self.base / (mode + ".zip")
            de.export_debug(self.run, output, mode)
            with zipfile.ZipFile(output) as archive:
                self.assertTrue(all(n == "debug-report.json" or n.startswith("raw/") for n in archive.namelist()))
                self.assertFalse(any(".." in Path(n).parts or "\\" in n or ":" in n for n in archive.namelist()))
                self.assertNotIn("raw/credentials.json", archive.namelist())
                self.assertNotIn("raw/source.py", archive.namelist())
            self.assertTrue(any("Not an allowed" in r.get("reason", "") for r in self.report(output)["inventory"]))

    def test_links_and_hardlinks_not_read(self):
        external = self.base / "external.txt"
        external.write_text("EXTERNAL")
        link = self.run / "task-1-01-a-answer.txt"
        try:
            link.symlink_to(external)
        except OSError:
            self.skipTest("Symlink privileges unavailable")
        os.link(external, self.run / "task-1-01-diff.txt")
        de.export_debug(self.run, self.out, "raw")
        with zipfile.ZipFile(self.out) as archive:
            self.assertNotIn("raw/" + link.name, archive.namelist())
            self.assertNotIn("raw/task-1-01-diff.txt", archive.namelist())
        self.assertTrue(any("Nonregular" in r.get("reason", "") for r in self.report()["inventory"]))

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO unavailable")
    def test_fifo_never_opened(self):
        os.mkfifo(self.run / "task-1-01-diff.txt")
        de.export_debug(self.run, self.out, "raw")
        self.assertTrue(any("Nonregular" in r.get("reason", "") for r in self.report()["inventory"]))

    def test_hardlink_escape_rejected_without_symlink_privileges(self):
        external = self.base / "outside.txt"
        external.write_text("EXTERNAL SECRET")
        os.link(external, self.run / "task-1-01-diff.txt")
        de.export_debug(self.run, self.out, "raw")
        with zipfile.ZipFile(self.out) as archive:
            self.assertNotIn("raw/task-1-01-diff.txt", archive.namelist())

    def test_redirected_allowed_file_is_not_read_and_regular_check_rejects_reparse(self):
        from types import SimpleNamespace
        self.assertFalse(de._regular(SimpleNamespace(st_mode=0o100600, st_nlink=1, st_file_attributes=0x400)))
        original = Path.resolve
        external = self.base / "outside.txt"
        external.write_text("EXTERNAL SECRET")
        def redirected(path, *args, **kwargs):
            if path == self.run / "state.json":
                return external
            return original(path, *args, **kwargs)
        with patch.object(Path, "resolve", redirected):
            de.export_debug(self.run, self.out, "raw")
        with zipfile.ZipFile(self.out) as archive:
            self.assertNotIn("raw/state.json", archive.namelist())
            self.assertFalse(any(b"EXTERNAL SECRET" in archive.read(n) for n in archive.namelist()))

    def test_structured_limits_bad_types_and_torn_timeline_are_visible(self):
        self.write("state.json", {"status": self.secret, "task_ids": ["task-1"] * 501,
                                "history": [{"tests": "invalid", "review": self.secret}]})
        (self.run / "events.jsonl").write_text('{"event":"test_started"}\n{torn')
        de.export_debug(self.run, self.out)
        report = self.report()
        self.assertEqual(len(report["summaries"]["state.json"]["task_ids"]), 500)
        self.assertTrue(report["summaries"]["state.json"]["task_ids_truncated"])
        self.assertNotIn("status", report["summaries"]["state.json"])
        self.assertTrue(any(r["name"] == "events.jsonl" and r["action"] == "omitted" for r in report["inventory"]))
        self.assertNotIn(self.secret, json.dumps(report))

    def test_run_identity_comes_from_metadata_not_directory_label(self):
        self.write("run-manifest.json", {"run_id": "recorded-uuid", "task_ids": ["task-1"]})
        de.export_debug(self.run, self.out)
        self.assertEqual(self.report()["run_id"], "recorded-uuid")
        self.assertEqual(self.report()["directory_label"], "run-1")
        self.assertEqual(self.report()["status"], "stopped")

    def test_huge_file_and_total_budget_omissions(self):
        with (self.run / "task-1-01-diff.txt").open("wb") as stream:
            stream.truncate(de.MAX_FILE + 1)
        de.export_debug(self.run, self.out, "raw")
        self.assertTrue(any("Individual file" in r.get("reason", "") for r in self.report()["inventory"]))
        with patch.object(de, "MAX_TOTAL", de.METADATA_BUDGET + 1):
            de.export_debug(self.run, self.base / "small.zip", "raw")
        self.assertTrue(any("Total input" in r.get("reason", "") for r in self.report(self.base / "small.zip")["inventory"]))

    def test_entry_limit_failure_cleans_temporary_and_keeps_logs(self):
        before = self.snapshot()
        with patch.object(de, "MAX_ENTRIES", 2):
            with self.assertRaisesRegex(ValueError, "entry limit"):
                de.export_debug(self.run, self.out)
        self.assertFalse(self.out.exists())
        self.assertEqual(list(self.base.glob("debug-export-*.tmp")), [])
        self.assertEqual(before, self.snapshot())

    def test_missing_corrupt_and_disappearing_files_visible(self):
        (self.run / "run-manifest.json").unlink()
        (self.run / "state.json").write_text("{torn")
        real_read = de._read
        def disappearing(root, name, root_id):
            if name == "events.jsonl":
                raise FileNotFoundError("concurrent replacement")
            return real_read(root, name, root_id)
        with patch.object(de, "_read", side_effect=disappearing):
            de.export_debug(self.run, self.out)
        inventory = {r["name"]: r for r in self.report()["inventory"]}
        self.assertIn("Missing", inventory["run-manifest.json"]["reason"])
        self.assertIn("invalid", inventory["state.json"]["reason"])
        self.assertIn("FileNotFoundError", inventory["events.jsonl"]["reason"])

    def test_detects_concurrent_append_without_including_partial_bytes(self):
        real_fstat = os.fstat
        calls = 0
        def racing(fd):
            nonlocal calls
            calls += 1
            if calls == 2:
                with (self.run / "state.json").open("a") as stream:
                    stream.write(" ")
            return real_fstat(fd)
        root_id = de._root_identity(self.run)
        with patch.object(de.os, "fstat", side_effect=racing):
            with self.assertRaisesRegex(ValueError, "changed during"):
                de._read(self.run, "state.json", root_id)

    def test_missing_run_and_invalid_mode_fail_without_zip(self):
        with self.assertRaises(OSError):
            de.export_debug(self.base / "missing", self.out)
        with self.assertRaises(ValueError):
            de.export_debug(self.run, self.out, "other")
        self.assertFalse(self.out.exists())

    def test_no_overwrite_including_publication_race_and_cleanup(self):
        self.out.write_bytes(b"existing")
        with self.assertRaisesRegex(ValueError, "already exists"):
            de.export_debug(self.run, self.out)
        self.assertEqual(self.out.read_bytes(), b"existing")
        self.out.unlink()
        real_link = os.link
        def racing(src, dst):
            Path(dst).write_bytes(b"racing creator")
            return real_link(src, dst)
        with patch.object(de.os, "link", side_effect=racing):
            with self.assertRaises(FileExistsError):
                de.export_debug(self.run, self.out)
        self.assertEqual(self.out.read_bytes(), b"racing creator")
        self.assertFalse(list(self.base.glob("debug-export-*.tmp")))

    def test_repo_destinations_refused_no_repo_dirtying_or_log_outputs(self):
        repo = self.base / "checkout"
        repo.mkdir()
        (repo / ".git").write_text("gitdir: elsewhere")  # linked worktree marker too
        nested = repo / "nested"
        nested.mkdir()
        for path in (nested / "report.zip", self.run / "report.zip"):
            with self.assertRaisesRegex(ValueError, "outside"):
                de.export_debug(self.run, path)
        with self.assertRaisesRegex(ValueError, "outside"):
            de.export_debug(self.run, self.out, excluded_roots=(self.base,))
        self.assertEqual(sorted(p.name for p in repo.iterdir()), [".git", "nested"])
        self.assertFalse(list(self.base.rglob("*.zip")))

    def test_write_failure_removes_temp_no_logs_mutated(self):
        before = self.snapshot()
        for mode in ("standard", "raw"):
            with patch.object(zipfile.ZipFile, "writestr", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    de.export_debug(self.run, self.out, mode)
            self.assertEqual(before, self.snapshot())
            self.assertFalse(self.out.exists())
            self.assertFalse(list(self.base.glob("debug-export-*.tmp")))

    def bridge(self):
        controller = Mock(repo=None)
        dialogs = Mock()
        dialogs.confirm_export.return_value = True
        dialogs.save_export.return_value = str(self.out)
        bridge = Bridge(controller=controller, dialogs=dialogs)
        bridge._root = self.run
        return bridge, controller, dialogs

    def test_bridge_standard_default_raw_extra_confirmation_and_no_git_processes(self):
        bridge, controller, dialogs = self.bridge()
        self.assertTrue(bridge.export_logs()["ok"])
        self.assertEqual(dialogs.confirm_export.call_count, 1)
        controller.assert_not_called()
        self.assertEqual(controller.mock_calls, [])
        self.out.unlink()
        dialogs.reset_mock()
        self.assertTrue(bridge.export_logs("raw")["ok"])
        self.assertEqual(dialogs.confirm_export.call_count, 2)
        self.assertIn("private data/API keys", dialogs.confirm_export.call_args.args[0])
        self.assertEqual(controller.mock_calls, [])

    def test_bridge_confirmation_and_save_cancellation_noop(self):
        for answers, save in (([False], str(self.out)), ([True, False], str(self.out)), ([True, True], None)):
            bridge, controller, dialogs = self.bridge()
            before = self.snapshot()
            dialogs.confirm_export.side_effect = answers
            dialogs.save_export.return_value = save
            self.assertTrue(bridge.export_logs("raw")["cancelled"])
            self.assertEqual(before, self.snapshot())
            self.assertFalse(self.out.exists())
            self.assertFalse(list(self.base.glob("debug-export-*.tmp")))
            self.assertEqual(controller.mock_calls, [])

    def test_bridge_only_current_selection_no_path_arguments_or_dialog_bypass(self):
        bridge, _, dialogs = self.bridge()
        bridge._root = None
        self.assertFalse(bridge.export_logs()["ok"])
        dialogs.confirm_export.assert_not_called()
        bridge._root = self.run
        bridge._dialogs = None
        self.assertFalse(bridge.export_logs()["ok"])
        with self.assertRaises(TypeError):
            bridge.export_logs("raw", str(self.run), str(self.out))
        self.assertFalse(bridge.export_logs("../raw")["ok"])

    def test_native_save_dialog_enum_legacy_and_cancel(self):
        from types import SimpleNamespace
        for module in (SimpleNamespace(FileDialog=SimpleNamespace(SAVE=3)), SimpleNamespace(SAVE_DIALOG=3)):
            window = Mock()
            window.create_file_dialog.return_value = (str(self.out),)
            dialogs = NativeDialogs(module, window)
            self.assertEqual(dialogs.save_export(), str(self.out))
            self.assertEqual(window.create_file_dialog.call_args.args, (3,))
            self.assertEqual(window.create_file_dialog.call_args.kwargs["file_types"], ("ZIP archives (*.zip)",))
            window.create_file_dialog.return_value = None
            self.assertIsNone(dialogs.save_export())
            dialogs.confirm_export("Inspect before sharing")
            window.create_confirmation_dialog.assert_called_once_with("Confirm debug package export", "Inspect before sharing")

    def test_open_folder_is_fixed_to_last_success_and_no_shell_arguments(self):
        bridge, _, _ = self.bridge()
        self.assertFalse(bridge.open_export_folder()["ok"])
        bridge.export_logs()
        with patch.object(os, "startfile", create=True) as startfile:
            self.assertTrue(bridge.open_export_folder()["ok"])
            startfile.assert_called_once_with(str(self.base))
        with self.assertRaises(TypeError):
            bridge.open_export_folder("arbitrary command")


if __name__ == "__main__":
    unittest.main()
