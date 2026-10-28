"""Preferences and packaged-path checks without Git, network, GUI or credentials."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from auto_coding.desktop_app import Bridge
from auto_coding.desktop_control import DesktopController
from auto_coding.desktop_settings import DesktopSettings


class DesktopSettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1])
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        env = patch.dict(os.environ, {"LOCALAPPDATA": str(self.root / "user-data")})
        env.start()
        self.addCleanup(env.stop)
        self.settings = DesktopSettings()

    def controller(self):
        controller = DesktopController(self.root / "runs")
        controller.github = Mock(return_value={"ok": True})
        controller.start = Mock()
        controller.resume = Mock()
        return controller

    def test_restart_restores_only_hints_and_valid_plan(self):
        plan = self.root / "plan.json"
        plan.write_text(json.dumps({"tasks": [{"id": "one", "prompt": "implement feature", "test_commands": []}]}))
        first = Bridge(controller=self.controller())
        first.save_setup("https://github.com/Owner/Repo.git", 7)
        self.assertTrue(first.select_plan(str(plan))["ok"])
        self.assertTrue(first.checkout_github_branch("owner/repo", "feature/remember")["ok"])
        controller = self.controller()
        second = Bridge(controller=controller)
        self.assertEqual(second._preferences, {"repository": "https://github.com/Owner/Repo.git",
            "branch": "feature/remember", "plan": str(plan), "attempts": 7})
        self.assertEqual(controller.plan, plan)
        self.assertIsNone(controller.repo)
        self.assertIsNone(controller.repository)
        controller.github.assert_not_called()
        controller.start.assert_not_called()
        controller.resume.assert_not_called()

    def test_frozen_exe_uses_user_data_and_never_package_directory(self):
        install = self.root / "read-only-install"
        install.mkdir()
        real_mkstemp = tempfile.mkstemp
        def confined_write(*args, **kwargs):
            self.assertEqual(Path(kwargs["dir"]), self.root / "user-data" / "AutoCoding")
            return real_mkstemp(*args, **kwargs)
        # Treat all package writes as forbidden; no writable install directory assumed.
        with patch.object(sys, "frozen", True, create=True), patch.object(sys, "executable", str(install / "AutoCoding.exe")), patch.object(sys, "_MEIPASS", str(install), create=True), patch("auto_coding.desktop_settings.tempfile.mkstemp", side_effect=confined_write):
            settings = DesktopSettings()
            self.assertTrue(settings.save({"repository": "owner/repo", "attempts": 5}))
            self.assertEqual(DesktopSettings().load()["attempts"], 5)
            self.assertEqual(list(install.iterdir()), [])
            self.assertEqual(settings.path, self.root / "user-data" / "AutoCoding" / "settings.json")

    def test_missing_plan_and_stale_branch_do_not_connect_or_launch(self):
        missing = self.root / "missing.json"
        self.settings.save({"repository": "owner/repo", "branch": "feature/deleted", "plan": str(missing)})
        controller = self.controller()
        bridge = Bridge(controller=controller)
        self.assertIsNone(controller.plan)
        self.assertIsNone(controller.repo)
        self.assertEqual(bridge._preferences["plan"], str(missing))
        self.assertEqual(bridge._preferences["branch"], "feature/deleted")
        controller.github.assert_not_called()
        controller.start.assert_not_called()
        controller.resume.assert_not_called()

    def test_corrupt_inaccessible_oversized_and_wrong_shape_fall_back(self):
        self.settings.path.parent.mkdir(parents=True)
        for raw in ("{", "[]", "null", "x" * 16385, "[" * 7000 + "0" + "]" * 7000):
            self.settings.path.write_text(raw)
            self.assertEqual(self.settings.load()["attempts"], 3)
        with patch.object(Path, "open", side_effect=PermissionError):
            self.assertEqual(self.settings.load()["repository"], "")
        with patch("auto_coding.desktop_settings.tempfile.mkstemp", side_effect=PermissionError):
            self.assertFalse(self.settings.save({}))

    def test_atomic_failure_preserves_previous_file_and_cleans_temporary(self):
        self.assertTrue(self.settings.save({"attempts": 5}))
        with patch("auto_coding.desktop_settings.os.replace", side_effect=PermissionError):
            self.assertFalse(self.settings.save({"attempts": 9}))
        self.assertEqual(self.settings.load()["attempts"], 5)
        self.assertEqual(list(self.settings.path.parent.glob("*.tmp")), [])

    def test_allowlist_ignores_legacy_policy_and_secret_fields(self):
        self.settings.save({"repository": "owner/repo", "attempts": 4, "auto_push": False,
            "token": "secret-sentinel", "password": "secret-sentinel", "environment": {"key": "secret-sentinel"},
            "prompt": "secret-sentinel", "logs": "secret-sentinel"})
        raw = self.settings.path.read_text()
        self.assertNotIn("secret-sentinel", raw)
        self.assertEqual(set(json.loads(raw)), {"repository", "branch", "plan", "attempts"})
        self.settings.save({"repository": "https://token:secret-sentinel@github.com/owner/repo"})
        self.assertNotIn("secret-sentinel", self.settings.path.read_text())

    def test_failed_or_protected_branch_is_not_saved(self):
        controller = self.controller()
        bridge = Bridge(controller=controller)
        bridge.save_setup("owner/repo", 3)
        controller.github.return_value = {"ok": False, "error": "stale branch"}
        bridge.checkout_github_branch("owner/repo", "feature/stale")
        self.assertEqual(self.settings.load()["branch"], "")
        controller.github.return_value = {"ok": True}
        bridge.checkout_github_branch("owner/repo", "main")
        self.assertEqual(self.settings.load()["branch"], "")
