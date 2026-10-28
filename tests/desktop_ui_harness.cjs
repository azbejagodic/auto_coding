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
const mode = process.argv[3];
if (mode === 'saved' || mode === 'stale') data.preferences = {
  repository:'owner/repo', branch:mode === 'saved' ? 'feature/existing' : 'feature/deleted',
  plan:'C:/missing-plan.json', attempts:7, auto_push:false
};
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
let clockNow = Date.parse('2026-10-25T12:00:00Z');
class ControlledDate extends Date { static now() { return clockNow; } }
const context = vm.createContext({document, Date:ControlledDate, window:{pywebview:{api}, addEventListener:(name, callback) => { listeners[name] = callback; }},
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
  assert(get('logs-tools').hidden, 'Overview retains its compact setup and status row');
};
(async () => {
  assert(get('start').disabled);
  await listeners.pywebviewready(); await flush();
  if (mode === 'tasks') { await taskContracts(); return; }
  if (mode === 'logs') { await logsContracts(); return; }
  if (mode === 'reopen') { await reopenContracts(); return; }
  if (mode === 'saved' || mode === 'stale') {
    assert.equal(get('github-repo').value, 'owner/repo');
    assert.equal(get('attempts').value, '7');
    assert.equal(get('plan-path').textContent, 'C:/missing-plan.json');
    assert.equal(get('github-branch').value, '');
    assert(get('preferences-notice').textContent.includes('Connect the repository'));
    assert(get('preferences-notice').textContent.includes('Choose an available task plan'));
    assert(get('auto-push').checked, 'Legacy unchecked preference cannot override new-run default');
    assert(get('start').disabled);
    assert.equal(calls.length, 0, 'Saved settings never connect, launch or push');
    await click('nav-logs'); await click('nav-overview');
    assert.equal(get('github-repo').value, 'owner/repo');
    assert.equal(get('attempts').value, '7');
    await click('connect-github');
    assert.equal(get('github-branch').value, mode === 'saved' ? 'feature/existing' : '');
    assert(get('start').disabled && get('push-approved').disabled, 'Protected main and missing plan remain blocked');
    if (mode === 'stale') assert(get('preferences-notice').textContent.includes('Saved branch is unavailable'));
    assert(!calls.some(call => ['start', 'resume', 'push_approved_commits', 'checkout_github_branch'].includes(call[0])));
    // An eligible current branch is still not the restored selected branch.
    data.controller.preflight = {branch:'feature/other', clean:true, errors:[]};
    data.controller.github.local.push('feature/other');
    data.controller.plan = 'C:/valid.json'; data.controller.start_ready = true;
    await poll();
    assert(get('start').disabled, 'Saved or missing selection cannot launch on another current feature branch');
    const before = calls.length; await click('start'); assert.equal(calls.length, before);
    get('github-branch').value = 'feature/remote'; get('github-branch').onchange();
    assert(!get('select-branch').disabled, 'Unavailable saved setup allows another choice');
    return;
  }
  assert(!get('browse-project').disabled);
  assert(get('start').disabled);
  assert.equal(get('header-state').textContent, 'Idle');
  assertCards('Idle', 'Unknown', 'Inactive');
  for (const id of ['github-state', 'start-reason', 'resume-reason', 'push-reason', 'preflight', 'message']) {
    assert.equal(get(id).textContent, '', 'Idle state must be quiet: ' + id);
  }
  assert(get('resume').disabled && get('push-approved').disabled && get('fetch-github').disabled);
  assert(get('auto-push').checked);
  assert(!get('auto-push').disabled);
  assert.equal(calls.length, 0, 'Initialization never connects, launches or pushes');
  assert(!contents().includes('Ready for your first run'));
  assert.equal(get('run-setup').hidden, false);
  const beforeLocalSelection = structuredClone(data);
  await click('nav-tasks');
  assert(!get('logs-tools').hidden);
  assert(!get('browse-project').disabled);
  await click('browse-project');
  assert.equal(calls.at(-1)[0], 'select_repo');
  data = beforeLocalSelection; calls.length = 0;
  await click('nav-overview'); await poll();
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
  // Recovery and local repository tools are reachable on Tasks; Logs is downloads only.
  for (const recoverable of [false, true]) {
    data = structuredClone(baseline);
    const diagnosis = {path:'managed/checkout.job.lock', recoverable,
      message:'Recovery needed — previous run may still have a child process; inspect Logs',
      instructions:recoverable ? 'Completion verified.' : 'Leave this lock untouched.'};
    data.controller.preflight = {errors:[diagnosis.message], locks:[diagnosis]};
    data.controller.repo = 'managed/checkout';
    data.controller.repository = 'owner/repo';
    data.controller.push = {eligible:false, reason:'An Auto-Coding runner is active or uncertain for this checkout.'};
    await poll();
    assert.equal(get('preflight').textContent, diagnosis.message);
    assert.equal(get('preflight').children.length, 1);
    assert.equal(get('push-reason').textContent, '');
    assert(!contents().includes('Archive verified completed lock'));
    await click('nav-tasks');
    assert(!get('logs-tools').hidden);
    const recovery = walk(get('lock-recovery')).filter(node => node.tag === 'button');
    assert.equal(recovery.length, recoverable ? 1 : 0);
    if (recoverable) {
      api.recover_workspace_lock = async suffix => {
        calls.push(['recover_workspace_lock', suffix]);
        return {ok:true, archive:'managed/archived.lock'};
      };
      assert(!recovery[0].disabled);
      await recovery[0].onclick(); await flush();
      assert.deepEqual(calls.at(-1), ['recover_workspace_lock', '.job.lock']);
      assert(get('message').textContent.includes('Lock archived:'));
    }
    await click('nav-logs');
    assert(get('logs-tools').hidden);
    assert(!contents().includes(diagnosis.instructions));
    assert(!walk(get('app')).some(node => node.tag === 'button' && node.textContent === 'Archive verified completed lock'));
    await click('nav-overview');
  }
  data = structuredClone(baseline);
  data.controller.repository = 'owner/repo';
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
  assert(contents().includes('No saved runs found.'));
  assert.equal(walk(get('app')).filter(node => node.textContent === 'Download ZIP').length, 0);
  await click('nav-overview');
  await click('list-github');
  assert(get('message').textContent.includes('Enter OWNER/REPO manually'));
  assert(get('github-repositories').hidden);
  get('github-repo').value = 'bad/repo'; await click('connect-github');
  assert(get('message').textContent.includes('<img onerror=bad()>'));
  get('github-repo').value = 'https:' + '//github.com/Owner/Repo.git';
  await click('connect-github');
  assert.deepEqual(calls.at(-1), ['connect_github', 'https:' + '//github.com/Owner/Repo.git']);
  assert.equal(get('project-path').textContent, 'managed/checkout');
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
  assert(contents().includes('No run loaded.'));
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
  get('auto-push').checked = false; get('auto-push').onchange();
  await poll();
  assert(!get('auto-push').checked, 'Unchanged polling preserves explicit OFF');
  data.controller.preflight.branch = 'feature/another-setup'; await poll();
  assert.equal(get('auto-push').checked, false, 'Live branch refresh preserves explicit OFF');
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
  get('auto-push').checked = false; get('auto-push').onchange();
  await click('start');
  assert.deepEqual(calls.at(-1), ['start', 3], 'Explicit OFF is forwarded to the new run');
  await poll();
  assert(!get('auto-push').checked, 'Active run refresh preserves durable OFF');
  const beforeActiveFailure = calls.length;
  failStatus = true; await poll();
  assert(!get('auto-push').checked && get('auto-push').disabled);
  failStatus = false; await poll();
  assert(!get('auto-push').checked && get('auto-push').disabled, 'Active run OFF survives status recovery');
  assert.equal(calls.length, beforeActiveFailure, 'Active status recovery never launches or pushes');
  data.controller.active = false; data.state.status = 'completed'; await poll();
  assert(get('auto-push').checked, 'The next new setup defaults ON after an OFF run');
  get('auto-push').checked = true;
  await click('start');
  assert.deepEqual(calls.at(-1), ['start', 3, true]);
  assert.equal(get('auto-push').checked, true, 'Active run shows its saved publication policy');
  assert(get('auto-push').disabled, 'Publication settings cannot change mid-run');
  await click('start');
  assert.equal(calls.filter(call => call[0] === 'start').length, 3);
  assert.equal(get('header-state').textContent, 'Running');
  assert(get('browse-project').disabled && get('resume').disabled);
  // A completed/stopped run can seed a new Start, whose policy must default ON.
  data.controller.active = false; await poll();
  assert.equal(get('auto-push').checked, true);
  get('auto-push').checked = true; get('auto-push').onchange();
  launchResult = {ok:false, cancelled:true};
  await click('start');
  assert.deepEqual(calls.at(-1), ['start', 3, true], 'Busy state must preserve explicit new-run opt-in');
  assert.equal(data.controller.active, false);
  get('auto-push').checked = false; get('auto-push').onchange();
  data.controller.start_ready = false; await poll();
  assert(!get('auto-push').checked, 'New setup OFF survives temporary validation failure on a previous run');
  data.controller.start_ready = true; await poll();
  assert(!get('auto-push').checked, 'Recovery of readiness must preserve OFF');
  const beforeStatusFailure = calls.length;
  for (const startReady of [true, false]) {
    data.controller.start_ready = startReady; await poll();
    failStatus = true; await poll(); await poll();
    assert(!get('auto-push').checked, 'Unknown status preserves the setup checkbox value');
    assert(get('auto-push').disabled && get('start').disabled && get('resume').disabled);
    await click('nav-logs'); await click('nav-overview');
    failStatus = false; await poll();
    assert(!get('auto-push').checked, 'Status recovery preserves explicit OFF on the same new setup');
    assert(!get('auto-push').disabled, 'Recovered setup remains editable');
  }
  assert.equal(calls.length, beforeStatusFailure, 'Status failure and recovery never launch or push');
  data.controller.start_ready = true; await poll();
  await click('start');
  assert.deepEqual(calls.at(-1), ['start', 3], 'Recovered setup forwards explicit OFF');
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
  assert(!contents().includes('Recent activity'));
  assert(get('evidence-viewer').hidden);
  await click('nav-overview');
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
  assert(!contents().includes('Runner stopped with exit code 1'));
  assert(!contents().includes('Prior process failure'));
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
  assert(!walk(get('app')).some(node => node.tag === 'details'));
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
  assert(get('logs-tools').hidden);
  assert(!contents().includes('legacy.txt'));
  await logsContracts();
  failStatus = true; await poll();
  assert(get('start').disabled && get('resume').disabled);
  assert(get('message').textContent.includes('status unavailable'));
  assert.equal(get('header-state').textContent, 'Unknown child status');
  assert(snapshotReads > 5);
  console.log('Dashboard button, navigation, polling, evidence and state contracts passed');
})().catch(error => { console.error(error); process.exitCode = 1; });

async function reopenContracts() {
  // Restart with no selected run: users must be able to reach the native picker.
  assert.equal(data.root, '');
  for (const view of ['overview', 'logs', 'tasks']) {
    await click('nav-' + view);
    assert.equal(get('run-selection').hidden, view !== 'tasks');
  }
  assert(!get('open-run').disabled);
  api.open_run = async () => {
    calls.push(['open_run']);
    data.root = data.controller.logs = 'saved/reopened-run';
    data.controller.repo = 'saved/project'; data.controller.plan = 'saved/plan.json';
    data.controller.resume_ready = true;
    data.controller.preflight = {branch:'feature/reopened', clean:true, errors:[]};
    data.exists = true; data.task_ids = ['restored'];
    data.state = {status:'stopped', completed:[], history:[{task:'restored', attempt:1, feedback:'saved feedback'}]};
    return {ok:true};
  };
  await click('open-run');
  assert.deepEqual(calls.at(-1), ['open_run']);
  assert(walk(get('app')).some(n => n.className === 'task-row'));
  assert(contents().includes('saved feedback'));
  await click('nav-overview');
  assert(!get('resume').disabled);
  await click('resume');
  assert.equal(calls.at(-1)[0], 'resume');
  await click('nav-logs');
  assert(get('run-selection').hidden);
  assert(walk(get('app')).filter(n => n.tag === 'button').every(n => n.textContent === 'Download ZIP'));
}

async function logsContracts() {
  data.root = data.controller.logs = 'saved/run-one';
  data.controller.active = false; data.controller.uncertain = false;
  data.exists = true; data.state = {status:'stopped', completed:[]};
  data.events = [{event:42, metadata:{reason:'Technical details stay in ZIP'}}];
  const originals = structuredClone(data.events);
  const runs = [{path:'saved/run-one', run_id:'private-long-uuid-one', plan_filename:'first.json', status:'stopped', time:'2026-10-28T12:00:00Z'},
    {path:'saved/run-two', run_id:'private-long-uuid-two', plan_filename:'second.json', status:'completed', time:'2026-10-27T12:00:00Z'},
    {path:'saved/run-three', run_id:'private-long-uuid-three', plan_filename:'active.json', status:'running'}];
  api.recent_runs = async () => ({runs:structuredClone(runs)});
  let outcome = {ok:true, message:'ZIP saved.'};
  api.download_run = async path => { calls.push(['download_run',path]); return outcome; };
  await click('nav-logs'); await poll();
  const buttons = () => walk(get('app')).filter(n => n.tag === 'button');
  assert.equal(buttons().length, 3);
  assert(buttons().every(n => n.textContent === 'Download ZIP'));
  assert(buttons()[2].disabled);
  assert.equal(walk(get('app')).filter(n => n.className === 'run-row').length, 3);
  assert(contents().indexOf('first.json') < contents().indexOf('second.json'));
  for (const text of ['Selected run','Recent activity','Raw evidence','Technical details','private-long-uuid','Open / View']) assert(!contents().includes(text));
  assert(!walk(get('app')).some(n => ['pre','details','input'].includes(n.tag)));
  assert(get('logs-tools').hidden && get('evidence-viewer').hidden);
  const beforeReads = evidenceReads, beforeSnapshots = snapshotReads;
  await buttons()[1].onclick(); await flush();
  assert.deepEqual(calls.at(-1), ['download_run','saved/run-two']);
  assert.equal(data.root, 'saved/run-one');
  assert.equal(evidenceReads, beforeReads); assert.equal(snapshotReads, beforeSnapshots);
  assert(contents().includes('ZIP saved.'));
  outcome = {ok:false, cancelled:true, error:'Export cancelled. No ZIP saved.'};
  await buttons()[0].onclick(); await flush();
  assert(contents().includes('Export cancelled. No ZIP saved.'));
  outcome = {ok:false, error:'Disk full <img onerror=bad()>'};
  await buttons()[0].onclick(); await flush();
  assert(contents().includes('Disk full <img onerror=bad()>'));
  assert(!buttons()[0].disabled);
  assert.deepEqual(data.events, originals);
  assert.equal(get('page-title').textContent, 'Logs');
  const reads = snapshotReads; await click('refresh'); assert(snapshotReads > reads);
}

async function taskContracts() {
  const base = Date.parse('2026-10-25T11:00:00Z');
  clockNow = base;
  data = {root:'saved/run', exists:true, task_ids:['one','two','three','four'], task_count:4,
    controller:{active:true, uncertain:false, logs:'saved/run', task_steps:[{id:'one',test_count:4}]},
    state:{status:'running', current_task:'one', completed:[], history:[], auto_push:null}, events:[]};
  const event = (second, actor, name, outcome, metadata={}, attempt=1, task='one') => {
    data.events.push({seq:data.events.length+1, timestamp_utc:new Date(base+second*1000).toISOString(),
      task_id:task, attempt, actor, event:name, outcome, metadata,
      _evidence_status:[{label:'output',path:'exact-evidence.txt',status:'available'}]});
    clockNow = base + second*1000;
  };
  const project = () => {
    context.fixture = structuredClone(data);
    return JSON.parse(vm.runInContext('JSON.stringify(taskPresentation(fixture))', context));
  };
  const current = () => project()[0].current;
  const phase = name => current().phases.find(p => p.name === name);
  const summaries = () => walk(get('app')).filter(n => n.tag === 'details' && n.className === 'task-row').map(n => n.children[0]);
  const defaultText = () => summaries().map(n => n.textContent).join('\n');
  event(0,'controller','task_started','started');
  event(0,'Codex A','process_started','started');
  await poll(); await click('nav-tasks');
  assert.equal(project()[0].status,'Running');
  assert.equal(phase('Codex A').status,'Running');
  assert.equal(summaries().length,4);
  assert(defaultText().includes('Push off'));
  assert(!defaultText().includes('exit '));
  assert(!defaultText().includes('timed out:'));
  assert(!contents().includes('Tasks and attempt history'));
  assert(!contents().includes('Current task:'));
  const initialText = defaultText();
  clockNow += 15000; await poll();
  assert.notEqual(defaultText(), initialText, 'Clock refresh must update unchanged active snapshot');
  assert.equal(current().duration,15000);
  assert.equal(phase('Codex A').duration,15000);
  const row = walk(get('app')).find(n => n.className === 'task-row');
  row.open = true; row.ontoggle(); row.children[0].focus();
  event(20,'Codex A','process_finished','success');
  event(20,'tests','test_started','started',{index:1});
  event(25,'tests','test_finished','success',{index:1,exit_code:0,timed_out:false});
  event(25,'tests','test_started','started',{index:2});
  clockNow += 3000; await poll();
  assert.equal(phase('Tests').label,'Tests 2/4');
  assert.equal(phase('Tests').status,'Running');
  assert.equal(phase('Tests').duration,8000);
  assert.equal(document.activeElement.taskFocus,'task-one');
  assert(walk(get('app')).find(n => n.className === 'task-row').open);
  event(30,'tests','test_finished','success',{index:2});
  assert.equal(phase('Tests').status,'Running', 'Two finishes cannot complete a four-test stage');
  for (let i=3;i<=4;i++) {
    event(30+(i-3)*5,'tests','test_started','started',{index:i});
    event(35+(i-3)*5,'tests','test_finished','success',{index:i});
  }
  assert.equal(phase('Tests').status,'Passed');
  event(40,'Codex B','review_started','started');
  assert.equal(phase('Codex B').status,'Running');
  event(50,'Codex B','review_process_finished','success');
  assert.equal(phase('Codex B').status,'Waiting for Codex B');
  assert.equal(project()[0].status,'Waiting for review');
  // Interrupted review with passed tests never becomes approved.
  data.controller.active=false; data.state.status='stopped';
  event(51,'controller','stopped','failed',{reason:'invalid review verdict'});
  await poll();
  assert.equal(project()[0].status,'Stopped');
  assert.equal(current().approved,false);
  assert(!defaultText().includes('Approved'));
  assert.equal(defaultText().split('STOPPED').length-1,1);
  assert.equal(defaultText().split('invalid review verdict').length-1,1);
  const firstDuration=current().duration;
  const firstPhases=JSON.stringify(current().phases);
  clockNow+=3600000; await poll();
  assert.equal(current().duration,firstDuration);
  assert.equal(JSON.stringify(current().phases),firstPhases);
  // A second attempt after a long stopped gap has an independent clock.
  data.state.history=[{task:'one',attempt:1,tests:[{index:1,exit_code:0,timed_out:false}],review:null,
    feedback:'Keep every original byte <script>unsafe()</script>', custom_metadata:{unmodified:true}}];
  data.controller.active=true; data.state.status='running';
  event(3600,'controller','resume_started','started',{},null,null);
  event(3601,'controller','task_started','started',{},2);
  event(3601,'Codex A','process_started','started',{},2);
  event(3611,'Codex A','process_finished','success',{},2);
  event(3611,'Codex B','review_started','started',{},2);
  event(3621,'Codex B','review_process_finished','success',{},2);
  event(3621,'Codex B','review_finished','rejected',{},2);
  event(3622,'controller','correction_requested','retry',{},2);
  data.state.history.push({task:'one',attempt:2,review:{decision:'REJECTED',issues:['Original rejection'],summary:'Exact review'},feedback:'Fix original blocker'});
  event(3623,'controller','task_started','started',{},3);
  event(3623,'Codex A','process_started','started',{},3);
  event(3633,'Codex A','process_finished','success',{},3);
  event(3633,'Codex B','review_started','started',{},3);
  event(3643,'Codex B','review_process_finished','success',{},3);
  event(3643,'Codex B','review_finished','approved',{},3);
  assert.equal(project()[0].status,'Running', 'B approval alone is not task completion');
  assert.equal(phase('Codex B').status,'Approved');
  assert.equal(phase('Codex B').duration,10000);
  event(3643,'Git','checkpoint_started','started',{},3);
  assert.equal(phase('Commit').status,'Running');
  assert.equal(project()[0].status,'Running');
  event(3645,'Git','checkpoint_committed','success',{commit:'abcdef0123456789'},3);
  event(3645,'controller','task_completed','success',{},3);
  data.state.completed=['one'];
  data.state.history.push({task:'one',attempt:3,review:{decision:'APPROVED',issues:[],summary:'Exact approval'},commit:'abcdef0123456789'});
  await poll();
  assert.equal(project()[0].duration,51000+21000+22000);
  assert.equal(project()[0].attempts[0].duration,firstDuration);
  assert.equal(phase('Commit').status,'Committed locally');
  assert.equal(phase('Push').status,'Off');
  assert(defaultText().includes('Local commit abcdef01'));
  assert(!defaultText().includes('Pushed to GitHub'));
  for (const text of ['Full attempt record','custom_metadata','Keep every original byte','Exact review','Exact approval','exact-evidence.txt','Original event','abcdef0123456789','REJECTED','APPROVED']) assert(contents().includes(text),text);
  assert(walk(get('app')).filter(n=>n.tag==='details'&&n.textContent.startsWith('Full attempt record')).every(n=>!n.open));
  // Optional publication is distinct from committing locally; retries exclude idle.
  data.state.auto_push={branch:'feature/ui'};
  event(3645,'Git','push_started','started',{commit:'abcdef0123456789'},null);
  assert.equal(phase('Push').status,'Running');
  event(3648,'Git','push_finished','failed',{reason:'connectivity'},null);
  data.state.status='stopped'; data.controller.active=false;
  data.state.current_task=null;
  assert.equal(project()[0].status,'Stopped');
  assert.equal(project()[0].reason,'connectivity');
  await poll();
  assert.equal(defaultText().split('connectivity').length-1,1);
  const beforeRetry=project()[0].duration;
  event(7200,'controller','resume_started','started',{},null,null);
  data.controller.active=true;
  event(7201,'Git','push_started','started',{commit:'abcdef0123456789'},null);
  event(7204,'Git','push_finished','success',{},null);
  assert.equal(phase('Push').duration,6000);
  assert.equal(project()[0].duration,beforeRetry+3000);
  assert.equal(phase('Push').status,'Pushed to GitHub');
  data.state.status='completed'; data.controller.active=false; data.state.current_task=null;
  await poll();
  const saved=structuredClone(data), savedProjection=JSON.stringify(project());
  data={root:'different',controller:{active:false},state:null,events:[]}; await poll();
  data=saved; clockNow+=10000000; await poll();
  assert.equal(JSON.stringify(project()),savedProjection,'Reopened run reconstructs the same recorded stage and timings');
  // Missing and inconsistent timestamps remain unknown, never negative.
  data.events=data.events.map(e=>({...e,timestamp_utc:undefined}));
  assert.equal(project()[0].duration,null);
  assert(current().phases.every(p=>p.duration===null));
  await poll(); assert(defaultText().includes('\u2014') || defaultText().includes(String.fromCharCode(8212)));
  data=saved;
  data.events[1].timestamp_utc=new Date(base+99999999).toISOString();
  assert.equal(project()[0].attempts[0].phases[0].duration,null);
  assert.equal(vm.runInContext('compactTime(-1)',context),String.fromCharCode(8212));
  // Timeout has a single actionable reason and the actual last stage.
  data={root:'timeout',task_ids:['one'],task_count:1,controller:{active:false,logs:'timeout'},
    state:{status:'stopped',current_task:'one',completed:[],history:[]},events:[]};
  event(0,'controller','task_started','started');
  event(0,'Codex A','process_started','started'); event(2,'Codex A','process_finished','success');
  event(2,'tests','test_started','started',{index:1});
  event(12,'tests','test_finished','timed_out',{index:1,timed_out:true,exit_code:1});
  event(12,'controller','stopped','failed',{reason:'test timeout'});
  await poll();
  assert.equal(phase('Tests').status,'Timed out');
  assert.equal(phase('Tests').duration,10000);
  assert(defaultText().includes('STOPPED at Tests: test timeout'));
  assert.equal(defaultText().split('test timeout').length-1,1);
  assert(!defaultText().includes('Approved'));
  // Resume can reuse a number when the interrupted attempt never reached history.
  data={root:'reused',controller:{active:false},state:{status:'completed',current_task:null,
    completed:['one'],history:[{task:'one',attempt:1,review:{decision:'APPROVED'}}]},events:[]};
  event(0,'controller','task_started','started');
  event(0,'Codex B','review_started','started');
  event(1000,'controller','resume_started','started',{},null,null);
  event(1001,'controller','task_started','started');
  event(1001,'Codex B','review_started','started');
  event(1005,'Codex B','review_finished','approved');
  event(1006,'controller','task_completed','success');
  assert.equal(project()[0].attempts.length,2);
  assert.equal(project()[0].attempts[0].approved,false,'Reused number must not approve interrupted review');
  assert.equal(project()[0].attempts[0].duration,null,'Unfinished session cannot span resume idle time');
  assert.equal(project()[0].attempts[1].duration,5000);
  assert.equal(project()[0].attempts[1].approved,true);
  // Checkpoint recovery adds only recorded active work, including live refresh.
  data={root:'checkpoint',task_ids:['one'],controller:{active:true},
    state:{status:'checkpointing',current_task:'one',completed:[],history:[]},events:[]};
  event(0,'controller','task_started','started');
  event(1,'Codex B','review_started','started');
  event(5,'Codex B','review_finished','approved');
  event(5,'Git','checkpoint_started','started');
  assert.equal(project()[0].status,'Running');
  event(7,'Git','stopped','failed',{reason:'checkpoint pending reconciliation'});
  data.controller.active=false;
  assert.equal(project()[0].status,'Stopped');
  assert.equal(project()[0].reason,'checkpoint pending reconciliation');
  assert.equal(current().approved,true, 'The original B verdict stays accessible');
  assert.equal(project()[0].duration,7000);
  event(3600,'controller','resume_started','started',{},null,null);
  data.controller.active=true;
  event(3601,'Git','checkpoint_started','started');
  clockNow+=2000; await poll();
  assert.equal(project()[0].duration,9000);
  assert.equal(phase('Commit').status,'Running');
  event(3604,'Git','checkpoint_committed','success',{commit:'abc123'});
  assert.equal(project()[0].duration,10000);
  assert.equal(phase('Commit').duration,5000);
  data.state.completed=['one']; data.state.current_task=null; data.controller.active=false;
  assert.equal(project()[0].status,'Approved');
  const recovered=JSON.stringify(project());
  clockNow+=1000000;
  assert.equal(JSON.stringify(project()),recovered);
  // Failed recovery is terminal even without a separate controller stopped event.
  data.state.current_task='one'; data.state.completed=[];
  event(7200,'Git','checkpoint_started','started');
  event(7203,'Git','checkpoint_stopped','failed',{reason:'checkpoint recovery failed'});
  assert.equal(project()[0].status,'Stopped');
  assert.equal(project()[0].reason,'checkpoint recovery failed');
  assert.equal(project()[0].duration,13000);
  // A prior failure remains evidence while the next configured test runs.
  data={root:'tests',task_ids:['one'],controller:{active:true,task_steps:[{id:'one',test_count:4}]},
    state:{status:'running',current_task:'one',completed:[],history:[]},events:[]};
  event(0,'controller','task_started','started');
  event(1,'tests','test_started','started',{index:1});
  event(2,'tests','test_finished','failed',{index:1,exit_code:1});
  event(3,'tests','test_started','started',{index:2});
  await poll();
  assert.equal(phase('Tests').status,'Running');
  assert.equal(phase('Tests').label,'Tests 2/4');
  assert.equal(phase('Tests').failures,1);
  assert(defaultText().includes('Tests 2/4: Running (1 earlier failed)'));
  data={root:'',controller:{active:false},state:null,events:[]}; await poll();
  assert.equal(contents(),'No run loaded.');
  console.log('Tasks phase, clock, resume, review, evidence and compact-row contracts passed');
}
