"""Headless dashboard contract tests; no native window or real runner."""
import shutil
import subprocess
import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path

from auto_coding.desktop_app import HTML


# Read permitted files directly: Node's file-entry loader/--check normalizes
# Windows ancestors outside the MXC read roots. Keep real syntax compilation
# and the CommonJS wrapper without using that file-entry loader.
CHECK_JAVASCRIPT = """
const fs = require('node:fs');
const vm = require('node:vm');
new vm.Script(fs.readFileSync(process.argv[1], 'utf8'), {filename: process.argv[1]});
"""
RUN_COMMONJS = """
const fs = require('node:fs');
const path = require('node:path');
const Module = require('node:module');
const filename = process.argv[1];
const entry = new Module(filename, module);
entry.filename = filename;
entry.paths = Module._nodeModulePaths(path.dirname(filename));
entry._compile(fs.readFileSync(filename, 'utf8'), filename);
entry.loaded = true;
"""


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
                     "connect-github", "list-github", "select-branch", "new-branch",
                     "create-branch", "cancel-branch", "fetch-github", "push-approved",
                     "nav-overview", "nav-tasks", "nav-logs"):
            self.assertEqual(parser.ids[name], "button")
        self.assertEqual(parser.ids["github-repo"], "input")
        self.assertEqual(parser.ids["auto-push"], "input")
        self.assertIn('id="auto-push" type="checkbox"> Automatically push approved tasks', HTML)
        self.assertEqual(parser.ids["github-branch"], "select")
        self.assertEqual(parser.ids["branch-base"], "select")
        self.assertEqual(parser.ids["branch-base-confirm"], "input")
        self.assertIn('Advanced / Local Project', HTML)
        self.assertIn('Push Approved Commits', HTML)
        self.assertNotIn('Merge to main</button>', HTML)
        self.assertEqual(parser.ids["attempts"], "input")
        for method in ("select_repo", "select_plan", "open_run", "start", "resume"):
            self.assertIn(method, HTML)
        self.assertIn('min="1" max="10" step="1" value="3" required', HTML)
        self.assertIn("Local Codex CLI authentication is required", HTML)
        self.assertIn("Closing this window leaves the runner alive", HTML)
        self.assertNotIn("innerHTML", HTML)
        self.assertNotIn("Read-only viewer", HTML)

    def test_layout_accessibility_and_text_safety_contract(self):
        for token in ('width:210px', 'margin-left:210px', 'font:15px/1.5',
                      'overflow-wrap:anywhere', ':focus-visible',
                      'prefers-reduced-motion:reduce', 'grid-template-columns:minmax(0,1fr)',
                      'aria-label="Main navigation"', 'aria-labelledby="project-label"',
                      'aria-labelledby="plan-label"', 'aria-describedby="preflight attempts-error"',
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

    def test_numeric_keyboard_and_compact_overview_contract(self):
        class Markup(HTMLParser):
            def __init__(self):
                super().__init__()
                self.nodes = {}
                self.labels = []

            def handle_starttag(self, tag, attrs):
                attrs = dict(attrs)
                if "id" in attrs:
                    self.nodes[attrs["id"]] = (tag, attrs)
                if tag == "label":
                    self.labels.append(attrs)

        markup = Markup()
        markup.feed(HTML)
        number = markup.nodes["attempts"][1]
        self.assertEqual(number["type"], "number")
        self.assertIn({"for": "attempts"}, markup.labels)
        self.assertEqual(number["aria-describedby"], "attempts-hint attempts-error")
        for name in ("attempts", "start", "resume", "nav-overview", "nav-tasks", "nav-logs"):
            tag, attrs = markup.nodes[name]
            self.assertIn(tag, ("input", "button"))  # Native Tab/Enter/Space semantics.
            self.assertNotIn("tabindex", attrs)  # No positive or negative tab-order overrides.
        for name in ("header-state", "attempts-error"):
            self.assertEqual(markup.nodes[name][1]["role"], "status")
            self.assertEqual(markup.nodes[name][1]["aria-live"], "polite")
        self.assertNotIn('open', markup.nodes["launch-details"][1])
        self.assertNotIn("Ready for your first run", HTML)
        self.assertNotIn("const steps", HTML)
        self.assertEqual(HTML.count('requires native confirmation.'), 1)

    def test_actual_ui_buttons_navigation_polling_and_state_transitions(self):
        node = shutil.which("node")
        self.assertIsNotNone(node, "Node.js is required for mandatory headless UI interaction checks")
        script = HTML.split("<script>", 1)[1].split("</script>", 1)[0]
        fixture = Path(__file__).with_name("desktop_ui_harness.cjs")
        with tempfile.TemporaryDirectory() as td:
            js = Path(td) / "dashboard.js"
            js.write_text(script, encoding="utf-8")
            syntax = subprocess.run([node, "-e", CHECK_JAVASCRIPT, str(js)], capture_output=True,
                                    text=True, timeout=20)
            self.assertEqual(syntax.returncode, 0, syntax.stderr)
            result = subprocess.run([node, "-e", RUN_COMMONJS, str(fixture), str(js)], capture_output=True,
                                    text=True, encoding="utf-8", errors="replace", timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Dashboard button, navigation, polling, evidence and state contracts passed", result.stdout)

    def test_node_launchers_preserve_compilation_commonjs_and_failure_exit(self):
        node = shutil.which("node")
        self.assertIsNotNone(node, "Node.js is required for mandatory headless UI interaction checks")
        with tempfile.TemporaryDirectory() as td:
            source = Path(td) / "fixture.cjs"
            source.write_text("const = ;", encoding="utf-8")
            invalid = subprocess.run([node, "-e", CHECK_JAVASCRIPT, str(source)],
                                     capture_output=True, text=True, timeout=20)
            self.assertNotEqual(invalid.returncode, 0)
            self.assertIn("SyntaxError", invalid.stderr)
            source.write_text("const assert = require('node:assert/strict'); "
                              "assert.equal(__filename, process.argv[1]); "
                              "assert.equal(__dirname, require('node:path').dirname(__filename)); "
                              "assert.equal(exports, module.exports); "
                              "exports.checked = true; console.log('COMMONJS_OK');", encoding="utf-8")
            valid = subprocess.run([node, "-e", RUN_COMMONJS, str(source)],
                                   capture_output=True, text=True, timeout=20)
            self.assertEqual(valid.returncode, 0, valid.stdout + valid.stderr)
            self.assertIn("COMMONJS_OK", valid.stdout)
            source.write_text("require('node:assert/strict').fail('INTENTIONAL_ASSERTION');", encoding="utf-8")
            failed = subprocess.run([node, "-e", RUN_COMMONJS, str(source)],
                                    capture_output=True, text=True, timeout=20)
            self.assertNotEqual(failed.returncode, 0)
            self.assertIn("INTENTIONAL_ASSERTION", failed.stderr)


if __name__ == "__main__":
    unittest.main()
