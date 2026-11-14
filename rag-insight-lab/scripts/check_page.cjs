// Offline page-script contract checks. Synthetic fixtures, no browser or network.
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const root = path.resolve(__dirname, '..');
const fixtures = process.argv[2] ? path.resolve(process.argv[2]) : path.join(root, 'tests/fixtures');
class Element {
  constructor(tag = 'div') {
    this.tag = tag; this.children = []; this._text = ''; this.value = '';
    this.classList = {toggle() {}};
  }
  set textContent(text) { this._text = String(text); this.children = []; }
  get textContent() { return this._text + this.children.map(c => c.textContent).join(' '); }
  set innerHTML(text) { this.textContent = text; }
  get innerHTML() { return this.textContent; }
  appendChild(child) { this.children.push(child); return child; }
  replaceChildren(...children) { this._text = ''; this.children = children; }
}
const html = fs.readFileSync(path.join(root, 'web/index.html'), 'utf8');
const code = html.split('<script>')[1].split('</script>')[0];
async function run(file, status) {
  const payload = JSON.parse(fs.readFileSync(path.join(fixtures, file), 'utf8'));
  if (status) payload.status = status;
  const nodes = new Map();
  const get = id => {
    if (!nodes.has(id)) nodes.set(id, new Element());
    return nodes.get(id);
  };
  get('service').value = 'action-lab-api'; get('window').value = '5';
  get('question').value = 'synthetic fixture'; get('qtype').value = 'window_requests';
  const ctx = vm.createContext({
    document: {getElementById: get, createElement: tag => new Element(tag)},
    fetch: async () => ({ok: true, json: async () => payload}),
    alert: message => { throw Error(message); },
  });
  vm.runInContext(code, ctx);
  vm.runInContext("token='synthetic-fixture-placeholder-not-a-credential'", ctx);
  await vm.runInContext("$('run').onclick()", ctx);
  return {facts: get('facts').textContent, hint: get('runhint').textContent, meta: get('meta').textContent};
}
(async () => {
  const c1 = await run('analysis-latency.json');
  assert(c1.facts.includes('model.chat'), 'stage rows');
  assert(c1.facts.includes('阶段口径'), 'stage note');
  assert(c1.meta.includes('编号已登记') && c1.meta.includes('支持关系待核对'), 'citation semantics');
  const c5 = await run('analysis-windows.json');
  assert(c5.facts.includes('窗口 A') && c5.facts.includes('窗口 B'), 'windows');
  assert(c5.facts.includes('观测增量 3 次') && c5.facts.includes('观测增量 4 次'), 'deltas');
  assert(c5.facts.includes('16 条序列'), 'series');
  const truncated = await run('analysis-latency.json', 'truncated');
  assert(truncated.hint.includes('分析未正常完成') && !truncated.hint.includes('返回成功'), 'non-ok status');
  console.log(JSON.stringify({synthetic_fixtures: true, c1_stage_fields: true,
    c5_window_fields: true, non_ok_status_visible: true, citation_semantics_label: true}));
})().catch(error => { console.error(error.stack); process.exitCode = 1; });
