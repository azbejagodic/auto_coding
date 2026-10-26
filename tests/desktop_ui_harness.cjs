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
    preflight:{branch:'feature/ui', clean:true, errors:[]}}};
const calls = [];
let launchResult = {ok:false, cancelled:true};
let pendingSelection = null;
let failStatus = false;
let evidenceReads = 0;
let snapshotReads = 0;
const api = {
  snapshot: async () => { snapshotReads++; if (failStatus) throw Error('status unavailable'); return structuredClone(data); },
  select_repo: async (...args) => { calls.push(['select_repo', ...args]); if (pendingSelection) await pendingSelection;
    data.controller.repo = 'C:\\project with spaces'; return {ok:true}; },
  select_plan: async (...args) => { calls.push(['select_plan', ...args]); data.controller.plan = 'C:\\plans\\task.json'; data.controller.start_ready = true; return {ok:true}; },
  open_run: async (...args) => { calls.push(['open_run', ...args]); return {ok:true, cancelled:true}; },
  start: async (...args) => { calls.push(['start', ...args]); if (launchResult.ok) { data.controller.active = true; data.controller.logs = data.root = 'C:\\runs\\one'; data.exists = true; data.empty = false; } return launchResult; },
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
(async () => {
  assert(get('start').disabled);
  await listeners.pywebviewready(); await flush();
  assert(!get('browse-project').disabled);
  assert(get('start').disabled);
  assert.equal(get('header-state').textContent, 'Idle');
  assert(contents().includes('Ready for your first run'));
  assert.equal(get('run-setup').hidden, false);
  await click('nav-tasks');
  assert(get('run-setup').hidden);
  assert(contents().includes('Your task history will appear here'));
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
  for (const error of ['Runs are blocked on main/master. Switch to a feature branch first.', 'Plan validation failed: malformed plan', 'Commit or discard changes before START.']) {
    data.controller.start_ready = false; data.controller.preflight.errors = [error];
    await poll();
    assert(get('start').disabled);
    assert(get('preflight').textContent.includes(error));
  }
  data.controller.start_ready = true; data.controller.preflight.errors = [];
  await poll();
  await click('open-run');
  assert(get('message').textContent.includes('Cancelled'));
  assert(get('plan-path').textContent.includes('C:\\plans\\task.json'));
  await click('start');
  assert.deepEqual(calls.at(-1), ['start', 3]);
  assert(get('message').textContent.includes('Cancelled'));
  assert.equal(data.controller.active, false);
  launchResult = {ok:true};
  await click('start');
  await click('start');
  assert.equal(calls.filter(call => call[0] === 'start').length, 2);
  assert(contents().includes('Running'));
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
  assert(contents().includes('Unknown run state'));
  assert.notEqual(get('header-state').textContent, 'Completed');
  assert(get('resume').disabled);
  data.state.status = 'stopped'; data.controller.resume_ready = true;
  await poll();
  assert(contents().includes('Stopped'));
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
  assert(contents().includes('Completed'));
  assert.equal(get('header-state').textContent, 'Completed');
  assert.equal(get('header-state').className, 'pill green');
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
  const processHistory = walk(get('app')).find(node => node.tag === 'details' && node.textContent.includes('Process history'));
  assert.equal(processHistory.open, false);
  assert(processHistory.textContent.includes('exit code 1'));
  assert(processHistory.textContent.includes('previous_child_exit_code'));
  get('message').textContent = 'Old launch warning';
  await click('refresh');
  assert.equal(get('message').textContent, '');
  data.state.status = 'checkpointing';
  await poll();
  assert(contents().includes('Awaiting recovery'));
  assert.notEqual(get('header-state').textContent, 'Completed');
  assert(get('resume').disabled);
  data.state.status = 'completed';
  api.open_run = async () => ({ok:true});
  get('message').textContent = 'Launch accepted. Old warning';
  await click('open-run');
  assert.equal(get('message').textContent, '');
  assert(contents().includes('Completed'));
  data.controller.active = true;
  await poll();
  assert(contents().includes('Running'));
  assert.notEqual(get('header-state').textContent, 'Completed');
  data.controller.active = null; data.controller.uncertain = true; data.controller.error = 'Unknown launch; inspect logs';
  await poll();
  assert(contents().includes('Unknown child status'));
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
