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
  snapshot: async () => { snapshotReads++; if (failStatus) throw Error('status unavailable'); return structuredClone(data); },
  select_repo: async (...args) => { calls.push(['select_repo', ...args]); if (pendingSelection) await pendingSelection;
    data.controller.repo = 'C:\\project with spaces'; return {ok:true}; },
  select_plan: async (...args) => { calls.push(['select_plan', ...args]); data.controller.plan = 'C:\\plans\\task.json'; data.controller.start_ready = true; return {ok:true}; },
  open_run: async (...args) => { calls.push(['open_run', ...args]); return {ok:true, cancelled:true}; },
  start: async (...args) => { calls.push(['start', ...args]); if (launchResult.ok) { data.controller.active = true; data.controller.logs = data.root = 'C:\\runs\\one'; data.exists = true; data.empty = false; } return launchResult; },
  start_audit: async (...args) => { calls.push(['start_audit', ...args]); return launchResult; },
  resume: async (...args) => { calls.push(['resume', ...args]); data.controller.active = true; data.controller.resume_ready = false; return {ok:true}; },
  evidence: async name => { evidenceReads++; return {ok:true, text:'<img src=x onerror=bad()> original evidence: ' + name}; }
};
const listeners = {};
let poll;
const context = vm.createContext({document, window:{pywebview:{api}, addEventListener:(name, callback) => { listeners[name] = callback; }},
  setInterval:(callback, ms) => { assert.equal(ms, 4000); poll = callback; }});
vm.runInContext(fs.readFileSync(process.argv[2], 'utf8'), context);
const flush = async () => { await new Promise(resolve => setImmediate(resolve)); };
const click = async id => { await get(id).onclick(); await flush(); };
const walk = node => [node, ...(node.children || []).flatMap(walk)];
const contents = () => get('app').textContent;
(async () => {
  assert(get('start').disabled);
  await listeners.pywebviewready(); await flush();
  assert(!get('browse-project').disabled);
  assert(get('start').disabled);
  assert.equal(get('header-state').textContent, 'Idle');
  assert(contents().includes('Select a plan and repository, then audit and authorize a new run.'));
  assert(!contents().includes('Ready for your first run'));
  assert.equal(get('run-setup').hidden, false);
  const initial = structuredClone(data);
  await click('list-github');
  assert(get('message').textContent.includes('Enter OWNER/REPO manually'));
  assert(get('github-repositories').hidden);
  get('github-repo').value = 'bad/repo'; await click('connect-github');
  assert(get('message').textContent.includes('<img onerror=bad()>'));
  get('github-repo').value = 'https:' + '//github.com/Owner/Repo.git';
  await click('connect-github');
  assert.deepEqual(calls.at(-1), ['connect_github', 'https:' + '//github.com/Owner/Repo.git']);
  assert(get('selected-project').textContent.includes('owner/repo'));
  assert(get('github-state').textContent.includes('main | Clean'));
  assert(get('start').disabled && get('push-approved').disabled);
  get('github-branch').value = 'feature/remote'; await click('select-branch');
  assert.deepEqual(calls.at(-1), ['checkout_github_branch', 'owner/repo', 'feature/remote']);
  await click('new-branch'); get('branch-name').value = 'feature/cancelled';
  const beforeCancel = calls.length; await click('cancel-branch');
  assert.equal(calls.length, beforeCancel);
  assert(get('branch-form').hidden);
  await click('new-branch'); get('branch-name').value = 'feature/new'; await click('create-branch');
  assert.deepEqual(calls.at(-1), ['create_github_branch', 'owner/repo', 'feature/new', 'main', false]);
  assert(get('branch-form').hidden);
  // A master-only remote still has a dashboard route to a feature branch.
  data.controller.github.remote = ['origin/master'];
  data.controller.github.local = ['master'];
  data.controller.preflight.branch = 'master'; await poll();
  await click('new-branch'); get('branch-name').value = 'feature/from-master';
  assert.equal(get('branch-base').value, 'master');
  assert(!get('branch-base-confirm-label').hidden);
  assert(get('create-branch').disabled);
  const beforeUnconfirmed = calls.length; await click('create-branch');
  assert.equal(calls.length, beforeUnconfirmed);
  get('branch-base-confirm').checked = true; get('branch-base-confirm').onchange();
  assert(!get('create-branch').disabled);
  get('branch-base').onchange();
  assert(get('create-branch').disabled, 'Changing base resets explicit confirmation');
  get('branch-base-confirm').checked = true; get('branch-base-confirm').onchange();
  await click('create-branch');
  assert.deepEqual(calls.at(-1), ['create_github_branch', 'owner/repo', 'feature/from-master', 'master', true]);
  await click('new-branch');
  assert(get('create-branch').disabled, 'Every alternate-base creation needs confirmation');
  await click('cancel-branch');
  assert(!calls.some(call => ['start', 'resume'].includes(call[0])));
  data.controller.preflight.clean = false; await poll();
  assert(get('new-branch').disabled && get('select-branch').disabled && get('push-approved').disabled);
  data.controller.preflight.clean = true; data.controller.active = true; await poll();
  assert(get('connect-github').disabled && get('select-branch').disabled);
  data.controller.active = false; data.controller.push = {eligible:true, reason:'Approved checkpoints'}; await poll();
  await click('push-approved');
  assert.deepEqual(calls.at(-1), ['push_approved_commits']);
  assert(get('message').textContent.includes('Remote branch changed'));
  data = initial; calls.length = 0; await poll();
  await click('nav-tasks');
  assert(get('run-setup').hidden);
  assert(contents().includes('No task history yet.'));
  assert.equal(get('nav-tasks')['aria-current'], 'page');
  await click('nav-overview');
  let release;
  pendingSelection = new Promise(resolve => { release = resolve; });
  const choosing = get('browse-project').onclick();
  await flush();
  await click('browse-project'); await click('start'); await poll();
  assert.equal(calls.length, 1, 'Repeated selection or launch while selecting');
  release(); await choosing; pendingSelection = null;
  assert(get('project-path').textContent.includes('C:\\project with spaces'));
  await click('browse-plan');
  assert(get('start').disabled, 'Plan selection is not audit authorization');
  for (const error of ['Runs are blocked on main/master. Switch to a feature branch first.', 'Plan validation failed: malformed plan', 'Commit or discard changes before START.']) {
    data.controller.start_ready = false; data.controller.preflight.errors = [error];
    await poll();
    assert(get('start').disabled);
    assert(get('preflight').textContent.includes(error));
  }
  data.controller.start_ready = false; data.controller.audit_ready = true; data.controller.preflight.errors = [];
  await poll();
  assert(get('start-audit').disabled, 'Audit requires an explicit model');
  get('audit-model').value = 'audit-model'; get('audit-model').oninput();
  get('audit-goal').value = 'Complete the goal';
  assert.equal(get('start-audit').disabled, false);
  await click('start-audit');
  assert.deepEqual(calls.at(-1), ['start_audit', 'audit-model', 'Complete the goal']);
  assert(get('message').textContent.includes('Cancelled'));
  data.controller.audit = {status:'REVISE', identity:{plan_sha256:'<img onerror=bad()>'}, report:{decision:'REVISE'}};
  await poll();
  assert.equal(get('audit-result').textContent, 'REVISE');
  assert(get('audit-identity').textContent.includes('<img onerror=bad()>'));
  delete data.controller.audit;
  const attempts = get('attempts');
  data.controller.resume_ready = true;
  await poll(); // Sync simulated status before asserting Resume is enabled.
  for (const value of ['0', '11', '2.5', '', '-1', 'NaN', '3.0', 'Infinity']) {
    attempts.value = value; attempts.oninput();
    assert.equal(attempts['aria-invalid'], 'true', value);
    assert(get('start').disabled && get('resume').disabled, value);
    assert(get('attempts-error').textContent.includes('whole number from 1 to 10'));
    const before = calls.length;
    await click('start'); await click('resume');
    assert.equal(calls.length, before, 'Invalid entry must not reach native confirmation');
  }
  for (const value of ['1', '3', '5', '10']) {
    attempts.value = value; attempts.oninput();
    assert.equal(attempts['aria-invalid'], 'false');
    assert(get('start').disabled && !get('resume').disabled);
    assert.equal(get('attempts-error').textContent, '');
    attempts.focus();
    data.controller.preflight.branch = 'feature/number-' + value;
    await poll();
    assert.equal(document.activeElement, attempts, 'Polling preserves keyboard focus on number input');
    assert.equal(attempts.value, value);
    const beforeStart = calls.length;
    await click('start');
    assert.equal(calls.length, beforeStart, 'Disabled Start cannot reach the bridge');
    assert.equal(data.controller.active, false, 'Cancelled confirmation must not launch');
  }
  calls.length = 0;
  data.controller.resume_ready = false;
  attempts.value = '3'; attempts.oninput();
  await click('open-run');
  assert(get('message').textContent.includes('Cancelled'));
  assert(get('plan-path').textContent.includes('C:\\plans\\task.json'));
  const beforeStart = calls.length;
  // Untrusted, unverified status fields must never manufacture approval.
  for (const result of ['Not audited', 'Running', 'Approved', 'Rejected', 'Blocked']) {
    data.controller.audit = {result, verified:true, authorized:true};
    data.controller.start_ready = true;
    await poll();
    assert(get('start').disabled);
    assert.equal(get('audit-result').className, 'pill muted');
    assert.equal(get('audit-result').textContent, 'Idle');
    await click('start');
    assert.equal(calls.length, beforeStart);
  }
  // Only the verified backend verdict can open the Start gate. Busy remains separate.
  data.controller.audit = {status:'APPROVE', verified:true, start_approved:true, identity:{plan_sha256:'exact'}};
  data.controller.start_ready = false;
  data.controller.start_reason = 'Run busy. Wait for the earlier run.';
  await poll();
  assert.equal(get('audit-result').className, 'pill green');
  assert(get('start').disabled);
  assert(get('start-reason').textContent.includes('Run busy'));
  data.controller.start_ready = true;
  await poll();
  assert(!get('start').disabled, 'Unchanged approval becomes usable when busy run ends');
  await click('start');
  assert.deepEqual(calls.at(-1), ['start', 3]);
  for (const status of ['Unknown', 'REVISE', 'BLOCK', 'Infrastructure error', 'Running', 'Idle', 'APPROVE']) {
    data.controller.audit = {status, verified:false, start_approved:false, error:'Revise exact bytes <img onerror=bad()>'};
    data.controller.start_ready = false;
    await poll();
    assert(get('start').disabled);
    assert.notEqual(get('audit-result').className, 'pill green');
    assert(get('audit-identity').textContent.includes('<img onerror=bad()>'));
  }
  // A failed poll must also clear the previously green result immediately.
  data.controller.audit = {status:'APPROVE', verified:true, start_approved:true};
  data.controller.start_ready = true;
  await poll(); failStatus = true; await poll();
  assert(get('start').disabled);
  assert.notEqual(get('audit-result').className, 'pill green');
  failStatus = false;
  data.controller.audit = {status:'Idle'};
  data.controller.start_ready = false;
  calls.length = 0;
  // Existing recovery still uses the same controller bridge.
  data.controller.logs = data.root = 'C:\\runs\\one';
  data.exists = true; data.empty = false;
  data.controller.resume_ready = true;
  await poll();
  await click('resume');
  assert.deepEqual(calls.at(-1), ['resume', 3]);
  assert(!calls.some(call => call[0] === 'start'));
  assert(get('auto-push').disabled, 'Publication settings cannot change mid-run');
  assert.equal(get('run-status').textContent, 'Running');
  assert(get('browse-project').disabled && get('resume').disabled);
  data.state = {status:'running', current_task:'task-1', completed:[], history:[
    {task:'task-1', attempt:1, tests:[{index:1, exit_code:1, timed_out:false}],
      review:{decision:'REJECTED', issues:['real blocker'], summary:'Original summary'}, feedback:'Original feedback <script>bad()</script>', commit:'abc123'}]};
  data.events = [{seq:1, timestamp_utc:'2026-10-25T11:00:00Z', actor:'Codex B', event:'review_finished', outcome:'rejected', task_id:'task-1', attempt:1,
    _evidence_status:[{label:'verdict', path:'review.json', status:'available'}, {label:'test', path:'gone.txt', status:'missing'}],
    metadata:{reason:'<script>bad()</script>'}}];
  data.event_errors = ['Line 2: corrupt event'];
  await poll();
  assert(contents().includes('Current task: task-1'));
  assert.equal(get('run-status').textContent, 'Running');
  assert.equal(get('run-task').textContent, '1 / --');
  const savedTaskState = structuredClone(data.state);
  for (const [current, completed, total, expected] of [
    ['task-1', [], 4, '1 / 4'],
    ['arbitrary-task-id', ['first', 'second'], 4, '3 / 4'],
    ['arbitrary-task-id', ['first', 'second'], 4, '3 / 4'], // Retry retains position.
    ['second', ['first', 'second'], 4, '2 / 4'], // Checkpoint already recorded.
    ['last', ['first', 'second', 'third'], 4, '4 / 4'],
    [null, [], 4, '-- / 4'],
    [null, ['first', 'second', 'third', 'last'], 4, '-- / 4'],
    ['unknown', undefined, 4, '-- / 4'],
    ['unknown', 'invalid', 4, '-- / 4'],
    ['unknown', ['duplicate', 'duplicate'], 4, '-- / 4'],
    ['unknown', [null], 4, '-- / 4'],
    ['overflow', ['first', 'second'], 2, '-- / 2'],
  ]) {
    data.state.current_task = current;
    data.state.completed = completed;
    data.task_count = total;
    await poll();
    assert.equal(get('run-task').textContent, expected);
  }
  delete data.task_count;
  data.controller.task_count = 4;
  data.state = savedTaskState;
  await poll();
  assert.equal(get('run-task').textContent, '1 / 4', 'Controller count fallback');
  delete data.controller.task_count;
  await poll();
  await click('nav-tasks');
  for (const text of ['Attempt 1', 'REJECTED', 'Original summary', 'Original feedback', 'Test 1: exit 1']) assert(contents().includes(text), text);
  assert(walk(get('app')).some(node => node.className === 'attempt rejected'));
  assert(contents().includes('Local commit: abc123'));
  assert(contents().includes('<script>bad()</script>'));
  const feedback = walk(get('app')).find(node => node.tag === 'details' && node.textContent.includes('Feedback (original)'));
  assert.equal(feedback.open, false);
  feedback.open = true; feedback.ontoggle();
  data.events.push({...data.events[0], seq:2});
  await poll();
  assert(walk(get('app')).some(node => node.tag === 'details' && node.open && node.textContent.includes('Feedback (original)')));
  await click('nav-logs');
  for (const text of ['Codex B', 'rejected', '2026-10-25T11:00:00Z', 'gone.txt (missing)', 'Line 2: corrupt event', '<script>bad()</script>']) assert(contents().includes(text), text);
  const search = walk(get('app')).find(node => node.tag === 'input');
  search.value = 'no matching actor'; search.oninput();
  assert(contents().includes('0 of 2 events'));
  assert(!contents().includes('review_finished'));
  search.value = 'rejected'; search.oninput();
  assert(contents().includes('2 of 2 events'));
  search.value = ''; search.oninput();
  search.focus(); search.setSelectionRange(0, 0);
  data.events.push({...data.events[0], seq:3});
  await poll();
  assert.equal(document.activeElement.id, 'log-search');
  assert.equal(document.activeElement.selectionStart, 0);
  const unchangedSearch = document.activeElement;
  await poll();
  assert.equal(document.activeElement, unchangedSearch, 'Unchanged polling preserves input DOM');
  document.activeElement = null;
  const missing = walk(get('app')).find(node => node.textContent === 'gone.txt (missing)');
  assert.equal(typeof missing.onclick, 'undefined');
  const link = walk(get('app')).find(node => node.textContent === 'review.json (available)');
  await link.onclick(); await flush();
  const viewer = get('evidence-viewer').children[0];
  assert(get('evidence-viewer').textContent.includes('<img src=x onerror=bad()>'));
  await poll(); await click('nav-overview'); await poll();
  assert.equal(get('evidence-viewer').children[0], viewer);
  assert.equal(evidenceReads, 1, 'Polling must not reload evidence');
  data.controller.active = false; data.controller.exit_code = 0;
  await poll();
  assert.equal(get('run-status').textContent, 'Unknown run state');
  assert.notEqual(get('header-state').textContent, 'Completed');
  assert(get('resume').disabled);
  data.state.status = 'stopped'; data.controller.resume_ready = true;
  await poll();
  assert.equal(get('run-status').textContent, 'Stopped');
  get('attempts').value = '5';
  await click('resume'); await click('resume');
  assert.deepEqual(calls.at(-1), ['resume', 5]);
  assert.equal(calls.filter(call => call[0] === 'resume').length, 2);
  data.controller.active = false; data.controller.resume_ready = false;
  data.state.status = 'completed'; data.state.completed = ['task-1'];
  await poll();
  data.controller.exit_code = 1;
  data.controller.error = 'Runner stopped with exit code 1; inspect state.json and events.jsonl.';
  data.controller.process_history = [{exit_code:1, message:'Prior process failure'}];
  await poll();
  assert.equal(get('run-status').textContent, 'Completed');
  assert.equal(get('header-state').textContent, 'Completed');
  assert.equal(get('header-state').className, 'pill green');
  assert(!contents().includes('Runner stopped with exit code 1'));
  await click('nav-logs');
  assert(contents().includes('Runner stopped with exit code 1'));
  assert(contents().includes('Prior process failure'));
  await click('nav-overview');
  get('message').textContent = 'Launch accepted. Waiting for durable runner progress.';
  await poll();
  assert.equal(get('message').textContent, '');
  data.state.history.push({task:'task-1', attempt:2, review:{decision:'APPROVED'}});
  await poll(); await click('nav-tasks');
  assert(walk(get('app')).some(node => node.className === 'attempt approved'));
  assert(walk(get('app')).some(node => node.className === 'attempt rejected'));
  await click('nav-overview');
  const primary = walk(get('app')).filter(node => node.className === 'notice warning');
  assert.equal(primary.length, 0, 'No mixed success/failure headline');
  await click('nav-logs');
  const processHistory = walk(get('app')).find(node => node.tag === 'details' && node.textContent.includes('Process history'));
  assert.equal(processHistory.open, false);
  assert(processHistory.textContent.includes('exit code 1'));
  assert(processHistory.textContent.includes('previous_child_exit_code'));
  await click('nav-overview');
  get('message').textContent = 'Old launch warning';
  await click('refresh');
  assert.equal(get('message').textContent, '');
  data.state.status = 'checkpointing';
  await poll();
  assert.equal(get('run-status').textContent, 'Awaiting recovery');
  assert.notEqual(get('header-state').textContent, 'Completed');
  assert(get('resume').disabled);
  data.state.status = 'completed';
  api.open_run = async () => ({ok:true});
  get('message').textContent = 'Launch accepted. Old warning';
  await click('open-run');
  assert.equal(get('message').textContent, '');
  assert.equal(get('run-status').textContent, 'Completed');
  data.controller.active = true;
  await poll();
  assert.equal(get('run-status').textContent, 'Running');
  assert.notEqual(get('header-state').textContent, 'Completed');
  data.controller.active = null; data.controller.uncertain = true; data.controller.error = 'Unknown launch; inspect logs';
  await poll();
  assert.equal(get('run-status').textContent, 'Unknown child status');
  assert(contents().includes('Unknown launch; inspect logs'));
  assert(get('start').disabled && get('resume').disabled);
  data.root = data.controller.logs = 'C:\\legacy'; data.controller.active = false; data.controller.uncertain = false;
  data.state = null; data.events = []; data.state_error = 'state.json is corrupt or unreadable';
  data.files = [{label:'File', path:'legacy.txt', status:'available'}];
  await poll(); await click('nav-logs');
  assert(contents().includes('No events recorded'));
  assert(contents().includes('state.json is corrupt'));
  assert(contents().includes('legacy.txt (available)'));
  assert.equal(get('evidence-viewer').children.length, 0, 'Changing runs must clear old evidence');
  api.evidence = async () => ({ok:false, text:'Evidence file is missing.'});
  await walk(get('app')).find(node => node.textContent === 'legacy.txt (available)').onclick();
  assert(get('evidence-viewer').textContent.includes('Evidence file is missing.'));
  failStatus = true; await poll();
  assert(get('start').disabled && get('resume').disabled);
  assert(get('message').textContent.includes('status unavailable'));
  assert.equal(get('header-state').textContent, 'Unknown child status');
  assert(snapshotReads > 5);
  console.log('Dashboard button, navigation, polling, evidence and state contracts passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
