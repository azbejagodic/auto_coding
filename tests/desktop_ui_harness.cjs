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
const document = {getElementById: get, createElement: tag => new Element(tag),
  createTextNode: text => ({textContent: String(text)})};
let data = {root:'', exists:false, empty:true, state:null, events:[], event_errors:[],
  controller:{active:false, uncertain:false, repo:null, plan:null, logs:null, resume_ready:false, start_ready:false,
    preflight:{branch:null, clean:null, errors:['Select a Git repository root.']},
    push:{eligible:false, reason:'Connect a managed GitHub repository with approved local checkpoints to publish.'}}};
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
    Object.assign(data.controller, {repository:'owner/repo', repo:'managed/checkout', github:{head:'abc123', remote_head:'abc123', local:['main', 'feature/existing'], remote:['origin/main', 'origin/master', 'origin/feature/remote']}, push:{eligible:false, reason:'No approved checkpoints'}});
    data.controller.preflight = {branch:'main', clean:true, errors:['Runs are blocked on main/master.']}; return {ok:true}; },
  checkout_github_branch: async (repository, name) => { calls.push(['checkout_github_branch', repository, name]); data.controller.preflight = {branch:name, clean:true, errors:[]}; return {ok:true}; },
  create_github_branch: async (repository, name, base, confirmed) => { calls.push(['create_github_branch', repository, name, base, confirmed]); data.controller.github.local.push(name); data.controller.preflight.branch = name; data.controller.github.remote_head = null; return {ok:true}; },
  fetch_github: async repository => { calls.push(['fetch_github', repository]); return {ok:true}; },
  push_approved_commits: async () => { calls.push(['push_approved_commits']); return {ok:false, error:'Remote branch changed. Fetch and review; no force push.'}; },
  snapshot: async () => { snapshotReads++; if (failStatus) throw Error('status unavailable'); return structuredClone(data); },
  select_repo: async (...args) => { calls.push(['select_repo', ...args]); if (pendingSelection) await pendingSelection;
    data.controller.repo = 'C:\\project with spaces'; return {ok:true}; },
  select_plan: async (...args) => { calls.push(['select_plan', ...args]); data.controller.plan = 'C:\\plans\\task.json'; data.controller.start_ready = true; return {ok:true}; },
  open_run: async (...args) => { calls.push(['open_run', ...args]); return {ok:true, cancelled:true}; },
  start: async (...args) => { calls.push(['start', ...args]); if (launchResult.ok) { data.controller.active = true; data.controller.logs = data.root = 'C:\\runs\\one'; data.exists = true; data.empty = false; data.state = {status:'running', auto_push: args[1] ? {branch:'feature/ui'} : null}; } return launchResult; },
  resume: async (...args) => { calls.push(['resume', ...args]); data.controller.active = true; return {ok:true}; },
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
const assertCards = (status, completed, child) => {
  const cards = get('app').children[0];
  assert.equal(cards.className, 'grid');
  assert.equal(get('app').children.length, 1, 'Overview ends with only the status row');
  assert.deepEqual(cards.children.map(card => card.children[0].textContent), ['RUN STATUS', 'COMPLETED TASKS', 'CHILD PROCESS']);
  assert.deepEqual(cards.children.map(card => card.children[1].textContent), [status, completed, child]);
  assert(get('logs-tools').hidden, 'Secondary controls are absent from Overview');
};
(async () => {
  assert(get('start').disabled);
  await listeners.pywebviewready(); await flush();
  assert(!get('browse-project').disabled);
  assert(get('start').disabled);
  assert.equal(get('header-state').textContent, 'Idle');
  assertCards('Idle', 'Unknown', 'Inactive');
  for (const id of ['github-state', 'start-reason', 'resume-reason', 'push-reason', 'preflight', 'message']) {
    assert.equal(get(id).textContent, '', 'Idle state must be quiet: ' + id);
  }
  assert(get('resume').disabled && get('push-approved').disabled && get('fetch-github').disabled);
  assert(!get('auto-push').checked);
  assert(!contents().includes('Ready for your first run'));
  assert.equal(get('run-setup').hidden, false);
  // Durable status, completion, and current activity are independent snapshot facts.
  const baseline = structuredClone(data);
  for (const [state, active, uncertain, total, expected] of [
    [{status:'running', completed:['a']}, true, false, 3, ['Running', '1 / 3', 'Running']],
    [{status:'completed', completed:['a','b','c']}, false, false, 3, ['Completed', '3 / 3', 'Inactive']],
    [{status:'stopped', completed:[]}, false, false, 3, ['Stopped', '0 / 3', 'Inactive']],
    [{status:'completed', completed:['a']}, true, false, 1, ['Completed', '1 / 1', 'Running']],
    [null, null, true, null, ['Unknown', 'Unknown', 'Unknown']],
    [{status:'future-status'}, false, false, 3, ['Unknown', 'Unknown', 'Inactive']],
    [{status:'stopped', completed:['a']}, false, false, 0, ['Stopped', '1 / Unknown', 'Inactive']],
  ]) {
    data.state = state; data.root = data.controller.logs = 'mock/run'; data.exists = true;
    Object.assign(data.controller, {active, uncertain, exit_code:0}); data.task_count = total;
    await poll(); assertCards(...expected);
    data.controller.exit_code = 1; await poll(); assertCards(...expected);
  }
  // Reopening saved runs must render one warning from overlapping state sources.
  for (const [active, uncertain, error, expected] of [
    [true, false, 'An Auto-Coding runner is active or uncertain for this checkout.', 'A run is already in progress. Check Tasks for its status.'],
    [false, false, 'An Auto-Coding runner is active or uncertain for this checkout.', 'This run may still be active. Check Logs before starting another.'],
    [null, true, 'Unknown launch; inspect logs', 'This run may still be active. Check Logs before starting another.'],
    [false, false, 'Runner stopped with exit code 7; inspect state.json and events.jsonl.', 'Runner stopped with exit code 7; inspect state.json and events.jsonl.'],
  ]) {
    data.state = {status:'stopped'}; data.exists = true;
    data.root = data.controller.logs = 'mock/saved';
    Object.assign(data.controller, {active, uncertain, error});
    data.controller.preflight.errors = [error, error];
    get('message').textContent = error;
    await poll();
    assert.equal(get('preflight').textContent, expected);
    assert.equal(get('preflight').children.length, 1);
    for (const id of ['start-reason', 'resume-reason', 'push-reason', 'message']) assert.equal(get(id).textContent, '');
    assert(get('preflight-details').textContent.includes(error));
    assert(get('start').disabled && get('resume').disabled);
    assertCards('Stopped', 'Unknown', active === true ? 'Running' : uncertain ? 'Unknown' : 'Inactive');
  }
  // The real managed-repository preflight and push-eligibility paths can report
  // the same checkout lock without setting controller.error or uncertainty.
  for (const saved of [false, true]) {
    data = structuredClone(baseline);
    const lock = 'An Auto-Coding runner is active or uncertain for this checkout.';
    Object.assign(data.controller, {repository:'owner/repo', repo:'managed/checkout',
      active:false, uncertain:false, push:{eligible:false, reason:lock}});
    data.controller.preflight.errors = [lock];
    if (saved) {
      data.root = data.controller.logs = 'mock/saved';
      data.exists = true; data.state = {status:'stopped'};
    }
    get('message').textContent = lock;
    await poll();
    assert.equal(get('preflight').textContent, 'This run may still be active. Check Logs before starting another.');
    assert.equal(get('preflight').children.length, 1);
    for (const id of ['start-reason', 'resume-reason', 'push-reason', 'message']) assert.equal(get(id).textContent, '');
    assert(get('preflight-details').textContent.includes(lock), 'Original lock remains in Logs diagnostics');
    assert(get('start').disabled && get('resume').disabled && get('push-approved').disabled);
    assertCards(saved ? 'Stopped' : 'Idle', 'Unknown', 'Inactive');
  }
  data.controller.preflight.errors = [];
  data.controller.push.reason = 'Remote branch changed. Fetch and review; no force push.';
  await poll();
  assert.equal(get('push-reason').textContent, data.controller.push.reason, 'Distinct publication failures stay visible');
  data = baseline; await poll();
  for (const id of ['preflight', 'start-reason', 'resume-reason', 'push-reason', 'message']) {
    assert.equal(get(id).textContent, '', 'Returning to idle clears stale run warnings');
  }
  const initial = structuredClone(data);
  await click('nav-logs');
  assert.equal(get('nav-logs')['aria-current'], 'page');
  assert(contents().includes('Inspect before sharing'));
  assert(walk(get('app')).find(node => node.textContent === 'Export DEBUG REPORT (default)').disabled);
  await click('nav-overview');
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
  assert(get('github-state').textContent.includes('Published at abc123 (cached origin/main)'));
  assert(get('github-branch').children.some(option => option.value === 'main' && option.textContent.includes('protected, base only')));
  assert(get('github-branch').children.some(option => option.value === 'feature/remote' && option.textContent.includes('remote-only')));
  assert(get('github-branch').children.some(option => option.value === 'feature/existing' && option.textContent.includes('local-only, unpublished')));
  assert(!get('github-branch').children.some(option => option.value === 'HEAD'));
  // Even inconsistent readiness metadata cannot make a protected branch writable.
  for (const branch of ['main', 'master', 'MAIN']) {
    Object.assign(data.controller, {start_ready:true, resume_ready:true, push:{eligible:true}});
    data.controller.preflight.branch = branch; await poll();
    assert(get('start').disabled && get('resume').disabled && get('push-approved').disabled);
    get('github-branch').value = branch; get('github-branch').onchange();
    assert(get('select-branch').disabled);
    const before = calls.length;
    for (const id of ['start', 'resume', 'push-approved', 'select-branch']) await click(id);
    assert.equal(calls.length, before, 'Protected actions must not reach the bridge');
  }
  Object.assign(data.controller, {start_ready:false, resume_ready:false, push:{eligible:false}});
  data.controller.preflight.branch = 'main'; await poll();
  const beforeBranchSelection = calls.length;
  get('github-branch').value = 'feature/remote'; get('github-branch').onchange();
  assert.equal(calls.length, beforeBranchSelection, 'Selecting a branch alone has no side effects');
  assert(get('github-state').textContent.includes('Current branch: main'), 'Status describes checkout, not pending dropdown choice');
  data.controller.github.remote.push('origin/feature/newly-fetched');
  await poll();
  assert.equal(get('github-branch').value, 'feature/remote', 'New branch options preserve the pending selection');
  assert.equal(calls.length, beforeBranchSelection, 'Refreshing branch options must not perform an action');
  await click('fetch-github');
  assert.deepEqual(calls.at(-1), ['fetch_github', 'owner/repo']);
  const workingFetch = api.fetch_github;
  api.fetch_github = async () => ({ok:false, error:'Cannot reach GitHub. Check your network connection, then retry.'});
  await click('fetch-github');
  assert.equal(get('github-branch').value, 'feature/remote', 'Failed fetch preserves pending selection');
  assert(get('message').textContent.includes('Cannot reach GitHub'));
  assert(!get('message').textContent.includes('Selection updated'));
  assert(get('message').className.includes('red'));
  assert(!get('fetch-github').disabled, 'Fetch failure permits safe retry');
  api.fetch_github = workingFetch;
  await click('select-branch');
  assert.deepEqual(calls.at(-1), ['checkout_github_branch', 'owner/repo', 'feature/remote']);
  await click('new-branch'); get('branch-name').value = 'feature/cancelled';
  const beforeCancel = calls.length; await click('cancel-branch');
  assert.equal(calls.length, beforeCancel);
  assert(get('branch-form').hidden);
  assert.equal(document.activeElement, get('new-branch'), 'Cancel restores branch focus');
  await click('new-branch'); get('branch-name').value = 'feature/new';
  get('branch-base').value = 'master'; get('branch-base').onchange();
  get('branch-base-confirm').checked = true; get('branch-base-confirm').onchange();
  get('branch-base').focus();
  data.controller.github.remote.push('origin/feature/unrelated'); await poll();
  assert(!get('branch-form').hidden, 'Polling keeps the in-progress branch form open');
  assert.equal(get('branch-base').value, 'master');
  assert(get('branch-base-confirm').checked, 'Unchanged alternate base keeps its confirmation');
  assert.equal(document.activeElement, get('branch-base'));
  assert.equal(get('branch-name').value, 'feature/new');
  get('branch-base').value = 'main'; get('branch-base').onchange();
  assert(get('create-branch').disabled, 'Main base also requires explicit confirmation');
  get('branch-base-confirm').checked = true; get('branch-base-confirm').onchange();
  await click('create-branch');
  assert.deepEqual(calls.at(-1), ['create_github_branch', 'owner/repo', 'feature/new', 'main', true]);
  assert(get('branch-form').hidden);
  assert(get('github-state').textContent.includes('Current branch: feature/new'));
  assert(get('github-state').textContent.includes('Local-only, unpublished'));
  data.controller.github.error = 'Branch metadata unavailable'; await poll();
  assert(get('github-state').textContent.includes('Publication unknown'));
  assert(!get('github-state').textContent.includes('Local-only'));
  delete data.controller.github.error;
  data.controller.preflight.branch = null; await poll();
  assert(get('github-state').textContent.includes('Detached HEAD / unavailable'));
  assert(get('github-state').textContent.includes('Publication unknown'));
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
  assert(get('connect-github').disabled && get('browse-project').disabled);
  assert(get('preflight').textContent.includes('clean working tree'));
  const beforeDirty = calls.length;
  for (const id of ['connect-github', 'browse-project', 'select-branch', 'create-branch', 'push-approved']) await click(id);
  assert.equal(calls.length, beforeDirty, 'Dirty checkout cannot change repository or branch or publish');
  assert(!get('fetch-github').disabled, 'Fetch preserves dirty local work');
  data.controller.preflight.clean = true; data.controller.active = true; await poll();
  assert(get('connect-github').disabled && get('select-branch').disabled);
  assert.equal(get('preflight').textContent, 'A run is already in progress. Check Tasks for its status.');
  const beforeActive = calls.length;
  for (const id of ['connect-github', 'browse-project', 'select-branch', 'create-branch', 'fetch-github', 'push-approved', 'start', 'resume']) await click(id);
  assert.equal(calls.length, beforeActive, 'Active runner locks workspace and launch actions');
  data.controller.active = false; data.controller.preflight.branch = 'feature/from-master'; data.controller.push = {eligible:true, reason:'Approved checkpoints'}; await poll();
  await click('push-approved');
  assert.deepEqual(calls.at(-1), ['push_approved_commits']);
  assert(get('message').textContent.includes('Remote branch changed'));
  data.controller.github.head = 'newsha'; data.controller.github.remote_head = 'oldsha';
  await poll();
  assert(get('github-state').textContent.includes('Approved checkpoints pending push'));
  data.controller.push = {eligible:false}; await poll();
  assert(!get('github-state').textContent.includes('Published at'));
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
  assert(!get('start').disabled);
  get('auto-push').checked = true; get('auto-push').onchange();
  await poll();
  assert(get('auto-push').checked, 'Unchanged polling preserves explicit opt-in');
  data.controller.preflight.branch = 'feature/another-setup'; await poll();
  assert.equal(get('auto-push').checked, false, 'A different branch defaults automatic push OFF');
  for (const error of ['Runs are blocked on main/master. Switch to a feature branch first.', 'Plan validation failed: malformed plan', 'Commit or discard changes before START.']) {
    data.controller.start_ready = false; data.controller.preflight.errors = [error];
    await poll();
    assert(get('start').disabled);
    assert(get('preflight').textContent.includes(error));
  }
  data.controller.start_ready = true; data.controller.preflight.errors = [];
  await poll();
  const cancelledLaunch = launchResult;
  launchResult = {ok:false, error:'Failed start: checkpoint could not be written. Check disk space and retry.'};
  await click('start');
  assert(get('message').textContent.includes('checkpoint could not be written'));
  assert(get('message').className.includes('red'));
  launchResult = cancelledLaunch;
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
    assert(!get('start').disabled && !get('resume').disabled);
    assert.equal(get('attempts-error').textContent, '');
    attempts.focus();
    data.controller.preflight.branch = 'feature/number-' + value;
    await poll();
    assert.equal(document.activeElement, attempts, 'Polling preserves keyboard focus on number input');
    assert.equal(attempts.value, value);
    await click('start');
    assert.deepEqual(calls.at(-1), ['start', Number(value)]);
    assert.equal(data.controller.active, false, 'Cancelled confirmation must not launch');
  }
  calls.length = 0;
  data.controller.resume_ready = false;
  attempts.value = '3'; attempts.oninput();
  await click('open-run');
  assert(get('message').textContent.includes('Cancelled'));
  assert(get('plan-path').textContent.includes('C:\\plans\\task.json'));
  await click('start');
  assert.deepEqual(calls.at(-1), ['start', 3]);
  assert(get('message').textContent.includes('Cancelled'));
  assert.equal(data.controller.active, false);
  launchResult = {ok:true};
  get('auto-push').checked = true;
  await click('start');
  assert.deepEqual(calls.at(-1), ['start', 3, true]);
  assert.equal(get('auto-push').checked, true, 'Active run shows its saved publication policy');
  assert(get('auto-push').disabled, 'Publication settings cannot change mid-run');
  await click('start');
  assert.equal(calls.filter(call => call[0] === 'start').length, 2);
  assert.equal(get('header-state').textContent, 'Running');
  assert(get('browse-project').disabled && get('resume').disabled);
  // A completed/stopped run can seed a new Start, whose policy must default OFF.
  data.controller.active = false; await poll();
  assert.equal(get('auto-push').checked, false);
  get('auto-push').checked = true; get('auto-push').onchange();
  launchResult = {ok:false, cancelled:true};
  await click('start');
  assert.deepEqual(calls.at(-1), ['start', 3, true], 'Busy state must preserve explicit new-run opt-in');
  assert.equal(data.controller.active, false);
  get('auto-push').checked = false; get('auto-push').onchange();
  data.controller.active = true; await poll();
  assert.equal(get('auto-push').checked, true, 'Saved run still shows its own policy');
  data.state = {status:'running', current_task:'task-1', completed:[], history:[
    {task:'task-1', attempt:1, tests:[{index:1, exit_code:1, timed_out:false}],
      review:{decision:'REJECTED', issues:['real blocker'], summary:'Original summary'}, feedback:'Original feedback <script>bad()</script>', commit:'abc123'}]};
  data.events = [{seq:1, timestamp_utc:'2026-10-25T11:00:00Z', actor:'Codex B', event:'review_finished', outcome:'rejected', task_id:'task-1', attempt:1,
    _evidence_status:[{label:'verdict', path:'review.json', status:'available'}, {label:'test', path:'gone.txt', status:'missing'}],
    metadata:{reason:'<script>bad()</script>'}}];
  data.event_errors = ['Line 2: corrupt event'];
  await poll();
  assert(!contents().includes('Current task: task-1'));
  assertCards('Running', '0 / Unknown', 'Running');
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
  assert(get('evidence-viewer').hidden, 'Selected raw rejection evidence stays out of Overview');
  assert.equal(evidenceReads, 1, 'Polling must not reload evidence');
  data.controller.active = false; data.controller.exit_code = 0;
  await poll();
  assert.equal(get('header-state').textContent, 'Unknown run state');
  assert.notEqual(get('header-state').textContent, 'Completed');
  assert(get('resume').disabled);
  data.state.status = 'stopped'; data.controller.resume_ready = true;
  await poll();
  assert.equal(get('header-state').textContent, 'Stopped');
  assertCards('Stopped', '0 / Unknown', 'Inactive');
  get('attempts').value = '5';
  await click('resume'); await click('resume');
  assert.deepEqual(calls.at(-1), ['resume', 5]);
  assert.equal(calls.filter(call => call[0] === 'resume').length, 1);
  data.controller.active = false; data.controller.resume_ready = false;
  data.state.status = 'completed'; data.state.completed = ['task-1'];
  await poll();
  data.controller.exit_code = 1;
  data.controller.error = 'Runner stopped with exit code 1; inspect state.json and events.jsonl.';
  data.controller.process_history = [{exit_code:1, message:'Prior process failure'}];
  await poll();
  assert.equal(get('header-state').textContent, 'Completed');
  assert(!contents().includes('Line 2: corrupt event'), 'Historical log diagnostics stay in Logs');
  assert.equal(get('header-state').className, 'pill green');
  data.task_count = 1; await poll();
  assertCards('Completed', '1 / 1', 'Inactive');
  delete data.task_count; await poll();
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
  assert.equal(get('header-state').textContent, 'Awaiting recovery');
  assert.notEqual(get('header-state').textContent, 'Completed');
  assert(get('resume').disabled);
  data.state.status = 'completed';
  api.open_run = async () => ({ok:true});
  get('message').textContent = 'Launch accepted. Old warning';
  await click('open-run');
  assert.equal(get('message').textContent, '');
  assert.equal(get('header-state').textContent, 'Completed');
  data.controller.active = true;
  await poll();
  assert.equal(get('header-state').textContent, 'Running');
  assert.notEqual(get('header-state').textContent, 'Completed');
  data.controller.active = null; data.controller.uncertain = true; data.controller.error = 'Unknown launch; inspect logs';
  await poll();
  assert.equal(get('header-state').textContent, 'Unknown child status');
  assert.equal(get('preflight').textContent, 'This run may still be active. Check Logs before starting another.');
  assert(get('preflight-details').textContent.includes('Unknown launch; inspect logs'));
  assertCards('Completed', '1 / Unknown', 'Unknown');
  assert(get('start').disabled && get('resume').disabled);
  data.root = data.controller.logs = 'C:\\legacy'; data.controller.active = false; data.controller.uncertain = false;
  data.state = null; data.events = []; data.state_error = 'state.json is corrupt or unreadable';
  data.files = [{label:'File', path:'legacy.txt', status:'available'}];
  await poll();
  assertCards('Unknown', 'Unknown', 'Inactive');
  await click('nav-logs');
  assert.equal(get('logs-tools').hidden, false);
  assert(contents().includes('No events recorded'));
  assert(contents().includes('state.json is corrupt'));
  assert(contents().includes('legacy.txt (available)'));
  data.controller.active = true; await poll();
  assert(get('auto-push').indeterminate, 'Missing active state must not be reported as policy OFF');
  data.controller.active = false; await poll();
  assert.equal(get('evidence-viewer').children.length, 0, 'Changing runs must clear old evidence');
  api.evidence = async () => ({ok:false, text:'Evidence file is missing.'});
  await walk(get('app')).find(node => node.textContent === 'legacy.txt (available)').onclick();
  assert(get('evidence-viewer').textContent.includes('Evidence file is missing.'));
  let savedRuns = [{path:'external/legacy', run_id:'<img onerror=bad()>', repo:'saved/repo',
    branch:null, time:null, status:'unknown', completed:null},
    {path:'missing/run', run_id:'Missing run', status:'missing', completed:2}];
  api.recent_runs = async () => ({runs:structuredClone(savedRuns)});
  api.open_recent_run = async path => { calls.push(['open_recent_run', path]);
    data.root = data.controller.logs = path; return {ok:true}; };
  api.forget_run = async path => { calls.push(['forget_run', path]); savedRuns = savedRuns.filter(run => run.path !== path); return {ok:true}; };
  await poll();
  assert(contents().includes('Recent runs'));
  assert(contents().includes('<img onerror=bad()>'));
  assert(contents().includes('Completed tasks not recorded'));
  assert(contents().includes('Time not recorded'));
  assert(contents().includes('Status: missing'));
  assert(contents().includes('kept until you deliberately delete them'));
  let openButtons = walk(get('app')).filter(node => node.tag === 'button' && node.textContent === 'Open / View');
  assert(!openButtons[0].disabled && openButtons[1].disabled);
  const launchesBeforeHistory = calls.filter(call => ['start', 'resume', 'checkout_github_branch'].includes(call[0])).length;
  await openButtons[0].onclick(); await flush();
  assert.deepEqual(calls.at(-1), ['open_recent_run', 'external/legacy']);
  assert.equal(get('evidence-viewer').children.length, 0, 'Switching runs clears selected raw evidence');
  data.controller.active = true; await poll();
  assert(walk(get('app')).filter(node => node.tag === 'button' && node.textContent === 'Open / View').every(node => node.disabled));
  assert(walk(get('app')).find(node => node.textContent === 'Open / Import run folder').disabled);
  data.controller.active = false; await poll();
  await walk(get('app')).find(node => node.tag === 'button' && node.textContent === 'Forget history entry').onclick(); await flush();
  assert.deepEqual(calls.at(-1), ['forget_run', 'external/legacy']);
  assert(!contents().includes('<img onerror=bad()>'));
  assert.equal(calls.filter(call => ['start', 'resume', 'checkout_github_branch'].includes(call[0])).length, launchesBeforeHistory);
  // Export uses only the selected run and mode. It must not refresh controller
  // status (which can invoke Git), inspect evidence, or launch/publish anything.
  data.exists = true; await poll();
  let exportOutcome = {ok:true, path:'C:/exports/<img onerror=bad()>.zip', contents:['debug-report.json'],
    inventory:[{name:'<script>bad()</script>', action:'omitted', reason:'sensitive'}], warning:'Inspect before sharing'};
  api.export_logs = async mode => { calls.push(['export_logs', mode]); return exportOutcome; };
  const beforeExportSnapshots = snapshotReads;
  const beforeExportEvidence = evidenceReads;
  const standard = () => walk(get('app')).find(node => node.textContent === 'Export DEBUG REPORT (default)');
  const raw = () => walk(get('app')).find(node => node.textContent === 'Export FULL RAW LOGS');
  assert(!standard().disabled);
  await standard().onclick(); await flush();
  assert.deepEqual(calls.at(-1), ['export_logs', 'standard']);
  assert.equal(snapshotReads, beforeExportSnapshots);
  assert.equal(evidenceReads, beforeExportEvidence);
  assert(contents().includes('Created: C:/exports/<img onerror=bad()>.zip'));
  assert(contents().includes('<script>bad()</script>'));
  exportOutcome = {ok:true, cancelled:true};
  await raw().onclick(); await flush();
  assert.deepEqual(calls.at(-1), ['export_logs', 'raw']);
  assert(get('message').textContent.includes('Export cancelled'));
  exportOutcome = {ok:false, error:'Disk full <img onerror=bad()>'};
  await standard().onclick(); await flush();
  assert(get('message').textContent.includes('Disk full <img onerror=bad()>'));
  assert(!standard().disabled);
  failStatus = true; await poll();
  assert(get('start').disabled && get('resume').disabled);
  assert(get('message').textContent.includes('status unavailable'));
  assert.equal(get('header-state').textContent, 'Unknown child status');
  assert(snapshotReads > 5);
  console.log('Dashboard button, navigation, polling, evidence and state contracts passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
