// Read-only reader regression scenarios against the current single-panel UI.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync(process.argv[2], 'utf8');
const scenario = process.argv[3];
const data = JSON.parse(fs.readFileSync(process.argv[4], 'utf8'));
class Element {
  constructor() { this.children = []; this._text = ''; this.value = ''; this.checked = false; }
  set textContent(value) { this._text = String(value); this.children = []; }
  get textContent() { return this._text + this.children.map(node => node.textContent).join(''); }
  set innerHTML(_) { throw Error('Unsafe HTML insertion'); }
  set outerHTML(_) { throw Error('Unsafe HTML replacement'); }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children = nodes; this._text = ''; }
  setAttribute(name, value) { this[name] = value; }
  addEventListener() {}
  focus() {}
  showModal() { this.open = true; }
  close() { this.open = false; }
}
const markup = html.split('<script>', 1)[0];
const nodes = new Map([...markup.matchAll(/\bid="([^"]+)"/g)].map(match => [match[1], new Element()]));
const get = id => nodes.get(id) || null;
get('attempts').value = '3';
const calls = [];
const api = {
  snapshot: async () => structuredClone(data),
  evidence: async name => { calls.push(['evidence', name]); return {ok:true, text:'<img onerror=bad()> private log'}; },
  download_logs: async () => { calls.push(['download_logs']); return {ok:true, message:'Export saved'}; }
};
const document = {getElementById:get, createElement:() => new Element(), hidden:false, addEventListener() {}};
const listeners = {};
const context = vm.createContext({document, window:{pywebview:{api}, addEventListener:(name, callback) => { listeners[name] = callback; }}, setInterval() {}});
vm.runInContext(html.split('<script>')[1].split('</script>')[0], context);
const render = () => vm.runInContext('render(' + JSON.stringify(data) + ')', context);
const flush = async () => new Promise(resolve => setImmediate(resolve));
(async () => {
  await listeners.pywebviewready(); await flush(); render();
  if (scenario === 'snapshot') {
    assert.equal(get('run-task').textContent, '1 / 1');
    assert.notEqual(get('run-status').textContent, 'Completed');
    assert.equal(calls.length, 0, 'Polling must not read evidence bodies');
    assert.deepEqual(get('export-evidence').children.map(node => node.value),
      ['', ...data.files.filter(file => file.status === 'available').map(file => file.path)]);
    await get('download-logs').onclick(); await flush();
    assert.deepEqual(calls, [['download_logs']]);
  } else if (scenario === 'current') {
    data.state.pending_checkpoint = {task:'one'}; data.state.status = 'checkpointing'; render();
    assert.equal(get('run-task').textContent, '1 / 1');
    assert.equal(get('run-status').textContent, 'Awaiting recovery');
    data.state.status = 'completed'; data.state.completed = ['one'];
    data.summary.completion_confirmed = false; render();
    assert.notEqual(get('run-status').textContent, 'Completed', 'A checkpoint alone cannot certify completion');
    delete data.state.pending_checkpoint; data.summary.completion_confirmed = true; render();
    assert.equal(get('run-status').textContent, 'Completed');
  } else if (scenario === 'evidence') {
    const select = get('export-evidence');
    assert.equal(get('include-evidence').disabled, true);
    assert.deepEqual(select.children.map(node => node.value), ['', 'log.txt']);
    select.value = 'log.txt'; select.onchange(); render();
    assert.equal(select.value, 'log.txt', 'Unchanged polling must preserve selection');
    assert.equal(get('include-evidence').disabled, false);
    await get('include-evidence').onclick(); await flush();
    assert.deepEqual(calls, [['evidence', 'log.txt']]);
    assert(!get('message').textContent.includes('private log'), 'Raw evidence stays in explicit export, not dashboard markup');
    data.files = [{path:'log.txt', status:'missing'}]; render();
    assert.deepEqual(select.children.map(node => node.value), ['']);
    assert.equal(get('include-evidence').disabled, true);
  } else if (scenario === 'nulls') {
    for (const value of [null, undefined]) {
      data.task_count = value; data.controller.task_count = value;
      data.state.current_task = null; data.summary.current = null; render();
      assert.equal(get('run-task').textContent, '-- / --');
    }
    data.task_count = 0; data.summary.current = 0; render();
    assert.equal(get('run-task').textContent, '0 / 0', 'A real zero must survive null fallback');
  } else if (scenario === 'text') {
    const payload = '<img src=x onerror=globalThis.injected=true>';
    data.controller.plan = payload; data.controller.selection_error = payload;
    data.files = [{path:payload, status:'available'}]; render();
    assert.equal(get('plan-path').textContent, payload);
    assert(get('progress-error').textContent.includes(payload));
    const option = get('export-evidence').children[1];
    assert.equal(option.textContent, payload);
    assert.equal(option.children.length, 0, 'Payload must remain text, not elements');
    assert.equal(vm.runInContext('globalThis.injected', context), undefined);
  } else { throw Error('Unknown reader scenario: ' + scenario); }
  console.log('Reader scenario passed: ' + scenario);
})().catch(error => { console.error(error); process.exitCode = 1; });
