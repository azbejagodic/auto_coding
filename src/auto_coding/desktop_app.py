"""Optional native desktop viewer for Auto-Coding run logs. Read-only Slice 1."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from auto_coding.desktop_reader import read_run, read_evidence


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


class Bridge:
    def __init__(self, root: Path | None): self.root = root
    def snapshot(self):
        return read_run(self.root) if self.root else {"root":"", "exists":False, "state":None, "state_error":"No log directory selected. Open a run with --logs PATH.", "events":[], "event_errors":[], "empty":True}
    def evidence(self, name):
        return read_evidence(self.root, name) if self.root else {"ok":False, "text":"No log directory selected."}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Read-only Auto-Coding run log desktop viewer")
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
        webview.start()
        return 0
    except Exception as exc:
        print(f"Could not start the desktop window. On Windows install Microsoft Edge WebView2 Runtime and pywebview: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
