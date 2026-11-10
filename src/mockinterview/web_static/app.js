const $ = (selector) => document.querySelector(selector);
const content = $('#content');
const sessionList = $('#session-list');

const state = {
  user: null,
  pass: null,
  topics: [],
  sessions: [],
  current: null,
  view: 'empty',
  detail: null,
  timer: null,
  topicsLoaded: false,
  error: '',
  emptyConfirm: false,
  clearDraft: false,
  reportHtml: '',
  reportFor: null,
  revision: null,
  lastBusy: false,
  stage: null,
  form: { select: '', custom: '', num: '', resume: '', jds: [] },
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
  const response = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  });
  if (response.status === 401 && !path.startsWith('/api/login')) {
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
    sessionList.innerHTML = '<div class="empty-list">还没有面试记录，点上方按钮新建一场。</div>';
    return;
  }
  sessionList.innerHTML = state.sessions.map((item) => {
    const done = item.status === 'done';
    const badge = done ? '已完成' : '进行中';
    const progress = `${item.completed}/${item.num_questions || '—'}`;
    return `<button class="session-item ${state.current === item.id && state.view === 'detail' ? 'active' : ''}"
      data-session="${escapeHtml(item.id)}" type="button">
      <span class="topic">${escapeHtml(item.topic || '未命名面试')}</span>
      <span class="sub"><span>${progress} 题 · ${escapeHtml(String(item.updated_at || '').slice(0, 16))}</span>
      <span class="badge ${done ? 'done' : 'running'}">${badge}</span></span>
    </button>`;
  }).join('');
}

function renderEmpty() {
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

function renderMessage(message) {
  const role = ['system', 'interviewer', 'candidate'].includes(message.role) ? message.role : 'system';
  return `<div class="msg ${role}">
    <div class="msg-role">${escapeHtml(message.label)}</div>
    <div class="msg-body">${multiline(message.text)}</div>
  </div>`;
}

function renderDetail(detail) {
  const session = detail.session || {};
  const chips = [
    ['主题', session.topic || '综合'],
    ['题数', session.num_questions || '—'],
    ['进度', `${session.completed || 0}/${session.num_questions || '—'}`],
    ['状态', session.status || '—'],
    ['JD', `${session.jd_count || 0} 份`],
    ['简历', session.has_resume ? '已提供' : '未提供'],
    ['Token', session.tokens || 0],
  ].map(([label, value]) =>
    `<span class="meta-chip">${escapeHtml(label)}<b>${escapeHtml(value)}</b></span>`
  ).join('');
  const messages = (detail.messages || []).map(renderMessage).join('')
    || '<div class="loading">正在准备题目…</div>';
  let bottom = '';
  if (detail.done) {
    bottom = `${notice('这场面试已完成，不能继续作答；以下为已保存的报告。', 'warn')}
      <div class="card"><h2>面试报告</h2>
        <div class="report" id="report-body">${state.reportFor === state.current
          ? state.reportHtml : '<div class="loading">正在读取报告…</div>'}</div>
      </div>`;
  } else if (detail.busy) {
    bottom = `${notice('面试官正在思考，结束后页面会自动刷新。')}
      <div class="card">
        <div class="working">
          <span class="spinner" aria-hidden="true"></span>
          <span class="working-text">请稍候，模型调用可能持续数十秒。</span>
        </div>
        <div class="stage-line" id="stage-line">${escapeHtml(stageText())}</div>
      </div>`;
  } else if (detail.pending) {
    const pending = detail.pending;
    const label = pending.kind === 'clarification' ? '题意澄清'
      : pending.hop === 0 ? '主问' : `追问 ${pending.hop}/3`;
    bottom = `<div class="card answer-box">
      <h2>继续作答</h2>
      <div class="answer-label"><span>第 ${pending.index + 1}/${pending.total} 题 · ${escapeHtml(pending.topic || '')}</span>
        <b>${escapeHtml(label)}</b></div>
      ${state.error ? notice(state.error, 'error') : ''}
      ${state.emptyConfirm ? notice('答案内容为空。再次点击「确认提交空回答」才会提交。', 'warn') : ''}
      <textarea id="answer-input" placeholder="在这里输入回答；空白提交需要二次确认"></textarea>
      <div class="answer-actions">
        <button class="primary" type="button" data-action="submit-answer">
          ${state.emptyConfirm ? '确认提交空回答' : '提交回答'}</button>
        <span class="answer-hint">已提交的内容会保存，未提交的草稿不会保存。</span>
      </div>
    </div>`;
  } else {
    bottom = `<div class="card">
      <h2>这场面试可以恢复</h2>
      <p class="card-sub">当前没有等待回答的中断点，需要推进已落盘的步骤。</p>
      ${state.error ? notice(state.error, 'error') : ''}
      <button class="primary" type="button" data-action="resume">继续推进</button>
    </div>`;
  }
  return `<div class="card">
      <h2>面试信息流</h2>
      <p class="card-sub">系统、面试官、候选人分角色展示。系统、面试官与评分约束沿用 CLI 的同一张图。</p>
      <div class="meta-grid">${chips}</div>
    </div>
    <div class="card"><div class="stream">${messages}</div></div>
    ${bottom}
    ${renderMetrics(detail)}`;
}

function render() {
  const previous = $('#answer-input');
  const draft = previous ? previous.value : null;
  if (state.view === 'new') content.innerHTML = renderNew();
  else if (state.view === 'detail' && state.detail) content.innerHTML = renderDetail(state.detail);
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

function scheduleStatus(delay = 1200) {
  if (state.timer) clearTimeout(state.timer);
  state.timer = setTimeout(() => pollStatus(), delay);
}

async function pollStatus() {
  if (state.view !== 'detail' || !state.current) return;
  try {
    const status = await api(`/api/interviews/${encodeURIComponent(state.current)}/status`);
    const changed = status.revision !== state.revision;
    const turnFinished = state.lastBusy && !status.busy;
    state.stage = status;
    if (changed || turnFinished) {
      await refreshDetail();
      return;
    }
    // Nothing new: update only the stage line, never rebuild the answer box.
    updateStageLine();
    state.lastBusy = status.busy;
    if (status.busy || status.scoring_pending) scheduleStatus();
  } catch (error) {
    state.error = error.message;
    render();
  }
}

async function loadSessions() {
  try {
    const result = await api('/api/interviews');
    state.sessions = result.sessions || [];
    renderSessions();
  } catch (error) {
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
  const result = await api(`/api/interviews/${encodeURIComponent(state.current)}/report`);
  state.reportHtml = mdToHtml(result.markdown);
  state.reportFor = state.current;
  const node = $('#report-body');
  if (node) node.innerHTML = state.reportHtml;
}

async function refreshDetail() {
  if (state.view !== 'detail' || !state.current) return;
  try {
    const detail = await api(`/api/interviews/${encodeURIComponent(state.current)}`);
    state.detail = detail;
    state.revision = detail.revision;
    state.lastBusy = detail.busy;
    state.error = '';
    render();
    updatePassStatus();
    if (detail.done) await loadReport(true);
    await loadSessions();
    if (detail.busy || detail.scoring_pending) scheduleStatus();
  } catch (error) {
    state.error = error.message;
    render();
  }
}

async function openSession(id) {
  state.current = id;
  state.view = 'detail';
  state.detail = null;
  state.reportFor = null;
  state.reportHtml = '';
  state.revision = null;
  state.lastBusy = false;
  state.stage = null;
  state.error = '';
  state.emptyConfirm = false;
  state.clearDraft = true;
  renderSessions();
  content.innerHTML = '<div class="card"><div class="loading">正在读取会话…</div></div>';
  await refreshDetail();
}

function passStatusText(info) {
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
  try {
    const me = await api('/api/me');
    state.pass = me.pass;
    const node = $('#pass-status');
    if (node) node.textContent = passStatusText(state.pass);
  } catch (_) {
    // Auth loss is handled by the auth-lost event; ignore transient errors.
  }
}

async function showMain() {
  $('#login-view').hidden = true;
  $('#main-view').hidden = false;
  $('#who').textContent = state.user;
  $('#pass-status').textContent = passStatusText(state.pass);
  updatePassStatus();
  await loadTopics();
  await loadSessions();
  render();
}

function showLogin() {
  $('#main-view').hidden = true;
  $('#login-view').hidden = false;
  state.user = null;
  state.pass = null;
  state.current = null;
  state.view = 'empty';
}

$('#login-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const box = $('#login-error');
  box.hidden = true;
  try {
    const result = await api('/api/login', {
      method: 'POST',
      body: JSON.stringify({
        username: $('#username').value,
        pass_token: $('#pass-input').value,
      }),
    });
    state.user = result.username;
    const me = await api('/api/me');
    state.pass = me.pass;
    await showMain();
  } catch (error) {
    box.textContent = error.message;
    box.hidden = false;
  }
});

window.addEventListener('auth-lost', () => {
  if (state.user) showLogin();
});

$('#logout').addEventListener('click', async () => {
  await api('/api/logout', { method: 'POST' }).catch(() => null);
  showLogin();
});

$('#new-btn').addEventListener('click', () => {
  state.view = 'new';
  state.current = null;
  state.detail = null;
  state.error = '';
  state.form = { select: '', custom: '', num: '', resume: '', jds: [] };
  renderSessions();
  render();
});

sessionList.addEventListener('click', (event) => {
  const button = event.target.closest('[data-session]');
  if (button) openSession(button.dataset.session);
});

content.addEventListener('click', async (event) => {
  const button = event.target.closest('[data-action]');
  if (!button) return;
  const action = button.dataset.action;
  try {
    if (action === 'cancel') {
      state.view = 'empty';
      state.error = '';
      render();
    } else if (action === 'add-jd' || action === 'remove-jd') {
      readForm();
      if (action === 'add-jd' && state.form.jds.length < 5) state.form.jds.push('');
      if (action === 'remove-jd') state.form.jds.splice(Number(button.dataset.index), 1);
      render();
    } else if (action === 'create') {
      readForm();
      const topic = state.form.custom.trim() || state.form.select;
      const num = Number(state.form.num);
      if (!topic || !num) {
        state.error = '主题和题目数量都是必填项，请补全后再开始。';
        render();
        return;
      }
      state.error = '';
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
      const node = $('#answer-input');
      const answer = node ? node.value : '';
      if (!answer.trim() && !state.emptyConfirm) {
        state.emptyConfirm = true;
        state.error = '';
        render();
        return;
      }
      state.error = '';
      state.emptyConfirm = false;
      state.clearDraft = true;
      await api(`/api/interviews/${encodeURIComponent(state.current)}/answer`, {
        method: 'POST',
        body: JSON.stringify({ answer, confirm_empty: !answer.trim() }),
      });
      state.detail = { ...state.detail, busy: true };
      render();
      scheduleStatus(400);
    } else if (action === 'resume') {
      state.error = '';
      await api(`/api/interviews/${encodeURIComponent(state.current)}/resume`, { method: 'POST' });
      state.detail = { ...state.detail, busy: true };
      render();
      scheduleStatus(400);
    }
  } catch (error) {
    state.error = error.message;
    render();
  }
});

(async () => {
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
    await showMain();
  } catch (error) {
    showLogin();
  }
})();
