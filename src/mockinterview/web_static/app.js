const $ = (selector) => document.querySelector(selector);
const content = $('#content');
const sessionList = $('#session-list');
const cleanToggle = $('#clean-toggle');
const CLEAN_KEY = 'mockinterview.cleanMode';

// 纯净模式是全局显示偏好，放在头部；选择记在 localStorage 里，
// 换一场面试、刷新页面都保持上次的状态。
function readCleanMode() {
  try {
    const saved = localStorage.getItem(CLEAN_KEY);
    return saved === null ? true : saved === '1';
  } catch (_) {
    return true;
  }
}

function writeCleanMode(value) {
  try {
    localStorage.setItem(CLEAN_KEY, value ? '1' : '0');
  } catch (_) {
    // 隐私模式等场景写不了，忽略即可，本次会话内依然生效。
  }
}

function syncCleanToggle() {
  if (!cleanToggle) return;
  const on = Boolean(state.cleanMode);
  cleanToggle.setAttribute('aria-checked', String(on));
  cleanToggle.title = on
    ? '纯净模式：只显示面试官与你的对话'
    : '纯净模式已关闭：显示系统提示与运行统计';
}

const state = {
  user: null,
  pass: null,
  readOnly: false,
  authVersion: 0,
  deleteTarget: null,
  deleting: false,
  topics: [],
  sessions: [],
  current: null,
  view: 'empty',
  detail: null,
  topicsLoaded: false,
  error: '',
  emptyConfirm: false,
  clearDraft: false,
  cleanMode: readCleanMode(),
  reportHtml: '',
  reportFor: null,
  revision: null,
  stage: null,
  form: { select: '', custom: '', num: '', resume: '', jds: [] },
  // 增量信息流：保留消息与 DOM 的对应关系，重连、重排时复用已有节点。
  streamRendered: [],
  streamSession: null,
  // 提交回答后的乐观节点：候选人回复 + 面试官加载块，服务端数据到达后被取代。
  optimistic: { candidate: null, interviewer: null },
  // 真流式面试官块：qstream 事件驱动的增量上屏，随最终消息定稿或替换。
  qstream: null,
  // 定稿之后到达的迟到 delta 要丢弃；新一轮提交/恢复时复位。
  qstreamSettled: false,
  // 题目分隔线在出题 token 之前上屏；最终权威消息到达后换成定稿节点。
  openDivider: null,
  submitting: false,
  bottomSig: '',
  es: null,
};

function stageText() {
  const status = state.stage;
  if (!status || !status.stage) return '正在等待模型响应…';
  let text = `当前阶段：${status.stage} · 已用时 ${(status.stage_ms / 1000).toFixed(1)}s`;
  if (status.last_stage) {
    text += ` · 上一步「${status.last_stage.label}」用时 ${(status.last_stage.ms / 1000).toFixed(1)}s`;
  }
  return text;
}

function updateStageLine() {
  const node = $('#stage-line');
  if (node) node.textContent = stageText();
}

function escapeHtml(value) {
  return String(value == null ? '' : value)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function multiline(value) {
  return escapeHtml(value).replace(/\n/g, '<br>');
}

async function api(path, options = {}) {
  // Always fetch relative: the app may be served at root (/) or under a
  // subpath (/interview/) behind nginx, so absolute /api/... would escape
  // the prefix. Stripping the leading slash makes both deployments work.
  const target = path.startsWith('/') ? path.slice(1) : path;
  const response = await fetch(target, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  });
  if (response.status === 401 && !target.startsWith('api/login')) {
    // Pass expired or revoked mid-session: back to the login gate.
    window.dispatchEvent(new CustomEvent('auth-lost'));
  }
  const data = await response.json().catch(() => ({ detail: '响应不是 JSON' }));
  if (!response.ok) {
    const detail = data.detail;
    const message = detail && typeof detail === 'object'
      ? (detail.message || '请求未完成')
      : (detail || `请求失败 ${response.status}`);
    const error = new Error(message);
    error.status = response.status;
    error.data = detail;
    throw error;
  }
  return data;
}

function mdToHtml(markdown) {
  const lines = String(markdown || '').split('\n');
  const out = [];
  let code = [];
  let inCode = false;
  for (const line of lines) {
    if (line.startsWith('```')) {
      if (inCode) {
        out.push(`<pre><code>${escapeHtml(code.join('\n'))}</code></pre>`);
        code = [];
      }
      inCode = !inCode;
      continue;
    }
    if (inCode) { code.push(line); continue; }
    // Allow only disclosure tags; arbitrary HTML remains escaped.
    if (line === '<details>' || line === '</details>') { out.push(line); continue; }
    const summary = line.match(/^<summary>([^<>]*)<\/summary>$/);
    if (summary) { out.push(`<summary>${escapeHtml(summary[1])}</summary>`); continue; }
    if (!line.trim()) { out.push('<div class="md-gap"></div>'); continue; }
    let text = escapeHtml(line)
      .replace(/`([^`]+)`/g, '<code>$1</code>')
      .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
    if (text.startsWith('### ')) out.push(`<h3>${text.slice(4)}</h3>`);
    else if (text.startsWith('## ')) out.push(`<h2>${text.slice(3)}</h2>`);
    else if (text.startsWith('# ')) out.push(`<h1>${text.slice(2)}</h1>`);
    else if (text.startsWith('&gt; ')) out.push(`<blockquote>${text.slice(5)}</blockquote>`);
    else if (text.startsWith('  - ')) out.push(`<div class="md-sub">${text.slice(4)}</div>`);
    else if (text.startsWith('- ')) out.push(`<div class="md-item">${text.slice(2)}</div>`);
    else out.push(`<p>${text}</p>`);
  }
  if (code.length) out.push(`<pre><code>${escapeHtml(code.join('\n'))}</code></pre>`);
  return out.join('');
}

function notice(text, kind = 'info') {
  return `<div class="notice ${kind}">${escapeHtml(text)}</div>`;
}

function formatTime(iso) {
  if (!iso) return '—';
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return String(iso).slice(0, 19).replace('T', ' ');
  return date.toLocaleString('zh-CN', { hour12: false });
}

function formatMs(ms) {
  const value = Number(ms) || 0;
  return value >= 1000 ? `${(value / 1000).toFixed(2)} s` : `${value} ms`;
}

function formatTokens(value) {
  return (Number(value) || 0).toLocaleString('zh-CN');
}

function renderMetrics(detail) {
  const metrics = detail.metrics || [];
  const summary = detail.metrics_summary || {};
  const chips = [
    ['调用次数', summary.calls ?? metrics.length],
    ['累计耗时', formatMs(summary.total_ms ?? 0)],
    ['累计 Token', formatTokens(summary.total_tokens ?? 0)],
    ['失败调用', summary.failures ?? 0],
  ].map(([label, value]) =>
    `<span class="meta-chip">${escapeHtml(label)}<b>${escapeHtml(value)}</b></span>`
  ).join('');
  const intro = '<p class="card-sub">逐条来自 operations.sqlite 的 stage_metrics 表，记录每一次真实模型调用的耗时与 token，重启服务后仍可查；命中已有结果收据的重放不重复记录。无 usage 的失败请求 token 记为 0，不能替代账单。</p>';
  if (!metrics.length) {
    return `<div class="card">
      <h2>阶段耗时与 Token 用量</h2>
      ${intro}
      <div class="metrics-empty">暂无模型调用记录。</div>
    </div>`;
  }
  const rows = metrics.map((item) => {
    const failed = item.ok === false;
    const retries = item.attempts > 1
      ? `<span class="attempts">重试至第 ${item.attempts} 次</span>` : '';
    const result = failed
      ? `<span class="metric-fail">失败 · ${escapeHtml(item.error || '未知')}</span>`
      : '<span class="metric-ok">成功</span>';
    return `<tr class="${failed ? 'failed' : ''}">
      <td>${escapeHtml(formatTime(item.started_at))}</td>
      <td>${escapeHtml(item.label || '模型调用')}${retries}</td>
      <td class="num">${escapeHtml(formatMs(item.duration_ms ?? item.ms ?? 0))}</td>
      <td class="num">${escapeHtml(formatTokens(item.tokens))}</td>
      <td>${result}</td>
    </tr>`;
  }).join('');
  return `<div class="card">
    <h2>阶段耗时与 Token 用量</h2>
    ${intro}
    <div class="meta-grid">${chips}</div>
    <div class="metrics-scroll"><table class="metrics-table">
      <thead><tr><th>开始时间</th><th>环节</th><th>耗时</th><th>Token</th><th>结果</th></tr></thead>
      <tbody>${rows}</tbody>
    </table></div>
  </div>`;
}

function renderSessions() {
  if (!state.sessions.length) {
    sessionList.innerHTML = `<div class="empty-list">${state.readOnly ? '作者暂未添加示例面试，稍后再来看看。' : '还没有面试记录，点上方按钮新建一场。'}</div>`;
    return;
  }
  sessionList.innerHTML = state.sessions.map((item) => {
    const done = item.status === 'done';
    const badge = done ? '已完成' : '进行中';
    const progress = `${item.completed}/${item.num_questions || '—'}`;
    const when = String(item.updated_at || '').slice(5, 16).replace('T', ' ');
    return `<div class="session-row"><button class="session-item ${state.current === item.id && state.view === 'detail' ? 'active' : ''}"
      data-session="${escapeHtml(item.id)}" type="button">
      <span class="topic">${escapeHtml(item.topic || '未命名面试')}</span>
      <span class="sub"><span class="badge ${done ? 'done' : 'running'}">${badge}</span>
      <span class="meta">${progress} 题 · ${escapeHtml(when)}</span></span>
    </button>${state.readOnly ? '' : `<button class="delete-session" type="button" data-delete-session="${escapeHtml(item.id)}" aria-label="删除面试：${escapeHtml(item.topic || '未命名面试')}">删除</button>`}</div>`;
  }).join('');
}

function renderEmpty() {
  if (state.readOnly) return `<div class="card"><h2>浏览示例面试</h2>
    <p class="card-sub">从左侧选择一场面试，查看问答过程、追问和评分报告。</p>
    ${notice('当前为只读 guest 身份。新建面试或亲自作答，需要线下联系作者获取访问令牌。')}</div>`;
  return `<div class="card">
    <h2>开始一场模拟面试</h2>
    <p class="card-sub">逐题出题、动态追问、异步单题评分，最后生成 Markdown 报告。</p>
    ${notice('左侧可以新建面试，或选择一场历史记录继续 / 查看报告。')}
  </div>`;
}

function topicOptions() {
  const preset = state.topics.map((topic) =>
    `<option value="${escapeHtml(topic)}" ${state.form.select === topic ? 'selected' : ''}>${escapeHtml(topic)}</option>`
  ).join('');
  return `<option value="" ${!state.form.select ? 'selected' : ''}>请选择一个主题</option>${preset}`;
}

function renderNew() {
  const nums = Array.from({ length: 10 }, (_, i) => i + 1).map((n) =>
    `<option value="${n}" ${String(state.form.num) === String(n) ? 'selected' : ''}>${n} 题</option>`
  ).join('');
  const jds = state.form.jds.map((value, index) => `
    <div class="jd-item">
      <div class="jd-head"><span>目标 JD ${index + 1}</span>
        <button class="link-btn" type="button" data-action="remove-jd" data-index="${index}">移除</button></div>
      <textarea data-jd-index="${index}" placeholder="粘贴岗位描述，可以留空">${escapeHtml(value)}</textarea>
    </div>`).join('');
  return `<div class="card">
    <h2>新建面试</h2>
    <p class="card-sub">主题与题目数量为必填；简历和多个目标 JD 都是可选的。</p>
    ${state.error ? notice(state.error, 'error') : ''}
    <div class="field-row">
      <div class="field">
        <label for="topic-select">面试主题<span class="required">*</span></label>
        <select id="topic-select">${topicOptions()}</select>
      </div>
      <div class="field">
        <label for="num-select">题目数量<span class="required">*</span></label>
        <select id="num-select"><option value="" ${!state.form.num ? 'selected' : ''}>请选择题数</option>${nums}</select>
      </div>
    </div>
    <div class="field" style="margin-top:16px">
      <label for="topic-custom">自定义主题<span class="optional">填写后优先于上方选择</span></label>
      <input id="topic-custom" type="text" maxlength="60" placeholder="例如：Kafka 消费者重平衡" value="${escapeHtml(state.form.custom)}">
    </div>
    <div class="field">
      <label for="resume-input">候选人简历<span class="optional">可选，会随答题内容发送给模型服务</span></label>
      <textarea id="resume-input" placeholder="可留空">${escapeHtml(state.form.resume)}</textarea>
    </div>
    <div class="field">
      <label>目标 JD<span class="optional">可选，最多 5 份</span></label>
      ${jds || '<p class="field-tip">尚未添加 JD。留空时按主题出通用假设题。</p>'}
      ${state.form.jds.length < 5 ? '<button class="subtle" type="button" data-action="add-jd" style="margin-top:8px">添加一份 JD</button>' : ''}
    </div>
    <div class="form-actions">
      <button class="primary" type="button" data-action="create">开始面试</button>
      <button class="ghost" type="button" data-action="cancel">取消</button>
    </div>
  </div>`;
}

// ---------- 信息流：DOM 节点构建、增量同步、打字机 ----------

function sameMsg(a, b) {
  return a && b && a.role === b.role && a.label === b.label
    && a.text === b.text && a.anchor === b.anchor;
}

function msgNode(message, animate) {
  const anchor = typeof message.anchor === 'string' ? message.anchor : '';
  const el = document.createElement('div');
  if (anchor.endsWith(':open')) {
    el.className = 'q-divider' + (animate ? ' enter' : '');
    el.innerHTML = `<span>${escapeHtml(message.text || '')}</span>`;
    return el;
  }
  const role = ['system', 'interviewer', 'candidate'].includes(message.role)
    ? message.role : 'system';
  el.hidden = state.cleanMode && role === 'system';
  el.className = `msg ${role}` + (animate ? ' enter' : '');
  el.dataset.text = message.text || '';
  el.innerHTML = `<div class="msg-role">${escapeHtml(message.label)}</div>
    <div class="msg-body">${multiline(message.text)}</div>`;
  return el;
}

function removeOptimisticNodes() {
  if (state.optimistic.candidate) {
    state.optimistic.candidate.node.remove();
    state.optimistic.candidate = null;
  }
  if (state.optimistic.interviewer) {
    state.optimistic.interviewer.node.remove();
    state.optimistic.interviewer = null;
  }
  if (state.qstream) {
    clearTimeout(state.qstream.node.typingTimer);
    state.qstream.node.remove();
    state.qstream = null;
  }
  if (state.openDivider) {
    state.openDivider.node.remove();
    state.openDivider = null;
  }
}

function loadingBodyHtml() {
  return `<span class="dots" aria-hidden="true"><i></i><i></i><i></i></span>
    <span class="loading-hint">面试官正在出题…</span>`;
}

// 每个节点只有一个输出队列；定稿仅更新目标文本，不跳过尚未显示的文字。
function typeTo(node, text, final = false) {
  const body = node.querySelector('.msg-body');
  if (!body) return;
  const previousTarget = node.dataset.text || '';
  if (final && previousTarget && text.endsWith(previousTarget) && node.shownText !== undefined) {
    node.shownText = text.slice(0, text.length - previousTarget.length) + node.shownText;
  }
  node.dataset.text = text;
  node.typingFinal = final;
  if (node.shownText === undefined) node.shownText = '';
  if (!text.startsWith(node.shownText)) node.shownText = '';
  if (window.matchMedia?.('(prefers-reduced-motion: reduce)').matches) {
    node.shownText = text;
    body.textContent = text;
    body.classList.remove('typing');
    syncMessageVisibility();
    return;
  }
  if (node.typingTimer) return;
  const tick = () => {
    const follow = nearBottom();
    const target = node.dataset.text || '';
    const chars = Array.from(target);
    const count = Array.from(node.shownText).length;
    node.shownText = chars.slice(0, count + Math.max(1, Math.ceil((chars.length - count) / 80))).join('');
    body.textContent = node.shownText;
    body.classList.toggle('typing', node.shownText !== target || !node.typingFinal);
    if (node.shownText !== target) node.typingTimer = setTimeout(tick, 24);
    else {
      node.typingTimer = null;
      syncMessageVisibility();
    }
    if (follow) followBottom();
  };
  tick();
}

function nearBottom() {
  const stream = $('#stream');
  const bottom = stream?.getBoundingClientRect().bottom;
  return bottom >= 0 && bottom <= window.innerHeight + 160;
}

function followBottom() {
  const stream = $('#stream');
  const lastVisible = stream && Array.from(stream.children).reverse().find(node => !node.hidden);
  lastVisible?.scrollIntoView({ block: 'nearest', behavior: 'instant' });
}

// 服务端进入等待状态时，浏览器可能还有文字没显示完。
// 等待提示跟随对应回合的显示进度，不能只看服务端 pending 状态。
function syncMessageVisibility() {
  const entries = state.streamRendered;
  for (let index = 0; index < entries.length; index++) {
    const { message, node } = entries[index];
    let waitingForText = false;
    if (message.role === 'system' && message.text === '等待候选人作答') {
      const anchor = message.anchor?.replace(/:wait$/, '');
      const question = anchor && anchor !== 'end'
        ? entries.find(entry => entry.message.role === 'interviewer' && entry.message.anchor === anchor)
        : entries.slice(0, index).reverse().find(entry => entry.message.role === 'interviewer');
      const source = question?.node;
      waitingForText = source?.shownText !== undefined
        && (!source.typingFinal || source.shownText !== source.dataset.text);
    }
    node.hidden = Boolean(waitingForText || (state.cleanMode
      && message.role === 'system' && !message.anchor?.endsWith(':open')));
  }
}

function syncStream(animate) {
  const stream = $('#stream');
  if (!stream || !state.detail) return;
  const follow = nearBottom();
  stream.querySelectorAll('.loading').forEach(node => node.remove());
  const old = state.streamSession === state.current ? [...state.streamRendered] : [];
  const entries = [];
  for (const message of state.detail.messages || []) {
    const index = old.findIndex(entry => sameMsg(entry.message, message));
    let node = index >= 0 ? old.splice(index, 1)[0].node : null;
    if (!node && message.role === 'candidate' && state.optimistic.candidate?.text === message.text) {
      node = state.optimistic.candidate.node;
      state.optimistic.candidate = null;
    }
    if (!node && message.anchor?.endsWith(':open') && state.openDivider?.text === message.text) {
      node = state.openDivider.node;
      state.openDivider = null;
    }
    if (!node && message.role === 'interviewer') {
      if (state.qstream && (!state.qstream.anchor || state.qstream.anchor === message.anchor)) {
        node = state.qstream.node;
        state.qstream = null;
      }
      if (state.optimistic.interviewer && !message.anchor?.endsWith(':close')) {
        state.optimistic.interviewer.node.remove();
        state.optimistic.interviewer = null;
      }
      state.qstreamSettled = true;
      if (!node) node = msgNode(message, animate);
      if (animate) typeTo(node, message.text, true);
      else if (node.shownText !== undefined) typeTo(node, message.text, true);
    }
    if (!node) node = msgNode(message, animate);
    node.hidden = state.cleanMode && message.role === 'system' && !message.anchor?.endsWith(':open');
    entries.push({ message, node });
  }
  for (const entry of old) {
    clearTimeout(entry.node?.typingTimer);
    entry.node?.remove();
  }
  const ordered = entries.map(entry => entry.node).concat([
    state.optimistic.candidate?.node, state.openDivider?.node,
    state.qstream?.node, state.optimistic.interviewer?.node,
  ].filter(Boolean));
  ordered.forEach((node, index) => {
    if (stream.children[index] !== node) stream.insertBefore(node, stream.children[index] || null);
  });
  state.streamRendered = entries;
  state.streamSession = state.current;
  syncMessageVisibility();
  if (!stream.children.length) stream.innerHTML = '<div class="loading">正在准备题目…</div>';
  if (animate && follow) followBottom();
}

// 截断和追加是一对同步消息：保留节点到新列表到达，再一次性核对。
function applyTruncate(payload) {
  if (state.view !== 'detail' || !state.detail) return;
  state.detail.messages = (state.detail.messages || []).slice(0, Math.max(0, Number(payload.keep) || 0));
}

function appendOptimisticCandidate(answer) {
  const stream = $('#stream');
  if (!stream) return;
  stream.querySelectorAll('.loading').forEach((node) => node.remove());
  const text = answer || '（已提交空回答）';
  const node = document.createElement('div');
  node.className = 'msg candidate enter';
  node.innerHTML = `<div class="msg-role">候选人</div>
    <div class="msg-body">${multiline(text)}</div>`;
  stream.appendChild(node);
  node.scrollIntoView({ block: 'nearest' });
  state.optimistic.candidate = { node, text };
}

function appendOptimisticInterviewer() {
  const stream = $('#stream');
  if (!stream) return;
  const node = document.createElement('div');
  node.className = 'msg interviewer enter';
  node.innerHTML = `<div class="msg-role">面试官</div>
    <div class="msg-body">${loadingBodyHtml()}</div>`;
  stream.appendChild(node);
  if (nearBottom()) node.scrollIntoView({ block: 'nearest' });
  state.optimistic.interviewer = { node };
}

// 分隔线先于题面 token 上屏：节点返回前 checkpoint 里还没有这道题，
// 等 build_messages 的权威消息到达，syncStream 会用同样的文本换掉这个临时节点。
function applyQOpen(payload) {
  if (state.view !== 'detail') return;
  if (payload.anchor && state.detail?.messages.some(m => m.anchor === payload.anchor)) return;
  const stream = $('#stream');
  if (!stream) return;
  // 上一题的收尾消息也会置 qstreamSettled=true；新题开流前必须复位，
  // 否则第二题的 token 会被当成迟到增量全部丢掉。
  state.qstreamSettled = false;
  if (state.openDivider) state.openDivider.node.remove();
  const text = `第 ${payload.index}/${payload.total} 题 · ${payload.topic || ''} · 难度 ${payload.difficulty || '—'}`;
  const node = document.createElement('div');
  node.className = 'q-divider';
  node.innerHTML = `<span>${escapeHtml(text)}</span>`;
  stream.insertBefore(node, state.optimistic.interviewer?.node || null);
  state.openDivider = { node, text };
  // 首题没有快评事件：分隔线出现后就挂 loading，保留到首个 token。
  // 后续主问题若快评已经挂过块，这里直接复用，不造第二个。
  if (!state.optimistic.interviewer && !state.qstream) appendOptimisticInterviewer();
}

// 真流式：模型 token 增量直达。加载态保留到首个 token，之后逐段上屏；
// reset（格式错误重试）清空回加载态。定稿由 syncStream 在最终消息到达时完成。
function applyQStream(payload) {
  if (state.view !== 'detail') return;
  if (payload.anchor && state.detail?.messages.some(m => m.role === 'interviewer' && m.anchor === payload.anchor)) return;
  if (payload.anchor) state.qstreamSettled = false;
  if (payload.kind === 'reset') {
    state.qstreamSettled = false;
    if (state.qstream) {
      clearTimeout(state.qstream.node.typingTimer);
      state.qstream.node.typingTimer = null;
      state.qstream.node.shownText = '';
      state.qstream.text = '';
      const body = state.qstream.node.querySelector('.msg-body');
      if (body) {
        body.classList.remove('typing');
        body.innerHTML = loadingBodyHtml();
      }
    }
    return;
  }
  if (payload.kind !== 'delta') return;
  // 定稿后到达的迟到增量直接丢弃：这一题已经展示完毕。
  if (state.qstreamSettled) return;
  if (!state.qstream) {
    const stream = $('#stream');
    if (!stream) return;
    stream.querySelectorAll('.loading').forEach((node) => node.remove());
    if (state.optimistic.interviewer) {
      // 提升：乐观加载块直接变成流式块，避免出现两个面试官块。
      state.qstream = { node: state.optimistic.interviewer.node, text: '' };
      state.optimistic.interviewer = null;
    } else {
      const node = document.createElement('div');
      node.className = 'msg interviewer';
      node.innerHTML = `<div class="msg-role">面试官</div><div class="msg-body"></div>`;
      stream.appendChild(node);
      state.qstream = { node, text: '' };
    }
  }
  state.qstream.anchor = payload.anchor || state.qstream.anchor;
  state.qstream.text += payload.text || '';
  typeTo(state.qstream.node, state.qstream.text);
}

// ---------- 详情视图：head / stream / bottom / metrics 四个分区 ----------

function renderHead() {
  const node = $('#detail-head');
  if (!node) return;
  const session = (state.detail && state.detail.session) || {};
  const chips = [
    ['主题', session.topic || '综合'],
    ['题数', session.num_questions || '—'],
    ['进度', `${session.completed || 0}/${session.num_questions || '—'}`],
    ['状态', state.detail?.busy ? '处理中' : session.status || '—'],
    ['JD', `${session.jd_count || 0} 份`],
    ['简历', session.has_resume ? '已提供' : '未提供'],
    ['Token', session.tokens || 0],
  ].map(([label, value]) =>
    `<span class="meta-chip">${escapeHtml(label)}<b>${escapeHtml(value)}</b></span>`
  ).join('');
  node.innerHTML = `<h2>面试对话</h2>
    ${state.cleanMode ? '' : '<p class="card-sub">展示完整系统提示、对话及运行统计。</p>'}
    <div class="meta-grid">${chips}</div>`;
}

function answerBoxHtml(locked) {
  const stageLine = state.cleanMode ? '' : `<div class="stage-line" id="stage-line">${escapeHtml(stageText())}</div>`;
  if (locked) {
    // 只锁提交按钮；编辑框保持可交互，候选人可以预写下一轮草稿。
    return `<div class="card answer-box locked">
      <h2>已提交，等待面试官</h2>
      ${state.error ? notice(state.error, 'error') : ''}
      <textarea id="answer-input" placeholder=""></textarea>
      <div class="answer-actions">
        <button class="primary" type="button" disabled>等待面试官…</button>
        <span class="answer-hint">面试官出题后会自动流式显示在对话里。</span>
      </div>
      ${stageLine}
    </div>`;
  }
  return `<div class="card answer-box">
    <h2>继续作答</h2>
    ${state.error ? notice(state.error, 'error') : ''}
    ${state.emptyConfirm ? notice('答案内容为空。再次点击「确认提交空回答」才会提交。', 'warn') : ''}
    <textarea id="answer-input" placeholder="在这里输入回答；空白提交需要二次确认"></textarea>
    <div class="answer-actions">
      <button class="primary" type="button" data-action="submit-answer">
        ${state.emptyConfirm ? '确认提交空回答' : '提交回答'}</button>
      <span class="answer-hint">Ctrl / ⌘ + Enter 提交 · 未提交的草稿不会保存。</span>
    </div>
  </div>`;
}

function workingCardHtml() {
  return `${state.cleanMode ? '' : notice('面试官正在思考，结束后页面会自动刷新。')}
    <div class="card">
      <div class="working" role="status" aria-live="polite">
        <span class="spinner" aria-hidden="true"></span>
        <span class="working-text">${state.cleanMode ? '面试官正在思考，请稍候…' : '请稍候，模型调用可能持续数十秒。'}</span>
      </div>
      ${state.cleanMode ? '' : `<div class="stage-line" id="stage-line">${escapeHtml(stageText())}</div>`}
    </div>`;
}

function scoringCardHtml() {
  const stageLine = state.cleanMode ? '' : `<div class="stage-line" id="stage-line">${escapeHtml(stageText())}</div>`;
  return `<div class="card">
    <div class="working" role="status" aria-live="polite">
      <span class="spinner" aria-hidden="true"></span>
      <span class="working-text">本题已结束，正在等待单题评分完成…</span>
    </div>
    ${stageLine}
  </div>`;
}

function bottomSignature() {
  const detail = state.detail || {};
  const session = detail.session || {};
  return JSON.stringify([
    Boolean(detail.done), state.submitting, Boolean(detail.busy),
    Boolean(detail.pending), detail.pending ? detail.pending.ask : '',
    state.cleanMode, state.readOnly, state.error, state.emptyConfirm, detail.scoring_pending ? 1 : 0,
    `${session.completed || 0}/${session.num_questions || 0}`,
  ]);
}

function renderBottom() {
  const host = $('#detail-bottom');
  if (!host) return;
  // 分区重建不经过 render()：草稿在这里自己保留（提交后的清空靠 clearDraft）。
  const previous = $('#answer-input');
  const draft = previous ? previous.value : null;
  const focused = previous && document.activeElement === previous;
  const selection = focused ? [previous.selectionStart, previous.selectionEnd] : null;
  const keepDraft = state.clearDraft ? null : draft;
  const detail = state.detail || {};
  const session = detail.session || {};
  // 最后一题答完：不会再有下一题，等待区换成评分进行中的卡片，
  // 不再摆一个永远不会被用到的输入框。
  const lastDone = Boolean(session.num_questions)
    && (session.completed || 0) >= session.num_questions;
  let html;
  if (detail.done) {
    html = `${notice('这场面试已完成，不能继续作答；以下为已保存的报告。', 'warn')}
      <div class="card"><h2>面试报告</h2>
        <div class="report" id="report-body">${state.reportFor === state.current
          ? state.reportHtml : '<div class="loading">正在读取报告…</div>'}</div>
      </div>`;
  } else if (state.readOnly) {
    html = notice(detail.busy || detail.scoring_pending
      ? '这场示例面试正在更新，内容会自动显示。当前为只读身份。'
      : '当前为只读身份，可查看对话。想亲自作答，请联系作者获取访问令牌。');
  } else if (state.submitting && lastDone) {
    html = scoringCardHtml();
  } else if (detail.scoring_pending && !detail.pending && !detail.busy) {
    html = scoringCardHtml();
  } else if (state.submitting) {
    html = answerBoxHtml(true);
  } else if (detail.busy) {
    html = workingCardHtml();
  } else if (detail.pending) {
    html = answerBoxHtml(false);
  } else {
    html = `<div class="card">
      <h2>这场面试可以恢复</h2>
      <p class="card-sub">当前没有等待回答的中断点，需要推进已落盘的步骤。</p>
      ${state.error ? notice(state.error, 'error') : ''}
      <button class="primary" type="button" data-action="resume">继续推进</button>
    </div>`;
  }
  host.innerHTML = html;
  state.clearDraft = false;
  state.bottomSig = bottomSignature();
  if (keepDraft !== null) {
    const next = $('#answer-input');
    if (next && !next.value) next.value = keepDraft;
    if (next && focused) {
      next.focus({ preventScroll: true });
      next.setSelectionRange(...selection);
    }
  }
}

function renderMetricsCard() {
  const host = $('#detail-metrics');
  if (!host) return;
  host.innerHTML = state.cleanMode ? '' : renderMetrics(state.detail || {});
}

function renderDetail() {
  // 同一会话保留消息 DOM 和输出队列，切换模式仅调整系统消息可见性。
  if ($('#stream') && state.streamSession === state.current) {
    renderHead();
    renderBottom();
    renderMetricsCard();
    syncStream(false);
    return;
  }
  content.innerHTML = '';
  const head = document.createElement('div');
  head.id = 'detail-head';
  head.className = 'card';
  content.appendChild(head);
  const streamCard = document.createElement('div');
  streamCard.className = 'card';
  streamCard.innerHTML = '<div class="stream" id="stream"></div>';
  content.appendChild(streamCard);
  const bottom = document.createElement('div');
  bottom.id = 'detail-bottom';
  content.appendChild(bottom);
  const metrics = document.createElement('div');
  metrics.id = 'detail-metrics';
  content.appendChild(metrics);
  renderHead();
  renderBottom();
  renderMetricsCard();
  state.streamSession = null; // 强制信息流整段重建
  syncStream(false);

}

function render() {
  const previous = $('#answer-input');
  const draft = previous && !previous.disabled ? previous.value : null;
  syncCleanToggle();
  if (state.view === 'new') content.innerHTML = renderNew();
  else if (state.view === 'detail' && state.detail) renderDetail();
  else content.innerHTML = renderEmpty();
  const next = $('#answer-input');
  if (next && draft !== null && !state.clearDraft) next.value = draft;
  state.clearDraft = false;
}

function readForm() {
  const jdNodes = Array.from(document.querySelectorAll('[data-jd-index]'));
  state.form = {
    select: $('#topic-select') ? $('#topic-select').value : '',
    custom: $('#topic-custom') ? $('#topic-custom').value : '',
    num: $('#num-select') ? $('#num-select').value : '',
    resume: $('#resume-input') ? $('#resume-input').value : '',
    jds: jdNodes.map((node) => node.value),
  };
}

// ---------- SSE 事件通道：hello 快照 / append 增量 / state 状态 ----------

function subscribe(sessionId) {
  if (state.es) { state.es.close(); state.es = null; }
  if (!sessionId) return;
  const es = new EventSource(`api/interviews/${encodeURIComponent(sessionId)}/events`);
  es.onmessage = (event) => {
    let payload;
    try { payload = JSON.parse(event.data); } catch (_) { return; }
    if (state.es !== es || state.current !== sessionId) return;
    if (payload.type === 'hello') applyHello(payload);
    else if (payload.type === 'append') applyAppend(payload);
    else if (payload.type === 'truncate') applyTruncate(payload);
    else if (payload.type === 'state') applyState(payload);
    else if (payload.type === 'qstream') applyQStream(payload);
    else if (payload.type === 'qopen') applyQOpen(payload);
    else if (payload.type === 'deleted') {
      forgetSession(sessionId);
      loadSessions();
    }
  };
  es.onerror = () => {
    // 只有连接彻底关闭（例如令牌失效返回 401）才回登录页；
    // 普通断线由 EventSource 自动重连，重连后的 hello 会重新同步全量。
    if (state.es === es && es.readyState === EventSource.CLOSED) {
      window.dispatchEvent(new CustomEvent('auth-lost'));
    }
  };
  state.es = es;
}

function applyHello(payload) {
  if (state.view !== 'detail') return;
  if (!payload.busy) state.submitting = false;
  state.detail = {
    session: payload.session,
    messages: payload.messages || [],
    pending: payload.pending,
    done: payload.done,
    busy: payload.busy,
    scoring_pending: payload.scoring_pending,
    initializing: payload.initializing,
    revision: payload.revision,
    metrics: payload.metrics || [],
    metrics_summary: payload.metrics_summary || {},
  };
  state.revision = payload.revision;
  state.stage = {
    stage: payload.stage,
    stage_ms: payload.stage_ms,
    last_stage: payload.last_stage,
  };
  render();
  updatePassStatus();
  loadSessions();
  if (state.detail.done) loadReport(true);
}

function applyAppend(payload) {
  if (state.view !== 'detail' || !state.detail) return;
  const items = payload.items || [];
  state.detail.messages = [...(state.detail.messages || []), ...items];
  syncStream(true);
  // 快评落地之后、下一道题开流之前，才挂面试官的 loading 块。
  // 若同一批已经带最终面试官消息，就不需要再挂临时块。
  const decided = items.some((message) =>
    message.role === 'system' && message.text.startsWith('快评决策'));
  const answered = items.some((message) => message.role === 'interviewer');
  if (decided && !answered && state.submitting && !state.optimistic.interviewer) {
    appendOptimisticInterviewer();
  }
}

async function refreshTimings() {
  if (!state.current) return;
  const sessionId = state.current;
  try {
    const result = await api(`/api/interviews/${encodeURIComponent(sessionId)}/timings`);
    if (sessionId !== state.current) return;
    if (state.detail) {
      state.detail.metrics = result.stages || [];
      state.detail.metrics_summary = result.summary || {};
    }
    renderMetricsCard();
  } catch (_) {
    // 指标是辅助信息，失败不打扰面试流程。
  }
}

function applyState(payload) {
  if (state.view !== 'detail') return;
  const detail = state.detail || {};
  const revisionChanged = payload.revision !== state.revision;
  const wasDone = detail.done;
  state.revision = payload.revision;
  state.stage = {
    stage: payload.stage,
    stage_ms: payload.stage_ms,
    last_stage: payload.last_stage,
  };
  detail.busy = payload.busy;
  detail.done = payload.done;
  detail.pending = payload.pending;
  detail.scoring_pending = payload.scoring_pending;
  detail.session = payload.session;
  state.detail = detail;
  if (!payload.busy) {
    state.submitting = false;
    const lastMessage = detail.messages?.at(-1);
    if (lastMessage?.text.startsWith('本轮执行中断')) state.error = lastMessage.text;
    if (!detail.pending && !detail.scoring_pending) removeOptimisticNodes();
  }
  updateStageLine();
  renderHead();
  if (bottomSignature() !== state.bottomSig) renderBottom();
  if (revisionChanged) {
    loadSessions();
    updatePassStatus();
    refreshTimings();
  }
  if (detail.done && !wasDone) loadReport(true);
}

async function loadSessions() {
  const authVersion = state.authVersion;
  try {
    const result = await api('/api/interviews');
    if (authVersion !== state.authVersion) return;
    state.sessions = result.sessions || [];
    renderSessions();
  } catch (error) {
    if (authVersion !== state.authVersion) return;
    state.error = error.message;
  }
}

async function loadTopics() {
  if (state.topicsLoaded) return;
  const result = await api('/api/topics');
  state.topics = result.topics || [];
  state.topicsLoaded = true;
}

async function loadReport(force = false) {
  if (!state.current) return;
  if (!force && state.reportFor === state.current) return;
  const sessionId = state.current;
  const result = await api(`/api/interviews/${encodeURIComponent(sessionId)}/report`);
  if (sessionId !== state.current) return;
  state.reportHtml = mdToHtml(result.markdown);
  state.reportFor = state.current;
  const node = $('#report-body');
  if (node) node.innerHTML = state.reportHtml;
}

async function openSession(id) {
  removeOptimisticNodes();
  for (const entry of state.streamRendered) clearTimeout(entry.node?.typingTimer);
  state.current = id;
  state.view = 'detail';
  state.detail = null;
  state.reportFor = null;
  state.reportHtml = '';
  state.revision = null;
  state.stage = null;
  state.error = '';
  state.emptyConfirm = false;
  state.clearDraft = true;
  state.streamRendered = [];
  state.streamSession = null;
  state.optimistic = { candidate: null, interviewer: null };
  state.qstream = null;
  state.qstreamSettled = false;
  state.submitting = false;
  state.bottomSig = '';
  renderSessions();
  content.innerHTML = '<div class="card"><div class="loading">正在读取会话…</div></div>';
  // 连接即同步：服务端 hello 快照负责首屏渲染，之后全部走增量事件。
  subscribe(id);
}

function passStatusText(info) {
  if (state.readOnly) return '只读浏览';
  if (!info) return '';
  if (info.kind !== 'temp') return '长期令牌 · 不限配额';
  const remaining = Math.max(
    0, Math.round((new Date(info.expires_at).getTime() - Date.now()) / 60000)
  );
  const minutes = remaining >= 60
    ? `${Math.floor(remaining / 60)} 小时 ${remaining % 60} 分`
    : `${remaining} 分钟`;
  const used = info.used_tokens ?? 0;
  const balance = info.quota_tokens != null
    ? `余额 ${formatTokens(Math.max(0, info.quota_tokens - used))} token`
    : `已用 ${formatTokens(used)} token`;
  return `临时令牌 · 剩 ${minutes} · ${balance}`;
}

async function updatePassStatus() {
  // Balance comes from stage_metrics sums, so it moves as the interview burns
  // tokens; refresh it whenever the detail view catches a revision change.
  const authVersion = state.authVersion;
  try {
    const me = await api('/api/me');
    if (authVersion !== state.authVersion) return;
    state.pass = me.pass;
    const changed = state.readOnly !== Boolean(me.read_only);
    state.readOnly = Boolean(me.read_only);
    syncAccessControls();
    if (changed) { renderSessions(); render(); }
    const node = $('#pass-status');
    if (node) node.textContent = passStatusText(state.pass);
  } catch (_) {
    // Auth loss is handled by the auth-lost event; ignore transient errors.
  }
}

async function showMain() {
  // Clear the previous user's content before revealing a newly logged-in session.
  state.sessions = [];
  state.current = null;
  state.detail = null;
  state.view = 'empty';
  renderSessions();
  render();
  $('#login-view').hidden = true;
  $('#main-view').hidden = false;
  $('#who').textContent = state.user;
  syncAccessControls();
  $('#pass-status').textContent = passStatusText(state.pass);
  updatePassStatus();
  await loadTopics();
  await loadSessions();
  render();
}

function showLogin() {
  state.authVersion++;
  $('#main-view').hidden = true;
  $('#login-view').hidden = false;
  if (state.es) { state.es.close(); state.es = null; }
  removeOptimisticNodes();
  for (const entry of state.streamRendered) clearTimeout(entry.node?.typingTimer);
  state.streamRendered = [];
  state.streamSession = null;
  state.detail = null;
  state.sessions = [];
  state.user = null;
  state.pass = null;
  state.readOnly = false;
  state.deleteTarget = null;
  $('#delete-dialog')?.close();
  state.current = null;
  state.view = 'empty';
}

function syncAccessControls() {
  if ($('#new-btn')) $('#new-btn').hidden = state.readOnly;
  if ($('#readonly-intro')) $('#readonly-intro').hidden = !state.readOnly;
  if ($('#session-title')) $('#session-title').textContent = state.readOnly ? '示例面试' : '我的面试';
}

function forgetSession(id) {
  state.sessions = state.sessions.filter(item => item.id !== id);
  if (state.current === id) {
    if (state.es) { state.es.close(); state.es = null; }
    removeOptimisticNodes();
    for (const entry of state.streamRendered) clearTimeout(entry.node?.typingTimer);
    state.current = null;
    state.detail = null;
    state.streamRendered = [];
    state.streamSession = null;
    state.reportFor = null;
    state.reportHtml = '';
    state.view = 'empty';
    state.error = '';
    render();
  }
  renderSessions();
}

function requestDeleteSession(id) {
  if (state.readOnly || state.deleting) return;
  const item = state.sessions.find(item => item.id === id);
  if (!item) return;
  state.deleteTarget = id;
  $('#delete-description').textContent = `${item.topic || '未命名面试'} · ${item.num_questions || '—'} 题 · ${formatTime(item.updated_at)}`;
  $('#delete-error').hidden = true;
  $('#delete-dialog').showModal();
}

async function confirmDeleteSession() {
  if (state.readOnly || state.deleting || !state.deleteTarget) return;
  const id = state.deleteTarget;
  const user = state.user;
  state.deleting = true;
  $('#confirm-delete').disabled = true;
  $('#cancel-delete').disabled = true;
  try {
    await api(`/api/interviews/${encodeURIComponent(id)}`, { method: 'DELETE' });
    if (state.user !== user) return;
    $('#delete-dialog').close();
    state.deleteTarget = null;
    forgetSession(id);
  } catch (error) {
    if (state.user !== user) return;
    $('#delete-error').textContent = error.message;
    $('#delete-error').hidden = false;
  } finally {
    state.deleting = false;
    $('#confirm-delete').disabled = false;
    $('#cancel-delete').disabled = false;
  }
}

async function enterSession(readOnly = false) {
  const authVersion = ++state.authVersion;
  const box = $('#login-error');
  box.hidden = true;
  const buttons = document.querySelectorAll('#login-form button');
  buttons.forEach(button => { button.disabled = true; });
  try {
    const result = await api('/api/login', {
      method: 'POST',
      body: JSON.stringify({
        username: readOnly ? 'guest' : $('#username').value,
        pass_token: readOnly ? '' : $('#pass-input').value,
      }),
    });
    if (authVersion !== state.authVersion) return;
    state.user = result.username;
    const me = await api('/api/me');
    if (authVersion !== state.authVersion) return;
    state.pass = me.pass;
    state.readOnly = Boolean(me.read_only);
    await showMain();
  } catch (error) {
    box.textContent = error.message;
    box.hidden = false;
  } finally {
    buttons.forEach(button => { button.disabled = false; });
  }
}

$('#login-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  await enterSession(false);
});

$('#guest-login')?.addEventListener('click', () => enterSession(true));
$('#token-login')?.addEventListener('click', async () => {
  await api('/api/logout', { method: 'POST' }).catch(() => null);
  showLogin();
  $('#username').value = 'guest';
  $('#pass-input').focus();
});
$('#confirm-delete')?.addEventListener('click', confirmDeleteSession);
$('#cancel-delete')?.addEventListener('click', () => {
  state.deleteTarget = null;
  $('#delete-dialog').close();
});
$('#delete-dialog')?.addEventListener('cancel', event => {
  if (state.deleting) event.preventDefault();
  else state.deleteTarget = null;
});

window.addEventListener('auth-lost', () => {
  if (state.user) showLogin();
});

$('#logout').addEventListener('click', async () => {
  await api('/api/logout', { method: 'POST' }).catch(() => null);
  showLogin();
});

// 旧版 index.html（浏览器缓存里没有这个按钮）也要能用，缺元素就跳过绑定。
cleanToggle?.addEventListener('click', () => {
  state.cleanMode = !state.cleanMode;
  writeCleanMode(state.cleanMode);
  // 新建表单里的输入没有实时同步到 state，切换前先收一遍，免得被重渲染冲掉。
  if (state.view === 'new') readForm();
  render();
});

$('#new-btn').addEventListener('click', () => {
  if (state.readOnly) return;
  state.view = 'new';
  state.current = null;
  state.detail = null;
  state.error = '';
  state.form = { select: '', custom: '', num: '', resume: '', jds: [] };
  if (state.es) { state.es.close(); state.es = null; }
  renderSessions();
  render();
});

sessionList.addEventListener('click', (event) => {
  const remove = event.target.closest('[data-delete-session]');
  if (remove) { requestDeleteSession(remove.dataset.deleteSession); return; }
  const button = event.target.closest('[data-session]');
  if (button) openSession(button.dataset.session);
});

content.addEventListener('keydown', (event) => {
  if (event.target.id === 'answer-input' && event.key === 'Enter'
      && (event.ctrlKey || event.metaKey) && !event.isComposing) {
    const button = content.querySelector('[data-action="submit-answer"]');
    if (button && !button.disabled) { event.preventDefault(); button.click(); }
  }
});

content.addEventListener('click', async (event) => {
  const button = event.target.closest('[data-action]');
  if (!button) return;
  const action = button.dataset.action;
  if (state.readOnly) return;
  try {
    if (action === 'cancel') {
      state.view = 'empty';
      state.error = '';
      if (state.es) { state.es.close(); state.es = null; }
      render();
    } else if (action === 'add-jd' || action === 'remove-jd') {
      readForm();
      if (action === 'add-jd' && state.form.jds.length < 5) state.form.jds.push('');
      if (action === 'remove-jd') state.form.jds.splice(Number(button.dataset.index), 1);
      render();
    } else if (action === 'create') {
      if (button.disabled) return;
      readForm();
      const topic = state.form.custom.trim() || state.form.select;
      const num = Number(state.form.num);
      if (!topic || !num) {
        state.error = '主题和题目数量都是必填项，请补全后再开始。';
        render();
        return;
      }
      state.error = '';
      button.disabled = true;
      button.textContent = '正在创建…';
      const result = await api('/api/interviews', {
        method: 'POST',
        body: JSON.stringify({
          topic,
          num_questions: num,
          resume: state.form.resume,
          jds: state.form.jds.filter((value) => value.trim()),
        }),
      });
      await openSession(result.id);
    } else if (action === 'submit-answer') {
      if (state.submitting || state.detail?.busy) return;
      const node = $('#answer-input');
      const answer = node ? node.value : '';
      if (!answer.trim() && !state.emptyConfirm) {
        state.emptyConfirm = true;
        state.error = '';
        render();
        return;
      }
      const submittingSession = state.current;
      state.error = '';
      state.emptyConfirm = false;
      state.clearDraft = true;
      state.qstreamSettled = false;
      // 乐观流水线：按钮与输入区立即锁定 → 候选人回复入流 → 面试官加载块。
      // 之后由 SSE append 事件接管：候选人消息被服务端数据取代、加载块换成打字机。
      state.submitting = true;
      renderBottom();
      // 只先把候选人的回复顶上去。事实时序是「提交 → 快评 → 出题」，
      // 面试官的 loading 等快评消息到达再出现，不能抢在快评前面。
      appendOptimisticCandidate(answer);
      try {
        await api(`/api/interviews/${encodeURIComponent(state.current)}/answer`, {
          method: 'POST',
          body: JSON.stringify({ answer, confirm_empty: !answer.trim() }),
        });
      } catch (error) {
        if (state.current !== submittingSession) return;
        removeOptimisticNodes();
        state.submitting = false;
        state.error = error.message;
        renderBottom();
        const input = $('#answer-input');
        if (input) input.value = answer;
      }
    } else if (action === 'resume') {
      state.error = '';
      state.qstreamSettled = false;
      await api(`/api/interviews/${encodeURIComponent(state.current)}/resume`, { method: 'POST' });
      state.detail = { ...(state.detail || {}), busy: true };
      renderBottom();
    }
  } catch (error) {
    state.error = error.message;
    render();
  }
});

(async () => {
  syncCleanToggle();
  const params = new URLSearchParams(window.location.search);
  const urlPass = params.get('pass');
  const tokenInput = $('#pass-input');
  if (urlPass && tokenInput) {
    tokenInput.value = urlPass;
    $('#username').focus();
  }
  if (params.get('pass_error') && tokenInput) {
    const box = $('#login-error');
    box.textContent = '链接中的令牌无效或已过期，请联系作者重新获取。';
    box.hidden = false;
  }
  try {
    const result = await api('/api/me');
    state.user = result.username;
    state.pass = result.pass;
    state.readOnly = Boolean(result.read_only);
    await showMain();
  } catch (error) {
    showLogin();
  }
})();
