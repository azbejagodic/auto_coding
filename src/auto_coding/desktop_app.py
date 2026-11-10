"""Optional native desktop bridge; importing this module never loads a GUI."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import threading
from pathlib import Path

from auto_coding.desktop_reader import (read_run, read_evidence, export_run, export_audit,
                                       export_file, directory_identity, redact_export)
from auto_coding.desktop_control import DesktopController
from auto_coding.simple_run import load_tasks


HTML = r'''<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>Auto-Coding</title><style>
*{box-sizing:border-box}
body{margin:0;min-height:100vh;display:grid;place-items:center;padding:18px 4px;background:#000;color:#c9c9c9;font:20px/1.5 "Segoe UI",Arial,sans-serif}
button,input,select{font:inherit;color:inherit}
button{border:1px solid #202020;border-radius:999px;padding:10px 24px;background:transparent;font-size:22px;font-weight:600;cursor:pointer;white-space:nowrap}
button:hover:enabled{background:#191919}
button:disabled{color:#aaa;cursor:not-allowed}
button:focus-visible,input:focus-visible,select:focus-visible,[tabindex]:focus-visible{outline:3px solid #8bbcff;outline-offset:3px}
[hidden]{display:none!important}
.dashboard{width:min(100%,1000px);min-height:555px;border:1px solid #292929;border-radius:20px;padding:20px 18px 28px;background:#020202}
h1{font-size:25px;line-height:1.5;margin:0 0 14px;color:#ddd}
.file-row{display:flex;align-items:center;gap:12px}
.selected-file{display:flex;align-items:center;gap:14px;flex:1;min-width:0;border:1px solid #303030;border-radius:14px;min-height:68px;padding:14px}
#plan-path{overflow-wrap:anywhere;min-width:0;user-select:text}
.icon{width:24px;height:24px;display:inline-block;vertical-align:middle;flex-shrink:0;margin-right:8px}
.selected-file .icon{margin:0}
.controls{display:flex;align-items:center;flex-wrap:wrap;gap:12px;margin-top:12px}
.filled{background:#101010;border-color:#101010}
.pill{border-radius:999px;font-weight:600;font-size:19px;padding:2px 12px;min-width:133px;text-align:center}
.muted{color:#aaa}.green{color:#00c25a;background:#002512}.red{color:#ffb0b0}.amber{color:#e5ca92}
.section-divider{border:0;border-top:1px solid #292929;margin:32px 0 36px}
.unlock{display:flex;align-items:center;gap:12px;margin:0 0 26px}
.indicator{display:inline-block;width:21px;height:21px;background:#666;border-radius:50%;flex-shrink:0}
.indicator.green{background:#00bd56}
.run-summary{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:20px;margin:22px 0 0}
.run-summary dt{font-size:18px}.run-summary dd{margin:2px 0 0;font-size:25px;font-weight:650;color:#ddd;overflow-wrap:anywhere}
.notice{font-size:15px;white-space:pre-wrap;overflow-wrap:anywhere;margin:8px 0 0}.notice:empty{display:none}
.error-link{font-size:14px;padding:2px 10px;margin-top:6px}
dialog{color:#ccc;background:#0b0b0b;border:1px solid #444;border-radius:16px;width:min(650px,calc(100% - 24px));max-height:85vh;overflow:auto;padding:24px}
dialog::backdrop{background:#000b}dialog label{display:block;margin-top:12px}dialog input:not([type=checkbox]),dialog select{background:#161616;border:1px solid #555;border-radius:8px;padding:8px;width:100%}dialog button{font-size:15px;padding:8px 14px}dialog p{font-size:14px;overflow-wrap:anywhere}dialog .controls{gap:8px}
@media(max-width:700px){body{font-size:16px;padding:12px 6px}.dashboard{padding:18px 14px;min-height:0}button{font-size:17px;padding:10px 16px}.pill{font-size:16px}.file-row{gap:8px}.selected-file{min-height:60px;padding:10px;gap:8px}.run-summary{gap:12px}.run-summary dd{font-size:21px}.section-divider{margin:26px 0}.unlock{margin-bottom:20px}}
@media(max-width:400px){.file-row{flex-wrap:wrap}.selected-file{flex-basis:100%}.controls{gap:8px}}
@media(prefers-reduced-motion:reduce){*{scroll-behavior:auto}}
</style></head><body>
<main class="dashboard" aria-labelledby="audit-title">
<section aria-labelledby="audit-title">
<h1 id="audit-title">Plan Audit</h1>
<div class="file-row"><div class="selected-file"><svg class="icon" aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M14 2H6a2 2 0 0 0-2 2v16h16V8zM14 2v6h6M10 10l-3 4 3 4m4-8 3 4-3 4"/></svg><div id="plan-path" tabindex="0" role="textbox" aria-readonly="true" aria-label="Selected JSON file">Selected JSON file</div></div><button id="browse-plan" title="Browse JSON plan and configure selections">Browse</button></div>
<div class="controls"><button id="start-audit" class="filled" disabled>Start Audit</button><span id="audit-result" class="pill muted" role="status" aria-live="polite">Idle</span><button id="download-audit" disabled title="Download the verified audit report/log; no fix artifact is generated">Download Fix / Log</button></div>
<p id="audit-error" class="notice red" role="alert"></p>
</section>
<hr class="section-divider">
<section aria-label="Run controls and status">
<p class="unlock"><span id="approval-indicator" class="indicator" aria-hidden="true"></span>Run controls unlock after approved audit</p>
<div class="controls"><button id="start" class="filled" disabled aria-describedby="start-reason"><svg class="icon" aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="9"/><path d="m10 8 6 4-6 4z"/></svg><span id="start-label">Start</span></button><button id="stop" disabled title="Stop after current task" aria-label="Stop after current task"><svg class="icon" aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="3" width="18" height="18" rx="3"/></svg>Stop</button><button id="download-logs" disabled><svg class="icon" aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 2v13m-5-5 5 5 5-5M3 15v5h18v-5"/></svg>Download Logs</button></div>
<span id="start-reason" hidden>Start requires verified approval for the exact selected inputs.</span>
<dl class="run-summary"><div><dt>Status</dt><dd id="run-status" role="status" aria-live="polite">Idle</dd></div><div><dt>Task</dt><dd id="run-task">-- / --</dd></div><div><dt>Time</dt><dd id="run-time">--:--:--</dd></div></dl>
<p id="progress-error" class="notice red" role="alert"></p>
</section>
<p id="message" class="notice" role="status" aria-live="polite"></p>
<button id="configure-error" class="error-link" hidden>Review configuration</button>
</main>
<dialog id="configuration" aria-label="Audit and run configuration">
<p>Choose a target checkout and feature branch, or select saved run logs. Selection never launches work. Model and reasoning defaults for implementation and review remain in your local Codex CLI configuration.</p>
<div class="controls"><button id="browse-project">Choose checkout</button><button id="open-run">Select saved run / audit logs</button></div><p id="project-path"></p><p id="run-folder"></p>
<label for="export-evidence">Saved evidence to include in Download Logs</label><select id="export-evidence"></select><div class="controls"><button id="include-evidence" disabled>Include selected file in log export</button></div>
<label for="audit-model">Audit model (High reasoning)</label><input id="audit-model" placeholder="Explicit model ID"><label for="audit-goal">Audit goal</label><input id="audit-goal" maxlength="4000">
<label for="attempts">Attempts per task (1–10)</label><input id="attempts" type="number" min="1" max="10" step="1" value="3" required aria-describedby="attempts-error"><p id="attempts-error" class="notice red" role="status" aria-live="polite"></p>
<label for="github-repo">Managed GitHub repository (optional)</label><input id="github-repo" placeholder="OWNER/REPO"><div class="controls"><button id="connect-github">Connect repository</button><button id="list-github">My repositories</button><select id="github-repositories" aria-label="Authenticated repositories" hidden></select></div><p id="github-state"></p>
<div class="controls"><select id="github-branch" aria-label="Feature branch"></select><button id="select-branch">Select branch</button><button id="new-branch">New branch</button><button id="fetch-github">Fetch branches</button></div>
<div id="branch-form" hidden><label for="branch-name">New feature branch</label><input id="branch-name"><label for="branch-base">Remote base</label><select id="branch-base"></select><label id="branch-base-confirm-label" hidden><input id="branch-base-confirm" type="checkbox"> I confirm this alternate remote base instead of origin/main.</label><div class="controls"><button id="create-branch">Create branch</button><button id="cancel-branch">Cancel</button></div></div>
<label><input id="auto-push" type="checkbox"> Automatically push approved tasks</label><p>Off by default for a new Start. Resume preserves the saved authorization. No automatic merge or force-push.</p><div class="controls"><button id="push-approved">Push Approved Commits</button></div><p id="push-reason"></p>
<p id="configuration-status" class="notice" role="status" aria-live="polite"></p><p>Every launch requires native confirmation. Local Codex CLI authentication is required. Closing this window leaves the runner alive. Download Fix / Log exports the verified audit report/log, not a generated fix.</p><div class="controls"><button id="configuration-done">Done</button></div>
</dialog>
<script>
const get = id => document.getElementById(id);
let latest = null, busy = false, ready = false, refreshing = null, closed = false;
function el(tag, text) { const node = document.createElement(tag); if (text !== undefined) node.textContent = String(text); return node; }
function message(text, error = false) {
  get('message').textContent = text || '';
  get('message').className = 'notice ' + (error ? 'red' : 'muted');
  get('configuration-status').textContent = text || '';
}
function configure() { if (!busy && ready && !get('configuration').open) get('configuration').showModal(); }
const controls = ['github-repo','connect-github','list-github','github-repositories','github-branch','select-branch','new-branch','fetch-github','branch-name','branch-base','branch-base-confirm','create-branch','cancel-branch','push-approved','browse-project','browse-plan','open-run','attempts','audit-model','audit-goal','auto-push'];
function validateAttempts() {
  const input = get('attempts'), value = input.value.trim(), number = Number(value);
  const valid = /^\d+$/.test(value) && Number.isInteger(number) && number >= 1 && number <= 10;
  input.setAttribute('aria-invalid', String(!valid));
  get('attempts-error').textContent = valid ? '' : 'Cannot launch: attempts must be a whole number from 1 to 10.';
  return valid;
}
function jsonPlan() { return typeof latest?.controller?.plan === 'string' && /\.json$/i.test(latest.controller.plan); }
function resumable() { return jsonPlan() && latest?.controller?.resume_ready === true; }
function approved() {
  const c = latest?.controller || {}, a = c.audit || {};
  return jsonPlan() && c.start_ready === true && a.status === 'APPROVE' && a.verified === true && a.start_approved === true;
}
function updateControls() {
  const c = latest?.controller || {}, audit = c.audit || {};
  const locked = busy || !ready || c.active !== false || c.uncertain;
  controls.forEach(id => { get(id).disabled = locked; });
  get('browse-plan').disabled = busy || !ready;
  get('browse-plan').title = c.active !== false || c.uncertain
    ? 'View configuration and saved evidence; selections are locked while running'
    : 'Browse JSON plan and configure selections';
  ['github-branch','select-branch','new-branch','fetch-github','branch-base','branch-base-confirm','create-branch'].forEach(id => { get(id).disabled = locked || !c.repository || c.preflight?.clean !== true; });
  const base = get('branch-base').value;
  get('branch-base-confirm-label').hidden = !base || base === 'main';
  get('create-branch').disabled ||= !base || (base !== 'main' && !get('branch-base-confirm').checked);
  get('push-approved').disabled = locked || !c.push?.eligible;
  get('push-reason').textContent = c.push?.reason || 'Select a managed repository to publish approved local checkpoints.';
  get('start-audit').disabled = locked || (c.plan && !jsonPlan()) || (c.repo && c.plan && !c.audit_ready);
  get('download-audit').disabled = busy || !ready || !c.audit_export_available;
  get('download-logs').disabled = busy || !ready || !latest?.exists;
  get('export-evidence').disabled = busy || !ready || !latest?.exists;
  get('include-evidence').disabled = busy || !ready || !latest?.exists || !get('export-evidence').value;
  const verified = audit.status === 'APPROVE' && audit.verified === true;
  get('audit-result').textContent = verified ? '\u2713 Approved' : audit.status === 'APPROVE' ? 'Unverified' : audit.status || 'Idle';
  get('audit-result').className = 'pill ' + (verified ? 'green' : ['REVISE','REJECT','BLOCK','Infrastructure error'].includes(audit.status) ? 'red' : ['Running','Unknown'].includes(audit.status) ? 'amber' : 'muted');
  get('approval-indicator').className = 'indicator' + (!locked && approved() ? ' green' : '');
  get('audit-error').textContent = audit.error || (['REVISE','REJECT','BLOCK','Infrastructure error'].includes(audit.status) ? c.audit_reason || 'Audit did not approve this plan. Download the report for details.' : '');
  get('start').disabled = locked || !validateAttempts() || !(resumable() || approved());
  get('start-label').textContent = resumable() ? 'Resume' : 'Start';
  get('start-reason').textContent = resumable() ? 'Resume uses persisted authorization and identity checks. Pending publication retries the same approved commit first.' : c.start_reason || 'Start requires verified approval for the exact selected inputs and native authorization.';
  get('start').title = get('start-reason').textContent;
  get('stop').disabled = busy || !ready || !c.stop_identity || c.stop_requested || latest?.state?.status === 'completed' || (c.active !== true && !c.uncertain);
  get('stop').title = c.stop_requested ? 'Stop after current task: request saved' : 'Stop after current task';
}
function runPresentation(data) {
  const c = data.controller || {}, state = data.state || {};
  if (c.uncertain || (c.active !== false && c.active !== true)) return {label:'Unknown child status', color:'amber'};
  if (c.active) return {label:state.status === 'stopping' ? 'Stopping' : 'Running', color:'blue'};
  if (state.publication?.outcome === 'pending') return {label:'Approved locally, push pending', color:'amber'};
  if (data.summary?.verification_status === 'failed' || state.status === 'failed') return {label:'Failed', color:'red'};
  if (state.status === 'completed' && data.summary?.completion_confirmed === true) return {label:'Completed', color:'green'};
  if (state.status === 'checkpointing' || state.pending_checkpoint) return {label:'Awaiting recovery', color:'amber'};
  if (state.status === 'stopped') return {label:'Stopped', color:'amber'};
  if (c.logs || data.root) return {label:state.status ? 'Unknown run state' : 'Awaiting recorded state', color:'amber'};
  return {label:'Idle', color:'muted'};
}
function updateElapsed(now = Date.now()) {
  const data = latest || {}, c = data.controller || {}, summary = data.summary || {};
  const start = Date.parse(summary.start_utc);
  const live = c.active === true && !c.uncertain && Number.isFinite(start) && now >= start;
  const seconds = live ? Math.floor((now - start) / 1000) : summary.duration_seconds;
  const valid = typeof seconds === 'number' && Number.isFinite(seconds) && seconds >= 0;
  const whole = Math.floor(seconds);
  const text = valid
    ? [Math.floor(whole / 3600), Math.floor(whole / 60) % 60, whole % 60].map(n => String(n).padStart(2, '0')).join(':') : '--:--:--';
  const hint = live ? 'Live wall time since persisted start UTC (includes pauses).'
    : valid ? 'Persisted duration from recorded UTC events (includes pauses).'
    : 'Elapsed time is unavailable: no confirmed start and end, or process status is unknown.';
  if (document.getElementById('run-time').textContent !== text) document.getElementById('run-time').textContent = text;
  document.getElementById('run-time').title = hint;
}
function render(data) {
  latest = data;
  const c = data.controller || {};
  get('plan-path').textContent = c.plan || 'Selected JSON file';
  get('project-path').textContent = c.repo || 'No checkout selected';
  get('run-folder').textContent = c.logs ? 'Saved run: ' + c.logs : '';
  const evidence = get('export-evidence');
  const available = (data.files || []).filter(file => file.status === 'available');
  const evidenceSignature = JSON.stringify([data.root, available.map(file => file.path)]);
  if (evidence.datasetSignature !== evidenceSignature) {
    evidence.replaceChildren();
    const placeholder = el('option', 'Choose an evidence file'); placeholder.value = ''; evidence.append(placeholder);
    available.forEach(file => { const option = el('option', file.path); option.value = file.path; evidence.append(option); });
    evidence.value = '';
    evidence.datasetSignature = evidenceSignature;
  }
  const connection = document.getElementById('github-state');
  connection.textContent = c.repository ? c.repository + ' | GitHub origin verified locally | Branch: ' + (c.preflight?.branch || 'Detached HEAD') + ' | ' + (c.preflight?.clean === true ? 'Clean' : 'Changes or unavailable status') + (c.github?.error ? ' | ' + c.github.error : '') : 'No GitHub repository selected. Enter OWNER/REPO or a GitHub URL; GitHub CLI is optional.';
  const branches = document.getElementById('github-branch');
  const bases = document.getElementById('branch-base');
  const remoteBases = [...new Set((c.github?.remote || []).filter(name => name.startsWith('origin/') && name !== 'origin/HEAD').map(name => name.slice(7)))];
  const baseSignature = JSON.stringify([c.repository, remoteBases]);
  if (bases.datasetSignature !== baseSignature) {
    bases.replaceChildren();
    remoteBases.forEach(name => { const option = el('option', 'origin/' + name); option.value = name; bases.append(option); });
    bases.value = remoteBases.includes('main') ? 'main' : remoteBases.includes('master') ? 'master' : remoteBases[0] || '';
    bases.datasetSignature = baseSignature;
    document.getElementById('branch-base-confirm').checked = false;
    document.getElementById('branch-form').hidden = true;
  }
  updateControls();
  const names = [...new Set([...(c.github?.local || []), ...(c.github?.remote || []).filter(name => name.startsWith('origin/') && name !== 'origin/HEAD').map(name => name.slice(7))])].filter(name => !['main', 'master'].includes(name.toLowerCase()));
  const signature = JSON.stringify([c.repository, c.preflight?.branch, names]);
  if (branches.datasetSignature !== signature) {
    branches.replaceChildren();
    const placeholder = el('option', 'Choose a feature branch'); placeholder.value = ''; branches.append(placeholder);
    names.forEach(name => { const option = el('option', name); option.value = name; branches.append(option); });
    branches.value = names.includes(c.preflight?.branch) ? c.preflight.branch : '';
    branches.datasetSignature = signature;
  }
  const git = c.preflight || {};
  const count = data.task_count ?? c.task_count;
  const currentTask = data.state?.current_task;
  const completedTasks = data.state?.completed;
  let taskNumber = '--';
  // Tasks execute in plan order. During checkpointing the current ID may
  // already be recorded as completed; keep its position until it advances.
  if (typeof currentTask === 'string' && currentTask && Array.isArray(completedTasks)
      && completedTasks.every(id => typeof id === 'string' && id)
      && new Set(completedTasks).size === completedTasks.length) {
    const completedIndex = completedTasks.indexOf(currentTask);
    const position = completedIndex >= 0 ? completedIndex + 1 : completedTasks.length + 1;
    if (count == null || Number.isInteger(count) && position <= count) taskNumber = position;
  }
  document.getElementById('run-task').textContent = taskNumber + ' / ' + (count ?? '--');
  if (Number.isInteger(data.summary?.current) && data.summary.current >= 0
      && (count == null || data.summary.current <= count)) {
    document.getElementById('run-task').textContent = data.summary.current + ' / ' + (count ?? '--');
  }

  get('run-status').textContent = runPresentation(data).label;
  const reconciled = runPresentation(data).label === 'Completed'
    || c.active === false && !c.uncertain && data.state?.status === 'checkpointing';
  const errors = [...(c.repo || c.plan || c.logs ? git.errors || [] : []), ...Object.values(c.validation_errors || {}), c.selection_error, git.resume_error, git.start_error, reconciled ? null : c.error, data.summary?.error];
  if (c.plan && !jsonPlan()) errors.push('Select a JSON plan (.json) using Browse.');
  if (c.logs && data.state_error) errors.push(data.state_error);
  if (c.logs && !data.exists) errors.push('Selected log directory is missing or unavailable.');
  get('progress-error').textContent = [...new Set(errors.filter(Boolean))].join(' ');
  get('configure-error').hidden = !errors.some(Boolean) && !c.audit?.error;
  if (runPresentation(data).label === 'Completed' && get('message').textContent.startsWith('Launch accepted.')) message('');
  updateControls(); updateElapsed();
}
async function refresh(clearMessage = true) {
  if (!ready || busy || closed || document.hidden) return;
  if (refreshing) return refreshing;
  refreshing = (async () => {
    try { const data = await window.pywebview.api.snapshot(); if (closed || document.hidden) return; if (clearMessage) message(''); render(data); }
    catch (error) {
      if (closed || document.hidden) return;
      latest = {...(latest || {}), controller: {...(latest?.controller || {}), active: null, uncertain: true, audit:{status:'Unknown', verified:false}, start_ready:false, audit_ready:false}};
      render(latest);
      message('Could not load status: ' + error, true);
    }
  })();
  try { await refreshing; } finally { refreshing = null; }
}
async function act(method) {
  if (busy || !ready) return;
  const launch = ['start','resume'].includes(method);
  const button = {select_repo:'browse-project',select_plan:'browse-plan',open_run:'open-run',start:'start',resume:'start',start_audit:'start-audit',stop_after_task:'stop',download_audit:'download-audit',download_logs:'download-logs',evidence:'include-evidence'}[method];
  if (get(button).disabled || launch && !validateAttempts()) return;
  if (method === 'start_audit' && (!latest?.controller?.audit_ready || !get('audit-model').value.trim())) { configure(); message('Choose the target checkout, feature branch and explicit audit model before auditing.'); return; }
  // A native selection or confirmation can race polling. Wait, then recheck authorization.
  busy = true; updateControls();
  try {
    if (refreshing) await refreshing;
    if (launch && (latest?.controller?.active !== false || latest?.controller?.uncertain || (method === 'resume' ? !resumable() : !approved()))) throw Error('Selection or authorization changed. Review current status.');
    const args = launch ? [Number(get('attempts').value)] : [];
    if (method === 'stop_after_task') args.push(latest?.controller?.stop_identity);
    if (method === 'evidence') args.push(get('export-evidence').value);
    if (method === 'start_audit') args.push(get('audit-model').value.trim(), get('audit-goal').value);
    if (method === 'start' && get('auto-push').checked === true) args.push(true);
    const result = await window.pywebview.api[method](...args);
    if (result.ok && !result.cancelled && !method.startsWith('download_') && method !== 'evidence') get('auto-push').checked = false;
    message(result.cancelled ? 'Cancelled. No work launched.' : result.error || result.message || result.warning || (method === 'stop_after_task' && result.ok ? 'Stop requested. Current task and safe checkpoint handling finish first.' : result.ok ? launch ? 'Launch accepted. Waiting for durable runner progress.' : method === 'download_audit' ? 'Verified audit report/log exported. No fix artifact was generated.' : method === 'evidence' ? 'Selected evidence included in Download Logs. Changed files must be selected again.' : '' : result.text || 'Action failed.'), !result.ok);
  } catch (error) { message('Action failed: ' + error, true); }
  finally {
    busy = false; ready = false; updateControls(); ready = true;
    await refresh(false);
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
    busy = false; ready = false; updateControls(); ready = true;
    await refresh(false);
  }
}
document.getElementById('connect-github').onclick = () => workspaceAction('connect_github', [document.getElementById('github-repo').value.trim()], 'connect-github');
document.getElementById('list-github').onclick = () => workspaceAction('github_repositories', [], 'list-github');
document.getElementById('github-repositories').onchange = () => {
  const value = document.getElementById('github-repositories').value;
  if (value) { document.getElementById('github-repo').value = value; return workspaceAction('connect_github', [value], 'connect-github'); }
};
document.getElementById('select-branch').onclick = () => workspaceAction('checkout_github_branch', [latest?.controller?.repository, document.getElementById('github-branch').value], 'select-branch');
document.getElementById('fetch-github').onclick = () => workspaceAction('fetch_github', [latest?.controller?.repository], 'fetch-github');
document.getElementById('new-branch').onclick = () => { if (!document.getElementById('new-branch').disabled) { document.getElementById('branch-base-confirm').checked = false; document.getElementById('branch-form').hidden = false; updateControls(); document.getElementById('branch-name').focus(); } };
document.getElementById('cancel-branch').onclick = () => { document.getElementById('branch-form').hidden = true; document.getElementById('branch-name').value = ''; };
document.getElementById('branch-base').onchange = () => { document.getElementById('branch-base-confirm').checked = false; updateControls(); };
document.getElementById('branch-base-confirm').onchange = updateControls;
document.getElementById('create-branch').onclick = () => workspaceAction('create_github_branch', [latest?.controller?.repository, document.getElementById('branch-name').value.trim(), document.getElementById('branch-base').value, document.getElementById('branch-base-confirm').checked === true], 'create-branch');
document.getElementById('push-approved').onclick = () => workspaceAction('push_approved_commits', [], 'push-approved');

get('attempts').oninput = updateControls;
get('audit-model').oninput = updateControls;
get('export-evidence').onchange = updateControls;
get('include-evidence').onclick = () => act('evidence');
get('configuration-done').onclick = () => { if (!busy && validateAttempts()) get('configuration').close(); };
get('configuration').addEventListener('cancel', event => { if (busy) event.preventDefault(); });
get('configure-error').onclick = configure;
get('browse-plan').onclick = async () => {
  if (get('browse-plan').disabled) return;
  if (latest?.controller?.active === false && !latest?.controller?.uncertain) await act('select_plan');
  configure();
};
get('start').onclick = () => act(resumable() ? 'resume' : 'start');
Object.entries({'browse-project':'select_repo','open-run':'open_run',stop:'stop_after_task','start-audit':'start_audit','download-audit':'download_audit','download-logs':'download_logs'}).forEach(([id,method]) => { get(id).onclick = () => act(method); });
updateControls();
window.addEventListener('pywebviewready', () => { ready = true; refresh(); });
window.addEventListener('pagehide', () => { closed = true; });
window.addEventListener('pageshow', () => { closed = false; refresh(false); });
document.addEventListener('visibilitychange', () => { if (!document.hidden && !closed) { updateElapsed(); refresh(false); } });
setInterval(() => { if (!closed && !document.hidden) updateElapsed(); }, 1000);
setInterval(() => refresh(false), 4000);
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
            options["file_types"] = ("JSON task plans (*.json)",)
        result = self._window.create_file_dialog(dialog_type, **options)
        return result[0] if result and not isinstance(result, str) else result

    def confirm(self, message):
        return self._window.create_confirmation_dialog("Confirm local runner launch", message)

    def save(self, filename):
        enum = getattr(self._webview, 'FileDialog', None)
        kind = getattr(enum, 'SAVE') if enum is not None else self._webview.SAVE_DIALOG
        result = self._window.create_file_dialog(kind, save_filename=filename,
                                                file_types=('JSON report (*.json)',))
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
        self._dialogs = dialogs
        self._lock = threading.RLock()
        self._launch_lock = threading.Lock()
        self._root = Path(root).expanduser() if root is not None else None
        self._selection_error = None
        self._approved_evidence = {}
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

    def selected_paths(self):
        with self._lock:
            return self._controller.selected_paths()

    def job_status(self):
        with self._lock:
            try:
                status = dict(self._controller.job_status())
                if hasattr(self._controller, "audit_status"):
                    status["audit"] = self._controller.audit_status()
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
                eligible = status["start_ready"]
                audit = status.get("audit", {})
                status["audit_ready"] = eligible and audit.get("audit_ready") is True
                status["start_ready"] = eligible and audit.get("start_approved") is True
                unavailable = ("Run busy or child status unknown. Wait, then unchanged approval will be revalidated."
                    if status["active"] is not False or status.get("uncertain")
                    else " ".join(status["preflight"].get("errors", [])) or "Target must be clean and eligible." if not eligible else None)
                status["start_reason"] = unavailable or ("Verified APPROVE. Start requires native authorization."
                    if status["start_ready"] else audit.get("error") or "Start requires verified APPROVE evidence for the exact selected inputs.")
                status["audit_reason"] = unavailable or audit.get("error") or (
                    "This exact plan and target already consumed their one-shot audit. Inspect the saved result; revise the plan before another audit."
                    if not audit.get("audit_ready") else "Ready for one read-only audit. Configure an explicit model supporting High.")
                # The controller and CLI revalidate again on launch.
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
                approval = None
                if action == "start":
                    approval = self._controller.audit_status()
                    if not approval.get("start_approved"):
                        raise ValueError(approval.get("error") or "Start requires verified APPROVE audit evidence.")
                message = (f"{action.upper()} the local runner?\nRepository: {paths['repo']}"
                           f"\nBranch: {branch or 'Unknown (launch will be blocked)'}"
                           f"\nPlan: {paths['plan']}\nSelected logs: {paths['logs']}"
                           + (f"\nApproved audit: {approval['directory']}"
                              f"\nAudit identity: {json.dumps(approval.get('identity'), sort_keys=True)}"
                              if approval is not None else "\nResume uses this run's persisted authorization and recovery checks.")
                           + f"\nAttempts: {attempts}\nThe runner may modify and commit project files."
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
                if approval is not None:
                    current = self._controller.audit_status()
                    if (not current.get("start_approved")
                            or any(current.get(key) != approval.get(key) for key in ("identity", "directory"))):
                        raise ValueError("Approved audit changed during confirmation. Inspect the selected inputs and confirm again.")
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

    def start_audit(self, model="", goal=""):
        if not self._launch_lock.acquire(blocking=False):
            return {"ok": False, "error": "A launch confirmation is already in progress."}
        try:
            with self._lock:
                if self._dialogs is None:
                    raise ValueError("Audit launch requires native confirmation; dialogs are unavailable.")
                if not isinstance(model, str) or not model.strip():
                    raise ValueError("Configure an explicit audit model supporting High reasoning first.")
                paths = self._controller.selected_paths()
                message = (f"Start one read-only plan audit?\nRepository: {paths['repo']}\nPlan: {paths['plan']}"
                           f"\nAudit model: {model}\nReasoning: High\nGoal: {goal}"
                           "\nNo implementation will start. Logs and the one-shot ledger stay outside the target."
                           "\nClosing the window leaves the runner alive. Failed or uncertain calls cannot be repeated.")
                if self._dialogs.confirm(message) is not True:
                    return {"ok": False, "cancelled": True, "error": "Audit launch cancelled."}
                result = self._controller.start_audit(model=model, goal=goal)
                if self._controller.logs is not None:
                    self._root = self._controller.logs
                return result
        except Exception as exc:
            return self._failure(exc)
        finally:
            self._launch_lock.release()

    def stop_after_task(self, expected):
        with self._lock:
            try:
                return self._controller.stop_after_task(expected)
            except Exception as exc:
                return self._failure(exc)

    def resume(self, attempts=3):
        return self._launch("resume", attempts)

    def _audit_export_root(self):
        if self._root:
            for root in (self._root, self._root / 'audit'):
                if (root / 'audit-evidence.json').exists():
                    return root
            # CLI runs do not persist a selectable plan path. Their matching
            # state/manifest binding still identifies the historical audit.
            records = [export_file(self._root, name) for name in ('state.json', 'run-manifest.json')]
            if all(record['ok'] for record in records):
                state, manifest = [json.loads(record['text']) for record in records]
                binding = state.get('plan_audit')
                if binding and binding == manifest.get('plan_audit'):
                    from auto_coding import plan_audit
                    root = Path(binding['directory'])
                    saved = export_file(root, 'audit-evidence.json')
                    if saved['ok']:
                        evidence = json.loads(saved['text'])
                        if (plan_audit.digest(evidence) == binding.get('evidence_sha256')
                                and evidence['identity']['plan_sha256'] == state.get('plan_sha256')
                                and evidence['identity']['target']['repo'] == state.get('repo')):
                            return root  # export_audit independently verifies its ledger/report.
        audit = self._controller.audit_status()
        if audit.get('report') and audit.get('directory'):
            return Path(audit['directory'])
        raise ValueError('No verified audit report available. Open saved audit logs.')

    def _download(self, kind):
        with self._lock:
            try:
                if self._dialogs is None:
                    raise ValueError('Native Save As is unavailable.')
                root = self._audit_export_root() if kind == 'audit' else self._root
                if root is None:
                    raise ValueError('No run selected. Open run logs first.')
                identity = directory_identity(root)
                selected = self.selected_paths()
                build = lambda: (export_audit(root) if kind == 'audit' else
                    export_run(root, self._approved_evidence.get(identity, {})))
                initial = build()  # Validate before Save As; re-read after the dialog.
                destination = self._dialogs.save('audit-report.json' if kind == 'audit' else 'run-logs.json')
                if not destination:
                    return {'ok': False, 'cancelled': True, 'message': 'Download cancelled. No export written.'}
                if not isinstance(destination, str) or not Path(destination).is_absolute():
                    raise ValueError('Save As must select an absolute filename.')
                if self.selected_paths() != selected or directory_identity(root) != identity:
                    raise ValueError('Selected directory changed during Save As. Reopen and retry.')
                destination = Path(destination)
                resolved = destination.resolve(strict=False)
                if destination.parent.resolve(strict=True) != destination.parent.absolute():
                    raise ValueError('Save As directory must not contain links.')
                forbidden = [Path(identity[0])]
                if self._root:
                    forbidden.append(self._root.resolve())
                if selected.get('repo'):
                    forbidden.append(Path(selected['repo']).resolve())
                if kind == 'audit':
                    forbidden.append(Path(initial['identity']['target']['repo']).resolve())
                if any(resolved == path or resolved.is_relative_to(path) for path in forbidden):
                    raise ValueError('Save exports outside the target checkout and original log directories.')
                payload = build()
                # Redact values before encoding, so exported JSON stays valid.
                def clean(value):
                    if isinstance(value, dict):
                        return {redact_export(str(k)): ('[redacted]' if any(s in str(k).casefold()
                            for s in ('password', 'secret', 'credential', 'api_key', 'authorization')) else clean(v))
                            for k, v in value.items()}
                    if isinstance(value, list):
                        return [clean(v) for v in value]
                    return redact_export(value) if isinstance(value, str) else value
                content = json.dumps(clean(payload), ensure_ascii=False, indent=2)
                if len(content.encode('utf-8')) > 12 * 1024 * 1024:
                    raise ValueError('Export exceeds the 12 MB total limit; select fewer evidence files.')
                # Exclusive creation prevents silent overwrite, including symlinks.
                with destination.open('x', encoding='utf-8') as stream:
                    stream.write(content)
                return {'ok': True, 'message': f'Download saved: {destination}'}
            except FileExistsError:
                return {'ok': False, 'error': 'Destination exists. Choose a new filename; no file was overwritten.'}
            except Exception as exc:
                return self._failure(exc)

    def download_audit(self):
        return self._download('audit')

    def download_logs(self):
        return self._download('run')

    def snapshot(self):
        with self._lock:
            status = self.job_status()
            try:
                data = read_run(self._root) if self._root else {"root":"", "exists":False, "state":None, "state_error":"Ready. Select a project and task plan, or open an existing run for inspection.", "events":[], "event_errors":[], "empty":True}
            except Exception as exc:
                data = {"root": str(self._root), "exists": False, "state": None,
                        "state_error": str(exc), "events": [], "event_errors": [], "empty": True}
            try:
                export_audit(self._audit_export_root(), diagnostics=False)
                status['audit_export_available'] = True
            except Exception:
                status['audit_export_available'] = False
            data["controller"] = {**self.selected_paths(), **status}
            return data

    def evidence(self, name):
        with self._lock:
            if not isinstance(name, str):
                return {"ok": False, "text": "Evidence name must be a relative path string."}
            try:
                result = read_evidence(self._root, name) if self._root else {"ok":False, "text":"No log directory selected."}
                if result['ok'] and export_file(self._root, name)['ok']:
                    self._approved_evidence.setdefault(directory_identity(self._root), {})[name] = hashlib.sha256(
                        result['text'].encode('utf-8')).hexdigest()
                return result
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
        window = webview.create_window("Auto-Coding - Run Dashboard", html=HTML, js_api=api, width=1280, height=800, min_size=(360, 400))
        api._dialogs = NativeDialogs(webview, window)
        webview.start()
        return 0
    except Exception as exc:
        print(f"Could not start the desktop window. On Windows install Microsoft Edge WebView2 Runtime and pywebview: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
