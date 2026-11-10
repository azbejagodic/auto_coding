// Runs the actual embedded dashboard JavaScript against a deliberately tiny DOM.
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
class Element {
  constructor(tag) { this.tag = tag; this.children = []; this._text = ''; this.disabled = false; }
  set textContent(value) { this._text = String(value); this.children = []; }
  get textContent() { return this._text + this.children.map(child => child.textContent).join(''); }
  set innerHTML(value) { throw Error('Untrusted HTML insertion'); }
  append(...items) { this.children.push(...items); }
  replaceChildren(...items) { this.children = items; this._text = ''; }
  setAttribute(name, value) { this[name] = value; }
  focus() { document.activeElement = this; }
  setSelectionRange(start, end) { this.selectionStart = start; this.selectionEnd = end; }
  addEventListener() {}
  showModal() { this.open = true; this.focus(); }
  close() { this.open = false; }
  contains(target) { return this === target || this.children.some(child => child.contains?.(target)); }
}
const nodes = new Map();
const get = id => { if (!nodes.has(id)) nodes.set(id, new Element(id)); return nodes.get(id); };
get('attempts').value = '3';
get('audit-model').value = '';
get('audit-goal').value = '';
get('audit-result').textContent = 'Not audited';
get('audit-result').className = 'pill muted';
const document = {getElementById: get, createElement: tag => new Element(tag),
  createTextNode: text => ({textContent: String(text)})};
let data = {root:'', exists:false, empty:true, state:null, events:[], event_errors:[],
  controller:{active:false, uncertain:false, repo:null, plan:null, logs:null, resume_ready:false, start_ready:false,
    preflight:{branch:'feature/ui', clean:true, errors:[]}}};
const calls = [];
let launchResult = {ok:false, cancelled:true};
let pendingSelection = null;
let failStatus = false;
let evidenceReads = 0;
let snapshotReads = 0;
let pendingSnapshot = null;
const api = {
  github_repositories: async () => ({ok:true, available:false, repositories:[], message:'Enter OWNER/REPO manually; Git authentication remains available.'}),
  connect_github: async repository => { calls.push(['connect_github', repository]);
    if (repository === 'bad/repo') return {ok:false, error:'GitHub authentication failed <img onerror=bad()>; authenticate Git and retry.'};
    Object.assign(data.controller, {repository:'owner/repo', repo:'managed/checkout', github:{local:['main', 'feature/existing'], remote:['origin/main', 'origin/master', 'origin/feature/remote']}, push:{eligible:false, reason:'No approved checkpoints'}});
    data.controller.preflight = {branch:'main', clean:true, errors:['Runs are blocked on main/master.']}; return {ok:true}; },
  checkout_github_branch: async (repository, name) => { calls.push(['checkout_github_branch', repository, name]); data.controller.preflight = {branch:name, clean:true, errors:[]}; return {ok:true}; },
  create_github_branch: async (repository, name, base, confirmed) => { calls.push(['create_github_branch', repository, name, base, confirmed]); data.controller.github.local.push(name); data.controller.preflight.branch = name; return {ok:true}; },
  fetch_github: async repository => { calls.push(['fetch_github', repository]); return {ok:true}; },
  push_approved_commits: async () => { calls.push(['push_approved_commits']); return {ok:false, error:'Remote branch changed. Fetch and review; no force push.'}; },
  snapshot: async () => { snapshotReads++; if (pendingSnapshot) await pendingSnapshot; if (failStatus) throw Error('status unavailable'); return structuredClone(data); },
  select_repo: async (...args) => { calls.push(['select_repo', ...args]); if (pendingSelection) await pendingSelection;
    data.controller.repo = 'C:\\project with spaces'; return {ok:true}; },
  select_plan: async (...args) => { calls.push(['select_plan', ...args]); data.controller.plan = 'C:\\plans\\task.json'; data.controller.start_ready = true; return {ok:true}; },
  open_run: async (...args) => { calls.push(['open_run', ...args]); return {ok:true, cancelled:true}; },
  start: async (...args) => { calls.push(['start', ...args]); if (launchResult.ok) { data.controller.active = true; data.controller.logs = data.root = 'C:\\runs\\one'; data.exists = true; data.empty = false; } return launchResult; },
  start_audit: async (...args) => { calls.push(['start_audit', ...args]); return launchResult; },
  stop_after_task: async (...args) => { calls.push(['stop_after_task', ...args]); data.controller.stop_requested = true; return {ok:true}; },
  resume: async (...args) => { calls.push(['resume', ...args]); data.controller.active = true; data.controller.resume_ready = false; return {ok:true}; },
  evidence: async name => { evidenceReads++; return {ok:true, text:'<img src=x onerror=bad()> original evidence: ' + name}; }
};
const listeners = {};
let poll;
let tick;
let now = Date.parse('2026-10-25T12:00:00Z');
const documentListeners = {};
document.addEventListener = (name, callback) => { documentListeners[name] = callback; };
const context = vm.createContext({document, window:{pywebview:{api}, addEventListener:(name, callback) => { listeners[name] = callback; }},
  Date: class extends Date { static now() { return now; } },
  setInterval:(callback, ms) => { if (ms === 1000) tick = callback; else { assert.equal(ms, 4000); poll = callback; } }});
vm.runInContext(fs.readFileSync(process.argv[2], 'utf8'), context);
const flush = async () => { await new Promise(resolve => setImmediate(resolve)); };
const click = async id => { await get(id).onclick(); await flush(); };
(async () => {
  assert(get('start').disabled);
  await listeners.pywebviewready(); await flush();
  assert.equal(get('run-status').textContent, 'Idle');
  assert.equal(get('plan-path').textContent, 'Selected JSON file');
  assert(get('start').disabled && get('download-audit').disabled && get('download-logs').disabled);
  assert.equal(get('approval-indicator').className, 'indicator');
  data.controller.preflight.errors = ['Select a Git repository root.'];
  await poll();
  assert.equal(get('progress-error').textContent, '');
  assert(get('configure-error').hidden, 'Fresh launch has only the reference controls');
  data.controller.preflight.errors = [];
  // Native Browse and contextual settings; no launch on selection.
  await click('browse-plan');
  assert.equal(calls.at(-1)[0], 'select_plan');
  assert.equal(get('configuration').open, true);
  assert.equal(get('plan-path').textContent, 'C:\\plans\\task.json');
  assert(get('start').disabled);
  await click('browse-project');
  assert.equal(calls.at(-1)[0], 'select_repo');
  assert.equal(get('project-path').textContent, 'C:\\project with spaces');
  await click('open-run'); assert.equal(calls.at(-1)[0], 'open_run');
  assert(get('message').textContent.includes('Cancelled'));
  await click('configuration-done'); assert.equal(get('configuration').open, false);

  // Keep existing managed checkout and branch operations available contextually.
  await click('list-github'); assert(get('github-repositories').hidden);
  get('github-repo').value = 'bad/repo'; await click('connect-github');
  assert(get('message').textContent.includes('<img onerror=bad()>'));
  get('github-repo').value = 'owner/repo'; await click('connect-github');
  get('github-branch').value = 'feature/remote'; await click('select-branch');
  assert.deepEqual(calls.at(-1), ['checkout_github_branch', 'owner/repo', 'feature/remote']);
  await click('new-branch'); get('branch-name').value = 'feature/test';
  get('branch-base').value = 'master'; get('branch-base').onchange();
  assert(get('create-branch').disabled);
  get('branch-base-confirm').checked = true; get('branch-base-confirm').onchange();
  await click('create-branch');
  assert.deepEqual(calls.at(-1), ['create_github_branch', 'owner/repo', 'feature/test', 'master', true]);
  await click('new-branch'); assert(get('create-branch').disabled, 'Alternate base consent resets for each creation');
  const beforeCancel = calls.length; await click('cancel-branch');
  assert.equal(calls.length, beforeCancel); assert(get('branch-form').hidden);
  data.controller.github.remote = ['origin/master'];
  data.controller.github.local = ['master']; data.controller.preflight.branch = 'master';
  await poll(); await click('new-branch');
  assert.equal(get('branch-base').value, 'master');
  assert(!get('branch-base-confirm-label').hidden && get('create-branch').disabled);
  get('branch-base-confirm').checked = true; get('branch-base-confirm').onchange();
  get('branch-base').onchange(); assert(get('create-branch').disabled, 'Changing base clears consent');
  await click('cancel-branch');
  data.controller.preflight.clean = false; await poll();
  assert(get('new-branch').disabled && get('select-branch').disabled && get('push-approved').disabled);
  data.controller.preflight.clean = true; data.controller.active = true; await poll();
  assert(get('connect-github').disabled && get('select-branch').disabled);
  data.controller.active = false; data.controller.push = {eligible:true, reason:'Approved checkpoints'};
  await poll(); await click('push-approved');
  assert.deepEqual(calls.at(-1), ['push_approved_commits']);
  assert(get('message').textContent.includes('Remote branch changed'));
  let releaseSelection;
  pendingSelection = new Promise(resolve => { releaseSelection = resolve; });
  const selecting = click('browse-project'); await flush();
  const beforeSelectionRepeat = calls.length;
  await click('browse-project'); await click('start'); await poll();
  assert.equal(calls.length, beforeSelectionRepeat, 'Repeated selections and launch stay blocked during native selection');
  releaseSelection(); await selecting; pendingSelection = null;
  for (const error of ['Runs are blocked on main/master. Switch to a feature branch first.',
    'Plan validation failed: malformed plan', 'Commit or discard changes before START.']) {
    data.controller.start_ready = false; data.controller.preflight.errors = [error];
    await poll(); assert(get('start').disabled); assert(get('progress-error').textContent.includes(error));
  }
  data.controller.preflight.errors = [];

  data.controller.audit_ready = true;
  await poll(); await click('start-audit');
  assert.equal(get('configuration').open, true, 'Missing model opens configuration');
  assert(!calls.some(call => call[0] === 'start_audit'));
  get('audit-model').value = 'explicit-auditor'; get('audit-model').oninput();
  get('audit-goal').value = 'Exact goal';
  await click('configuration-done');
  let releaseAudit;
  api.start_audit = async (...args) => { calls.push(['start_audit', ...args]); await new Promise(resolve => { releaseAudit = resolve; }); return {ok:true}; };
  const auditing = click('start-audit'); await flush();
  const auditCalls = calls.length;
  await click('start-audit'); assert.equal(calls.length, auditCalls, 'No concurrent audit');
  assert(get('start').disabled && get('browse-plan').disabled);
  releaseAudit(); await auditing;
  assert.deepEqual(calls.at(-1), ['start_audit', 'explicit-auditor', 'Exact goal']);
  // A completed bridge action must stay disabled until its resulting snapshot arrives.
  let releaseAfterAction;
  pendingSnapshot = new Promise(resolve => { releaseAfterAction = resolve; });
  api.start_audit = async (...args) => { calls.push(['start_audit', ...args]); return {ok:true}; };
  const settlingAudit = click('start-audit'); await flush();
  const beforeDuplicate = calls.length;
  assert(get('start-audit').disabled);
  await click('start-audit'); assert.equal(calls.length, beforeDuplicate);
  releaseAfterAction(); await settlingAudit; pendingSnapshot = null;

  const approved = {status:'APPROVE', verified:true, start_approved:true, identity:{plan_sha256:'exact'}};
  // No UI-local approval, alternate field names, or partial backend flags unlock Start.
  for (const audit of [undefined, {result:'Approved', verified:true}, {status:'APPROVE'},
    {status:'APPROVE', verified:true}, {status:'APPROVE', start_approved:true},
    {status:'APPROVE', verified:false, start_approved:true}]) {
    data.controller.audit = audit; data.controller.start_ready = true;
    await poll(); assert(get('start').disabled);
    assert.equal(get('approval-indicator').className, 'indicator');
    const before = calls.length; await click('start'); assert.equal(calls.length, before);
  }
  for (const status of ['Idle','Running','REVISE','REJECT','BLOCK','Infrastructure error','Unknown']) {
    data.controller.audit = {status, verified:false, error:'Reason <img onerror=bad()>'};
    data.controller.start_ready = false; data.controller.audit_ready = status !== 'Running';
    if (status === 'Running') data.controller.active = true;
    await poll(); assert(get('start').disabled);
    assert.notEqual(get('audit-result').className, 'pill green');
    assert(get('audit-error').textContent.includes('<img onerror=bad()>'));
    if (status === 'Running') assert(get('start-audit').disabled);
    data.controller.active = false;
  }
  data.controller.audit = approved; data.controller.start_ready = false;
  await poll(); assert(get('start').disabled);
  assert.equal(get('audit-result').textContent, '\u2713 Approved');
  assert.equal(get('approval-indicator').className, 'indicator');
  data.controller.start_ready = true;
  await poll(); assert(!get('start').disabled);
  assert.equal(get('approval-indicator').className, 'indicator green');
  // Wrong file types and controller plan errors fail closed even with stale approval flags.
  data.controller.plan = 'C:\\plans\\wrong.md';
  await poll(); assert(get('start').disabled && get('start-audit').disabled);
  assert(get('progress-error').textContent.includes('Select a JSON plan'));
  data.controller.plan = 'C:\\plans\\invalid.json';
  data.controller.validation_errors = {plan:'Invalid JSON <script>bad()</script>'};
  data.controller.start_ready = data.controller.audit_ready = false;
  await poll(); assert(get('start').disabled && get('start-audit').disabled);
  assert(get('progress-error').textContent.includes('<script>bad()</script>'));
  delete data.controller.validation_errors;
  data.controller.plan = 'C:\\plans\\task.json';
  data.controller.start_ready = data.controller.audit_ready = true;
  await poll();
  const attempts = get('attempts');
  for (const value of ['0','11','2.5','','-1','NaN','3.0','Infinity']) {
    attempts.value = value; attempts.oninput();
    assert.equal(attempts['aria-invalid'], 'true'); assert(get('start').disabled);
    const before = calls.length; await click('start'); assert.equal(calls.length, before);
  }
  for (const value of ['1','3','5','10']) {
    attempts.value = value; attempts.oninput();
    assert.equal(attempts['aria-invalid'], 'false'); assert(!get('start').disabled);
    assert.equal(get('attempts-error').textContent, '');
  }
  attempts.value = '3'; attempts.oninput();
  attempts.focus(); await poll();
  assert.equal(document.activeElement, attempts, 'Polling preserves keyboard focus');
  assert.equal(attempts.value, '3');
  await click('start'); assert.deepEqual(calls.at(-1), ['start', 3]);
  assert(get('message').textContent.includes('Cancelled'));
  failStatus = true; await poll();
  assert(get('start').disabled); assert.notEqual(get('audit-result').className, 'pill green');
  assert.equal(get('approval-indicator').className, 'indicator');
  assert(get('message').textContent.includes('status unavailable'));
  failStatus = false; await poll();

  // Exports only use the bridge; report availability never implies authorization.
  api.download_audit = async () => { calls.push(['download_audit']); return {ok:true, message:'Download saved: audit-report.json'}; };
  api.download_logs = async () => { calls.push(['download_logs']); return {ok:false, error:'Save exports outside the target checkout'}; };
  data.controller.audit_export_available = true;
  data.exists = true; data.root = data.controller.logs = 'C:\\runs\\one';
  data.files = [{path:'review.json', status:'available'}, {path:'gone.txt', status:'missing'}];
  await poll();
  assert(get('include-evidence').disabled);
  assert.equal(get('export-evidence').children.length, 2, 'Only available evidence is offered');
  get('export-evidence').value = 'review.json'; get('export-evidence').onchange();
  get('auto-push').checked = true;
  await click('include-evidence');
  assert.equal(evidenceReads, 1);
  assert.equal(get('auto-push').checked, true, 'Evidence selection preserves explicit publication preference');
  get('auto-push').checked = false;
  assert(get('message').textContent.includes('included in Download Logs'));
  assert(!get('message').textContent.includes('<img'), 'Raw evidence is exported, never rendered');
  data.files.push({path:'another.json', status:'available'});
  await poll(); assert(get('include-evidence').disabled, 'Changed evidence list resets selection');
  api.evidence = async () => ({ok:false, text:'Evidence changed or missing.'});
  get('export-evidence').value = 'another.json'; get('export-evidence').onchange();
  await click('include-evidence'); assert(get('message').textContent.includes('Evidence changed or missing'));
  await poll(); await click('download-audit');
  assert.deepEqual(calls.at(-1), ['download_audit']);
  assert(get('message').textContent.includes('audit-report.json'));
  await click('download-logs'); assert.deepEqual(calls.at(-1), ['download_logs']);
  assert(get('message').textContent.includes('outside the target'));

  // Saved recovery reuses Start's position and only the controller's strict readiness.
  data.state = {status:'stopped', repo:'untrusted', plan_sha256:'wrong'};
  data.controller.start_ready = false; data.controller.resume_ready = false;
  await poll(); assert(get('start').disabled); assert.equal(get('start-label').textContent, 'Start');
  data.controller.resume_ready = true;
  await poll(); assert.equal(get('start-label').textContent, 'Resume');
  attempts.value = '5'; attempts.oninput();
  await click('start'); assert.deepEqual(calls.at(-1), ['resume', 5]);
  assert.equal(get('run-status').textContent, 'Running');
  assert(get('auto-push').disabled);
  const beforeActiveBrowse = calls.length;
  await click('browse-plan');
  assert.equal(get('configuration').open, true);
  assert.equal(calls.length, beforeActiveBrowse, 'Active-run Browse opens evidence settings without changing selections');
  assert(get('browse-project').disabled && get('open-run').disabled);
  await click('configuration-done');
  delete data.controller.stop_identity; await poll(); assert(get('stop').disabled);
  data.controller.stop_identity = {run_id:'cli-run', generation:0};
  data.controller.stop_requested = false;
  await poll(); assert(!get('stop').disabled);
  await click('stop');
  assert.equal(calls.at(-1)[0], 'stop_after_task');
  assert.deepEqual(calls.at(-1)[1], data.controller.stop_identity);
  assert(get('stop').disabled); assert(get('stop').title.includes('Stop after current task'));
  assert(get('message').textContent.includes('Stop requested'));
  data.state = {status:'running', current_task:'third', completed:['first','second']};
  data.task_count = 8;
  data.summary = {start_utc:'2026-10-25T10:00:00Z'};
  await poll(); assert.equal(get('run-task').textContent, '3 / 8');
  assert.equal(get('run-time').textContent, '02:00:00');
  const beforeTick = snapshotReads; now += 1000; tick();
  assert.equal(get('run-time').textContent, '02:00:01'); assert.equal(snapshotReads, beforeTick);
  delete data.summary;
  for (const [current, completed, total, expected] of [
    ['first', [], 4, '1 / 4'], ['second', ['first','second'], 4, '2 / 4'],
    [null, [], 4, '-- / 4'], ['x', undefined, 4, '-- / 4'],
    ['x', ['dup','dup'], 4, '-- / 4'], ['x', [null], 4, '-- / 4'],
    ['overflow', ['first','second'], 2, '-- / 2']]) {
    data.state.current_task = current; data.state.completed = completed; data.task_count = total;
    await poll(); assert.equal(get('run-task').textContent, expected);
  }
  delete data.task_count; data.controller.task_count = 4;
  data.state.current_task = 'third'; data.state.completed = ['first','second'];
  await poll(); assert.equal(get('run-task').textContent, '3 / 4', 'Controller task-count fallback');
  delete data.controller.task_count;
  data.state.status = 'stopping'; await poll(); assert.equal(get('run-status').textContent, 'Stopping');
  data.controller.active = false; data.controller.resume_ready = false;
  data.state.status = 'completed';
  await poll(); assert.notEqual(get('run-status').textContent, 'Completed');
  data.summary = {completion_confirmed:true, verification_status:'passed', current:2, duration_seconds:3661};
  await poll(); assert.equal(get('run-status').textContent, 'Completed');
  data.controller.error = 'Previous child exited with code 1';
  await poll(); assert.equal(get('run-status').textContent, 'Completed');
  assert(!get('progress-error').textContent.includes('Previous child'));
  assert.equal(get('run-time').textContent, '01:01:01');
  now += 1000; tick(); assert.equal(get('run-time').textContent, '01:01:01');
  data.summary.completion_confirmed = false; data.summary.verification_status = 'failed';
  data.summary.error = 'Final check failed <script>bad()</script>';
  await poll(); assert.equal(get('run-status').textContent, 'Failed');
  assert(get('progress-error').textContent.includes('<script>bad()</script>'));
  data.controller.active = null; data.controller.uncertain = true;
  await poll(); assert.equal(get('run-status').textContent, 'Unknown child status');
  assert(get('start').disabled);
  data.controller.active = false; data.controller.uncertain = false;
  data.state.status = 'stopped'; await poll();
  assert.equal(get('run-status').textContent, 'Failed', 'Final verification failure takes precedence over stopped state');
  delete data.summary;
  data.state.status = 'checkpointing'; await poll();
  assert.equal(get('run-status').textContent, 'Awaiting recovery'); assert(get('start').disabled);
  data.state.status = 'stopped';
  await poll(); assert.equal(get('run-status').textContent, 'Stopped');
  data.state = null; data.state_error = 'state.json is corrupt or unreadable';
  await poll(); assert(get('progress-error').textContent.includes('state.json is corrupt'));
  data.exists = false; await poll(); assert(get('download-logs').disabled);
  assert(get('progress-error').textContent.includes('missing or unavailable'));
  // Coalesce reads; background/closed windows do not poll or tick.
  let releaseSnapshot;
  pendingSnapshot = new Promise(resolve => { releaseSnapshot = resolve; });
  const beforeSlow = snapshotReads; const slow = poll(), overlapping = poll();
  assert.equal(snapshotReads, beforeSlow + 1);
  releaseSnapshot(); await slow; await overlapping; pendingSnapshot = null;
  document.hidden = true; const beforeHidden = snapshotReads;
  await poll(); tick(); assert.equal(snapshotReads, beforeHidden);
  document.hidden = false; listeners.pagehide(); await poll(); tick();
  assert.equal(snapshotReads, beforeHidden);
  listeners.pageshow(); await flush(); assert(snapshotReads > beforeHidden);
  console.log('Dashboard selection, audit, authorization, recovery, export and state contracts passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
