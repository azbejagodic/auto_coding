// Render real bridge snapshots with the actual embedded dashboard JavaScript.
// No browser, network, simulated approval flags or replacement render functions.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const fixture = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
class Element {
  constructor(tag) { this.tag = tag; this.children = []; this._text = ''; this.value = ''; }
  set textContent(value) { this._text = String(value); this.children = []; }
  get textContent() { return this._text + this.children.map(child => child.textContent).join(''); }
  set innerHTML(value) { throw Error('Unsafe log HTML'); }
  append(...items) { this.children.push(...items); }
  replaceChildren(...items) { this.children = items; this._text = ''; }
  setAttribute(name, value) { this[name] = value; }
  addEventListener() {}
  contains(target) { return this === target || this.children.some(child => child.contains?.(target)); }
}
const nodes = new Map();
const get = id => { if (!nodes.has(id)) nodes.set(id, new Element(id)); return nodes.get(id); };
get('attempts').value = '3';
get('audit-model').value = 'fixture-auditor';
const document = {getElementById: get, createElement: tag => new Element(tag),
  createTextNode: text => ({textContent: String(text)}), addEventListener() {}};
let snapshot = fixture.snapshots[0];
let poll;
const listeners = {};
const context = vm.createContext({document,
  window: {pywebview: {api: {snapshot: async () => structuredClone(snapshot)}},
    addEventListener: (name, callback) => { listeners[name] = callback; }},
  setInterval: (callback, milliseconds) => { if (milliseconds === 4000) poll = callback; }});
vm.runInContext(fixture.script, context);
const flush = () => new Promise(resolve => setImmediate(resolve));
(async () => {
  await listeners.pywebviewready();
  await flush();
  assert(get('start').disabled);
  snapshot = fixture.snapshots[1];
  await poll();
  await flush();
  assert.equal(get('audit-result').textContent, '\u2713 Approved');
  assert.equal(get('audit-result').className, 'pill green');
  assert.equal(get('start').disabled, false);
  snapshot = fixture.snapshots[2];
  await poll();
  await flush();
  assert.equal(get('run-status').textContent, 'Completed');
  assert.equal(get('run-task').textContent, '3 / 3');
  assert.equal(get('run-time').textContent, '00:00:40');
  assert(get('start').disabled);
})().catch(error => { console.error(error); process.exitCode = 1; });
