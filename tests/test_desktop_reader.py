import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from auto_coding.desktop_reader import read_run, read_evidence
from auto_coding.desktop_app import HTML


class DesktopReaderTests(unittest.TestCase):
    def test_populated_run_and_sequence_sort(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "state.json").write_text(json.dumps({"status":"completed", "completed":["one"], "history":[{"task":"one", "attempt":1}]}))
            events = [{"seq":2,"timestamp_utc":"t2","actor":"Git","event":"task_completed","outcome":"success"}, {"seq":1,"timestamp_utc":"t1","actor":"controller","event":"run_started","outcome":"started"}]
            (root / "events.jsonl").write_text("\n".join(json.dumps(e) for e in events) + "\n")
            data = read_run(root)
            self.assertEqual(data["state"]["completed"], ["one"])
            self.assertEqual([e["seq"] for e in data["events"]], [1, 2])

    def test_timeline_reports_missing_and_safe_evidence_without_reading_it(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            evidence = root / "present.txt"
            evidence.write_text("possibly sensitive body", encoding="utf-8")
            event = {"seq": 1, "evidence": {"output": "missing.txt", "trace": "present.txt",
                                               "unsafe": "../outside.txt"}}
            (root / "events.jsonl").write_text(json.dumps(event), encoding="utf-8")
            listed = read_run(root)["events"][0]["_evidence_status"]
            self.assertEqual([(item["path"], item["status"]) for item in listed], [
                ("missing.txt", "missing"), ("present.txt", "available"),
                ("../outside.txt", "unsafe path")])
            self.assertNotIn("possibly sensitive body", json.dumps(read_run(root)))
            self.assertIn("file.status", HTML)

    def test_browser_render_executes_with_real_log_snapshot_when_node_available(self):
        node = shutil.which("node")
        if node is None:
            self.skipTest("Node.js is optional; browser JS smoke test unavailable")
        javascript = HTML.split("<script>", 1)[1].split("</script>", 1)[0]
        # Minimal DOM stand-in: run actual dashboard JS, rather than only matching strings.
        fixture = r"""
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
class Element {
  constructor(tag) { this.tag = tag; this.children = []; this._text = ''; this.value = tag === 'attempts' ? '3' : ''; }
  set textContent(value) { this._text = String(value); this.children = []; }
  get textContent() { return this._text + this.children.map(child => child.textContent).join(''); }
  append(...items) { this.children.push(...items); }
  replaceChildren(...items) { this.children = items; this._text = ''; }
  setAttribute(name, value) { this[name] = value; }
  contains(target) { return this === target || this.children.some(child => child.contains?.(target)); }
}
const app = new Element('app');
const evidenceViewer = new Element('evidence-viewer');
const document = {
  getElementById: id => id === 'app' ? app : id === 'evidence-viewer' ? evidenceViewer : new Element(id),
  createElement: tag => new Element(tag),
  createTextNode: value => ({textContent: String(value)})
};
const snapshot = {
  root: 'mock-run', controller: {logs: 'mock-run', active: false, uncertain: false},
  exists: true, empty: false, state_error: null,
  state: {status: 'running', completed: [], current_task: 'desktop-readonly-logs', history: [
    {task: 'desktop-readonly-logs', attempt: 2, review: {decision: 'REJECTED'}, feedback: 'Fix a real issue'}
  ]},
  events: [{ timestamp_utc: '2026-10-25T12:00:00Z', actor: 'Codex B', event: 'review_finished',
    outcome: 'rejected', task_id: 'desktop-readonly-logs', attempt: 2,
    _evidence_status: [
      {label: 'missing', path: 'gone.txt', status: 'missing'},
      {label: 'available', path: 'log.txt', status: 'available'}
    ]}],
  event_errors: ['Line 3: corrupt event']
};
const context = { document, window: {addEventListener() {}, pywebview: {api: {
  snapshot: async () => snapshot, evidence: async () => ({ok: true, text: 'raw log evidence'})
}}}, setInterval() {} };
vm.createContext(context);
vm.runInContext(fs.readFileSync(process.argv[2], 'utf8'), context);
vm.runInContext('render(' + JSON.stringify(snapshot) + ')', context);
vm.runInContext("navigate('tasks')", context);
const contents = app.textContent;
for (const text of ['Current task: desktop-readonly-logs', 'Attempt 2', 'REJECTED',
  'Fix a real issue']) {
  assert(contents.includes(text), 'Missing UI text: ' + text);
}
assert(!contents.includes('Line 3: corrupt event'), 'Historical parse diagnostics belong in Logs');
vm.runInContext("navigate('logs')", context);
assert(app.textContent.includes('Line 3: corrupt event'));
assert(app.textContent.includes('gone.txt (missing)'));
assert(!contents.includes('Raw evidence'), 'Evidence must be opened explicitly');
const walk = node => [node, ...(node.children || []).flatMap(walk)];
const available = walk(app).find(node => node.textContent === 'log.txt (available)');
assert(typeof available.onclick === 'function', 'Available evidence must be selectable');
const missing = walk(app).find(node => node.textContent === 'gone.txt (missing)');
assert(missing && typeof missing.onclick !== 'function', 'Missing evidence must not be a working link');
(async () => {
  await available.onclick();
  await new Promise(resolve => setImmediate(resolve));
  assert(evidenceViewer.textContent.includes('raw log evidence'));
  vm.runInContext('render(' + JSON.stringify(snapshot) + ')', context);
  assert(evidenceViewer.textContent.includes('raw log evidence'), 'Refresh cleared selected evidence');
})().catch(error => {console.error(error); process.exitCode = 1;});
"""
        with tempfile.TemporaryDirectory() as td:
            script_path = Path(td) / "dashboard.js"
            fixture_path = Path(td) / "smoke.cjs"
            script_path.write_text(javascript, encoding="utf-8")
            fixture_path.write_text(fixture, encoding="utf-8")
            result = subprocess.run([node, str(fixture_path), str(script_path)], capture_output=True,
                                    text=True, encoding="utf-8", errors="replace", timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_missing_empty_and_state_only_legacy(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "logs"
            self.assertFalse(read_run(root)["exists"])
            root.mkdir()
            self.assertTrue(read_run(root)["empty"])
            (root / "state.json").write_text('{"status":"stopped","completed":[]}')
            data = read_run(root)
            self.assertFalse(data["empty"])
            self.assertEqual(data["events"], [])

    def test_legacy_files_are_listed_without_reading_bodies_and_manifest_counts(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "original-answer.txt").write_text("body is not loaded by polling")
            (root / "run-manifest.json").write_text('{"task_ids":["one","two"]}')
            data = read_run(root)
            self.assertEqual(data["task_count"], 2)
            self.assertEqual(data["task_ids"], ["one", "two"])
            self.assertIn({"label":"File", "path":"original-answer.txt", "status":"available"}, data["files"])
            self.assertNotIn("body is not loaded", json.dumps(data))
            self.assertEqual(read_evidence(root, "original-answer.txt")["text"], "body is not loaded by polling")
            (root / "run-manifest.json").write_text("{bad")
            data = read_run(root)
            self.assertIsNone(data["task_count"])
            self.assertIn("manifest.json is corrupt", data["event_errors"][0])

    def test_corrupt_state_and_event_line_reported(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "state.json").write_text("{")
            (root / "events.jsonl").write_text('{"seq":1}\nnot-json\n')
            data = read_run(root)
            self.assertIn("corrupt", data["state_error"])
            self.assertEqual(len(data["events"]), 1)
            self.assertIn("Line 2", data["event_errors"][0])

    def test_malformed_only_timeline_is_not_reported_as_empty(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "events.jsonl").write_text("not-json\n")
            data = read_run(root)
            self.assertFalse(data["empty"])
            self.assertIn("Line 1", data["event_errors"][0])

    def test_symlinked_standard_logs_cannot_escape_root(self):
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as outside:
            root, elsewhere = Path(td), Path(outside)
            secret = elsewhere / "state.json"
            secret.write_text('{"status":"completed","secret":"do not show"}')
            try:
                (root / "state.json").symlink_to(secret)
            except (OSError, NotImplementedError):
                self.skipTest("symlinks unavailable")
            data = read_run(root)
            self.assertIsNone(data["state"])
            self.assertIn("confined", data["state_error"])

    def test_attempt_history_shows_actual_review_and_feedback_fields(self):
        from auto_coding.desktop_app import HTML
        self.assertIn("record?.review?.decision", HTML)
        self.assertIn("item?.feedback", HTML)

    def test_current_task_is_displayed_without_fabricating_completion(self):
        self.assertIn("state.current_task === id", HTML)
        self.assertIn("task.status", HTML)
        self.assertNotIn("Current task: ", HTML)
        self.assertIn("A checkpoint is pending reconciliation", HTML)

    def test_selected_evidence_survives_refresh_and_invalid_links_are_not_clickable(self):
        self.assertIn("id=\"evidence-viewer\"", HTML)
        self.assertIn("evidenceViewer.replaceChildren(box)", HTML)
        self.assertIn("if (available)", HTML)
        self.assertIn("file.path + ' (' + file.status + ')'", HTML)

    def test_no_corrupted_ui_text_and_no_invalid_fallback_operator(self):
        for bad in ("â", "Â", "v·"):
            self.assertNotIn(bad, HTML)
        self.assertIn("value === null || value === undefined", HTML)

    def test_embedded_javascript_parses_when_node_available(self):
        node = shutil.which("node")
        if node is None:
            self.skipTest("Node.js is optional; no JavaScript runtime found")
        script = HTML.split("<script>", 1)[1].split("</script>", 1)[0]
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "desktop.js"
            path.write_text(script, encoding="utf-8")
            result = subprocess.run([node, "--check", str(path)], capture_output=True,
                                    text=True, encoding="utf-8", errors="replace", timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_evidence_path_confinement_and_cap(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "ok.txt").write_text("safe")
            self.assertEqual(read_evidence(root, "ok.txt")["text"], "safe")
            self.assertFalse(read_evidence(root, "../outside")["ok"])
            self.assertFalse(read_evidence(root, str(root / "ok.txt"))["ok"])
            (root / "large").write_bytes(b"x" * (1024 * 1024 + 1))
            self.assertIn("1 MB", read_evidence(root, "large")["text"])

    def test_payload_rendering_uses_text_content(self):
        self.assertIn("textContent", HTML)
        self.assertNotIn("innerHTML", HTML)
        self.assertNotIn("eval(", HTML)
        self.assertIn("createTextNode", HTML)


if __name__ == "__main__":
    unittest.main()
