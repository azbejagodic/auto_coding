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
*{box-sizing:border-box}
body{margin:0;background:#0e1115;color:#f0f3f7;font:15px/1.5 "Segoe UI",system-ui,sans-serif}
button,input,select{font:inherit}
button,select,input{border:1px solid #475361;border-radius:10px;background:#222b36;color:#f0f3f7;padding:10px 16px}
button{cursor:pointer;transition:background .15s}
button:hover{background:#303e4f}
button:disabled{color:#abb5c2;background:#1d232b;cursor:not-allowed}
button:focus-visible,input:focus-visible,select:focus-visible,summary:focus-visible,pre:focus-visible,.selected-path:focus-visible{outline:3px solid #74b6ff;outline-offset:3px}
[hidden]{display:none!important}
.shell{display:flex;min-height:100vh}
.side{position:fixed;inset:0 auto 0 0;width:210px;padding:24px 16px;background:#14181e;border-right:1px solid #2c333e;overflow:auto}
.brand{font-size:21px;font-weight:700;margin:0 0 32px}
.nav{display:block;width:100%;text-align:left;background:transparent;border:1px solid transparent;margin:8px 0;padding:12px 16px;color:#bdc7d4}
.nav.active{background:#1e3149;border-color:#3d6490;color:#fff}
.side p{font-size:14px;margin-top:24px}
.main{margin-left:210px;padding:24px;flex:1;min-width:0;max-width:1700px}
.top{display:flex;justify-content:space-between;gap:16px;align-items:center;margin-bottom:24px}
.top>div:first-child{min-width:0}
.top>div:last-child{flex-shrink:0}
.top h1{font-size:26px;margin:4px 0}
.muted{color:#b0bccb}
.label{color:#b0bccb;font-size:13px;font-weight:600;letter-spacing:.06em;text-transform:uppercase}
.panel,.card{background:#191f27;border:1px solid #303946;border-radius:16px;padding:24px}
.panel{margin:0 0 16px}
.panel h2{font-size:19px;margin:0 0 16px}
.panel h3{font-size:16px;margin:0 0 12px}
.grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:16px;margin:16px 0}
.card{padding:20px}
.value{font-size:22px;font-weight:650;margin-top:8px;overflow-wrap:anywhere}
.blue{color:#81baff}
.green{color:#7be4a5}
.amber,.warning{color:#f5ca7b}
.red{color:#ff9494}
.warning{margin:12px 0}
.setup-fields{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr) 110px;gap:16px}
.field label{display:block;font-weight:600;margin-bottom:8px}
.field-line{display:flex;align-items:flex-start;gap:8px}
.selected-path{margin:0;min-height:46px;flex:1;min-width:0;white-space:pre-wrap;overflow-wrap:anywhere;user-select:text;color:#dbe4ef;padding:10px 12px;background:#0e1115;border-radius:8px}
.controls{display:flex;flex-wrap:wrap;gap:12px;align-items:center;margin-top:16px}
#start{background:#3578ce;border-color:#67a7f3;color:white;font-weight:650;min-width:130px}
#start:hover:enabled{background:#4388df}
#start:disabled{background:#253547;color:#b3c0d1;border-color:#475361}
#resume{border-color:#6f9ac9}
.notice,.path{white-space:pre-wrap;overflow-wrap:anywhere}
.notice p{margin:12px 0}
.empty{padding:24px;background:#191f27;border:1px solid #303946;border-radius:16px}
.empty h2{margin-top:0}
.empty li{margin:12px 0}
details{margin:12px 0}
summary{cursor:pointer;color:#c0d4eb;padding:6px 0}
pre{font:14px/1.6 Consolas,monospace;white-space:pre-wrap;overflow-wrap:anywhere;background:#0e1115;border:1px solid #303946;border-radius:10px;padding:16px;max-height:320px;overflow:auto}
.events{width:100%;border-collapse:collapse;table-layout:fixed}
.events th{text-align:left;color:#b0bccb;font-size:13px}
.events th,.events td{padding:12px 8px;border-bottom:1px solid #303946;vertical-align:top;overflow-wrap:anywhere}
.events th:first-child{width:20%}
.events th:last-child{width:32%}
.table-scroll{overflow-x:auto}
.evidence{display:inline-block;color:#99caff;background:transparent;text-align:left;padding:4px 8px;margin:4px 0;overflow-wrap:anywhere;max-width:100%}
.tasks{display:flex;gap:8px;flex-wrap:wrap;margin:12px 0}
.pill{background:#252f3b;border-radius:16px;padding:6px 12px}
.attempt{border-left:4px solid #7b8b9e;margin-top:16px;padding:16px;background:#141a22;border-radius:12px}
.attempt.approved{border-color:#7be4a5}
.attempt.rejected{border-color:#ff9494}
.filters{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:16px}
.filters label{flex:1;min-width:180px}
.filters input{display:block;width:100%;margin-top:8px;background:#0e1115}
progress{width:100%;height:12px;accent-color:#72adf9}
.project-heading{overflow-wrap:anywhere;max-width:650px}
#header-state{white-space:nowrap}
#evidence-viewer:empty{display:none}
@media(max-width:1100px){.setup-fields{grid-template-columns:minmax(0,1fr)}
.field-line{align-items:center}
.main{padding:20px}
.grid{grid-template-columns:1fr 1fr}
.grid .card:first-child{grid-column:1/-1}
.events{font-size:14px}
}
@media(prefers-reduced-motion:reduce){button{transition:none}
}

</style></head><body>
<div class="shell">
<aside class="side">
<div class="brand">Auto-Coding</div>
<nav aria-label="Main navigation">
<button class="nav active" id="nav-overview" aria-current="page">Overview</button>
<button class="nav" id="nav-tasks">Tasks</button>
<button class="nav" id="nav-logs">Logs</button>
</nav>
<p class="muted">Local Codex CLI authentication is required.</p>
<p class="muted">Closing this window leaves the runner alive. Reopen its logs to inspect progress.</p>
</aside>
<main class="main">
<header class="top">
<div>
<div class="label">Local developer workspace</div>
<h1 id="page-title">Run overview</h1>
<div class="muted project-heading" id="selected-project">No project selected</div>
</div>
<div>
<div id="header-state" class="pill">Idle</div>
<button id="refresh" style="margin-top:12px">Refresh</button>
</div>
</header>
<section class="panel" id="run-setup" aria-labelledby="setup-title">
<h2 id="setup-title">Run setup</h2>
<p class="muted">Choose your project and work plan. Selection never starts work.</p>
<div class="setup-fields">
<div class="field">
<label id="project-label">Project folder</label>
<div class="field-line">
<div id="project-path" class="selected-path" tabindex="0" role="textbox" aria-readonly="true" aria-labelledby="project-label">No project selected</div>
<button id="browse-project" aria-label="Browse project folder">Browse</button>
</div>
</div>
<div class="field">
<label id="plan-label">Task plan</label>
<div class="field-line">
<div id="plan-path" class="selected-path" tabindex="0" role="textbox" aria-readonly="true" aria-labelledby="plan-label">No plan selected</div>
<button id="browse-plan" aria-label="Browse task plan">Browse</button>
</div>
</div>
<div class="field">
<label for="attempts">Attempts</label>
<select id="attempts">
<option value="3" selected>3</option>
<option value="5">5</option>
</select>
</div>
</div>
<div class="controls">
<button id="start" aria-describedby="preflight">Start</button>
<button id="resume" aria-describedby="resume-reason">Resume</button>
<button id="open-run">Open Run</button>
<span id="resume-reason" class="muted">Open a matching stopped run to resume.</span>
</div>
<div id="paths">
</div>
<div id="preflight" class="notice muted">
</div>
<details>
<summary>Launch safeguards</summary>
<p class="muted">Every launch requires native confirmation. START requires a clean feature branch. RESUME requires a stopped run and its original plan, repository and logs. Attempts is the total limit per task; choose 5 if 3 were exhausted. Checkpoints stay local; no automatic push, merge, force-push or branch creation. Model and reasoning settings come from your local Codex CLI.</p>
</details>
</section>
<div id="message" class="notice" role="status" aria-live="polite">
</div>
<div id="evidence-viewer">
</div>
<div id="app">
</div>
</main>
</div><script>
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
let logQuery = '';
function runPresentation(data) {
  const c = data.controller || {}, state = data.state || {};
  if (c.uncertain || (c.active !== false && c.active !== true)) return {label:'Unknown child status', color:'amber'};
  if (c.active) return {label:'Running', color:'blue'};
  if (state.status === 'completed') return {label:'Completed', color:'green'};
  if (state.status === 'checkpointing' || state.pending_checkpoint) return {label:'Awaiting recovery', color:'amber'};
  if (state.status === 'stopped') return {label:'Stopped', color:'amber'};
  if (c.logs || data.root) return {label:state.status ? 'Unknown run state' : 'Awaiting recorded state', color:'amber'};
  return {label:'Idle', color:'muted'};
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
  const content = el('pre', text);
  content.setAttribute('tabindex', '0');
  box.append(el('summary', title), content);
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
  document.getElementById('start').disabled = locked || !c.start_ready;
  document.getElementById('resume').disabled = locked || !c.resume_ready;
  document.getElementById('refresh').disabled = busy || !ready;
}
function filesInto(parent, files) {
  if (!files.length) parent.append(el('span', 'None listed', 'muted'));
  files.forEach(file => {
    const available = file.status === 'available';
    const info = el(available ? 'button' : 'span', file.path + ' (' + file.status + ')', available ? 'evidence' : 'warning');
    if (available) {
      info.onclick = () => showEvidence(file.path);
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
  const scroll = el('div', undefined, 'table-scroll');
  scroll.append(table);
  parent.append(scroll);
}
function render(data) {
  const searchFocused = document.activeElement?.id === 'log-search';
  const searchCursor = searchFocused ? document.activeElement.selectionStart : null;
  latest = data;
  const c = data.controller || {};
  const key = page + JSON.stringify(data);
  updateControls();
  if (runPresentation(data).label === 'Completed' && document.getElementById('message').textContent.startsWith('Launch accepted.')) message('');
  if (renderKey === key) return;
  renderKey = key;
  const paths = document.getElementById('paths');
  paths.replaceChildren();
  [['project-path', c.repo || 'No project selected'], ['plan-path', c.plan || 'No plan selected']].forEach(([id, value]) => {
    const field = document.getElementById(id);
    if (field.textContent !== value) field.textContent = value;
  });
  document.getElementById('selected-project').textContent = c.repo || 'No project selected';
  const git = c.preflight || {};
  if (c.repo) paths.append(el('div', 'Branch: ' + (git.branch || 'Unavailable'), 'path muted'));
  if (c.logs) paths.append(detail('Run folder (select text to copy)', c.logs, 'paths'));
  const errors = [...(git.errors || []), ...Object.entries(c.validation_errors || {}).map(([k,v]) => k + ': ' + v)];
  if (c.selection_error) errors.push(c.selection_error);
  if (git.resume_error) errors.push(git.resume_error);
  if (git.start_error) errors.push(git.start_error);
  const preflight = document.getElementById('preflight');
  preflight.replaceChildren();
  const instruction = c.uncertain || c.active === null ? 'Child status is unknown. Inspect this run before launching more work.'
    : c.active ? 'Runner active. Follow progress below.'
    : c.resume_ready ? 'Ready to RESUME the selected stopped run. START creates a new run.'
    : c.selection_error ? c.selection_error
    : Object.keys(c.validation_errors || {}).length ? errors[0]
    : !c.repo ? 'Choose a project folder to begin.'
    : !c.plan ? 'Choose a valid task plan next.'
    : errors.length ? errors[0]
    : git.clean !== true ? 'Commit or discard project changes before START. A matching stopped run may be eligible for RESUME.'
    : 'Ready to START. Review the confirmation before launching.';
  preflight.append(el('p', (c.active ? 'Running - ' : c.start_ready || c.resume_ready ? 'Ready - ' : 'Action needed - ') + instruction));
  preflight.append(detail('Details - preflight and launch safeguards', JSON.stringify({branch:git.branch, clean:git.clean, start_ready:c.start_ready, resume_ready:c.resume_ready, diagnostics:errors}, null, 2), 'preflight'));
  document.getElementById('resume-reason').textContent = c.resume_ready ? 'Continue the original stopped run.' : c.active || c.uncertain ? 'Resume unavailable while the runner is active or uncertain.' : 'Resume requires a matching stopped run, original plan and available attempts.';
  document.getElementById('run-setup').hidden = page !== 'overview';
  const presentation = runPresentation(data);
  const badge = document.getElementById('header-state');
  badge.textContent = presentation.label;
  badge.className = 'pill ' + presentation.color;
  if (presentation.label === 'Completed' && document.getElementById('message').textContent.startsWith('Launch accepted.')) message('');
  updateControls();
  if (evidenceRoot !== data.root) {
    evidenceViewer.replaceChildren();
    expanded.clear();
    evidenceRoot = data.root;
  }
  app.replaceChildren();
  if (!c.logs && !data.root) {
    const empty = el('section', undefined, 'empty');
    empty.append(el('h2', page === 'overview' ? 'Ready for your first run' : page === 'tasks' ? 'Your task history will appear here' : 'Your run evidence will appear here'));
    empty.append(el('p', c.start_ready ? 'Ready to begin. Click Start to review your launch confirmation.' : 'Start with a project and task plan, or open a saved run to inspect earlier work.'));
    const steps = el('ol');
    ['Choose a clean Git project on a feature branch.', 'Select a TXT, MD or JSON task plan and an attempt limit.', 'Review the native confirmation, then follow progress here.'].forEach(text => steps.append(el('li', text)));
    empty.append(steps);
    if (page !== 'overview') { const setup = el('button', 'Go to run setup'); setup.onclick = () => navigate('overview'); empty.append(setup); }
    app.append(empty);
    if (c.error) app.append(el('div', c.error, 'warning'));
    return;
  }
  const state = data.state || {};
  const events = data.events || [];
  const history = Array.isArray(state.history) ? state.history.filter(item => item && typeof item === 'object') : [];
  const completed = Array.isArray(state.completed) ? state.completed : null;
  const status = presentation.label;
  const reconciled = c.active === false && !c.uncertain && ['completed', 'checkpointing'].includes(state.status);
  if (c.error && !reconciled) app.append(el('div', c.error, 'notice warning'));
  if (data.state_error) app.append(el('div', data.state_error, 'warning'));
  if (data.empty) app.append(el('div', 'No run state or events found. Select a project and plan to START, or inspect available raw files in Logs.', 'empty'));
  if (c.logs && !data.exists && !data.state_error) app.append(el('div', 'Log directory is missing or unavailable.', 'warning'));
  if (state.pending_checkpoint) app.append(el('div', 'A checkpoint is pending reconciliation. No completion is inferred.', 'warning'));
  (data.event_errors || []).forEach(error => app.append(el('div', error, 'warning')));
  if (page === 'overview') {
    const summary = el('div', undefined, 'grid');
    const count = data.task_count ?? c.task_count;
    [['Run status', status], ['Completed tasks', count != null ? (completed ? completed.length : '--') + ' / ' + count : c.logs ? 'Not recorded' : 'Select a plan'], ['Child process', c.active === true ? 'Running' : c.active === false ? 'Inactive' : 'Unknown']].forEach(([label,value]) => {
      const card = el('div', undefined, 'card');
      card.append(el('div', label, 'label'), el('div', value, 'value ' + (label === 'Run status' ? presentation.color : '')));
      summary.append(card);
    });
    app.append(summary);
    if (count > 0 && completed) {
      const progress = el('progress');
      progress.setAttribute('max', count); progress.setAttribute('value', completed.length);
      progress.setAttribute('aria-label', 'Completed tasks');
      app.append(progress);
    }
    const current = panel('Current task');
    current.append(el('div', 'Current task: ' + (state.current_task || 'No task in progress'), 'value'));
    if (state.status === 'checkpointing') current.append(el('p', 'Checkpoint recovery is pending. Inspect the logs; RESUME remains subject to controller safety checks.', 'warning'));
    if (state.status === 'stopped') current.append(el('p', 'Run stopped. Inspect the recorded attempt and runner-output.txt in Logs. If Codex reports authentication required, sign in through the local Codex CLI before retrying.', 'muted'));
    if (c.exit_code != null || c.error || c.process_history?.length) current.append(detail('Process history - prior exits and diagnostics', JSON.stringify({previous_child_exit_code:c.exit_code, diagnostic:c.error, history:c.process_history || [], note:'Historical process results do not replace the current durable run outcome.'}, null, 2), 'process-history'));
    if (count != null && completed) current.append(el('p', 'Tasks remaining: ' + Math.max(0, count - completed.length)));
    if (history.length) current.append(detail('Latest attempt details', JSON.stringify(history[history.length - 1], null, 2), 'latest-attempt'));
    app.append(current);
    const recent = panel('Recent activity');
    events.slice(-5).reverse().forEach(event => {
      recent.append(el('p', [event.timestamp_utc || 'Time not recorded', event.actor, event.event, event.outcome].filter(Boolean).join(' | ')));
    });
    if (!events.length) recent.append(el('p', 'No events recorded yet. Full evidence is available in Logs.', 'muted'));
    const logs = el('button', 'View all activity'); logs.onclick = () => navigate('logs'); recent.append(logs);
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
      const decision = item.review && item.review.decision;
      const outcome = String(decision || '').toLowerCase();
      const attempt = el('section', undefined, 'attempt ' + (outcome === 'approved' ? 'approved' : outcome === 'rejected' ? 'rejected' : ''));
      const verdict = el('div', decision || 'Review not recorded', 'pill ' + (outcome === 'approved' ? 'green' : outcome === 'rejected' ? 'red' : 'muted'));
      attempt.append(verdict);
      if (item.commit) attempt.append(el('p', 'Local commit: ' + item.commit, 'path green'));
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
    const filters = el('div', undefined, 'filters');
    const label = el('label', 'Search actor, outcome or event');
    const search = el('input'); search.id = 'log-search'; search.setAttribute('type', 'search'); search.setAttribute('aria-label', 'Search actor, outcome or event');
    search.value = logQuery;
    label.append(search); filters.append(label); timeline.append(filters);
    const results = el('div');
    const draw = () => {
      results.replaceChildren();
      const query = logQuery.toLowerCase();
      const filtered = events.filter(event => JSON.stringify(event).toLowerCase().includes(query));
      results.append(el('p', filtered.length + ' of ' + events.length + ' events', 'muted'));
      timelineInto(results, filtered);
    };
    search.oninput = () => { logQuery = search.value; draw(); };
    draw(); timeline.append(results);
    app.append(timeline);
    const files = panel('Raw evidence files');
    files.append(el('p', 'Select a file to read its original text below. Display is bounded to 1 MB per file.', 'muted'));
    (data.files || []).forEach(file => { const row = el('p'); filesInto(row, [file]); files.append(row); });
    if (!data.files?.length) files.append(el('p', 'No raw files listed.', 'muted'));
    app.append(files);
    if (searchFocused) { search.focus(); search.setSelectionRange(searchCursor, searchCursor); }
  }
}
function navigate(name) {
  page = name;
  ['overview', 'tasks', 'logs'].forEach(view => { const nav = document.getElementById('nav-' + view); nav.className = 'nav' + (view === page ? ' active' : ''); nav.setAttribute('aria-current', view === page ? 'page' : 'false'); });
  document.getElementById('page-title').textContent = {overview:'Run overview', tasks:'Tasks', logs:'Logs'}[page];
  if (latest) render(latest);
}
async function refresh(clearMessage = true) {
  if (!ready || busy) return;
  if (refreshing) return refreshing;
  refreshing = (async () => {
    try { const data = await window.pywebview.api.snapshot(); if (clearMessage) message(''); render(data); }
    catch (error) {
      latest = {...(latest || {}), controller: {...(latest?.controller || {}), active: null, uncertain: true}};
      render(latest);
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
  let accepted = false;
  message(method === 'start' || method === 'resume' ? 'Waiting for native launch confirmation...' : 'Waiting for selection...');
  try {
    if (refreshing) await refreshing;
    const args = method === 'start' || method === 'resume' ? [Number(document.getElementById('attempts').value)] : [];
    const result = await window.pywebview.api[method](...args);
    accepted = result.ok && !result.cancelled;
    if (args.length && accepted) navigate('overview');
    message(result.cancelled ? 'Cancelled. No work launched.' : result.error || result.warning || (result.ok ? (args.length ? 'Launch accepted. Waiting for durable runner progress.' : 'Selection updated. No work launched.') : 'Action failed.'), !result.ok && !result.cancelled);
  } catch (error) { message('Action failed: ' + error, true); }
  finally {
    busy = false;
    ready = false;
    updateControls();
    ready = true;
    await refresh(false);
    if (accepted && ['start', 'resume', 'open_run'].includes(method)) message('');
  }
}
async function showEvidence(name) {
  const box = panel('Raw evidence: ' + name);
  const close = el('button', 'Close evidence');
  close.onclick = () => evidenceViewer.replaceChildren();
  box.append(close);
  box.append(el('div', 'This file is displayed only because you selected it. Treat contents as potentially sensitive.', 'warning'));
  const content = el('pre', 'Loading...');
  content.setAttribute('tabindex', '0');
  content.setAttribute('aria-label', 'Raw evidence text');
  box.append(content);
  // Keep selected evidence visible while the timeline periodically refreshes.
  evidenceViewer.replaceChildren(box);
  box.scrollIntoView?.({block:'nearest'});
  content.focus?.({preventScroll:true});
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
                status["start_ready"] = bool(
                    paths["repo"] and plan_sha and not errors
                    and status["active"] is False and not status.get("uncertain")
                    and status["preflight"].get("clean") is True
                    and not status["preflight"].get("start_error")
                    and not status["preflight"].get("errors"))
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
        window = webview.create_window("Auto-Coding - Run Dashboard", html=HTML, js_api=api, width=1280, height=800, min_size=(1000, 600))
        api._dialogs = NativeDialogs(webview, window)
        webview.start()
        return 0
    except Exception as exc:
        print(f"Could not start the desktop window. On Windows install Microsoft Edge WebView2 Runtime and pywebview: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
