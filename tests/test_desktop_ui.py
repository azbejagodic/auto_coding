"""Headless dashboard contract tests; no native window or real runner."""
import shutil
import subprocess
import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path

from auto_coding.desktop_app import HTML


class DesktopUITests(unittest.TestCase):
    def test_controls_are_visible_and_scoped_to_bridge(self):
        class Controls(HTMLParser):
            def __init__(self):
                super().__init__()
                self.ids = {}

            def handle_starttag(self, tag, attrs):
                attrs = dict(attrs)
                if "id" in attrs:
                    self.ids[attrs["id"]] = tag

        parser = Controls()
        parser.feed(HTML)
        for name in ("browse-project", "browse-plan", "open-run", "start", "resume",
                     "nav-overview", "nav-tasks", "nav-logs"):
            self.assertEqual(parser.ids[name], "button")
        self.assertEqual(parser.ids["attempts"], "select")
        for method in ("select_repo", "select_plan", "open_run", "start", "resume"):
            self.assertIn(method, HTML)
        self.assertIn('value="3" selected', HTML)
        self.assertIn('value="5"', HTML)
        self.assertIn("Local Codex CLI authentication is required", HTML)
        self.assertIn("Closing this window leaves the runner alive", HTML)
        self.assertNotIn("innerHTML", HTML)
        self.assertNotIn("Read-only viewer", HTML)

    def test_layout_accessibility_and_text_safety_contract(self):
        for token in ('width:210px', 'margin-left:210px', 'font:15px/1.5',
                      'overflow-wrap:anywhere', ':focus-visible',
                      'prefers-reduced-motion:reduce', 'grid-template-columns:minmax(0,1fr)',
                      'aria-label="Main navigation"', 'aria-labelledby="project-label"',
                      'aria-labelledby="plan-label"', 'aria-describedby="preflight"',
                      'id="project-path"', 'id="plan-path"', 'id="run-setup"',
                      'Project folder', 'Task plan', 'Run setup', 'Open Run',
                      "'Awaiting recovery'", "'Completed', color:'green'",
                      "'Idle', color:'muted'", "'Unknown run state'",
                      'Search actor, outcome or event', '1 MB per file',
                      "content.setAttribute('tabindex', '0')"):
            self.assertIn(token, HTML)
        for unsafe in ('innerHTML', 'outerHTML', 'insertAdjacentHTML', '<script src=',
                       'https://', 'Unknown / Unknown'):
            self.assertNotIn(unsafe, HTML)

    def test_actual_ui_buttons_navigation_polling_and_state_transitions(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js absent; headless UI interaction checks unavailable")
        script = HTML.split("<script>", 1)[1].split("</script>", 1)[0]
        fixture = Path(__file__).with_name("desktop_ui_harness.cjs")
        with tempfile.TemporaryDirectory() as td:
            js = Path(td) / "dashboard.js"
            js.write_text(script, encoding="utf-8")
            syntax = subprocess.run([node, "--check", str(js)], capture_output=True,
                                    text=True, timeout=20)
            self.assertEqual(syntax.returncode, 0, syntax.stderr)
            result = subprocess.run([node, str(fixture), str(js)], capture_output=True,
                                    text=True, encoding="utf-8", errors="replace", timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
