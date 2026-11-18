const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

function app() {
  const nodes = { '#content': { innerHTML: '' }, '#answer-input': { value: '还没有提交的回答' } };
  const context = vm.createContext({
    document: { querySelector: selector => nodes[selector] || {} },
  });
  const source = fs.readFileSync('src/mockinterview/web_static/app.js', 'utf8');
  vm.runInContext(source.slice(0, source.indexOf("$('#login-form').addEventListener")), context);
  return { run: code => vm.runInContext(code, context), nodes };
}

const detail = {
  session: { topic: '并发', num_questions: 3, tokens: 100 },
  messages: [
    { role: 'system', label: '系统', text: '快评决策：继续追问' },
    { role: 'interviewer', label: '面试官', text: '如何保证并发安全？' },
    { role: 'candidate', label: '候选人', text: '使用锁' },
  ],
  pending: { index: 0, total: 3, topic: '并发', hop: 0 },
};

test('clean mode defaults on and preserves dialogue, metadata and answer controls', () => {
  const { run } = app();
  const html = run(`renderDetail(${JSON.stringify(detail)})`);
  assert(!html.includes('快评决策'));
  assert(!html.includes('metrics-table'));
  assert(html.includes('如何保证并发安全？'));
  assert(html.includes('使用锁'));
  assert(html.includes('主题<b>并发</b>'));
  assert(html.includes('Token<b>100</b>'));
  assert(html.includes('id="answer-input"'));
  assert(html.includes('aria-checked="true"'));
  run('state.cleanMode = false');
  const full = run(`renderDetail(${JSON.stringify(detail)})`);
  assert(full.includes('快评决策'));
  assert(full.includes('aria-checked="false"'));
});

test('clean mode keeps waiting, error and empty answer confirmation states', () => {
  const { run } = app();
  const busy = run(`renderDetail(${JSON.stringify({ ...detail, busy: true })})`);
  assert(busy.includes('面试官正在思考'));
  assert(!busy.includes('id="stage-line"'));
  run("state.error = '提交失败，请重试'; state.emptyConfirm = true");
  const pending = run(`renderDetail(${JSON.stringify(detail)})`);
  assert(pending.includes('提交失败，请重试'));
  assert(pending.includes('确认提交空回答'));
});

test('rerendering after a mode change preserves the unsent draft', () => {
  const { run, nodes } = app();
  run(`state.detail = ${JSON.stringify(detail)}; state.view = 'detail'; state.cleanMode = false; render()`);
  assert.equal(nodes['#answer-input'].value, '还没有提交的回答');
  assert(nodes['#content'].innerHTML.includes('快评决策'));
  run('state.cleanMode = true; render()');
  assert.equal(nodes['#answer-input'].value, '还没有提交的回答');
  assert(!nodes['#content'].innerHTML.includes('快评决策'));
});
