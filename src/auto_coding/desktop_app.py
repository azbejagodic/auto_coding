"""Optional native desktop bridge; importing this module never loads a GUI."""
from __future__ import annotations

import argparse
import sys
import threading
from pathlib import Path

from auto_coding.desktop_reader import read_run, read_evidence
from auto_coding.desktop_control import DesktopController
from auto_coding.simple_run import load_tasks
from auto_coding.debug_export import export_debug, WARNING


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
.top{display:flex;justify-content:space-between;gap:16px;align-items:center;margin-bottom:16px}
.overview-top{justify-content:flex-start}
.overview-top #refresh{margin-top:0!important}
.overview-top>div:last-child{display:flex;align-items:center;gap:12px}
.top>div:first-child{min-width:0}
.top>div:last-child{flex-shrink:0}
.top h1{font-size:26px;margin:4px 0}
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
.events{font-size:14px}
}
@media(max-width:760px){
.side{position:static;width:auto;padding:12px 16px}.shell{display:block}.brand{margin:0 0 8px}.side nav{display:flex;gap:8px}.nav{margin:0;padding:10px}.main{margin-left:0;padding:16px}.top{align-items:flex-start}.top h1{font-size:24px}.repository-line{grid-template-columns:minmax(0,1fr)}.setup-fields{grid-template-columns:minmax(0,1fr)}.grid{grid-template-columns:minmax(0,1fr)}.field-line{flex-wrap:wrap}.selected-path{flex-basis:100%}.panel{padding:16px}.filters label{min-width:0}.top>div:last-child{flex-shrink:1}#header-state{white-space:normal}
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
<header id="dashboard-header" class="top overview-top">
<div id="header-context" hidden>
<div class="label">Local developer workspace</div>
<h1 id="page-title">Overview</h1>
<div class="muted project-heading" id="selected-project">No project selected</div>
</div>
<div>
<div id="header-state" class="pill" role="status" aria-live="polite" aria-atomic="true">Idle</div>
<button id="refresh" style="margin-top:12px">Refresh</button>
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
<p id="github-state" class="muted">Connect a repository to choose a feature branch.</p>
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
<div id="attempts-error" class="notice red" role="status" aria-live="polite"></div>
<div class="launch-row">
<button id="start" aria-describedby="start-reason attempts-error">Start</button>
<button id="resume" aria-describedby="resume-reason attempts-error">Resume</button>
<button id="push-approved" aria-describedby="push-reason">Push Approved Commits</button>
</div>
<div id="start-reason" class="muted" role="status" aria-live="polite"></div>
<div id="resume-reason" class="muted">Open a matching stopped run to resume.</div>
<div id="push-reason" class="muted"></div>
<div id="preflight" class="notice muted"></div>
<div id="message" class="notice" role="status" aria-live="polite"></div>
<label class="push-policy"><input id="auto-push" type="checkbox"> Automatically push approved tasks</label>
</section>
<section id="logs-tools" hidden aria-label="Repository tools and diagnostics">
<details id="secondary-controls">
<summary>Repository tools and preflight diagnostics</summary>
<div class="controls">
<button id="open-run">Open Run</button>
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
<div id="run-folder" class="path muted"></div>
</details>
</details>
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
let logQuery = '';
let recentRuns = {runs:[]};
let exportResult = null;
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
  box.open = expanded.has(key);
  box.ontoggle = () => { if (box.open) expanded.add(key); else expanded.delete(key); };
  const content = el('pre', text);
  content.setAttribute('tabindex', '0');
  box.append(el('summary', title), content);
  return box;
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
  document.getElementById('push-reason').textContent = c.push?.reason || 'Select a managed repository to publish approved local checkpoints.';
  const savedPush = !!latest?.state?.auto_push;
  const savedRun = !!c.logs || !!latest?.root;
  const push = document.getElementById('auto-push');
  // A stopped run may also be used as setup for a new Start: retain that opt-in separately.
  const showingSavedPolicy = savedRun && (c.active !== false || c.uncertain || !c.start_ready);
  const pushContext = JSON.stringify([c.repo, c.preflight?.branch, c.plan, c.logs, latest?.root]);
  if (showingSavedPolicy) push.checked = savedPush;
  // An opt-in belongs to this setup, never to another repository, branch or plan.
  else if (push.showingSavedPolicy || push.setupContext !== pushContext) push.checked = false;
  push.setupContext = pushContext;
  push.showingSavedPolicy = showingSavedPolicy;
  push.disabled = locked || showingSavedPolicy || protectedBranch;
  if (protectedBranch && !showingSavedPolicy) push.checked = false;
  push.indeterminate = showingSavedPolicy && !latest?.state;
  const validAttempts = validateAttempts();
  document.getElementById('start').disabled = locked || protectedBranch || !c.start_ready || !validAttempts;
  document.getElementById('resume').disabled = locked || protectedBranch || !c.resume_ready || !validAttempts;
  const startReason = !ready ? 'Loading repository status...' : busy ? 'Finish the current action first.'
    : c.uncertain || c.active === null ? 'Resolve unknown child status in Logs.' : c.active ? 'A run is already active.'
    : !validAttempts ? 'Enter 1–10 whole attempts.' : !c.repo ? 'Connect a repository first.' : !c.plan ? 'Choose a task plan.'
    : !c.start_ready ? c.preflight?.errors?.[0] || c.preflight?.start_error || 'Resolve setup diagnostics in Logs.' : 'Ready. Review the native confirmation to launch.';
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
function render(data) {
  evidenceViewer.hidden = page !== 'logs';
  document.getElementById('logs-message').hidden = page === 'overview';
  document.getElementById('header-context').hidden = page === 'overview';
  document.getElementById('dashboard-header').className = 'top' + (page === 'overview' ? ' overview-top' : '');
  const searchFocused = document.activeElement?.id === 'log-search';
  const searchCursor = searchFocused ? document.activeElement.selectionStart : null;
  latest = data;
  const c = data.controller || {};
  const key = page + JSON.stringify(data);
  updateControls();
  if (runPresentation(data).label === 'Completed' && document.getElementById('message').textContent.startsWith('Launch accepted.')) message('');
  if (renderKey === key) return;
  renderKey = key;
  [['project-path', c.repo || 'No project selected'], ['plan-path', c.plan || 'No plan selected']].forEach(([id, value]) => {
    const field = document.getElementById(id);
    if (field.textContent !== value) field.textContent = value;
  });
  document.getElementById('selected-project').textContent = c.repository || c.repo || 'No project selected';
  const connection = document.getElementById('github-state');
  const currentBranch = c.preflight?.branch;
  const publication = c.github?.error || !currentBranch ? 'Publication unknown' : c.push?.eligible ? 'Approved checkpoints pending push'
    : c.github?.head && c.github?.remote_head === c.github.head
      ? 'Published at ' + c.github.head + ' (cached origin/' + currentBranch + ')'
      : c.github?.head && !c.github?.remote_head ? 'Local-only, unpublished'
      : c.github?.head ? 'Local and origin commits differ; fetch and review before publishing'
      : 'Publication unknown';
  connection.textContent = c.repo ? 'Current branch: ' + (currentBranch || 'Detached HEAD / unavailable') + ' | ' + (c.preflight?.clean === true ? 'Clean' : 'Changes or unavailable status') + ' | ' + publication + (c.github?.error ? ' | ' + c.github.error : '') : 'Connect a repository to choose a feature branch.';
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
    const selected = branches.selectionContext === selectionContext && names.includes(branches.value)
      ? branches.value : names.includes(c.preflight?.branch) ? c.preflight.branch : '';
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
  // Only actionable errors occupy the main view; the empty state supplies setup guidance.
  if (errors.length) [...new Set(errors)].forEach(error => preflight.append(el('p', error, 'red')));
  else if (c.uncertain || c.active === null) preflight.append(el('p', 'Child status is unknown. Inspect this run before launching more work.', 'warning'));
  else if (c.repo && c.plan && !c.active && !c.resume_ready && git.clean !== true) preflight.append(el('p', 'START requires a clean working tree. Commit or discard project changes first.', 'warning'));
  document.getElementById('preflight-details').textContent = JSON.stringify({branch:git.branch, clean:git.clean, start_ready:c.start_ready, resume_ready:c.resume_ready, diagnostics:errors}, null, 2);
  document.getElementById('resume-reason').textContent = c.resume_ready ? 'Continue the stopped run.' : c.active || c.uncertain ? 'Runner active or uncertain.' : 'Select a matching stopped run to resume.';
  document.getElementById('run-setup').hidden = page !== 'overview';
  document.getElementById('logs-tools').hidden = page !== 'logs';
  const presentation = runPresentation(data);
  const badge = document.getElementById('header-state');
  badge.textContent = presentation.label;
  badge.className = 'pill ' + presentation.color;
  if (presentation.label === 'Completed' && document.getElementById('message').textContent.startsWith('Launch accepted.')) message('');
  updateControls();
  if (evidenceRoot !== data.root) {
    evidenceViewer.replaceChildren();
    exportResult = null;
    expanded.clear();
    evidenceRoot = data.root;
  }
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
    // Keep current failures above the checkbox; historical process exits stay in Logs.
    if (c.error && !(presentation.label === 'Completed' || c.active === false && !c.uncertain && state.status === 'checkpointing')) preflight.append(el('p', c.error, 'red'));
    if (data.state_error) preflight.append(el('p', data.state_error, 'red'));
    if (c.logs && !data.exists && !data.state_error) preflight.append(el('p', 'Log directory is missing or unavailable.', 'warning'));
    if (state.pending_checkpoint) preflight.append(el('p', 'A checkpoint is pending reconciliation. Inspect Logs before resuming.', 'warning'));
    if (state.publication?.outcome === 'pending') preflight.append(el('p', 'Approved locally, push pending. Use Resume (Retry Push); inspect Logs for details.', 'warning'));
    return;
  }
  if (page === 'logs') {
    const library = panel('Recent runs');
    library.append(el('p', 'Runs and logs are kept until you deliberately delete them. Forget removes only the history entry; log files stay on disk.', 'muted'));
    const importRun = el('button', 'Open / Import run folder');
    importRun.disabled = busy || !ready || c.active !== false || c.uncertain;
    importRun.onclick = () => act('open_run');
    library.append(importRun);
    if (recentRuns.error) library.append(el('p', recentRuns.error, 'warning'));
    if (!recentRuns.runs.length) library.append(el('p', 'No saved runs found. Start a run or explicitly import an older run folder. Only managed runs and folders you open are listed.', 'muted'));
    recentRuns.runs.forEach(run => {
      const row = el('section', undefined, 'attempt');
      row.append(el('h3', run.run_id), el('p', [run.repo || 'Repository not recorded', run.branch ? 'Branch: ' + run.branch : 'Branch not recorded', run.time || 'Time not recorded', 'Status: ' + run.status, run.completed == null ? 'Completed tasks not recorded' : 'Completed tasks: ' + run.completed].join(' | ')), el('div', run.path, 'path muted'));
      const open = el('button', 'Open / View');
      open.disabled = busy || !ready || c.active !== false || c.uncertain || run.status === 'missing';
      open.onclick = () => historyAction('open_recent_run', run.path);
      const forget = el('button', 'Forget history entry');
      forget.disabled = busy || !ready;
      forget.onclick = () => historyAction('forget_run', run.path);
      row.append(open, forget); library.append(row);
    });
    library.append(el('p', 'Showing up to 200 runs. Active or uncertain jobs keep run switching locked; their current evidence remains available below.', 'muted'));
    app.append(library);
  }
  if (page === 'logs') {
    const exports = panel('Download Logs / Export Debug Package');
    exports.append(el('p', 'Inspect before sharing every ZIP. Run identifiers and filenames may be private.', 'warning'));
    exports.append(el('p', 'DEBUG REPORT (recommended default): bounded structured state, timeline, test results and review decisions plus inventory. Prompts, output and free-form review text are omitted. This is not a guarantee of perfect redaction.'));
    exports.append(el('p', 'FULL RAW LOGS: allowed files from this run only; prompts, review text and subprocess output may contain private data/API keys. Requires an extra native confirmation.', 'warning'));
    [['standard', 'Export DEBUG REPORT (default)'], ['raw', 'Export FULL RAW LOGS']].forEach(([mode, label]) => {
      const button = el('button', label);
      button.disabled = busy || !ready || !data.root || !data.exists;
      button.onclick = () => exportLogs(mode);
      exports.append(button);
    });
    if (!data.root || !data.exists) exports.append(el('p', 'Select an available run using Open / View or Open / Import run folder to export.'));
    if (exportResult) {
      exports.append(el('pre', exportResult));
      if (window.pywebview.api.open_export_folder) {
        const folder = el('button', 'Open export destination folder');
        folder.disabled = busy || !ready;
        folder.onclick = async () => {
          const result = await window.pywebview.api.open_export_folder();
          if (!result.ok) message(result.error, true);
        };
        exports.append(folder);
      }
    }
    app.append(exports);
  }
  if (!c.logs && !data.root) {
    if (page !== 'overview') {
      const empty = el('section', undefined, 'empty');
      empty.append(el('p', page === 'tasks' ? 'No task history yet. Start or open a run.' : 'No run evidence yet. Start or open a run.'));
      const setup = el('button', 'Go to run setup');
      setup.onclick = () => navigate('overview');
      empty.append(setup);
      app.append(empty);
    }
    if (c.error) app.append(el('div', c.error, 'warning'));
    return;
  }
  const state = data.state || {};
  const events = data.events || [];
  const history = Array.isArray(state.history) ? state.history.filter(item => item && typeof item === 'object') : [];
  const completed = Array.isArray(state.completed) ? state.completed : null;
  const reconciled = presentation.label === 'Completed' || c.active === false && !c.uncertain && state.status === 'checkpointing';
  if (c.error && !reconciled) app.append(el('div', c.error, 'notice warning'));
  if (data.state_error) app.append(el('div', data.state_error, 'warning'));
  if (data.empty) app.append(el('div', 'No run state or events found. Select a project and plan to START, or inspect available raw files in Logs.', 'empty'));
  if (c.logs && !data.exists && !data.state_error) app.append(el('div', 'Log directory is missing or unavailable.', 'warning'));
  if (state.pending_checkpoint) app.append(el('div', 'A checkpoint is pending reconciliation. No completion is inferred.', 'warning'));
  if (state.auto_push && page !== 'overview') app.append(el('div', 'This run automatically pushes approved tasks to origin/' + state.auto_push.branch + '. Resume preserves this setting.', 'notice'));
  if (state.publication?.outcome === 'pending') app.append(el('div', 'Approved locally, push pending. Resolve connectivity or permissions, then use Resume (Retry Push). The same SHA is retried before any further tasks; Codex is not rerun for the approved task. Remote divergence requires manual review.', 'warning'));
  if (page === 'logs') (data.event_errors || []).forEach(error => app.append(el('div', error, 'warning')));
  if (page === 'tasks') {
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
    if (c.exit_code != null || c.error || c.process_history?.length) app.append(detail('Process history - prior exits and diagnostics', JSON.stringify({previous_child_exit_code:c.exit_code, diagnostic:c.error, history:c.process_history || [], note:'Historical process results do not replace the current durable run outcome.'}, null, 2), 'process-history'));
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
  document.getElementById('page-title').textContent = {overview:'Overview', tasks:'Tasks', logs:'Logs'}[page];
  if (latest) render(latest);
  if (name === 'logs') refresh(false);
}
async function exportLogs(mode) {
  if (busy || !ready || !latest?.root || !latest?.exists) return;
  exportResult = null;
  busy = true; updateControls(); renderKey = null; render(latest);
  try {
    if (refreshing) await refreshing;
    const result = await window.pywebview.api.export_logs(mode);
    if (result.ok && !result.cancelled) {
      exportResult = 'Created: ' + result.path + '\nContents: ' + result.contents.join(', ') + '\n' + result.warning + '\n' + JSON.stringify(result.inventory, null, 2);
      message('ZIP created: ' + result.path + '. Inspect before sharing.');
    } else message(result.cancelled ? 'Export cancelled. No ZIP created.' : result.error, !result.cancelled);
  } catch (error) { message('Export failed: ' + error, true); }
  finally { busy = false; renderKey = null; render(latest); updateControls(); }
}
async function historyAction(method, path) {
  if (busy || !ready) return;
  if (method === 'open_recent_run' && (latest?.controller?.active !== false || latest?.controller?.uncertain)) return;
  busy = true; updateControls();
  try {
    if (refreshing) await refreshing;
    const result = await window.pywebview.api[method](path);
    message(result.error || (method === 'forget_run' ? 'History entry forgotten. Log data remains on disk.' : 'Run opened for inspection. No work launched.'), !result.ok);
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
    if (accepted) document.getElementById('auto-push').checked = false;
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
document.getElementById('attempts').oninput = updateControls;
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

    def save_export(self):
        enum = getattr(self._webview, "FileDialog", None)
        kind = getattr(enum, "SAVE") if enum is not None else self._webview.SAVE_DIALOG
        result = self._window.create_file_dialog(kind, allow_multiple=False,
                    save_filename="debug-report.zip", file_types=("ZIP archives (*.zip)",))
        return result[0] if result and not isinstance(result, str) else result


class Bridge:
    """Small JS API. Selection never launches; each launch needs native confirmation.

    A fresh controller on run selection prevents stale metadata from a previous
    run making a legacy run appear resumable. Never replace a tracked live job.
    """

    def __init__(self, root: Path | None = None, *, controller=None,
                 dialogs=None, controller_factory=DesktopController):
        self._controller_factory = controller_factory
        self._controller = controller if controller is not None else controller_factory()
        self._history = getattr(self._controller, "run_history", None)
        self._dialogs = dialogs
        self._lock = threading.RLock()
        self._launch_lock = threading.Lock()
        self._root = Path(root).expanduser() if root is not None else None
        self._selection_error = None
        self._export_folder = None
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
                    if self._history is not None and isinstance(candidate, DesktopController):
                        candidate.runs_dir = self._history.runs_dir
                        candidate.run_history = self._history
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

    def _github(self, action, *args, **kwargs):
        try:
            result = self._controller.github(action, *args, **kwargs)
            self._selection_error = result.get("error")
            return result
        except Exception:
            self._selection_error = "Workspace operation failed. Check Git availability and repository access, then retry."
            return {"ok": False, "error": self._selection_error}

    def connect_github(self, repository):
        with self._lock:
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
