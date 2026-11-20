const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const { parseHTML } = require('linkedom');

function app() {
  const { document } = parseHTML('<html><body><button id="clean-toggle"></button><div id="content"></div><div id="session-list"></div></body></html>');
  const timers = new Map();
  let sequence = 0;
  const context = vm.createContext({ document, window: { innerHeight: 800, scrollY: 0, scrollTo() {}, matchMedia: () => ({ matches: false }) },
    setTimeout: fn => { timers.set(++sequence, fn); return sequence; }, clearTimeout: id => timers.delete(id),
  });
  document.defaultView.HTMLElement.prototype.scrollIntoView = () => {};
  const source = fs.readFileSync('src/mockinterview/web_static/app.js', 'utf8');
  vm.runInContext(source.slice(0, source.indexOf("$('#login-form').addEventListener")), context);
  const run = code => vm.runInContext(code, context);
  run("state.current='test'; state.view='detail'; state.detail={session:{},messages:[],pending:{ask:'问题'}}; render()");
  return { run, document, flush() { for (let i = 0; timers.size && i < 10000; i++) { const [id, fn] = timers.entries().next().value; timers.delete(id); fn(); } } };
}
const msg = (role, text, anchor = 'q:1:r0') => ({ role, text, anchor, label: role });
function append(a, items) { a.run(`applyAppend(${JSON.stringify({ items })})`); }

test('normal/clean modes preserve nodes, live typing and drafts', () => {
  const a = app(); append(a, [msg('system','会话已创建'), msg('interviewer','主问：如何保证并发安全？')]);
  const node = a.document.querySelector('.interviewer');
  a.document.querySelector('#answer-input').value = '草稿';
  assert(a.document.querySelector('.system').hidden);
  a.run('state.cleanMode=false; render()');
  assert.equal(a.document.querySelector('.interviewer'), node);
  assert.equal(a.document.querySelector('#answer-input').value, '草稿');
  assert(!a.document.querySelector('.system').hidden);
  a.flush(); assert.equal(node.querySelector('.msg-body').textContent, '主问：如何保证并发安全？');
});

test('submitting retains waiting history and acknowledges the same candidate node', () => {
  const a = app(); append(a, [msg('interviewer','问题'), msg('system','等待候选人作答','q:1:r0:wait')]);
  a.run("appendOptimisticCandidate('回答')");
  const candidate = a.document.querySelector('.candidate');
  assert(a.document.querySelector('#stream').textContent.includes('等待候选人作答'));
  append(a, [msg('candidate','回答'), msg('system','已提交','q:1:r0:submit')]);
  assert.equal(a.document.querySelectorAll('.candidate').length, 1);
  assert.equal(a.document.querySelector('.candidate'), candidate);
});

test('truncate and replay reuse finalized streaming node without duplicates', () => {
  const a = app(); append(a, [msg('system','开始','start')]);
  a.run(`applyQStream({kind:'delta', text:'逐字输出的问题', anchor:'q:1:r0'})`);
  const node = a.document.querySelector('.interviewer');
  append(a, [msg('interviewer','逐字输出的问题')]);
  assert.equal(a.document.querySelector('.interviewer'), node);
  a.run('applyTruncate({keep:1})');
  assert.equal(a.document.querySelector('.interviewer'), node);
  append(a, [msg('system','插入系统事件','event'),msg('interviewer','逐字输出的问题')]);
  a.flush();
  assert.equal(a.document.querySelectorAll('.interviewer').length, 1);
  assert.equal(a.document.querySelector('.interviewer'), node);
  assert.equal(node.querySelector('.msg-body').textContent,'逐字输出的问题');
});

test('late tokens and qopen after final snapshot do not create ghost messages', () => {
  const a = app(); append(a, [msg('system','第 1 题','q:1:open'),msg('interviewer','问题')]);
  a.run("applyQOpen({anchor:'q:1:open',index:1,total:1}); applyQStream({anchor:'q:1:r0',kind:'delta',text:'迟到'})");
  assert.equal(a.document.querySelectorAll('.interviewer').length,1);
  assert.equal(a.document.querySelectorAll('.q-divider').length,1);
});

test('question divider precedes existing loading indicator', () => {
  const a = app(); a.run("appendOptimisticInterviewer(); applyQOpen({index:1,total:2,topic:'并发'})");
  const children = a.document.querySelector('#stream').children;
  assert(children[children.length-2].classList.contains('q-divider'));
  assert(children[children.length-1].classList.contains('interviewer'));
});

test('stream reset cancels queued text and new question can stream after earlier finalization', () => {
  const a = app(); append(a,[msg('interviewer','上一问')]);
  a.run("applyQStream({anchor:'q:1:r1',kind:'delta',text:'需要撤销的很长一段内容'}); applyQStream({anchor:'q:1:r1',kind:'reset'}); applyQStream({anchor:'q:1:r1',kind:'delta',text:'新的题面'})");
  a.flush();
  assert.equal(a.document.querySelectorAll('.interviewer')[1].querySelector('.msg-body').textContent,'新的题面');
});

test('final label preserves already streamed characters instead of replaying the question', () => {
  const a = app(); a.run("applyQStream({anchor:'q:1:r0',kind:'delta',text:'已经输出的题目正文'})"); a.flush();
  const node = a.document.querySelector('.interviewer');
  append(a,[msg('interviewer','主问1：已经输出的题目正文')]);
  assert.equal(node.querySelector('.msg-body').textContent,'主问1：已经输出的题目正文');
});

test('snapshot reconciliation keeps streaming question and mode toggle does not restart it', () => {
  const a = app(); a.run("applyQStream({anchor:'q:1:r0',kind:'delta',text:'完整的问题文字'})");
  const node = a.document.querySelector('.interviewer');
  a.run('render(); state.cleanMode=false; render()');
  assert.equal(a.document.querySelector('.interviewer'),node);
  a.flush(); assert.equal(node.querySelector('.msg-body').textContent,'完整的问题文字');
});

test('previous question closing cannot consume the next question streaming node', () => {
  const a = app(); a.run("applyQStream({anchor:'q:2:r0',kind:'delta',text:'第二题正文'})");
  const node = a.document.querySelector('.interviewer');
  append(a,[msg('interviewer','收尾：第一题结束','q:1:close')]);
  assert.equal(a.document.querySelectorAll('.interviewer')[1], node);
  append(a,[msg('interviewer','主问2：第二题正文','q:2:r0')]); a.flush();
  assert.equal(a.document.querySelectorAll('.interviewer').length,2);
  assert.equal(node.querySelector('.msg-body').textContent,'主问2：第二题正文');
});

test('waiting hint appears only after its question finishes typing, including mode switches', () => {
  const a = app(); a.run('state.cleanMode=false');
  a.run("applyQStream({anchor:'q:1:r0',kind:'delta',text:'需要逐字显示的完整题目正文'})");
  append(a,[msg('interviewer','主问1：需要逐字显示的完整题目正文'),msg('system','等待候选人作答','q:1:r0:wait')]);
  const wait = a.document.querySelector('.system');
  assert(wait.hidden);
  a.run('state.cleanMode=true; render(); state.cleanMode=false; render()');
  assert(wait.hidden);
  a.flush();
  assert(!wait.hidden);
  assert.equal(a.document.querySelector('.interviewer .msg-body').textContent,'主问1：需要逐字显示的完整题目正文');
  a.run('state.cleanMode=true; render()');
  assert(wait.hidden);
});

test('historical waiting hints and completed earlier rounds remain visible', () => {
  const a = app(); a.run('state.cleanMode=false');
  append(a,[msg('interviewer','第一问'),msg('system','等待候选人作答','q:1:r0:wait')]);
  a.flush();
  append(a,[msg('interviewer','第二问仍在逐字输出','q:1:r1'),msg('system','等待候选人作答','q:1:r1:wait')]);
  const waits = a.document.querySelectorAll('.system');
  assert(!waits[0].hidden);
  assert(waits[1].hidden);
  a.flush(); assert(!waits[1].hidden);
  const b = app(); b.run(`state.cleanMode=false; state.detail.messages=${JSON.stringify([msg('interviewer','历史问题'),msg('system','等待候选人作答','q:1:r0:wait')])}; syncStream(false)`);
  assert(!b.document.querySelector('.system').hidden);
});

test('legacy waiting hint waits for the nearest preceding question', () => {
  const a = app(); a.run('state.cleanMode=false');
  append(a,[msg('interviewer','兼容旧会话的完整问题'),msg('system','等待候选人作答','end')]);
  assert(a.document.querySelector('.system').hidden);
  a.flush(); assert(!a.document.querySelector('.system').hidden);
});

test('scroll following ignores the hidden waiting hint', () => {
  const a = app(); a.run('state.cleanMode=false');
  append(a,[msg('interviewer','还在输出的题目正文'),msg('system','等待候选人作答','q:1:r0:wait')]);
  let target;
  const question = a.document.querySelector('.interviewer');
  const wait = a.document.querySelector('.system');
  question.scrollIntoView = () => { target = 'question'; };
  wait.scrollIntoView = () => { target = 'wait'; };
  a.run('followBottom()'); assert.equal(target,'question');
  a.flush(); a.run('followBottom()'); assert.equal(target,'wait');
});

test('reduced motion shows the waiting hint immediately after the complete question', () => {
  const a = app(); a.run("state.cleanMode=false; window.matchMedia=()=>({matches:true})");
  append(a,[msg('interviewer','完整题目'),msg('system','等待候选人作答','q:1:r0:wait')]);
  assert.equal(a.document.querySelector('.interviewer .msg-body').textContent,'完整题目');
  assert(!a.document.querySelector('.system').hidden);
});
