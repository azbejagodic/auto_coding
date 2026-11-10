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
    def test_single_panel_reference_structure(self):
        class Markup(HTMLParser):
            def __init__(self):
                super().__init__()
                self.stack, self.nodes, self.tags, self.text = [], {}, [], []

            def handle_starttag(self, tag, attrs):
                attrs = dict(attrs)
                self.tags.append(tag)
                if "id" in attrs:
                    if attrs["id"] in self.nodes:
                        raise AssertionError("Duplicate ID")
                    self.nodes[attrs["id"]] = (tag, attrs, tuple(self.stack))
                if tag not in {"input", "hr", "meta", "br", "path", "circle", "rect"}:
                    self.stack.append(attrs.get("id", tag))

            def handle_endtag(self, tag):
                if tag not in {"path", "circle", "rect"}:
                    self.stack.pop()

            def handle_data(self, data):
                if "script" not in self.stack and "style" not in self.stack:
                    self.text.append(data)

        markup = Markup()
        markup.feed(HTML)
        self.assertEqual(markup.tags.count("main"), 1)
        self.assertEqual(markup.tags.count("h1"), 1)
        for tag in ("aside", "nav", "details", "h2", "h3", "pre", "table", "progress"):
            self.assertNotIn(tag, markup.tags)
        for obsolete in ("nav-overview", "nav-tasks", "nav-logs", "advanced", "app",
                         "evidence-viewer", "resume", "header-state", "refresh"):
            self.assertNotIn(obsolete, markup.nodes)
        for name in ("browse-plan", "start-audit", "download-audit", "start", "stop", "download-logs"):
            tag, attrs, ancestors = markup.nodes[name]
            self.assertEqual(tag, "button")
            self.assertNotIn("configuration", ancestors)
            if name != "browse-plan":
                self.assertIn("disabled", attrs)
        for name in ("browse-project", "open-run", "export-evidence", "include-evidence", "audit-model", "audit-goal", "attempts",
                     "github-branch", "auto-push", "push-approved"):
            self.assertIn("configuration", markup.nodes[name][2])
        self.assertNotIn("open", markup.nodes["configuration"][1])
        self.assertEqual(markup.nodes["configuration"][0], "dialog")
        labels = " ".join(markup.text)
        for text in ("Plan Audit", "Selected JSON file", "Browse", "Start Audit", "Download Fix / Log",
                     "Run controls unlock after approved audit", "Start", "Stop", "Download Logs",
                     "Status", "Task", "Time"):
            self.assertIn(text, labels)
        for text in ("simplified layout preview", "Overview", "Task current/total", "Run setup"):
            self.assertNotIn(text, labels)
        order = ["plan-path", "browse-plan", "start-audit", "audit-result", "download-audit",
                 "approval-indicator", "start", "stop", "download-logs", "run-status", "run-task", "run-time"]
        self.assertEqual([HTML.index('id="' + name + '"') for name in order],
                         sorted(HTML.index('id="' + name + '"') for name in order))
        self.assertEqual(markup.nodes["stop"][1]["title"], "Stop after current task")
        self.assertEqual(markup.nodes["audit-result"][1]["aria-live"], "polite")
        self.assertEqual(markup.nodes["plan-path"][1]["tabindex"], "0")
        self.assertEqual(markup.nodes["attempts"][1]["type"], "number")
        self.assertIn("hidden", markup.nodes["configure-error"][1])

    def test_responsive_accessible_and_safe_text_contract(self):
        for token in ('width:min(100%,1000px)', 'min-height:555px', 'place-items:center',
                      'grid-template-columns:repeat(3,minmax(0,1fr))', 'overflow-wrap:anywhere',
                      ':focus-visible', '@media(max-width:700px)', '@media(max-width:400px)',
                      'prefers-reduced-motion:reduce', 'showModal()', "addEventListener('cancel'",
                      'textContent'):
            self.assertIn(token, HTML)
        for unsafe in ('innerHTML', 'outerHTML', 'insertAdjacentHTML', '<script src=', 'https://'):
            self.assertNotIn(unsafe, HTML)

    def test_actual_ui_buttons_polling_and_state_transitions(self):
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
        self.assertIn("Dashboard selection, audit, authorization, recovery, export and state contracts passed", result.stdout)

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
