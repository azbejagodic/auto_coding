"""Optional native desktop bridge; importing this module never loads a GUI."""
from __future__ import annotations

import argparse
import sys
import threading
from pathlib import Path

from auto_coding.desktop_reader import read_run, read_evidence
from auto_coding.desktop_control import DesktopController
from auto_coding.desktop_settings import DesktopSettings, clean_preferences
from auto_coding.simple_run import load_tasks
from auto_coding.debug_export import export_debug, export_run, WARNING


HTML = r'''<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>Auto-Coding</title><style>
*{box-sizing:border-box}
body{margin:0;background:#202935;color:#f0f3f7;font:16px/1.5 "Segoe UI",system-ui,sans-serif}
button,input,select{font:inherit}
input::placeholder{color:#b0bccb;opacity:1}
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
.main{margin-left:210px;padding:24px 32px;flex:1;min-width:0;max-width:1700px;overflow-wrap:anywhere}
.top{display:flex;justify-content:space-between;gap:16px;align-items:center;min-height:46px;margin-bottom:12px}
.top h1{font-size:26px;line-height:1.5;margin:0;min-width:0}
.header-actions{display:flex;align-items:center;gap:12px;margin-left:auto;flex-shrink:0}
.header-actions .pill{min-width:112px;height:46px;display:flex;align-items:center;justify-content:center;padding:10px 12px;white-space:nowrap}
.header-actions button{width:104px;height:46px;padding:10px 16px;flex-shrink:0}
.muted{color:#b0bccb}
.label{color:#b0bccb;font-size:13px;font-weight:600;letter-spacing:.06em;text-transform:uppercase}
.panel,.card{background:#293544;border:1px solid #536174;border-radius:16px;padding:16px}
.panel{margin:0 0 16px}
.panel h2{font-size:19px;margin:0 0 16px}
.panel h3{font-size:16px;margin:0 0 12px}
.grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:16px;margin:16px 0}
.card{padding:16px}
.value{font-size:22px;font-weight:650;margin-top:8px;overflow-wrap:anywhere}
.blue{color:#81baff}
.green{color:#7be4a5}
.amber,.warning{color:#f5ca7b}
.red{color:#ff9494}
.warning{margin:12px 0}
.setup-fields{display:grid;grid-template-columns:minmax(0,1fr) 150px;gap:20px;margin-top:20px}
.field label{display:block;font-weight:600;margin-bottom:8px}
#attempts{width:100%;padding:10px}
#github-repo{flex:1;min-width:0;width:100%}
.repository-line{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:12px}
.branch-line{display:flex;flex-wrap:wrap;gap:12px;align-items:center;margin-top:12px}
#github-branch{flex:1;min-width:0;max-width:100%}
#github-state{margin:8px 0 12px;overflow-wrap:anywhere}
.step-title{display:block;font-weight:650;margin-bottom:8px}
.launch-row{display:flex;gap:16px;align-items:center;flex-wrap:wrap;margin-top:20px}
#start-reason{flex:1;min-width:180px}
.push-policy{display:block;margin-top:12px}
.secondary{background:#242e3b;border:1px solid #536174;border-radius:12px;padding:8px 16px;margin:16px 0}
.secondary>summary{font-weight:650}
.secondary .controls{margin:12px 0}
.controls>*{max-width:100%;min-width:0}
#branch-form{padding:16px;background:#202935;border-radius:10px}
#branch-name,#branch-base{max-width:100%;min-width:0}
.notice:empty{display:none}
#attempts[aria-invalid="true"]{border-color:#ff9494}
.field-line{display:flex;align-items:flex-start;gap:12px;min-width:0}
.selected-path{margin:0;min-height:46px;max-height:96px;overflow:auto;flex:1;min-width:0;white-space:pre-wrap;overflow-wrap:anywhere;user-select:text;color:#dbe4ef;padding:10px 12px;background:#0e1115;border-radius:8px}
.controls{display:flex;flex-wrap:wrap;gap:12px;align-items:center;margin-top:16px}
#start{background:#8bc3ff;border-color:#b8dbff;color:#10283f;font-size:18px;font-weight:700;min-width:160px;padding:12px 24px}
#start:hover:enabled{background:#b8dbff}
#start:disabled{background:#253547;color:#b3c0d1;border-color:#475361}
#resume{border-color:#6f9ac9;font-weight:650}
.notice,.path{white-space:pre-wrap;overflow-wrap:anywhere}
.notice p{margin:12px 0}
.empty{padding:16px;background:#293544;border:1px solid #536174;border-radius:16px}
.empty p{margin:0}
details{margin:12px 0}
summary{cursor:pointer;color:#c0d4eb;padding:6px 0}
pre{font:14px/1.6 Consolas,monospace;white-space:pre-wrap;overflow-wrap:anywhere;background:#0e1115;border:1px solid #303946;border-radius:10px;padding:16px;max-height:320px;overflow:auto}
.events{width:100%;border-collapse:collapse;table-layout:fixed}
.events th{text-align:left;color:#b0bccb;font-size:13px}
.events th,.events td{padding:12px 8px;border-bottom:1px solid #303946;vertical-align:top;overflow-wrap:anywhere}
.events th:first-child{width:20%}
.events th:last-child{width:32%}
.table-scroll{overflow-x:auto}
.events{min-width:900px}
.evidence{display:inline-block;color:#99caff;background:transparent;text-align:left;padding:4px 8px;margin:4px 0;overflow-wrap:anywhere;max-width:100%}
.tasks{display:flex;gap:8px;flex-wrap:wrap;margin:12px 0}
.pill{background:#252f3b;border-radius:16px;padding:6px 12px}
.attempt{border-left:4px solid #7b8b9e;margin-top:16px;padding:16px;background:#141a22;border-radius:12px}
.attempt.approved{border-color:#7be4a5}
.attempt.rejected{border-color:#ff9494}
.run-row{display:flex;flex-wrap:wrap;align-items:center;gap:6px 12px;border-top:1px solid #536174;padding:6px 0;font-size:14px}
.run-row .run-label{flex:1;min-width:180px}
.run-row details{width:100%;margin:0}
.run-row button{padding:6px 12px}
.run-download{width:145px;flex:none;background:#8bc3ff;border-color:#b8dbff;color:#10283f;font-weight:700}
.run-download:hover:enabled{background:#b8dbff}
.run-label small{display:block}
.run-result{width:100%}
.run-list{max-height:65vh;overflow-y:auto}
.activity-row{border-top:1px solid #536174;margin:0;font-size:14px}
.activity-row>summary{padding:6px 0}
.logs-status p{margin:6px 0}
.filters{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:16px}
.filters label{flex:1;min-width:180px}
.filters input{display:block;width:100%;margin-top:8px;background:#0e1115}
progress{width:100%;height:12px;accent-color:#72adf9}
#evidence-viewer:empty{display:none}
/* Compact Tasks rows; technical evidence stays in native expanders. */
.task-list{padding:0;border:1px solid #536174;border-radius:12px;background:#293544}
.task-progress{margin:0 0 10px;font-size:14px;color:#b0bccb}
.task-row{margin:0;border-bottom:1px solid #475361;padding:8px 12px;min-width:0}
.task-row:last-child{border-bottom:0}
.task-row>summary{padding:2px 0;list-style-position:inside}
.task-heading{display:inline;font-weight:600;color:#f0f3f7}
.task-meta{font-size:13px;color:#b0bccb;margin-left:8px;font-weight:400}
.phase-strip{display:flex;flex-wrap:wrap;gap:2px 10px;margin:3px 0 0 18px;font-size:13px;color:#b0bccb}
.phase-strip span{max-width:100%;overflow-wrap:anywhere}
.task-stop{font-size:13px;margin:4px 0 0 18px;color:#f5ca7b}
.task-row .attempt{padding:10px;margin:8px 0;font-size:14px}
.task-row .attempt h3{font-size:14px;margin:0 0 4px}
.task-row pre{font-size:13px;padding:10px}
.task-row .events{min-width:0;font-size:12px}
.task-row .events th,.task-row .events td{padding:4px}
/* Compact Overview setup. */
#run-setup{padding:12px;margin-bottom:12px}
#run-setup .setup-fields{grid-template-columns:minmax(0,1fr) 100px;gap:12px;margin-top:12px}
#run-setup .launch-row{gap:12px;margin-top:12px}
#run-setup .launch-row button{height:48px;padding:8px 16px;white-space:nowrap}
#run-setup #start,#run-setup #resume{width:140px;min-width:140px;font-size:18px}
#run-setup .branch-line{gap:8px}
#run-setup .branch-line button,#run-setup .repository-line button,#run-setup #github-repo{padding:8px 12px}
#run-setup:not([hidden]) ~ #app .grid{margin:12px 0}
#run-setup .step-title,#run-setup .field label{margin-bottom:4px}
#run-setup .notice p{margin:6px 0}
#run-setup .push-policy{margin-top:8px}
#run-setup .selected-path{max-height:48px}
#run-setup #github-state{font-size:14px;margin:4px 0}
#run-setup:not([hidden]) ~ #app .card{padding:12px}
#run-setup:not([hidden]) ~ #app .value{margin-top:4px}
#github-state:empty,#start-reason:empty,#resume-reason:empty,#push-reason:empty{display:none}
@media(max-width:1100px){.setup-fields{grid-template-columns:minmax(0,1fr)}
.field-line{align-items:center}
.main{padding:20px}
.events{font-size:14px}
}
@media(max-width:760px){
.side{position:static;width:auto;padding:12px 16px}.shell{display:block}.brand{margin:0 0 8px}.side nav{display:flex;gap:8px}.nav{margin:0;padding:10px}.main{margin-left:0;padding:16px}.top h1{font-size:24px}.repository-line{grid-template-columns:minmax(0,1fr)}#run-setup .setup-fields{grid-template-columns:minmax(0,1fr)}.grid{grid-template-columns:minmax(0,1fr)}.field-line{flex-wrap:wrap}.selected-path{flex-basis:100%}.panel{padding:16px}.filters label{min-width:0}
}
@media(max-width:480px){
.top{flex-wrap:wrap}
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
</aside>
<main class="main">
<header id="dashboard-header" class="top" aria-labelledby="page-title">
<h1 id="page-title">Overview</h1>
<div class="header-actions">
<div id="header-state" class="pill" role="status" aria-live="polite" aria-atomic="true">Idle</div>
<button id="refresh">Refresh</button>
</div>
</header>
<section class="panel" id="run-setup" aria-labelledby="setup-title">
<label id="setup-title" class="step-title" for="github-repo">Repository</label>
<div class="repository-line">
<input id="github-repo" aria-label="GitHub repository" placeholder="OWNER/REPO or GitHub URL">
<button id="connect-github">Connect Repository</button>
</div>
<label class="step-title" for="github-branch" style="margin-top:12px">Branch</label>
<div class="branch-line" style="margin-top:0">
<select id="github-branch" aria-label="Feature branch"></select>
<button id="fetch-github">Fetch Branches</button>
<button id="select-branch">Select Branch</button>
<button id="new-branch">New Branch</button>
</div>
<p id="github-state" class="muted"></p>
<div id="branch-form" class="controls" hidden>
<label for="branch-name">New feature branch</label>
<input id="branch-name">
<label for="branch-base">Remote base</label>
<select id="branch-base" aria-describedby="branch-base-confirm-label"></select>
<label id="branch-base-confirm-label" hidden><input id="branch-base-confirm" type="checkbox"> I confirm creating this feature branch from the selected remote base.</label>
<button id="create-branch">Create branch</button><button id="cancel-branch">Cancel</button>
</div>
<div class="setup-fields">
<div class="field">
<label id="plan-label">Task plan</label>
<div class="field-line">
<div id="plan-path" class="selected-path" tabindex="0" role="textbox" aria-readonly="true" aria-labelledby="plan-label">No plan selected</div>
<button id="browse-plan" aria-label="Browse task plan">Browse</button>
</div>
</div>
<div class="field">
<label for="attempts">Attempts</label>
<input id="attempts" type="number" min="1" max="10" step="1" value="3" required aria-describedby="attempts-hint attempts-error">
<small id="attempts-hint" class="muted">1–10 per task</small>
</div>
</div>
<div id="preferences-notice" class="notice muted" role="status"></div>
<div id="attempts-error" class="notice red" role="status" aria-live="polite"></div>
<div class="launch-row">
<button id="start" aria-describedby="preflight start-reason attempts-error">Start</button>
<button id="resume" aria-describedby="preflight resume-reason attempts-error">Resume</button>
<button id="push-approved" aria-describedby="push-reason">Push Approved Commits</button>
</div>
<div id="start-reason" class="muted" role="status" aria-live="polite"></div>
<div id="resume-reason" class="muted"></div>
<div id="push-reason" class="muted"></div>
<div id="preflight" class="notice muted" role="status" aria-live="polite" aria-atomic="true"></div>
<div id="message" class="notice" role="status" aria-live="polite"></div>
<label class="push-policy"><input id="auto-push" type="checkbox" checked> Automatically push approved tasks</label>
</section>
<section id="logs-tools" hidden aria-label="Repository tools and diagnostics">
<details id="secondary-controls">
<summary>Repository tools and preflight diagnostics</summary>
<div class="controls">
<button id="list-github">My repositories</button>
<select id="github-repositories" aria-label="Authenticated repositories" hidden></select>
</div>
<details id="local-project"><summary>Local folder fallback</summary>
<div class="field">
<label id="project-label">Project folder</label>
<div class="field-line">
<div id="project-path" class="selected-path" tabindex="0" role="textbox" aria-readonly="true" aria-labelledby="project-label">No project selected</div>
<button id="browse-project" aria-label="Browse project folder">Browse</button>
</div>
</div>
</details>
<details id="launch-details">
<summary>Preflight diagnostics</summary>
<p class="muted">Start and Resume require native confirmation. Local Codex CLI authentication is required. Closing this window leaves the runner alive.</p>
<pre id="preflight-details" tabindex="0" aria-label="Preflight diagnostics"></pre>
<div id="lock-recovery"></div>
<div id="run-folder" class="path muted"></div>
</details>
</details>
</section>
<section id="run-selection" hidden aria-label="Saved run selection">
<button id="open-run">Open Run</button>
</section>
<div id="logs-message" class="notice" role="status" aria-live="polite" hidden></div>
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
let recentRuns = {runs:[]};
function runPresentation(data) {
  const c = data.controller || {}, state = data.state || {};
  if (c.uncertain || (c.active !== false && c.active !== true)) return {label:'Unknown child status', color:'amber'};
  if (c.active) return {label:'Running', color:'blue'};
  if (state.publication?.outcome === 'pending') return {label:'Approved locally, push pending', color:'amber'};
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
const controls = ['github-repo', 'connect-github', 'list-github', 'github-repositories', 'github-branch', 'select-branch', 'new-branch', 'fetch-github', 'branch-name', 'branch-base', 'branch-base-confirm', 'create-branch', 'cancel-branch', 'push-approved', 'browse-project', 'browse-plan', 'open-run', 'start', 'resume', 'attempts'];
function detail(title, text, key) {
  const box = el('details');
  box.detailKey = key;
  box.open = expanded.has(key);
  box.ontoggle = () => { if (box.open) expanded.add(key); else expanded.delete(key); };
  const content = el('pre', text);
  content.setAttribute('tabindex', '0');
  const summary = el('summary', title);
  summary.taskFocus = key;
  box.append(summary, content);
  return box;
}
function disclosure(title, key, cls) {
  const box = detail(title, '', key);
  box.replaceChildren(box.children[0]);
  if (cls) box.className = cls;
  return box;
}
function eventRowsInto(parent, events, scope) {
  events.forEach(event => {
    const row = disclosure([event.actor, event.event, event.outcome,
      event.task_id, event.attempt == null ? null : 'Attempt ' + event.attempt].filter(Boolean).join(' · '),
      scope + '-event-' + (event.seq ?? event._source_order), 'activity-row');
    row.append(el('p', 'UTC timestamp: ' + (event.timestamp_utc ?? '--')));
    // Keep every recorded field and the original evidence actions available.
    filesInto(row, event._evidence_status || []);
    row.append(detail('Original event', JSON.stringify(event, null, 2), scope + '-original-' + (event.seq ?? event._source_order)));
    parent.append(row);
  });
}
function message(text, error = false) {
  for (const id of ['message', 'logs-message']) {
    const box = document.getElementById(id);
    box.textContent = text || '';
    box.className = 'notice ' + (error ? 'red' : 'blue');
  }
}
function restoreActionFocus(id) {
  // Disabling the pressed button during a bridge call can move focus to body.
  // Preserve navigation if the user has already focused another control.
  const button = document.getElementById(id);
  if (document.body && document.activeElement === document.body && !button.disabled) button.focus();
}
function updateControls() {
  const c = latest?.controller || {};
  const locked = busy || !ready || c.active !== false || c.uncertain;
  controls.forEach(id => { document.getElementById(id).disabled = locked; });
  ['github-branch', 'select-branch', 'new-branch', 'branch-base', 'branch-base-confirm', 'create-branch'].forEach(id => {
    document.getElementById(id).disabled = locked || !c.repository || c.preflight?.clean !== true;
  });
  document.getElementById('fetch-github').disabled = locked || !c.repository;
  const protectedBranch = ['main', 'master'].includes((c.preflight?.branch || '').toLowerCase());
  ['connect-github', 'browse-project', 'github-repositories'].forEach(id => {
    document.getElementById(id).disabled = locked || (!!c.repo && c.preflight?.clean !== true);
  });
  const selectedBranch = document.getElementById('github-branch').value;
  document.getElementById('select-branch').disabled ||= !selectedBranch || ['main', 'master'].includes(selectedBranch.toLowerCase());
  document.getElementById('auto-push').disabled = locked;
  const base = document.getElementById('branch-base').value;
  document.getElementById('branch-base-confirm-label').hidden = !base;
  document.getElementById('create-branch').disabled ||= !base || !document.getElementById('branch-base-confirm').checked;
  document.getElementById('push-approved').disabled = locked || protectedBranch || c.preflight?.clean !== true || !c.push?.eligible;
  const pushReason = c.push?.reason || '';
  // Managed checkout locks can block both launch and push while this controller
  // itself is inactive. The stable launch status already explains that blocker.
  const launchExplainsPush = (c.preflight?.errors || []).includes('An Auto-Coding runner is active or uncertain for this checkout.')
    || c.preflight?.locks?.length && pushReason === 'An Auto-Coding runner is active or uncertain for this checkout.'
    || document.getElementById('preflight').textContent === pushReason;
  document.getElementById('push-reason').textContent = c.repository && c.active === false && !c.uncertain
    && !c.push?.eligible && !launchExplainsPush ? pushReason : '';
  const savedPush = !!latest?.state?.auto_push;
  const savedRun = !!c.logs || !!latest?.root;
  const push = document.getElementById('auto-push');
  // A stopped run may also be used as setup for a new Start: retain that opt-in separately.
  const pushContext = JSON.stringify([c.logs, latest?.root]);
  const statusKnown = (c.active === false || c.active === true) && !c.uncertain;
  // An unavailable status is not a run transition. Keep the setup choice and
  // context until a successful snapshot can establish whether a run started.
  if (statusKnown) {
    const showingSavedPolicy = savedRun && (c.active
      || (!c.start_ready && push.newSetupContext !== pushContext));
    if (showingSavedPolicy) {
      push.checked = savedPush;
      if (c.active) push.newSetupContext = null;
    }
    // A new setup defaults ON; polling preserves an explicit OFF until the run ends.
    else if (push.showingSavedPolicy || push.setupContext !== pushContext) push.checked = true;
    if (!showingSavedPolicy) push.newSetupContext = pushContext;
    push.setupContext = pushContext;
    push.showingSavedPolicy = showingSavedPolicy;
  }
  push.disabled = locked || !!push.showingSavedPolicy;
  push.indeterminate = !statusKnown || (!!push.showingSavedPolicy && !latest?.state);
  const validAttempts = validateAttempts();
  const branchPending = !!c.repository && (!selectedBranch || selectedBranch !== c.preflight?.branch || !!c.github?.error);
  document.getElementById('start').disabled = locked || protectedBranch || branchPending || !c.start_ready || !validAttempts;
  document.getElementById('resume').disabled = locked || protectedBranch || !c.resume_ready || !validAttempts;
  const startReason = !ready ? 'Loading repository status...' : busy ? 'Finish the current action first.'
    : branchPending && c.start_ready ? 'Select and verify the chosen feature branch before starting.' : '';
  document.getElementById('start-reason').textContent = startReason;
  document.getElementById('resume').textContent = latest?.state?.publication?.outcome === 'pending' ? 'Retry Push' : 'Resume';
  document.getElementById('refresh').disabled = busy || !ready;
}
function validateAttempts() {
  const input = document.getElementById('attempts');
  const value = input.value.trim();
  const number = Number(value);
  const valid = /^\d+$/.test(value) && Number.isInteger(number) && number >= 1 && number <= 10;
  input.setAttribute('aria-invalid', String(!valid));
  document.getElementById('attempts-error').textContent = valid ? '' : 'Cannot launch: attempts must be a whole number from 1 to 10.';
  return valid;
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
// Pure projection of durable events: no runner state or inferred review verdicts.
function compactTime(ms) {
  if (ms === null || !Number.isFinite(ms) || ms < 0) return '\u2014';
  const seconds = Math.floor(ms / 1000);
  if (seconds < 60) return seconds + 's';
  if (seconds < 3600) return Math.floor(seconds / 60) + ':' + String(seconds % 60).padStart(2, '0');
  return Math.floor(seconds / 3600) + ':' + String(Math.floor(seconds / 60) % 60).padStart(2, '0') + 'h';
}
function eventTime(event) {
  const time = typeof event?.timestamp_utc === 'string' ? Date.parse(event.timestamp_utc) : NaN;
  return Number.isFinite(time) ? time : null;
}
function spanTime(start, end) {
  return start !== null && end !== null && end >= start ? end - start : null;
}
function sumTimes(values) {
  return values.length && values.every(value => value !== null) ? values.reduce((a, b) => a + b, 0) : null;
}
function taskPresentation(data, now = Date.now()) {
  const state = data.state || {}, c = data.controller || {};
  const events = Array.isArray(data.events) ? data.events.filter(e => e && typeof e === 'object') : [];
  const history = Array.isArray(state.history) ? state.history.filter(item => item && typeof item === 'object') : [];
  const plan = Array.isArray(c.task_steps) ? c.task_steps : [];
  const ids = [...new Set([...(data.task_ids || plan.map(t => t.id)), ...history.map(h => h.task),
    ...events.map(e => e.task_id), ...(Array.isArray(state.completed) ? state.completed : []), state.current_task].filter(Boolean))];
  return ids.map(id => {
    const records = history.filter(h => h.task === id);
    // Push/checkpoint recovery events can omit attempt. Associate them with the
    // latest reached attempt, without altering their original evidence records.
    let reachedTask = null, reachedAttempt = null;
    const attributed = [];
    events.forEach((event, index) => {
      if (event.task_id) {
        if (event.task_id !== reachedTask) reachedAttempt = null;
        reachedTask = event.task_id;
        if (event.attempt != null) reachedAttempt = event.attempt;
      }
      if ((event.task_id || (event.event === 'stopped' ? reachedTask : null)) === id) {
        attributed.push({event, index, number:event.attempt ?? reachedAttempt ?? records.at(-1)?.attempt ?? 1});
      }
    });
    const groups = [];
    attributed.forEach(entry => {
      if (!groups.length || entry.event.event === 'task_started' || groups.at(-1).number !== entry.number) {
        groups.push({number:entry.number, entries:[], record:null});
      }
      groups.at(-1).entries.push(entry);
    });
    records.forEach(record => {
      // An interrupted session can reuse an attempt number on Resume. Its
      // eventual history record belongs only to the last matching session.
      const number = record.attempt ?? 1;
      const group = groups.filter(g => g.number === number && !g.record).at(-1);
      if (group) group.record = record;
      else groups.push({number, entries:[], record});
    });
    groups.sort((a, b) => Number(a.number) - Number(b.number));
    const attempts = groups.map(({number, entries, record}, session) => {
      const own = entries.map(e => e.event);
      const lastIndex = entries.at(-1)?.index ?? -1;
      const live = c.active === true && !c.uncertain && session === groups.length - 1
        && !events.slice(lastIndex + 1).some(e => ['resume_started', 'stopped', 'task_started'].includes(e.event))
        && own.length > 0 && !['stopped', 'checkpoint_stopped', 'correction_requested', 'task_completed'].includes(own.at(-1)?.event)
        && (state.current_task === id || ['push_started', 'checkpoint_started'].includes(own.at(-1)?.event));
      const phaseFor = e => e.actor === 'Codex A' ? 'Codex A' : String(e.event || '').startsWith('test_') ? 'Tests'
        : e.actor === 'Codex B' ? 'Codex B' : String(e.event || '').startsWith('checkpoint_') ? 'Commit'
        : String(e.event || '').startsWith('push_') ? 'Push' : null;
      const terminal = e => ['stopped', 'checkpoint_stopped', 'correction_requested', 'task_completed'].includes(e.event);
      const decision = record?.review?.decision || own.filter(e => e.event === 'review_finished').at(-1)?.outcome;
      const approved = String(decision || '').toLowerCase() === 'approved';
      const tests = Array.isArray(record?.tests) ? record.tests.filter(t => t && typeof t === 'object') : [];
      const phases = ['Codex A', 'Tests', 'Codex B', 'Commit', 'Push'].map(name => {
        const relevant = own.filter(e => phaseFor(e) === name);
        let status = 'Waiting', open = null;
        const intervals = [];
        let missing = false;
        own.forEach(e => {
          if (phaseFor(e) === name) {
            if (String(e.event || '').endsWith('_started')) {
              if (open) { intervals.push(null); }
              open = e; status = 'Running';
            } else if (String(e.event || '').endsWith('_finished') || e.event === 'checkpoint_committed') {
              // review_process_finished ends work, but success is not a verdict.
              if (open) { intervals.push(spanTime(eventTime(open), eventTime(e))); open = null; }
              else if (e.event !== 'review_finished') missing = true;
              status = e.outcome === 'timed_out' ? 'Timed out' : ['failed', 'rejected'].includes(e.outcome) ? 'Failed'
                : name === 'Codex B' ? (e.event === 'review_finished' && e.outcome === 'approved' ? 'Approved' : 'Waiting for Codex B')
                : name === 'Push' ? 'Pushed to GitHub' : name === 'Commit' ? 'Committed locally' : 'Passed';
            }
          }
          if (open && terminal(e)) { intervals.push(spanTime(eventTime(open), eventTime(e))); open = null; status = 'Stopped'; }
        });
        if (open) {
          intervals.push(live ? spanTime(eventTime(open), now) : null);
          if (!live) status = 'Interrupted';
        }
        if (!relevant.length) {
          if (name === 'Codex B') status = approved ? 'Approved' : 'Review not recorded';
          if (name === 'Codex A' && record?.a_exit != null) status = record.a_timed_out ? 'Timed out' : record.a_exit === 0 ? 'Passed' : 'Failed';
          if (name === 'Tests' && tests.length) status = tests.some(t => t.timed_out) ? 'Timed out' : tests.some(t => t.exit_code !== 0) ? 'Failed' : 'Passed';
        }
        if (name === 'Codex B' && decision) status = approved ? 'Approved' : 'Rejected';
        if (name === 'Commit' && record?.commit) status = 'Committed locally';
        const configured = plan.find(t => t.id === id)?.test_count;
        if (name === 'Tests' && configured === 0 && !relevant.length) status = 'Not configured';
        const started = relevant.filter(e => e.event === 'test_started');
        const currentTest = started.at(-1)?.metadata?.index;
        const total = configured ?? relevant.find(e => Number.isInteger(e.metadata?.total))?.metadata.total;
        const label = name === 'Tests' && currentTest ? 'Tests ' + currentTest + (Number.isInteger(total) ? '/' + total : '') : name;
        if (name === 'Tests' && relevant.length) {
          const finished = relevant.filter(e => e.event === 'test_finished');
          const failed = finished.find(e => ['failed', 'timed_out'].includes(e.outcome));
          const advanced = own.some(e => ['Codex B', 'Commit'].includes(phaseFor(e)));
          if (open && live) status = 'Running';
          else if (failed) status = failed.outcome === 'timed_out' ? 'Timed out' : 'Failed';
          else if (!advanced && (!Number.isInteger(total) || finished.length < total) && !['Stopped', 'Interrupted'].includes(status)) status = live ? 'Running' : 'Incomplete';
        }
        if (name === 'Push' && !relevant.length) status = state.auto_push == null
          ? (state.auto_push === null ? 'Off' : 'Not recorded') : state.auto_push === false ? 'Off' : 'Waiting';
        const failures = name === 'Tests' ? relevant.filter(e => e.event === 'test_finished' && ['failed', 'timed_out'].includes(e.outcome)).length : 0;
        return {name, label, status, failures, duration:missing ? null : sumTimes(intervals)};
      });
      // Sum active sessions and recovery intervals. A resume is a boundary,
      // never evidence of work throughout the preceding stopped interval.
      const intervals = [];
      const entryIndexes = new Set(entries.map(entry => entry.index));
      let activeStart = null, recovery = false;
      const close = end => {
        intervals.push(spanTime(eventTime(activeStart), eventTime(end)));
        activeStart = null;
      };
      events.forEach((e, index) => {
        if (activeStart && ['resume_started', 'task_started'].includes(e.event)) {
          intervals.push(null); activeStart = null;
        }
        if (!entryIndexes.has(index)) return;
        if (e.event === 'task_started') { activeStart = e; recovery = false; }
        else if (!activeStart && String(e.event || '').endsWith('_started') && phaseFor(e)) {
          activeStart = e; recovery = true;
        }
        if (activeStart && (terminal(e) || recovery && ['checkpoint_committed', 'push_finished'].includes(e.event))) close(e);
      });
      if (activeStart) intervals.push(live ? spanTime(eventTime(activeStart), now) : null);
      const duration = sumTimes(intervals);
      const commit = record?.commit || own.filter(e => e.event === 'checkpoint_committed').at(-1)?.metadata?.commit;
      const stop = own.filter(e => e.event === 'stopped').at(-1);
      return {number, session, record, decision, events:own, phases, duration, approved, commit, live, stop};
    });
    const current = attempts.at(-1);
    const last = current?.events.at(-1);
    const publicationFailure = last?.event === 'push_finished' && last.outcome === 'failed';
    const publicationPending = state.publication?.task === id && state.publication?.outcome === 'pending'
      && state.publication?.error_code && current?.phases.find(p => p.name === 'Push')?.status !== 'Pushed to GitHub';
    const stopped = ['stopped', 'checkpoint_stopped'].includes(last?.event) || publicationFailure
      || c.active === false && (publicationPending || ['stopped', 'running', 'checkpointing'].includes(state.status) && state.current_task === id);
    const lastStage = current?.phases.filter(p => !['Waiting', 'Review not recorded', 'Not configured', 'Not recorded', 'Off'].includes(p.status)).at(-1)?.name;
    // B's verdict is evidence of review only. Completion requires a checkpoint.
    const committed = current?.commit || (state.completed || []).includes(id);
    const status = stopped ? 'Stopped' : current?.live
      ? (current.phases.find(p => p.name === 'Codex B')?.status === 'Waiting for Codex B' ? 'Waiting for review' : 'Running')
      : committed && current?.approved ? 'Approved' : attempts.length ? 'Waiting for review' : 'Waiting';
    const reason = stopped ? (publicationFailure ? last.metadata?.reason : null)
      || (publicationPending ? state.publication.error_code : null)
      || (last?.event === 'checkpoint_stopped' ? last.metadata?.reason : null) || current?.stop?.metadata?.reason
      || (current?.record?.error ? 'Attempt failed. Open Error (original) for details.' : null)
      || (c.error ? 'Runner stopped. Open run diagnostics for details.' : 'Run interrupted.') : null;
    return {id, attempts, current, status, lastStage, reason, duration:sumTimes(attempts.map(a => a.duration))};
  });
}
function renderTasks(parent, data, focusedKey) {
  const tasks = taskPresentation(data);
  const total = data.task_count ?? data.controller?.task_count;
  parent.append(el('p', (data.state?.completed?.length ?? 0) + ' / ' + (total ?? 'Unknown') + ' completed', 'task-progress'));
  const list = el('section', undefined, 'task-list');
  list.setAttribute('aria-label', 'Plan tasks');
  tasks.forEach(task => {
    const row = el('details', undefined, 'task-row');
    const key = 'task-' + task.id;
    row.detailKey = key;
    row.open = expanded.has(key);
    row.ontoggle = () => { if (row.open) expanded.add(key); else expanded.delete(key); };
    const summary = el('summary');
    summary.taskFocus = key;
    summary.append(el('span', task.id, 'task-heading'), el('span', task.status, task.status === 'Approved' ? 'task-meta green' : 'task-meta'));
    summary.append(el('span', task.attempts.length + ' attempt' + (task.attempts.length === 1 ? '' : 's') + ' | Total ' + compactTime(task.duration), 'task-meta'));
    const current = task.current;
    if (current?.commit) summary.append(el('span', 'Local commit ' + current.commit.slice(0, 8), 'task-meta'));
    const strip = el('div', undefined, 'phase-strip');
    (current?.phases || ['Codex A','Tests','Codex B','Commit','Push'].map(name => ({name, label:name, status:name === 'Push' && data.state?.auto_push === null ? 'Off' : 'Waiting', duration:null}))).forEach(phase => {
      strip.append(el('span', phase.label + (phase.status === 'Off' ? ' off' : ': ' + phase.status) + (phase.failures && phase.status === 'Running' ? ' (' + phase.failures + ' earlier failed)' : '') + (phase.duration !== null ? ' ' + compactTime(phase.duration) : ' | \u2014')));
    });
    summary.append(strip);
    if (current && task.attempts.length > 1) summary.append(el('span', 'Current attempt ' + current.number + ' | ' + compactTime(current.duration), 'task-meta'));
    if (task.reason) summary.append(el('div', 'STOPPED' + (task.lastStage ? ' at ' + task.lastStage : '') + ': ' + task.reason + ' | Check evidence, then Resume.', 'task-stop'));
    row.append(summary);
    task.attempts.forEach(attempt => {
      const item = attempt.record;
      const decision = attempt.decision;
      const section = el('section', undefined, 'attempt ' + (decision === 'APPROVED' ? 'approved' : decision === 'REJECTED' ? 'rejected' : ''));
      section.append(el('h3', 'Attempt ' + attempt.number + ' | ' + compactTime(attempt.duration) + ' | Codex B: ' + (decision || 'Review not recorded')));
      if (attempt.commit) section.append(el('p', 'Local commit: ' + attempt.commit, 'path green'));
      (Array.isArray(item?.tests) ? item.tests.filter(t => t && typeof t === 'object') : []).forEach(test => section.append(el('p', 'Test ' + test.index + ': exit ' + (test.exit_code ?? 'unknown') + ', timed out: ' + (test.timed_out ?? 'unknown'))));
      const attemptKey = key + '-' + attempt.number + '-' + attempt.session;
      if (item?.feedback) section.append(detail('Feedback (original)', item.feedback, attemptKey + '-feedback'));
      if (item?.error) section.append(detail('Error (original)', item.error, attemptKey + '-error'));
      if (item?.review) section.append(detail('Codex B summary and verdict (original)', JSON.stringify(item.review, null, 2), attemptKey + '-verdict'));
      if (item) section.append(detail('Full attempt record', JSON.stringify(item, null, 2), attemptKey + '-record'));
      const timing = detail('Step timing and original events / evidence', JSON.stringify(attempt.phases, null, 2), attemptKey + '-events');
      timelineInto(timing, attempt.events);
      section.append(timing);
      row.append(section);
    });
    list.append(row);
  });
  if (!tasks.length) list.append(el('p', 'No task records available.', 'task-progress'));
  const evidence = detail('All run evidence files', 'Original files for this selected run.', 'task-files');
  filesInto(evidence, data.files || []);
  if (data.controller?.error) evidence.append(detail('Run diagnostics (original snapshot)', JSON.stringify({error:data.controller.error, exit_code:data.controller.exit_code, process_history:data.controller.process_history}, null, 2), 'task-diagnostics'));
  parent.append(list, evidence);
  const restore = node => {
    if (focusedKey && node.taskFocus === focusedKey) node.focus();
    Array.from(node.children || []).forEach(restore);
  };
  restore(parent);
}
function render(data) {
  evidenceViewer.hidden = page !== 'tasks';
  document.getElementById('logs-message').hidden = page === 'overview';
  latest = data;
  const c = data.controller || {};
  const preferences = data.preferences || {};
  if (!render.preferencesLoaded) {
    if (preferences.repository) document.getElementById('github-repo').value = preferences.repository;
    if (preferences.attempts) document.getElementById('attempts').value = String(preferences.attempts);
    render.preferencesLoaded = true;
  }
  const savedRepository = preferences.repository && !c.repository;
  document.getElementById('preferences-notice').textContent = savedRepository
    ? 'Saved setup restored. Connect the repository and verify its feature branch before starting.' + (preferences.plan && !c.plan ? ' Choose an available task plan.' : '')
    : preferences.plan && !c.plan ? 'Saved task plan is unavailable. Choose another plan.' : '';
  if (c.repository && preferences.branch && !c.github?.error
      && ![...(c.github?.local || []), ...(c.github?.remote || []).map(name => name.replace(/^origin\//, ''))].includes(preferences.branch)) {
    document.getElementById('preferences-notice').textContent = 'Saved branch is unavailable. Fetch branches or choose another feature branch.';
  }
  const key = page + JSON.stringify(data) + (page === 'tasks' && c.active === true ? Math.floor(Date.now() / 1000) : '');
  updateControls();
  if (runPresentation(data).label === 'Completed' && document.getElementById('message').textContent.startsWith('Launch accepted.')) message('');
  if (renderKey === key) return;
  renderKey = key;
  [['project-path', c.repo || 'No project selected'], ['plan-path', c.plan || preferences.plan || 'No plan selected']].forEach(([id, value]) => {
    const field = document.getElementById(id);
    if (field.textContent !== value) field.textContent = value;
  });
  const connection = document.getElementById('github-state');
  const currentBranch = c.preflight?.branch;
  const publication = c.github?.error || !currentBranch ? 'Publication unknown' : c.push?.eligible ? 'Approved checkpoints pending push'
    : c.github?.head && c.github?.remote_head === c.github.head
      ? 'Published at ' + c.github.head + ' (cached origin/' + currentBranch + ')'
      : c.github?.head && !c.github?.remote_head ? 'Local-only, unpublished'
      : c.github?.head ? 'Local and origin commits differ; fetch and review before publishing'
      : 'Publication unknown';
  connection.textContent = c.repo ? 'Current branch: ' + (currentBranch || 'Detached HEAD / unavailable') + ' | ' + (c.preflight?.clean === true ? 'Clean' : 'Changes or unavailable status') + ' | ' + publication + (c.github?.error ? ' | ' + c.github.error : '') : c.github?.error || '';
  const branches = document.getElementById('github-branch');
  const bases = document.getElementById('branch-base');
  const remoteBases = [...new Set((c.github?.remote || []).filter(name => name.startsWith('origin/') && name !== 'origin/HEAD').map(name => name.slice(7)))];
  const baseSignature = JSON.stringify([c.repository, remoteBases]);
  if (bases.datasetSignature !== baseSignature) {
    const preserveBase = bases.repositoryContext === c.repository && remoteBases.includes(bases.value);
    const selectedBase = preserveBase ? bases.value
      : remoteBases.includes('main') ? 'main' : remoteBases.includes('master') ? 'master' : remoteBases[0] || '';
    bases.replaceChildren();
    remoteBases.forEach(name => { const option = el('option', 'origin/' + name); option.value = name; bases.append(option); });
    bases.value = selectedBase;
    bases.datasetSignature = baseSignature;
    bases.repositoryContext = c.repository;
    // Polling must not discard an in-progress form when unrelated branches appear.
    if (!preserveBase) {
      document.getElementById('branch-base-confirm').checked = false;
      document.getElementById('branch-form').hidden = true;
    }
  }
  updateControls();
  const names = [...new Set([...(c.github?.local || []), ...(c.github?.remote || []).filter(name => name.startsWith('origin/') && name !== 'origin/HEAD').map(name => name.slice(7))])];
  const signature = JSON.stringify([c.repository, c.preflight?.branch, names, c.github?.local, c.github?.remote]);
  if (branches.datasetSignature !== signature) {
    const selectionContext = JSON.stringify([c.repository, c.preflight?.branch]);
    const savedBranch = c.repository && !c.github?.error && preferences.repository && preferences.branch
      && names.includes(preferences.branch) ? preferences.branch : '';
    const selected = branches.selectionContext === selectionContext && names.includes(branches.value)
      ? branches.value : preferences.branch ? savedBranch : (names.includes(c.preflight?.branch) ? c.preflight.branch : '');
    branches.replaceChildren();
    const placeholder = el('option', 'Choose a feature branch'); placeholder.value = ''; branches.append(placeholder);
    names.forEach(name => {
      const local = (c.github?.local || []).includes(name);
      const remote = (c.github?.remote || []).includes('origin/' + name);
      const protectedName = ['main', 'master'].includes(name.toLowerCase());
      const label = name + (protectedName ? ' (protected, base only)' : '') + ' (' + (local && remote ? 'local + origin' : local ? 'local-only, unpublished' : 'origin, remote-only') + ')';
      const option = el('option', label); option.value = name; branches.append(option);
    });
    branches.value = selected;
    branches.datasetSignature = signature;
    branches.selectionContext = selectionContext;
  }
  updateControls();
  const git = c.preflight || {};
  document.getElementById('run-folder').textContent = c.logs ? 'Run folder: ' + c.logs : '';
  const errors = [...(git.errors || []), ...Object.entries(c.validation_errors || {}).map(([k,v]) => k + ': ' + v)];
  if (c.selection_error) errors.push(c.selection_error);
  if (git.resume_error) errors.push(git.resume_error);
  if (git.start_error) errors.push(git.start_error);
  const preflight = document.getElementById('preflight');
  preflight.replaceChildren();
  // One stable, actionable launch status; full diagnostics remain in Logs.
  const presentation = runPresentation(data);
  const statusState = data.state || {};
  const statusReconciled = presentation.label === 'Completed' || c.active === false && !c.uncertain && statusState.status === 'checkpointing';
  const diagnostics = [...errors, c.error, data.state_error].filter(Boolean);
  const visibleErrors = diagnostics.filter(error => c.repo || error !== 'Select a Git repository root.');
  const checkoutLock = errors.includes('An Auto-Coding runner is active or uncertain for this checkout.');
  let statusLine = '';
  const lockDiagnosis = c.preflight?.locks?.[0];
  const stoppedReason = [...(data.events || [])].reverse().find(e => e.event === 'stopped')?.metadata?.reason;
  if (lockDiagnosis && c.active === true) statusLine = 'Another run is active — open Tasks';
  else if (lockDiagnosis) statusLine = lockDiagnosis.message;
  else if (c.uncertain || c.active === null) statusLine = 'This run may still be active. Check Logs before starting another.';
  else if (c.active === true) statusLine = 'A run is already in progress. Check Tasks for its status.';
  else if (checkoutLock) statusLine = 'This run may still be active. Check Logs before starting another.';
  else if (statusState.status === 'stopped' && stoppedReason === 'git index lock') statusLine = 'Git index.lock is still busy — inspect Logs, then Resume when the owning operation finishes.';
  else if (statusState.status === 'stopped' && stoppedReason === 'test timeout') statusLine = 'Test process timed out — inspect Logs and child status before Resume.';
  else if (data.state_error) statusLine = data.state_error;
  else if (c.error && !statusReconciled) statusLine = c.error;
  else if (c.logs && !data.exists) statusLine = 'Log directory is missing or unavailable.';
  else if (statusState.pending_checkpoint) statusLine = 'A checkpoint is pending reconciliation. Inspect Logs before resuming.';
  else if (statusState.publication?.outcome === 'pending') statusLine = 'Approved locally, push pending. Use Resume (Retry Push); inspect Logs for details.';
  else if (visibleErrors.length) statusLine = visibleErrors.find(error => error !== c.error) || '';
  else if (c.repo && (git.clean === false || c.plan && !c.resume_ready && git.clean !== true)) statusLine = 'START requires a clean working tree. Commit or discard project changes first.';
  if (statusLine) preflight.append(el('p', statusLine, c.active !== false || c.uncertain || checkoutLock ? 'warning' : 'red'));
  // A bridge rejection may repeat the same snapshot failure. Keep its diagnostics here.
  if (statusLine && (diagnostics.includes(document.getElementById('message').textContent)
      || document.getElementById('message').textContent === statusLine)) message('');
  document.getElementById('preflight-details').textContent = JSON.stringify({branch:git.branch, clean:git.clean, start_ready:c.start_ready, resume_ready:c.resume_ready, diagnostics}, null, 2);
  const recovery = document.getElementById('lock-recovery');
  recovery.replaceChildren();
  (git.locks || []).forEach(lock => {
    if (!lock.recoverable) return;
    const suffix = lock.path?.endsWith('.job.lock') ? '.job.lock' : '.lock';
    const button = el('button', 'Archive verified completed lock');
    button.disabled = busy || !ready || c.active !== false || c.uncertain;
    button.onclick = () => historyAction('recover_workspace_lock', suffix);
    recovery.append(button);
  });
  document.getElementById('run-setup').hidden = page !== 'overview';
  document.getElementById('run-selection').hidden = page !== 'tasks';
  document.getElementById('logs-tools').hidden = page !== 'tasks';
  const badge = document.getElementById('header-state');
  badge.textContent = presentation.label;
  badge.className = 'pill ' + presentation.color;
  if (presentation.label === 'Completed' && document.getElementById('message').textContent.startsWith('Launch accepted.')) message('');
  updateControls();
  // Read native state before replacing nodes: toggle events can be delivered
  // after a snapshot refresh, especially when navigating with the keyboard.
  if (evidenceRoot === data.root && (page === 'tasks' || page === 'logs')) {
    const remember = node => {
      if (node.detailKey) {
        if (node.open) expanded.add(node.detailKey); else expanded.delete(node.detailKey);
      }
      Array.from(node.children || []).forEach(remember);
    };
    remember(app);
  }
  if (evidenceRoot !== data.root) {
    evidenceViewer.replaceChildren();
    expanded.clear();
    evidenceRoot = data.root;
  }
  const taskFocus = document.activeElement?.taskFocus;
  app.replaceChildren();
  if (page === 'overview') {
    const state = data.state || {};
    const status = {running:'Running', completed:'Completed', stopped:'Stopped', checkpointing:'Awaiting recovery'}[state.status]
      || (c.active === false && !c.uncertain && !data.root && !c.logs && !state.status ? 'Idle' : 'Unknown');
    const total = data.task_count ?? c.task_count;
    const done = Array.isArray(state.completed) ? state.completed.length : null;
    const count = done !== null && Number.isInteger(total) && total >= done ? done + ' / ' + total
      : done !== null ? done + ' / Unknown' : 'Unknown';
    const child = c.uncertain || typeof c.active !== 'boolean' ? 'Unknown' : c.active ? 'Running' : 'Inactive';
    const cards = el('div', undefined, 'grid');
    cards.setAttribute('aria-label', 'Run status summary');
    [['RUN STATUS', status], ['COMPLETED TASKS', count], ['CHILD PROCESS', child]].forEach(([label, value]) => {
      const card = el('section', undefined, 'card');
      card.append(el('div', label, 'label'), el('div', value, 'value'));
      cards.append(card);
    });
    app.append(cards);
    return;
  }
  if (page === 'logs') {
    const runs = [...recentRuns.runs];
    const current = data.root || c.logs;
    if (current && !runs.some(run => run.path === current)) runs.push({path:current,
      plan_filename:(c.plan || '').split(/[\\/]/).pop(), run_id:current.split(/[\\/]/).pop(),
      status:c.active !== false || c.uncertain ? 'running' : data.state?.status || 'unknown'});
    runs.sort((a, b) => (Date.parse(b.time) || 0) - (Date.parse(a.time) || 0));
    const list = el('div', undefined, 'run-list');
    list.setAttribute('aria-label', 'Run downloads');
    if (!runs.length) list.append(el('p', 'No saved runs found.', 'muted'));
    runs.forEach(run => {
      const row = el('section', undefined, 'run-row');
      const label = el('div', undefined, 'run-label');
      label.append(el('span', run.plan_filename || 'Run ' + (run.run_id || '').replace(/^run-/, '').slice(0, 8)));
      const active = ['running', 'unknown'].includes(run.status)
        || run.path === current && (c.active !== false || c.uncertain);
      const state = {completed:'Completed', stopped:'Stopped', running:'Running', unknown:'Status unknown', unavailable:'Status unavailable', missing:'Unavailable'}[run.status] || 'Stopped';
      label.append(el('small', active ? state + ' - Available when run stops' : state + (run.time ? ' - ' + run.time.slice(0, 10) : ''), 'muted'));
      const download = el('button', 'Download ZIP', 'primary run-download');
      download.disabled = busy || !ready || active || run.status === 'missing';
      download.onclick = () => downloadRun(run.path);
      row.append(label, download);
      if (runDownloads[run.path]) row.append(el('small', runDownloads[run.path], 'run-result'));
      list.append(row);
    });
    app.append(list);
    if (runs.length) app.append(el('small', 'ZIPs can contain private prompts, code and output. Share carefully.', 'muted'));
    if (recentRuns.error) app.append(el('small', recentRuns.error, 'warning'));
    return;
  }
  if (!c.logs && !data.root) {
    if (page !== 'overview') {
      const empty = el('section', undefined, page === 'tasks' ? 'muted' : 'empty');
      empty.append(el('p', page === 'tasks' ? 'No run loaded.' : 'No run evidence yet. Start or open a run.'));
      const setup = el('button', 'Go to run setup');
      setup.onclick = () => navigate('overview');
      if (page !== 'tasks') empty.append(setup);
      app.append(empty);
    }
    if (c.error) app.append(el('div', c.error, 'warning'));
    return;
  }
  const state = data.state || {};
  const events = data.events || [];
  const reconciled = presentation.label === 'Completed' || c.active === false && !c.uncertain && state.status === 'checkpointing';
  if (page !== 'tasks' && c.error && !reconciled && c.error !== statusLine) app.append(el('div', c.error, 'notice warning'));
  if (data.state_error && (page !== 'logs' || data.state_error !== statusLine)) app.append(el('div', data.state_error, 'warning'));
  if (data.empty && page !== 'tasks') app.append(el('div', 'No run state or events found. Select a project and plan to START, or inspect available raw files in Logs.', 'empty'));
  if (c.logs && !data.exists && !data.state_error) app.append(el('div', 'Log directory is missing or unavailable.', 'warning'));
  if (state.pending_checkpoint && page !== 'tasks') app.append(el('div', 'A checkpoint is pending reconciliation. No completion is inferred.', 'warning'));
  if (state.auto_push && page === 'logs') app.append(detail('Saved push policy', 'This run automatically pushes approved tasks to origin/' + state.auto_push.branch + '. Resume preserves this setting.', 'push-policy'));
  if (state.publication?.outcome === 'pending' && page !== 'tasks') app.append(el('div', 'Approved locally, push pending. Resolve connectivity or permissions, then use Resume (Retry Push). The same SHA is retried before any further tasks; Codex is not rerun for the approved task. Remote divergence requires manual review.', 'warning'));
  if (page === 'logs') (data.event_errors || []).forEach(error => app.append(el('div', error, 'warning')));
  if (page === 'tasks') {
    renderTasks(app, data, taskFocus);
  }
}
function navigate(name) {
  page = name;
  ['overview', 'tasks', 'logs'].forEach(view => { const nav = document.getElementById('nav-' + view); nav.className = 'nav' + (view === page ? ' active' : ''); nav.setAttribute('aria-current', view === page ? 'page' : 'false'); });
  document.getElementById('page-title').textContent = {overview:'Overview', tasks:'Tasks', logs:'Logs'}[page];
  if (latest) render(latest);
  if (name === 'logs') refresh(false);
}
const runDownloads = {};
async function downloadRun(path) {
  if (busy || !ready) return;
  busy = true; renderKey = null; render(latest);
  try {
    const result = await window.pywebview.api.download_run(path);
    runDownloads[path] = result.ok ? result.message : result.error || 'Export failed. No ZIP saved.';
  } catch (error) { runDownloads[path] = 'Export failed: ' + error; }
  finally { busy = false; renderKey = null; render(latest); updateControls(); }
}
async function historyAction(method, path) {
  if (busy || !ready) return;
  if (method === 'open_recent_run' && (latest?.controller?.active !== false || latest?.controller?.uncertain)) return;
  busy = true; updateControls();
  try {
    if (refreshing) await refreshing;
    const result = await window.pywebview.api[method](path);
    message(result.error || (result.archive ? 'Lock archived: ' + result.archive : method === 'forget_run' ? 'History entry forgotten. Log data remains on disk.' : 'Run opened for inspection. No work launched.'), !result.ok);
  } catch (error) { message('History action failed: ' + error, true); }
  finally { busy = false; await refresh(false); }
}
async function refresh(clearMessage = true) {
  if (!ready || busy) return;
  if (refreshing) return refreshing;
  refreshing = (async () => {
    try {
      const data = await window.pywebview.api.snapshot();
      if (page === 'logs' && window.pywebview.api.recent_runs) {
        try { recentRuns = await window.pywebview.api.recent_runs(); }
        catch (error) { recentRuns = {runs:[], error:'Could not load run history: ' + error}; }
        renderKey = null;
      }
      if (clearMessage) message(''); render(data);
    }
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
  if (['start', 'resume'].includes(method) && !validateAttempts()) { updateControls(); return; }
  const button = {'select_repo':'browse-project', 'select_plan':'browse-plan', 'open_run':'open-run', start:'start', resume:'resume'}[method];
  if (document.getElementById(button).disabled) return;
  busy = true;
  updateControls();
  let accepted = false;
  message(method === 'start' || method === 'resume' ? 'Waiting for native launch confirmation...' : 'Waiting for selection...');
  try {
    if (refreshing) await refreshing;
    const args = method === 'start' || method === 'resume' ? [Number(document.getElementById('attempts').value)] : [];
    if (method === 'start' && document.getElementById('auto-push').checked === true) args.push(true);
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
    restoreActionFocus(button);
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


async function workspaceAction(method, args, button) {
  if (busy || !ready || document.getElementById(button).disabled) return;
  busy = true; updateControls();
  try {
    if (refreshing) await refreshing;
    const result = await window.pywebview.api[method](...args);
    if (method === 'github_repositories') {
      const list = document.getElementById('github-repositories'); list.replaceChildren();
      const placeholder = el('option', 'Choose repository'); placeholder.value = ''; list.append(placeholder);
      (result.repositories || []).forEach(name => { const option = el('option', name); option.value = name; list.append(option); });
      list.hidden = !result.available;
    }
    if (result.ok && method === 'create_github_branch') document.getElementById('branch-form').hidden = true;
    message(result.error || result.message || (result.ok ? (method === 'push_approved_commits' ? 'Approved checkpoints remotely published. No merge to main.' : 'Selection updated. No work launched.') : 'Action failed.'), !result.ok);
  } catch (error) { message('Workspace action failed: ' + error, true); }
  finally {
    busy = false; updateControls(); await refresh(false);
    restoreActionFocus(method === 'create_github_branch' && document.getElementById('branch-form').hidden ? 'new-branch' : button);
  }
}
document.getElementById('connect-github').onclick = () => workspaceAction('connect_github', [document.getElementById('github-repo').value.trim()], 'connect-github');
document.getElementById('list-github').onclick = () => workspaceAction('github_repositories', [], 'list-github');
document.getElementById('github-repositories').onchange = () => {
  const value = document.getElementById('github-repositories').value;
  if (value) { document.getElementById('github-repo').value = value; return workspaceAction('connect_github', [value], 'connect-github'); }
};
document.getElementById('select-branch').onclick = () => workspaceAction('checkout_github_branch', [latest?.controller?.repository, document.getElementById('github-branch').value], 'select-branch');
document.getElementById('github-branch').onchange = updateControls;
document.getElementById('fetch-github').onclick = () => workspaceAction('fetch_github', [latest?.controller?.repository], 'fetch-github');
document.getElementById('new-branch').onclick = () => { if (!document.getElementById('new-branch').disabled) { document.getElementById('branch-base-confirm').checked = false; document.getElementById('branch-form').hidden = false; updateControls(); document.getElementById('branch-name').focus(); } };
document.getElementById('cancel-branch').onclick = () => { document.getElementById('branch-form').hidden = true; document.getElementById('branch-name').value = ''; document.getElementById('new-branch').focus(); };
document.getElementById('branch-base').onchange = () => { document.getElementById('branch-base-confirm').checked = false; updateControls(); };
document.getElementById('branch-base-confirm').onchange = updateControls;
document.getElementById('create-branch').onclick = () => workspaceAction('create_github_branch', [latest?.controller?.repository, document.getElementById('branch-name').value.trim(), document.getElementById('branch-base').value, document.getElementById('branch-base-confirm').checked === true], 'create-branch');
document.getElementById('push-approved').onclick = () => workspaceAction('push_approved_commits', [], 'push-approved');

document.getElementById('refresh').onclick = refresh;
function saveSetup() {
  if (!ready || !window.pywebview.api.save_setup) return;
  window.pywebview.api.save_setup(document.getElementById('github-repo').value.trim(),
    Number(document.getElementById('attempts').value)).catch(() => {});
}
document.getElementById('github-repo').oninput = saveSetup;
document.getElementById('attempts').oninput = () => { updateControls(); if (validateAttempts()) saveSetup(); };
document.getElementById('auto-push').onchange = updateControls;
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

    def confirm_export(self, message):
        return self._window.create_confirmation_dialog("Confirm debug package export", message)

    def save_export(self, filename="debug-report.zip"):
        enum = getattr(self._webview, "FileDialog", None)
        kind = getattr(enum, "SAVE") if enum is not None else self._webview.SAVE_DIALOG
        result = self._window.create_file_dialog(kind, allow_multiple=False,
                    save_filename=filename, file_types=("ZIP archives (*.zip)",))
        return result[0] if result and not isinstance(result, str) else result


class Bridge:
    """Small JS API. Selection never launches; each launch needs native confirmation.

    A fresh controller on run selection prevents stale metadata from a previous
    run making a legacy run appear resumable. Never replace a tracked live job.
    """

    def __init__(self, root: Path | None = None, *, controller=None,
                 dialogs=None, controller_factory=DesktopController, settings=None):
        self._controller_factory = controller_factory
        self._controller = controller if controller is not None else controller_factory()
        self._history = getattr(self._controller, "run_history", None)
        self._dialogs = dialogs
        self._lock = threading.RLock()
        self._launch_lock = threading.Lock()
        self._root = Path(root).expanduser() if root is not None else None
        self._selection_error = None
        self._export_folder = None
        self._settings = settings if settings is not None else DesktopSettings()
        self._preferences = self._settings.load()
        if root is None and self._preferences["plan"] and self._controller.plan is None:
            # Local validation only; an unavailable hint gets one short UI notice.
            from auto_coding.simple_run import PilotError
            try:
                load_tasks(Path(self._preferences["plan"]))
            except (OSError, ValueError, UnicodeError, PilotError):
                pass
            else:
                self._controller.select_plan(self._preferences["plan"])
        if root is not None:
            result = self.open_run(str(root))
            if not result["ok"]:
                self._selection_error = result["error"]

    def _remember(self, **values):
        self._preferences = clean_preferences({**self._preferences, **values})
        self._settings.save(self._preferences)

    def save_setup(self, repository, attempts):
        with self._lock:
            if self._controller.job_status()["active"] is False:
                previous = self._preferences["repository"]
                self._remember(repository=repository, attempts=attempts,
                               branch=self._preferences["branch"] if repository == previous else "")
            return {"ok": True}

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
                    if self._history is not None and isinstance(candidate, DesktopController):
                        candidate.runs_dir = self._history.runs_dir
                        candidate.run_history = self._history
                    result = candidate.reopen(path)
                    if result["ok"]:
                        self._controller = candidate
                        self._root = candidate.logs
                else:
                    result = getattr(self._controller, "select_" + kind)(path)
                if kind == "plan" and result.get("ok"):
                    self._remember(plan=str(self._controller.plan))
                self._selection_error = result.get("error")
                return result
            except Exception as exc:
                self._selection_error = str(exc)
                return self._failure(exc)

    def select_repo(self, path=None):
        return self._select("repo", path)

    def _github(self, action, *args, **kwargs):
        try:
            result = self._controller.github(action, *args, **kwargs)
            if result.get("ok") and action in {"checkout_branch", "create_branch"}:
                self._remember(branch=kwargs["name"])
            self._selection_error = result.get("error")
            return result
        except Exception:
            self._selection_error = "Workspace operation failed. Check Git availability and repository access, then retry."
            return {"ok": False, "error": self._selection_error}

    def connect_github(self, repository):
        with self._lock:
            self._remember(repository=repository, branch=self._preferences["branch"]
                           if repository == self._preferences["repository"] else "")
            return self._github("connect", repository)

    def github_repositories(self):
        with self._lock:
            return self._github("list_repositories")

    def github_branches(self, repository):
        with self._lock:
            return self._github("branches", repository)

    def fetch_github(self, repository):
        with self._lock:
            return self._github("fetch", repository)

    def create_github_branch(self, repository, name, base="main", confirmed_base=False):
        with self._lock:
            return self._github("create_branch", repository, name=name,
                                           base=base, confirmed_base=confirmed_base)

    def checkout_github_branch(self, repository, name):
        with self._lock:
            return self._github("checkout_branch", repository, name=name)

    def push_approved_commits(self):
        with self._lock:
            return self._github("push_approved", getattr(self._controller, "repository", None))

    def select_plan(self, path=None):
        return self._select("plan", path)

    def open_run(self, path=None):
        return self._select("run", path)

    def recent_runs(self):
        with self._lock:
            return self._history.list() if self._history is not None else {"runs": [], "error": None}

    def open_recent_run(self, path):
        with self._lock:
            entries = self.recent_runs()["runs"]
            entry = next((item for item in entries if item["path"] == path), None)
            if entry is None or entry["status"] == "missing" or Path(path).resolve() != Path(path):
                return {"ok": False, "error": "Run is missing or unavailable. Import its new location or forget the history entry."}
            return self.open_run(path)

    def forget_run(self, path):
        with self._lock:
            ok = self._history is not None and self._history.forget(path)
            return {"ok": bool(ok), "error": self._history.error if self._history else "History unavailable."}

    def selected_paths(self):
        with self._lock:
            return self._controller.selected_paths()

    def export_logs(self, mode="standard"):
        # No caller-supplied source or destination; selection remains locked through
        # confirmations, native Save As and publication. No controller status/preflight
        # calls here: those may invoke Git. Export never launches any such operation.
        with self._lock:
            try:
                if mode not in ("standard", "raw"):
                    raise ValueError("Choose standard or raw export.")
                if self._root is None:
                    raise ValueError("No run selected.")
                if self._dialogs is None:
                    raise ValueError("Export requires native confirmation and Save As dialogs.")
                if self._dialogs.confirm_export(f"Export {mode.upper()} for selected run:\n{self._root}\n{WARNING}") is not True:
                    return {"ok": True, "cancelled": True}
                if mode == "raw" and self._dialogs.confirm_export(
                    "FULL RAW LOGS: Codex prompts, review text and subprocess output can contain private data/API keys. "
                    "Inspect before sharing. Include these raw files?") is not True:
                    return {"ok": True, "cancelled": True}
                destination = self._dialogs.save_export()
                if not destination:
                    return {"ok": True, "cancelled": True}
                if not isinstance(destination, str):
                    raise ValueError("Invalid native Save As result.")
                result = export_debug(self._root, destination, mode,
                                      excluded_roots=(self._controller.repo,))
                self._export_folder = Path(result["path"]).parent
                return result
            except Exception as exc:
                return self._failure(exc)

    def download_run(self, path):
        """Export a known history row without changing the selected/live run."""
        from auto_coding.run_history import RunHistory
        import re
        with self._lock:
            try:
                known = {item['path'] for item in self.recent_runs()['runs']}
                if self._root is not None:
                    known.add(str(self._root))
                if path not in known:
                    raise ValueError('Unknown run. Refresh the run list.')
                summary = RunHistory.summary(path)
                if summary['status'] in {'running', 'unknown'}:
                    raise ValueError('Available when run stops')
                if self._controller.logs is not None and Path(path) == self._controller.logs:
                    process = self._controller.process
                    if process is not None and process.poll() is None:
                        raise ValueError('Available when run stops')
                if self._dialogs is None:
                    raise ValueError('Native Save As dialog is unavailable.')
                label = re.sub(r'[^A-Za-z0-9_.-]', '-', summary.get('plan_filename') or Path(path).name)[:70]
                date = re.sub(r'[^0-9-]', '', (summary.get('time') or '')[:10])
                run_label = re.sub(r'[^A-Za-z0-9_-]', '', summary['run_id'].removeprefix('run-'))[:8]
                destination = self._dialogs.save_export(f'{label}-{date or "run"}-{run_label}.zip')
                if not destination:
                    return {'ok': False, 'cancelled': True, 'error': 'Export cancelled. No ZIP saved.'}
                if not isinstance(destination, str):
                    raise ValueError('Invalid native Save As result.')
                result = export_run(path, destination, excluded_roots=(self._controller.repo,))
                self._export_folder = Path(result['path']).parent
                return result
            except Exception as exc:
                return self._failure(exc)

    def open_export_folder(self):
        with self._lock:
            try:
                import os
                if self._export_folder is None or not hasattr(os, "startfile"):
                    raise ValueError("Opening the export folder is unavailable on this platform.")
                folder = self._export_folder
                if folder.resolve(strict=True) != folder or not folder.is_dir():
                    raise ValueError("Export folder is unavailable or redirects elsewhere.")
                os.startfile(str(folder))
                return {"ok": True}
            except Exception as exc:
                return self._failure(exc)

    def recover_workspace_lock(self, suffix):
        with self._lock:
            return self._controller.recover_workspace_lock(suffix)

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
                        status["task_steps"] = [{"id": t["id"], "test_count": len(t["test_commands"])} for t in tasks]
                    except Exception as exc:
                        errors["plan"] = str(exc)
                state = read_run(self._root).get("state") if self._root else None
                if self._root and (not state or state.get("plan_sha256") != plan_sha):
                    # A newly selected plan cannot supply counts for a different
                    # inspected run. Prefer its durable manifest or Unknown.
                    status.pop("task_count", None)
                    status.pop("task_steps", None)
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
                    and (state.get("status") == "stopped" or
                         (state.get("auto_push") and state.get("status") == "checkpointing"))
                    and state.get("repo") == paths["repo"]
                    and state.get("plan_sha256") == plan_sha
                    and status["active"] is False and not status.get("uncertain")
                    and not status["preflight"].get("errors")
                    and not status["preflight"].get("resume_error"))
                status["selection_error"] = self._selection_error
                repository = getattr(self._controller, "repository", None)
                status["repository"] = repository
                if repository:
                    status["github"] = self._controller.github("branches", repository)
                    push = self._controller.github("push_eligibility", repository)
                    status["push"] = {"eligible": push.get("eligible", False), "reason": push.get("reason") or push.get("error")}
                else:
                    status["push"] = {"eligible": False, "reason": "Connect a managed GitHub repository with approved local checkpoints to publish."}
                return status
            except Exception as exc:
                return {"active": None, "uncertain": True, "exit_code": None,
                        "error": str(exc), "resume_ready": False}

    def _launch(self, action, attempts, auto_push=False):
        if not self._launch_lock.acquire(blocking=False):
            return {"ok": False, "error": "A launch confirmation is already in progress."}
        try:
            return self._confirmed_launch(action, attempts, auto_push)
        finally:
            self._launch_lock.release()

    def _confirmed_launch(self, action, attempts, auto_push=False):
        with self._lock:
            try:
                if type(attempts) is not int or not 1 <= attempts <= 10:
                    raise ValueError("Attempts must be an integer between 1 and 10.")
                paths = self._controller.selected_paths()
                if type(auto_push) is not bool:
                    raise ValueError("Auto-push must be explicitly selected.")
                if action == "resume" and self._controller.logs:
                    auto_push = bool(read_run(self._controller.logs).get("state", {}).get("auto_push"))
                branch = self._controller.preflight()["branch"]
                message = (f"{action.upper()} the local runner?\nRepository: {getattr(self._controller, 'repository', None) or paths['repo']}"
                           f"\nBranch: {branch or 'Unknown (launch will be blocked)'}"
                           f"\nPlan: {paths['plan']}\nSelected logs: {paths['logs']}"
                           f"\nAttempts: {attempts}\nThe runner may modify and commit project files."
                           + ("\nAutomatically push approved tasks: ENABLED for this run to origin/" + str(branch)
                            if auto_push else "\nCheckpoints remain local. No automatic push, merge, force-push or branch creation.")
                           + "\nNO automatic main merge or force push."
                           "\nLocal Codex CLI authentication is required."
                           "\nClosing the window leaves the runner alive in the background.")
                if self._dialogs is None:
                    raise ValueError("Launch requires native confirmation; dialogs are unavailable.")
                if self._dialogs.confirm(message) is not True:
                    return {"ok": False, "cancelled": True, "error": "Launch cancelled."}
                if self._controller.preflight()["branch"] != branch:
                    raise ValueError("Git branch changed during confirmation. Inspect the current branch and confirm again.")
                options = {"attempts": attempts}
                if action == "start" and auto_push:
                    options["auto_push"] = True
                result = getattr(self._controller, action)(**options)
                # Even a failed launch may have created useful diagnostic logs.
                if self._controller.logs is not None:
                    self._root = self._controller.logs
                return result
            except Exception as exc:
                return self._failure(exc)

    def start(self, attempts=3, auto_push=False):
        return self._launch("start", attempts, auto_push)

    def resume(self, attempts=3):
        return self._launch("resume", attempts)

    def snapshot(self):
        with self._lock:
            status = self.job_status()
            try:
                data = read_run(self._root) if self._root else {"root":"", "exists":False, "state":None, "state_error":None, "events":[], "event_errors":[], "empty":True}
            except Exception as exc:
                data = {"root": str(self._root), "exists": False, "state": None,
                        "state_error": str(exc), "events": [], "event_errors": [], "empty": True}
            data["preferences"] = dict(self._preferences)
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


def _initial_window_geometry(webview):
    """Use logical desktop coordinates, including the Windows work area if exposed."""
    geometry = {"width": 1000, "height": 660}
    try:
        screen = webview.screens[0]
        frame = getattr(screen, "frame", None)
        if frame is not None and all(isinstance(getattr(frame, key, None), (int, float))
                                     for key in ("X", "Y", "Width", "Height")):
            x, y, width, height = frame.X, frame.Y, frame.Width, frame.Height
        else:
            x, y, width, height = screen.x, screen.y, screen.width, screen.height
        width, height = int(width), int(height)
        if width > 0 and height > 0:
            geometry.update(width=min(1000, width), height=min(660, height))
            geometry.update(x=int(x) + (width - geometry["width"]) // 2,
                            y=int(y) + (height - geometry["height"]) // 2)
    except (AttributeError, IndexError, TypeError, ValueError, RuntimeError):
        # Older renderers center their default restored window themselves.
        pass
    return geometry


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
        geometry = _initial_window_geometry(webview)
        minimum = (min(640, geometry["width"]), min(480, geometry["height"]))
        window = webview.create_window(
            "Auto-Coding - Run Dashboard", html=HTML, js_api=api, **geometry,
            min_size=minimum, resizable=True, frameless=False, fullscreen=False,
        )
        api._dialogs = NativeDialogs(webview, window)
        webview.start()
        return 0
    except Exception as exc:
        print(f"Could not start the desktop window. On Windows install Microsoft Edge WebView2 Runtime and pywebview: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
