"""Optional native desktop bridge; importing this module never loads a GUI."""
from __future__ import annotations

import argparse
import sys
import threading
from pathlib import Path

from auto_coding.desktop_reader import read_run, read_evidence
from auto_coding.desktop_control import DesktopController
from auto_coding.simple_run import load_tasks


HTML = r'''<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>Auto-Coding</title><style>
*{box-sizing:border-box}body{margin:0;background:#101214;color:#edf1f5;font:14px Segoe UI,Arial,sans-serif}.shell{display:flex;min-height:100vh}.side{width:210px;padding:28px 18px;background:#15181b;border-right:1px solid #282d32}.brand{font-size:18px;font-weight:700;margin:0 0 34px}.nav{padding:12px;border-radius:9px;color:#aeb7c0;margin:5px 0}.nav.active{background:#222a31;color:#fff}.main{padding:32px;flex:1;min-width:0}.top{display:flex;justify-content:space-between;align-items:center}.muted{color:#89949e}.grid{display:grid;grid-template-columns:repeat(3,minmax(150px,1fr));gap:14px;margin:24px 0}.card,.panel{background:#191d21;border:1px solid #2a3036;border-radius:14px;padding:18px}.label{color:#89949e;font-size:12px;text-transform:uppercase;letter-spacing:.08em}.value{font-size:20px;margin-top:9px;font-weight:650}.blue{color:#61a9ff}.green{color:#59d68b}.amber{color:#f3bd58}.red{color:#ff7272}.panel{margin-top:16px}.events{width:100%;border-collapse:collapse;margin-top:12px}th{text-align:left;color:#89949e;font-size:11px;text-transform:uppercase}td,th{padding:11px 9px;border-bottom:1px solid #292f35;vertical-align:top}td{color:#d8dee5;overflow-wrap:anywhere}button{background:#26323e;border:1px solid #384958;color:#dcecff;border-radius:8px;padding:8px 13px;cursor:pointer}button:hover{background:#314253}.evidence{color:#75b8ff;cursor:pointer;text-decoration:underline}.empty{padding:24px;color:#aeb7c0;background:#191d21;border-radius:12px}.tasks{display:flex;gap:8px;flex-wrap:wrap;margin-top:10px}.pill{background:#252b30;padding:7px 10px;border-radius:20px}.warning{color:#f3bd58;margin:7px 0}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#101214;border-radius:9px;padding:14px;max-height:50vh;overflow:auto}
button:disabled{opacity:.45;cursor:default}.nav{display:block;width:100%;text-align:left;border:0;background:transparent}.controls{display:flex;gap:9px;flex-wrap:wrap;align-items:center}.path{overflow-wrap:anywhere;margin:8px 0}.notice{white-space:pre-wrap;overflow-wrap:anywhere}select{background:#101214;color:#edf1f5;padding:8px;border:1px solid #384958;border-radius:8px}details{margin:10px 0}summary{cursor:pointer;color:#aeb7c0}h2{font-size:17px}h3{font-size:14px}.side p{line-height:1.6}.events{font-size:12px}
</style></head><body><div class="shell"><aside class="side"><div class="brand">Auto-Coding</div><button class="nav active" id="nav-overview">Overview</button><button class="nav" id="nav-tasks">Tasks</button><button class="nav" id="nav-logs">Logs</button><p class="muted" style="margin-top:38px">Local Codex CLI authentication is required.<br>Uses your configured CLI model and reasoning.</p><p class="muted">Closing this window leaves the runner alive. Reopen its logs to inspect progress.</p></aside><main class="main"><div class="top"><div><div class="label">Desktop dashboard</div><h1 style="margin:8px 0" id="page-title">Run overview</h1></div><button id="refresh">Refresh</button></div><section class="panel"><div class="controls"><button id="browse-project">Browse Project</button><button id="browse-plan">Browse Plan</button><button id="open-run">Open Existing Run</button><label>Attempt limit <select id="attempts"><option value="3" selected>3</option><option value="5">5</option></select></label><button id="start">START</button><button id="resume">RESUME</button></div><div id="paths"></div><div id="preflight" class="notice muted"></div><div id="message" class="notice" role="status" aria-live="polite"></div><p class="muted">Selection never starts work. Launch requires confirmation. Checkpoints stay local; no automatic push, merge, force-push or branch creation. START requires a clean feature branch. RESUME requires a stopped run and its original plan, repository and logs. Attempts is the total limit per task; choose 5 if 3 were exhausted.</p></section><div id="app"></div><div id="evidence-viewer"></div></main></div><script>
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
let page = 'overview';
let latest = null;
let busy = false;
let ready = false;
let refreshing = null;
let evidenceRoot = null;
let renderKey = null;
const expanded = new Set();
const controls = ['browse-project', 'browse-plan', 'open-run', 'start', 'resume', 'attempts'];
function detail(title, text, key) {
  const box = el('details');
  box.open = expanded.has(key);
  box.ontoggle = () => { if (box.open) expanded.add(key); else expanded.delete(key); };
  box.append(el('summary', title), el('pre', text));
  return box;
}
function message(text, error = false) {
  const box = document.getElementById('message');
  box.textContent = text || '';
  box.className = 'notice ' + (error ? 'red' : 'blue');
}
function updateControls() {
  const c = latest?.controller || {};
  const locked = busy || !ready || c.active !== false || c.uncertain;
  controls.forEach(id => { document.getElementById(id).disabled = locked; });
  document.getElementById('start').disabled = locked || !c.repo || !c.plan;
  document.getElementById('resume').disabled = locked || !c.resume_ready;
  document.getElementById('refresh').disabled = busy || !ready;
}
function filesInto(parent, files) {
  if (!files.length) parent.append(el('span', 'None listed', 'muted'));
  files.forEach(file => {
    const available = file.status === 'available';
    const info = el('span', file.path + ' (' + file.status + ')', available ? 'evidence' : 'warning');
    if (available) {
      info.setAttribute('role', 'button');
      info.setAttribute('tabindex', '0');
      info.onclick = () => showEvidence(file.path);
      info.onkeydown = key => { if (key.key === 'Enter' || key.key === ' ') return showEvidence(file.path); };
    }
    parent.append(el('span', file.label + ': '), info, document.createTextNode(' '));
  });
}
function timelineInto(parent, events) {
  if (!events.length) { parent.append(el('p', 'No events recorded. Legacy runs may have state and raw files only.', 'muted')); return; }
  const table = el('table', undefined, 'events');
  const header = el('thead');
  const headings = el('tr');
  ['UTC timestamp', 'Actor', 'Event', 'Outcome', 'Task', 'Attempt', 'Evidence / details'].forEach(label => headings.append(el('th', label)));
  header.append(headings);
  table.append(header);
  const body = el('tbody');
  events.forEach(event => {
    const line = el('tr');
    [event.timestamp_utc, event.actor, event.event, event.outcome, event.task_id, event.attempt].forEach(value => {
      line.append(el('td', value === null || value === undefined || value === '' ? '--' : value));
    });
    const cell = el('td');
    filesInto(cell, event._evidence_status || []);
    cell.append(detail('Original event', JSON.stringify(event, null, 2), 'event-' + (event.seq ?? event._source_order)));
    line.append(cell);
    body.append(line);
  });
  table.append(body);
  parent.append(table);
}
function render(data) {
  latest = data;
  const c = data.controller || {};
  const paths = document.getElementById('paths');
  paths.replaceChildren();
  ['repo', 'plan', 'logs'].forEach(name => paths.append(el('div', name.toUpperCase() + ': ' + (c[name] || 'Not selected'), 'path')));
  const git = c.preflight || {};
  const errors = [...(git.errors || []), ...Object.entries(c.validation_errors || {}).map(([k,v]) => k + ': ' + v)];
  if (c.selection_error) errors.push(c.selection_error);
  if (git.resume_error) errors.push(git.resume_error);
  document.getElementById('preflight').textContent = 'Branch: ' + (git.branch || 'Unknown') + ' | Working tree: ' + (git.clean === true ? 'clean' : git.clean === false ? 'has changes (START blocked; RESUME reconciles)' : 'unknown') + (errors.length ? '\n' + errors.join('\n') : '') + '\n' + (c.resume_ready ? 'Stopped state matches original plan and repo; RESUME eligible for launch-time checks.' : 'RESUME unavailable: requires matching stopped state, original paths and no active or unknown child.');
  updateControls();
  if (evidenceRoot !== data.root) {
    evidenceViewer.replaceChildren();
    expanded.clear();
    evidenceRoot = data.root;
  }
  const key = page + JSON.stringify(data);
  if (renderKey === key) return;
  renderKey = key;
  app.replaceChildren();
  const state = data.state || {};
  const events = data.events || [];
  const history = Array.isArray(state.history) ? state.history.filter(item => item && typeof item === 'object') : [];
  const completed = Array.isArray(state.completed) ? state.completed : null;
  const status = c.uncertain || c.active === null ? 'Unknown child status' : c.active === true ? 'Active' : state.status === 'completed' ? 'Completed (recorded)' : state.status === 'stopped' ? 'Stopped (recorded)' : state.status ? 'Unknown / child inactive; recorded: ' + state.status : c.logs ? 'Unknown / no durable state' : 'Ready / no durable state';
  if (c.error) app.append(el('div', c.error, 'notice warning'));
  if (data.state_error) app.append(el('div', data.state_error, 'warning'));
  if (data.empty) app.append(el('div', 'No run state or events found. Select a project and plan to START, or inspect available raw files in Logs.', 'empty'));
  if (!data.exists && !data.state_error) app.append(el('div', 'Log directory is missing or unavailable.', 'warning'));
  if (state.pending_checkpoint) app.append(el('div', 'A checkpoint is pending reconciliation. No completion is inferred.', 'warning'));
  (data.event_errors || []).forEach(error => app.append(el('div', error, 'warning')));
  if (page === 'overview') {
    const summary = el('div', undefined, 'grid');
    const count = data.task_count ?? c.task_count;
    [['Run status', status], ['Completed tasks', (completed ? completed.length : 'Unknown') + ' / ' + (count ?? 'Unknown')], ['Child process', c.active === true ? 'Running' : c.active === false ? 'Inactive' : 'Unknown']].forEach(([label,value]) => {
      const card = el('div', undefined, 'card');
      card.append(el('div', label, 'label'), el('div', value, 'value blue'));
      summary.append(card);
    });
    app.append(summary);
    const current = panel('Current task');
    current.append(el('div', 'Current task: ' + (state.current_task || 'No task in progress'), 'value'));
    current.append(el('p', 'Recorded state: ' + (state.status || 'Unavailable') + '. Child exit code: ' + (c.exit_code ?? 'Unavailable') + '. A child exit does not approve tasks.', 'muted'));
    if (count != null && completed) current.append(el('p', 'Tasks remaining: ' + Math.max(0, count - completed.length)));
    if (history.length) current.append(detail('Latest attempt details', JSON.stringify(history[history.length - 1], null, 2), 'latest-attempt'));
    app.append(current);
    const recent = panel('Recent activity');
    timelineInto(recent, events.slice(-5));
    app.append(recent);
  } else if (page === 'tasks') {
    const tasks = panel('Tasks and attempt history');
    tasks.append(el('div', 'Current task: ' + (state.current_task || 'No task in progress'), 'value'));
    const row = el('div', undefined, 'tasks');
    (completed || []).forEach(task => row.append(el('span', task, 'pill green')));
    if (!completed?.length) row.append(el('span', 'No completed tasks recorded', 'muted'));
    tasks.append(row);
    if (!history.length) tasks.append(el('p', 'No completed attempts recorded yet. Live test/review activity appears in Logs.', 'muted'));
    history.forEach((item, index) => {
      const attempt = el('section', undefined, 'panel');
      const decision = item.review && item.review.decision;
      attempt.append(el('h3', (item.task || 'Unknown task') + ' | Attempt ' + (item.attempt ?? 'unknown') + ' | Codex B: ' + (decision || 'No verdict recorded')));
      (Array.isArray(item.tests) ? item.tests : []).filter(test => test && typeof test === 'object').forEach(test => attempt.append(el('p', 'Test ' + test.index + ': exit ' + (test.exit_code ?? 'unknown') + ', timed out: ' + (test.timed_out ?? 'unknown'))));
      if (item.feedback) attempt.append(detail('Feedback (original)', item.feedback, 'feedback-' + index));
      if (item.error) attempt.append(detail('Error (original)', item.error, 'error-' + index));
      if (item.review) attempt.append(detail('Codex B summary and verdict (original)', JSON.stringify(item.review, null, 2), 'verdict-' + index));
      attempt.append(detail('Full attempt record', JSON.stringify(item, null, 2), 'attempt-' + index));
      tasks.append(attempt);
    });
    app.append(tasks);
  } else {
    const timeline = panel('Durable event timeline');
    timelineInto(timeline, events);
    app.append(timeline);
    const files = panel('Raw files (on demand)');
    (data.files || []).forEach(file => { const row = el('p'); filesInto(row, [file]); files.append(row); });
    if (!data.files?.length) files.append(el('p', 'No raw files listed.', 'muted'));
    app.append(files);
  }
}
function navigate(name) {
  page = name;
  ['overview', 'tasks', 'logs'].forEach(view => { document.getElementById('nav-' + view).className = 'nav' + (view === page ? ' active' : ''); });
  document.getElementById('page-title').textContent = {overview:'Run overview', tasks:'Tasks', logs:'Logs'}[page];
  if (latest) render(latest);
}
async function refresh() {
  if (!ready || busy) return;
  if (refreshing) return refreshing;
  refreshing = (async () => {
    try { render(await window.pywebview.api.snapshot()); }
    catch (error) {
      latest = {...(latest || {}), controller: {...(latest?.controller || {}), active: null, uncertain: true}};
      updateControls();
      message('Could not load status: ' + error, true);
    }
  })();
  try { await refreshing; } finally { refreshing = null; }
}
async function act(method) {
  if (busy || !ready) return;
  const button = {'select_repo':'browse-project', 'select_plan':'browse-plan', 'open_run':'open-run', start:'start', resume:'resume'}[method];
  if (document.getElementById(button).disabled) return;
  busy = true;
  updateControls();
  message(method === 'start' || method === 'resume' ? 'Waiting for native launch confirmation...' : 'Waiting for selection...');
  try {
    if (refreshing) await refreshing;
    const args = method === 'start' || method === 'resume' ? [Number(document.getElementById('attempts').value)] : [];
    const result = await window.pywebview.api[method](...args);
    message(result.cancelled ? 'Cancelled. No work launched.' : result.error || result.warning || (result.ok ? (args.length ? 'Launch accepted. Waiting for durable runner progress.' : 'Selection updated. No work launched.') : 'Action failed.'), !result.ok && !result.cancelled);
  } catch (error) { message('Action failed: ' + error, true); }
  finally {
    busy = false;
    ready = false;
    updateControls();
    ready = true;
    await refresh();
  }
}
async function showEvidence(name) {
  const box = panel('Raw evidence: ' + name);
  const close = el('button', 'Close evidence');
  close.onclick = () => evidenceViewer.replaceChildren();
  box.append(close);
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
['overview', 'tasks', 'logs'].forEach(name => { document.getElementById('nav-' + name).onclick = () => navigate(name); });
Object.entries({'browse-project':'select_repo', 'browse-plan':'select_plan', 'open-run':'open_run', start:'start', resume:'resume'}).forEach(([id, method]) => { document.getElementById(id).onclick = () => act(method); });
updateControls();
window.addEventListener('pywebviewready', () => { ready = true; refresh(); });
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
        self._launch_lock = threading.Lock()
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
                        tasks, plan_sha = load_tasks(Path(paths["plan"]))
                        status["task_count"] = len(tasks)
                    except Exception as exc:
                        errors["plan"] = str(exc)
                state = read_run(self._root).get("state") if self._root else None
                if self._root and (not state or state.get("plan_sha256") != plan_sha):
                    # A newly selected plan cannot supply counts for a different
                    # inspected run. Prefer its durable manifest or Unknown.
                    status.pop("task_count", None)
                status["validation_errors"] = errors
                status["preflight"] = self._controller.preflight()
                # This is eligibility only; the controller revalidates on launch.
                status["resume_ready"] = bool(
                    not errors and paths["repo"] and plan_sha and state
                    and state.get("status") == "stopped"
                    and state.get("repo") == paths["repo"]
                    and state.get("plan_sha256") == plan_sha
                    and status["active"] is False and not status.get("uncertain")
                    and not status["preflight"].get("errors")
                    and not status["preflight"].get("resume_error"))
                status["selection_error"] = self._selection_error
                return status
            except Exception as exc:
                return {"active": None, "uncertain": True, "exit_code": None,
                        "error": str(exc), "resume_ready": False}

    def _launch(self, action, attempts):
        if not self._launch_lock.acquire(blocking=False):
            return {"ok": False, "error": "A launch confirmation is already in progress."}
        try:
            return self._confirmed_launch(action, attempts)
        finally:
            self._launch_lock.release()

    def _confirmed_launch(self, action, attempts):
        with self._lock:
            try:
                if type(attempts) is not int or not 1 <= attempts <= 10:
                    raise ValueError("Attempts must be an integer between 1 and 10.")
                paths = self._controller.selected_paths()
                branch = self._controller.preflight()["branch"]
                message = (f"{action.upper()} the local runner?\nRepository: {paths['repo']}"
                           f"\nBranch: {branch or 'Unknown (launch will be blocked)'}"
                           f"\nPlan: {paths['plan']}\nSelected logs: {paths['logs']}"
                           f"\nAttempts: {attempts}\nThe runner may modify and commit project files."
                           "\nCheckpoints remain local. No automatic push, merge, force-push or branch creation."
                           "\nLocal Codex CLI authentication is required."
                           "\nClosing the window leaves the runner alive in the background.")
                if self._dialogs is None:
                    raise ValueError("Launch requires native confirmation; dialogs are unavailable.")
                if self._dialogs.confirm(message) is not True:
                    return {"ok": False, "cancelled": True, "error": "Launch cancelled."}
                if self._controller.preflight()["branch"] != branch:
                    raise ValueError("Git branch changed during confirmation. Inspect the current branch and confirm again.")
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
