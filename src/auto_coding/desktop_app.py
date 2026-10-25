"""Optional native desktop bridge; importing this module never loads a GUI."""
from __future__ import annotations

import argparse
import json
import sys
import threading
from pathlib import Path

from auto_coding.desktop_reader import read_run, read_evidence
from auto_coding.desktop_control import DesktopController
from auto_coding.simple_run import load_tasks


HTML = r'''<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>Auto-Coding</title><style>
*{box-sizing:border-box}body{margin:0;background:#101214;color:#edf1f5;font:14px Segoe UI,Arial,sans-serif}.shell{display:flex;min-height:100vh}.side{width:210px;padding:28px 18px;background:#15181b;border-right:1px solid #282d32}.brand{font-size:18px;font-weight:700;margin:0 0 34px}.nav{padding:12px;border-radius:9px;color:#aeb7c0;margin:5px 0}.nav.active{background:#222a31;color:#fff}.main{padding:32px;flex:1;min-width:0}.top{display:flex;justify-content:space-between;align-items:center}.muted{color:#89949e}.grid{display:grid;grid-template-columns:repeat(3,minmax(150px,1fr));gap:14px;margin:24px 0}.card,.panel{background:#191d21;border:1px solid #2a3036;border-radius:14px;padding:18px}.label{color:#89949e;font-size:12px;text-transform:uppercase;letter-spacing:.08em}.value{font-size:20px;margin-top:9px;font-weight:650}.blue{color:#61a9ff}.green{color:#59d68b}.amber{color:#f3bd58}.red{color:#ff7272}.panel{margin-top:16px}.events{width:100%;border-collapse:collapse;margin-top:12px}th{text-align:left;color:#89949e;font-size:11px;text-transform:uppercase}td,th{padding:11px 9px;border-bottom:1px solid #292f35;vertical-align:top}td{color:#d8dee5;overflow-wrap:anywhere}button{background:#26323e;border:1px solid #384958;color:#dcecff;border-radius:8px;padding:8px 13px;cursor:pointer}button:hover{background:#314253}.evidence{color:#75b8ff;cursor:pointer;text-decoration:underline}.empty{padding:24px;color:#aeb7c0;background:#191d21;border-radius:12px}.tasks{display:flex;gap:8px;flex-wrap:wrap;margin-top:10px}.pill{background:#252b30;padding:7px 10px;border-radius:20px}.warning{color:#f3bd58;margin:7px 0}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#101214;border-radius:9px;padding:14px;max-height:50vh;overflow:auto}
</style></head><body><div class="shell"><aside class="side"><div class="brand">Auto-Coding</div><div class="nav active">Overview</div><div class="nav">Tasks</div><div class="nav">Logs</div><p class="muted" style="margin-top:38px">Read-only viewer<br>Slice 1</p></aside><main class="main"><div class="top"><div><div class="label">Desktop dashboard</div><h1 style="margin:8px 0">Run overview</h1></div><button id="refresh">Refresh</button></div><div id="app"></div><div id="evidence-viewer"></div></main></div><script>
const app = document.getElementById('app');
const evidenceViewer = document.getElementById('evidence-viewer');

function el(tag, txt, cls) {
  const node = document.createElement(tag);
  if (txt !== undefined) node.textContent = String(txt);
  if (cls) node.className = cls;
  return node;
}
function panel(title) {
  const p = el('section', undefined, 'panel');
  p.append(el('h2', title));
  return p;
}
function render(data) {
  app.replaceChildren();
  if (!data.exists) {
    app.append(el('div', data.state_error || 'Log directory is missing or unavailable.', 'empty'));
    return;
  }
  if (data.empty) {
    app.append(el('div', 'No run state or events found in this log directory. Start a run with the separate runner, then refresh this viewer.', 'empty'));
    return;
  }
  const state = data.state || {};
  const summary = el('div', undefined, 'grid');
  const stats = [
    ['Run status', state.status || 'State unavailable', state.status === 'completed' ? 'green' : state.status === 'stopped' ? 'red' : 'blue'],
    ['Completed tasks', Array.isArray(state.completed) ? state.completed.length : 'Unknown', 'green'],
    ['Timeline events', data.events.length, 'blue']
  ];
  stats.forEach(([label, value, tone]) => {
    const card = el('div', undefined, 'card');
    card.append(el('div', label, 'label'), el('div', value, 'value ' + tone));
    summary.append(card);
  });
  app.append(summary);
  if (data.state_error) app.append(el('div', data.state_error, 'warning'));
  if (state.pending_checkpoint) app.append(el('div', 'A checkpoint is pending reconciliation. No completion is inferred.', 'warning'));

  const tasks = panel('Tasks and attempt history');
  tasks.append(el('div', 'Current task: ' + (state.current_task || 'No task in progress'), 'value'));
  if (Array.isArray(state.completed)) {
    const row = el('div', undefined, 'tasks');
    state.completed.forEach(task => row.append(el('span', task, 'pill green')));
    if (!state.completed.length) row.append(el('span', 'No completed tasks recorded', 'muted'));
    tasks.append(row);
  }
  if (Array.isArray(state.history)) {
    state.history.forEach(item => {
      const line = el('div', undefined, 'tasks');
      line.append(el('span', item.task || 'Unknown task', 'pill'));
      line.append(el('span', 'Attempt ' + (item.attempt ?? 'unknown'), 'pill'));
      const decision = item.review && item.review.decision;
      if (decision) line.append(el('span', decision, 'pill ' + (decision === 'APPROVED' ? 'green' : 'amber')));
      if (item.feedback) line.append(el('span', item.feedback, 'pill red'));
      if (item.error) line.append(el('span', item.error, 'pill red'));
      tasks.append(line);
    });
  }
  app.append(tasks);

  const timeline = panel('Durable event timeline');
  data.event_errors.forEach(error => timeline.append(el('div', error, 'warning')));
  const table = el('table', undefined, 'events');
  const header = el('thead');
  const headings = el('tr');
  ['UTC timestamp', 'Actor', 'Event', 'Outcome', 'Task', 'Attempt', 'Evidence'].forEach(label => headings.append(el('th', label)));
  header.append(headings);
  table.append(header);
  const body = el('tbody');
  data.events.forEach(event => {
    const line = el('tr');
    [event.timestamp_utc, event.actor, event.event, event.outcome, event.task_id, event.attempt].forEach(value => {
      line.append(el('td', value === null || value === undefined || value === '' ? '--' : value));
    });
    const cell = el('td');
    const files = event._evidence_status || [];
    if (!files.length) cell.textContent = 'None listed';
    files.forEach(file => {
      const available = file.status === 'available';
      const info = el('span', file.path + ' (' + file.status + ')', available ? 'evidence' : 'warning');
      if (available) {
        info.setAttribute('role', 'button');
        info.setAttribute('tabindex', '0');
        info.onclick = () => showEvidence(file.path);
        info.onkeydown = key => { if (key.key === 'Enter' || key.key === ' ') showEvidence(file.path); };
      }
      cell.append(el('span', file.label + ': '), info, document.createTextNode(' '));
    });
    line.append(cell);
    body.append(line);
  });
  table.append(body);
  timeline.append(table);
  app.append(timeline);
}

async function refresh() {
  try {
    render(await window.pywebview.api.snapshot());
  } catch (error) {
    app.replaceChildren(el('div', 'Could not load logs: ' + error, 'empty'));
  }
}
async function showEvidence(name) {
  const box = panel('Raw evidence - may contain sensitive information');
  box.append(el('div', 'This file is displayed only because you selected it. Treat contents as potentially sensitive.', 'warning'));
  const content = el('pre', 'Loading...');
  box.append(content);
  // Keep selected evidence visible while the timeline periodically refreshes.
  evidenceViewer.replaceChildren(box);
  try {
    const result = await window.pywebview.api.evidence(name);
    if (evidenceViewer.contains(content)) content.textContent = result.text;
  } catch (error) {
    if (evidenceViewer.contains(content)) content.textContent = 'Unable to read evidence: ' + error;
  }
}

document.getElementById('refresh').onclick = refresh;
window.addEventListener('pywebviewready', refresh);
setInterval(refresh, 4000);
</script></body></html>'''


class NativeDialogs:
    """Mockable boundary, bound to a window only after create_window succeeds."""

    def __init__(self, webview, window):
        self._webview, self._window = webview, window

    def choose(self, kind):
        name = "OPEN" if kind == "plan" else "FOLDER"
        enum = getattr(self._webview, "FileDialog", None)
        dialog_type = getattr(enum, name) if enum is not None else getattr(self._webview, name + "_DIALOG")
        options = {"allow_multiple": False}
        if kind == "plan":
            options["file_types"] = ("Task plans (*.txt;*.md;*.json)",)
        result = self._window.create_file_dialog(dialog_type, **options)
        return result[0] if result and not isinstance(result, str) else result

    def confirm(self, message):
        return self._window.create_confirmation_dialog("Confirm local runner launch", message)


class Bridge:
    """Small JS API. Selection never launches; each launch needs native confirmation.

    A fresh controller on run selection prevents stale metadata from a previous
    run making a legacy run appear resumable. Never replace a tracked live job.
    """

    def __init__(self, root: Path | None = None, *, controller=None,
                 dialogs=None, controller_factory=DesktopController):
        self._controller_factory = controller_factory
        self._controller = controller if controller is not None else controller_factory()
        self._dialogs = dialogs
        self._lock = threading.RLock()
        self._root = Path(root).expanduser() if root is not None else None
        self._selection_error = None
        if root is not None:
            result = self.open_run(str(root))
            if not result["ok"]:
                self._selection_error = result["error"]

    @staticmethod
    def _failure(exc):
        return {"ok": False, "error": str(exc) or type(exc).__name__}

    def _select(self, kind, path):
        with self._lock:
            try:
                if path is None:
                    if self._dialogs is None:
                        raise ValueError("Native dialogs are unavailable.")
                    path = self._dialogs.choose(kind)
                    if not path:
                        return {"ok": True, "cancelled": True}
                if not isinstance(path, str) or not path.strip():
                    raise ValueError("Select a nonempty path.")
                status = self._controller.job_status()
                if status["active"] is not False or status.get("uncertain"):
                    raise ValueError("Cannot change selections while a job is active or its launch status is unknown.")
                if kind == "run":
                    candidate = self._controller_factory()
                    result = candidate.reopen(path)
                    if result["ok"]:
                        self._controller = candidate
                        self._root = candidate.logs
                else:
                    result = getattr(self._controller, "select_" + kind)(path)
                self._selection_error = result.get("error")
                return result
            except Exception as exc:
                self._selection_error = str(exc)
                return self._failure(exc)

    def select_repo(self, path=None):
        return self._select("repo", path)

    def select_plan(self, path=None):
        return self._select("plan", path)

    def open_run(self, path=None):
        return self._select("run", path)

    def selected_paths(self):
        with self._lock:
            return self._controller.selected_paths()

    def job_status(self):
        with self._lock:
            try:
                status = dict(self._controller.job_status())
                paths = self._controller.selected_paths()
                errors = {}
                for name in ("repo", "plan", "logs"):
                    value = paths[name]
                    if value and not (Path(value).is_file() if name == "plan" else Path(value).is_dir()):
                        errors[name] = "Selected path is missing or unavailable."
                plan_sha = None
                if paths["plan"] and "plan" not in errors:
                    try:
                        _, plan_sha = load_tasks(Path(paths["plan"]))
                    except Exception as exc:
                        errors["plan"] = str(exc)
                state = read_run(self._root).get("state") if self._root else None
                status["validation_errors"] = errors
                # This is eligibility only; the controller revalidates on launch.
                status["resume_ready"] = bool(
                    not errors and paths["repo"] and plan_sha and state
                    and state.get("status") == "stopped"
                    and state.get("repo") == paths["repo"]
                    and state.get("plan_sha256") == plan_sha
                    and status["active"] is False and not status.get("uncertain"))
                status["selection_error"] = self._selection_error
                return status
            except Exception as exc:
                return {"active": None, "uncertain": True, "exit_code": None,
                        "error": str(exc), "resume_ready": False}

    def _launch(self, action, attempts):
        with self._lock:
            try:
                if type(attempts) is not int or not 1 <= attempts <= 10:
                    raise ValueError("Attempts must be an integer between 1 and 10.")
                paths = self._controller.selected_paths()
                message = (f"{action.upper()} the local runner?\nRepository: {paths['repo']}"
                           f"\nPlan: {paths['plan']}\nSelected logs: {paths['logs']}"
                           f"\nAttempts: {attempts}\nThe runner may modify and commit project files.")
                if self._dialogs is None:
                    raise ValueError("Launch requires native confirmation; dialogs are unavailable.")
                if self._dialogs.confirm(message) is not True:
                    return {"ok": False, "cancelled": True, "error": "Launch cancelled."}
                result = getattr(self._controller, action)(attempts=attempts)
                # Even a failed launch may have created useful diagnostic logs.
                if self._controller.logs is not None:
                    self._root = self._controller.logs
                return result
            except Exception as exc:
                return self._failure(exc)

    def start(self, attempts=3):
        return self._launch("start", attempts)

    def resume(self, attempts=3):
        return self._launch("resume", attempts)

    def snapshot(self):
        with self._lock:
            status = self.job_status()
            try:
                data = read_run(self._root) if self._root else {"root":"", "exists":False, "state":None, "state_error":"Ready. Select a project and task plan, or open an existing run for inspection.", "events":[], "event_errors":[], "empty":True}
            except Exception as exc:
                data = {"root": str(self._root), "exists": False, "state": None,
                        "state_error": str(exc), "events": [], "event_errors": [], "empty": True}
            data["controller"] = {**self.selected_paths(), **status}
            return data

    def evidence(self, name):
        with self._lock:
            if not isinstance(name, str):
                return {"ok": False, "text": "Evidence name must be a relative path string."}
            try:
                return read_evidence(self._root, name) if self._root else {"ok":False, "text":"No log directory selected."}
            except Exception as exc:
                return {"ok": False, "text": f"Evidence could not be read: {exc}"}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Auto-Coding desktop and run log viewer")
    parser.add_argument("--logs", type=Path, help="existing run log directory")
    args = parser.parse_args(argv)
    try:
        import webview
    except ImportError:
        print("Desktop window requires optional pywebview. Install with: python -m pip install 'pywebview[winforms]'", file=sys.stderr)
        return 2
    try:
        api = Bridge(args.logs)
        window = webview.create_window("Auto-Coding - Run Dashboard", html=HTML, js_api=api, width=1180, height=780, min_size=(800, 600))
        api._dialogs = NativeDialogs(webview, window)
        webview.start()
        return 0
    except Exception as exc:
        print(f"Could not start the desktop window. On Windows install Microsoft Edge WebView2 Runtime and pywebview: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
