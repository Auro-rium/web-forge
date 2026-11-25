/* WebForge front-end. Plain JS, no dependencies. All dynamic text is inserted with textContent / text nodes only
   (agent and page output is untrusted). */
(() => {
'use strict';

// ------------------------------------------------------------------ utils ---
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));

function el(tag, props, ...kids) {
  const e = document.createElement(tag);
  if (props) {
    for (const [k, v] of Object.entries(props)) {
      if (v == null || v === false) continue;
      if (k === 'class') e.className = v;
      else if (k === 'text') e.textContent = v;
      else if (k === 'on') for (const [ev, fn] of Object.entries(v)) e.addEventListener(ev, fn);
      else e.setAttribute(k, v === true ? '' : String(v));
    }
  }
  for (const c of kids.flat(Infinity)) {
    if (c == null || c === false) continue;
    e.append(c.nodeType ? c : document.createTextNode(String(c)));
  }
  return e;
}
const clear = (n) => { while (n.firstChild) n.removeChild(n.firstChild); return n; };
const lsGet = (k, d = '') => { try { const v = localStorage.getItem(k); return v == null ? d : v; } catch { return d; } };
const lsSet = (k, v) => { try { localStorage.setItem(k, v); } catch { /* storage unavailable */ } };
const trunc = (s, n) => { s = String(s ?? ''); return s.length > n ? s.slice(0, n - 1) + '…' : s; };
const fmtNum = (n, d = 0) => Number(n || 0).toLocaleString(undefined, { minimumFractionDigits: d, maximumFractionDigits: d });
const fmtUsd = (n) => '$' + Number(n || 0).toFixed(Number(n) < 0.1 ? 4 : 2);
const fmtArgs = (a) => { try { return trunc(JSON.stringify(a ?? {}), 90); } catch { return ''; } };

const S = { base: '', code: '', online: false, health: null, running: false, es: null, runId: null };

const EXAMPLES = [
  ['compare laptops', 'Compare 3 good student laptops under $1000 (price, battery, weight), recommend one and save outputs/laptops.csv'],
  ['fill a form', 'Go to https://httpbin.org/forms/post and order a large pizza with bacon and mushrooms for Ada Lovelace (phone 555-0100, ada@example.org), delivery at 19:00, then tell me what the server received'],
  ['HN digest', 'Summarize the top 3 stories on Hacker News right now, with the top comment of each, and save outputs/hn.md'],
  ['research + decide', 'Which is cheaper per month for 1 TB of cloud storage: Google One, iCloud+ or Dropbox? Show the prices with sources and recommend one'],
];

function normBase(v) {
  v = (v || '').trim().replace(/\/+$/, '');
  if (!v) return '';
  if (!/^[a-z][a-z0-9+.-]*:\/\//i.test(v)) v = (/^(localhost|127\.|\[::1\])/.test(v) ? 'http://' : 'https://') + v;
  return v;
}
const apiUrl = (p) => S.base + p;

async function fetchJSON(url, opts = {}, timeout = 8000) {
  const ctl = new AbortController();
  const t = setTimeout(() => ctl.abort(), timeout);
  try {
    const r = await fetch(url, { ...opts, signal: ctl.signal });
    const ct = r.headers.get('content-type') || '';
    if (!ct.includes('json')) throw new Error('not a JSON response');
    const body = await r.json();
    if (!r.ok) { const e = new Error(body.detail || body.error || ('HTTP ' + r.status)); e.status = r.status; throw e; }
    return body;
  } finally { clearTimeout(t); }
}
const api = (path, opts, timeout) => fetchJSON(apiUrl(path), { cache: 'no-store', ...(opts || {}) }, timeout);
const authHeaders = () => (S.code ? { 'X-Access-Code': S.code } : {});
const postJSON = (path, body, timeout) => api(path, {
  method: 'POST', headers: { 'Content-Type': 'application/json', ...authHeaders() }, body: JSON.stringify(body || {}),
}, timeout);

// ------------------------------------------------------------------- tabs ---
const TABS = ['run', 'arch'];
function showTab(name, opts = {}) {
  if (!TABS.includes(name)) name = 'run';
  for (const t of TABS) $('#tab-' + t).hidden = t !== name;
  for (const b of $$('#tabs button')) b.setAttribute('aria-selected', String(b.dataset.tab === name));
  if (!opts.noHash && location.hash !== '#' + name) history.replaceState(null, '', '#' + name);
  if (!opts.noScroll) window.scrollTo({ top: 0 });
}
const routeFromHash = () => showTab(location.hash.replace('#', ''), { noHash: true, noScroll: true });

// ------------------------------------------------------------- connection ---
async function connect() {
  S.base = normBase($('#backend').value);
  S.code = $('#code').value;
  lsSet('wf_backend', S.base);
  lsSet('wf_code', S.code);
  $('#conn-text').textContent = 'checking...';
  try {
    const h = await api('/api/health', {}, 6000);
    if (!h || h.ok !== true) throw new Error('bad health');
    S.online = true; S.health = h;
  } catch { S.online = false; S.health = null; }
  renderConn();
  if (S.online) loadSecrets();
}

function renderConn() {
  const h = S.health;
  const pill = $('#conn-pill');
  pill.classList.toggle('on', S.online);
  pill.classList.toggle('off', !S.online);
  $('#conn-text').textContent = S.online ? (h.mock ? 'connected (mock LLM)' : 'connected') : 'offline';
  $('#offline-banner').hidden = S.online;
  $('#conn-summary').textContent = S.online ? '- ' + (S.base || 'this site') : '- not connected';
  $('#conn-card').open = !S.online && !!S.base;
  $('#code-row').hidden = !(S.online && h.needs_code);
  const d = $('#health-detail');
  d.textContent = S.online
    ? `${h.provider} / ${h.model}. Spend ${fmtUsd(h.spend_usd)} of ${fmtUsd(h.budget_usd)}. Browser ${h.browser ? 'available' : 'NOT available'}.` +
      (h.needs_code ? ' Access code required to run.' : '')
    : (S.base ? 'Could not reach ' + S.base + ' (is it running, and does WEBFORGE_CORS_ORIGINS include this site?).' : '');
  renderKeyCard();
  updateRunButton();
}

// ----------------------------------------------------- backboard key setup ---
const needsKey = () => !!(S.online && S.health && S.health.needs_key);
function renderKeyCard() {
  $('#keycard').hidden = !needsKey();
  $('#btn-forgetkey').hidden = !(S.online && S.health && S.health.key_source === 'saved');
}
const setKeyStatus = (text) => { $('#key-status').textContent = text || ''; };
async function saveKey() {
  const input = $('#keyinput');
  const key = input.value.trim();
  if (!key) { setKeyStatus('Paste your Backboard API key first.'); return; }
  const btn = $('#btn-savekey');
  btn.disabled = true;
  setKeyStatus('Checking the key with Backboard...');
  try {
    await postJSON('/api/settings/key', { api_key: key }, 30000);
    input.value = '';
    setKeyStatus('Key saved. You can run the agent now.');
    await connect();
  } catch (e) {
    setKeyStatus(e.status === 403 ? 'Not allowed: ' + e.message : (e.message || 'Could not save the key.'));
  } finally { btn.disabled = false; }
}
async function forgetKey() {
  if (!confirm('Forget the saved Backboard key on this server?')) return;
  try {
    await api('/api/settings/key', { method: 'DELETE', headers: authHeaders() });
    setKeyStatus('');
    await connect();
  } catch (e) { alert('Could not forget the key: ' + e.message); }
}

// ---------------------------------------------------------------- secrets ---
async function loadSecrets() {
  try {
    const { names } = await api('/api/secrets');
    $('#secrets-summary').textContent = names.length ? '(' + names.join(', ') + ')' : '(none yet; for logins, the model never sees values)';
  } catch { /* older backend */ }
}
async function addSecret() {
  const name = $('#secret-name').value.trim(), value = $('#secret-value').value;
  const st = $('#secret-status');
  if (!name || !value) { st.textContent = 'Give a name and a value.'; return; }
  try {
    await postJSON('/api/secrets', { name, value });
    $('#secret-value').value = '';
    st.textContent = 'Saved. Refer to it in a task as "the secret ' + name + '".';
    loadSecrets();
  } catch (e) { st.textContent = e.message || 'Could not save the secret.'; }
}

// ------------------------------------------------------------------ tasks ---
function updateRunButton() {
  const b = $('#btn-run');
  const hint = $('#run-hint');
  b.disabled = S.running || !S.online || ($('#task').value.trim() === '');
  b.textContent = S.running ? 'Running...' : 'Run agent';
  if (needsKey()) { b.disabled = true; hint.textContent = 'Add your Backboard key first'; }
  else if (!S.online) hint.textContent = 'Start the backend and connect to run the agent.';
  else if (S.health && S.health.browser === false) hint.textContent = 'The backend reports no browser; runs will fail.';
  else if (S.health && S.health.needs_code && !S.code) hint.textContent = 'This backend needs an access code (see Connection).';
  else hint.textContent = '';
}
function renderChips() {
  const box = clear($('#chips'));
  for (const [label, prompt] of EXAMPLES) {
    box.append(el('button', { class: 'chip', type: 'button', title: prompt, on: { click: () => {
      $('#task').value = prompt; onTaskInput();
    } } }, label));
  }
}
function onTaskInput() {
  $('#charcount').textContent = $('#task').value.length + '/500';
  updateRunButton();
}

// ------------------------------------------------------------- live panel ---
const todoParts = (t) => {
  if (t && typeof t === 'object') {
    const text = t.text || t.title || t.task || t.content || JSON.stringify(t);
    const st = String(t.status || (t.done ? 'done' : 'pending')).toLowerCase();
    return [st, String(text)];
  }
  return ['pending', String(t)];
};
const statusClass = (st) => (['done', 'completed', 'complete', 'x'].includes(st) ? 'done'
  : (['doing', 'in_progress', 'active', 'running'].includes(st) ? 'doing' : 'pending'));

function describe(kind, d) {
  d = d || {};
  switch (kind) {
    case 'task_start': return trunc(d.task, 200);
    case 'resume': return 'Resuming from checkpoint: ' + d.files + ' file(s), ' + d.todos + ' todo(s)';
    case 'search': return 'Web search: ' + trunc(d.query, 160);
    case 'parallel': return 'Launching ' + d.n + ' sub-agents in parallel: ' + (d.agents || []).join(', ');
    case 'read_urls': return 'Reading ' + (d.urls || []).length + ' page(s) in parallel: ' + trunc((d.urls || []).join(', '), 160);
    case 'delegate': return 'Delegate: ' + trunc(d.subtask, 160) + (d.start_url ? ' (start ' + d.start_url + ')' : '');
    case 'observe': return 'Look at ' + trunc(d.url || 'page', 90) + ' (' + (d.n_elements ?? '?') + ' elements)';
    case 'action': return d.action + ' ' + fmtArgs(d.args) + (d.ok === false ? '  FAILED: ' : '  ') + trunc(d.message, 140);
    case 'ask': return 'Question for you: ' + trunc(d.question, 200);
    case 'answer': return 'Your answer: ' + trunc(d.answer, 160);
    case 'analyze': return 'Analyst: ' + trunc(d.instruction, 140) + ' (' + (d.files || []).length + ' file(s))';
    case 'output': return 'Deliverable saved: ' + d.name;
    case 'verdict': return d.complete ? 'Verifier: complete' : 'Verifier: incomplete, missing ' + trunc((d.missing || []).join('; '), 160);
    case 'final': return 'Result ready';
    case 'nudge': case 'budget': case 'verify': return trunc(d.message, 200);
    case 'warn': case 'error': return trunc(d.message, 240);
    default: return trunc(JSON.stringify(d), 160);
  }
}
const QUIET = new Set(['screenshot', 'ask_id']);

const Live = {
  t0: 0, timer: null, actions: 0, askTimer: null,
  reset(task, title) {
    this.actions = 0;
    $('#live').hidden = false;
    $('#live-title').textContent = title;
    $('#live-task').textContent = task;
    this.status('running');
    clear($('#todos')).append(el('li', { class: 'muted' }, 'waiting for the plan...'));
    clear($('#timeline'));
    clear($('#shots')).append(el('span', { class: 'muted small' }, 'no browser activity yet'));
    $('#answer-card').hidden = true;
    $('#verify-badge').hidden = true;
    $('#verify-note').hidden = true;
    clear($('#files')).hidden = true;
    this.hideAsk();
    $('#btn-cancel').hidden = false;
    $('#btn-resume').hidden = true;
    this.renderMetrics();
    this.stopClock();
    this.t0 = performance.now();
    $('#live-clock').textContent = '0.0s';
    this.timer = setInterval(() => this.clock((performance.now() - this.t0) / 1000), 200);
    $('#live').scrollIntoView({ behavior: 'smooth', block: 'start' });
  },
  stopClock() { if (this.timer) { clearInterval(this.timer); this.timer = null; } },
  clock(sec) { $('#live-clock').textContent = sec.toFixed(1) + 's'; },
  status(s) {
    const b = $('#live-status'); b.textContent = s;
    b.className = 'badge ' + (s === 'running' ? 'running' : s === 'done' ? 'done' : s === 'waiting for you' ? 'ask' : s === 'cancelled' ? '' : 'error');
  },
  renderTodos(todos) {
    const ul = clear($('#todos'));
    if (!todos.length) { ul.append(el('li', { class: 'muted' }, '(empty plan)')); return; }
    for (const t of todos) {
      const [st, text] = todoParts(t);
      const c = statusClass(st);
      ul.append(el('li', { class: c }, el('span', { class: 'ic', 'aria-hidden': 'true' }, c === 'done' ? '✓' : ''),
        el('span', { class: 'tx' }, text)));
    }
  },
  shot(d) {
    const box = $('#shots');
    if (box.querySelector('.muted')) clear(box);
    const id = 'shot-' + String(d.agent || 'w').replace(/[^a-z0-9]/gi, '');
    let fig = box.querySelector('#' + id);
    if (!fig) {
      fig = el('figure', { class: 'shot', id }, el('img', { alt: 'browser view of ' + (d.agent || 'agent') }),
        el('figcaption', null, el('span', { class: 'lane lane-' + (parseInt(String(d.agent || 'w0').slice(1), 10) % 6 || 0) }, d.agent || 'agent'),
          el('span', { class: 'u' }, '')));
      box.append(fig);
    }
    if (/^[A-Za-z0-9+/=]+$/.test(d.jpeg_b64 || '')) fig.querySelector('img').src = 'data:image/jpeg;base64,' + d.jpeg_b64;
    fig.querySelector('.u').textContent = trunc(d.url || '', 60);
  },
  showAsk(d) {
    const card = $('#ask-card');
    card.hidden = false;
    card.dataset.askId = d.ask_id;
    $('#ask-q').textContent = d.question || '';
    const opts = clear($('#ask-options'));
    const approval = /^Approve:/.test(d.question || '');
    card.classList.toggle('approval', approval);
    for (const o of (d.options || [])) {
      opts.append(el('button', { class: 'btn ' + (o === 'approve' ? 'primary' : o === 'deny' ? 'danger' : ''), type: 'button',
        on: { click: () => answer(o) } }, o));
    }
    $('#ask-text').value = '';
    this.status('waiting for you');
    let left = Math.round(Number(d.timeout_s) || 90);
    clearInterval(this.askTimer);
    const tick = () => { $('#ask-timer').textContent = left > 0 ? left + 's left' : 'timed out'; left -= 1; if (left < -1) this.hideAsk(); };
    tick();
    this.askTimer = setInterval(tick, 1000);
    card.scrollIntoView({ behavior: 'smooth', block: 'center' });
  },
  hideAsk() {
    clearInterval(this.askTimer);
    $('#ask-card').hidden = true;
    if (S.running) this.status('running');
  },
  push(ev) {
    if (!ev || !ev.kind) return;
    const { kind, data } = ev; const d = data || {};
    switch (kind) {
      case 'todos': this.renderTodos(Array.isArray(d.todos) ? d.todos : []); return;
      case 'screenshot': this.shot(d); return;
      case 'ask_id': this.showAsk(d); return;
      case 'answer': this.hideAsk(); break;
      case 'action': this.actions++; break;
      case 'final': this.answer(d); break;
      default: break;
    }
    if (QUIET.has(kind)) return;
    const fail = (kind === 'action' && d.ok === false) || (kind === 'verdict' && !d.complete);
    const tl = $('#timeline');
    const stick = tl.scrollTop + tl.clientHeight >= tl.scrollHeight - 30;
    tl.append(el('li', { class: 'k-' + kind + (fail ? ' fail' : '') },
      el('span', { class: 'ts' }, (typeof ev.t === 'number' ? ev.t.toFixed(1) : '') + 's'),
      el('span', { class: 'kd' }, kind.replace(/_/g, ' ')),
      el('span', { class: 'tx' }, d.agent ? el('span', { class: 'lane lane-' + (parseInt(String(d.agent).slice(1), 10) % 6 || 0) }, d.agent) : '',
        describe(kind, d))));
    if (stick) tl.scrollTop = tl.scrollHeight;
    this.renderMetrics();
  },
  answer(d) {
    $('#answer-card').hidden = false;
    $('#answer').textContent = String(d.answer || '(no answer)');
    const b = $('#answer-badge'); b.textContent = d.ok === false ? 'incomplete' : 'complete'; b.className = 'badge ' + (d.ok === false ? 'bad' : 'ok');
    const v = d.verification || {};
    if (v && Object.keys(v).length) {
      const vb = $('#verify-badge'); vb.hidden = false;
      vb.textContent = v.complete ? '✓ verified' : '⚠ verifier: incomplete';
      vb.className = 'badge ' + (v.complete ? 'ok' : 'bad');
      const note = $('#verify-note');
      const parts = [v.verdict, (v.missing || []).length ? 'Missing: ' + v.missing.join('; ') : '',
        (v.unsupported_claims || []).length ? 'Unsupported: ' + v.unsupported_claims.join('; ') : ''].filter(Boolean);
      note.textContent = parts.join(' ');
      note.hidden = !parts.length;
    }
  },
  async files(runId) {
    try {
      const { files } = await api('/api/runs/' + encodeURIComponent(runId) + '/files');
      const box = clear($('#files'));
      if (!files.length) { box.hidden = true; return; }
      box.hidden = false;
      box.append(el('h4', null, 'Deliverables'));
      for (const f of files) {
        box.append(el('a', { class: 'file', href: apiUrl('/api/runs/' + encodeURIComponent(runId) + '/files/' + f.name.split('/').map(encodeURIComponent).join('/')),
          download: f.name.split('/').pop(), target: '_blank', rel: 'noopener' }, '⬇ ' + f.name + '  (' + fmtNum(f.size) + ' B)'));
      }
    } catch { /* no files */ }
  },
  renderMetrics(res) {
    const m = clear($('#metrics'));
    const items = res
      ? [['Browser steps', fmtNum(res.steps)], ['LLM calls', fmtNum(res.llm_calls)], ['Tokens', fmtNum(res.tokens)],
         ['Cost', fmtUsd(res.cost_usd)], ['Seconds', fmtNum(res.seconds, 1)], ['Deliverables', fmtNum((res.outputs || []).length)]]
      : [['Browser steps', fmtNum(this.actions)]];
    for (const [l, v] of items) m.append(el('div', { class: 'metric' }, el('div', { class: 'v' }, v), el('div', { class: 'l' }, l)));
  },
  finish(result, status, error) {
    this.stopClock();
    this.hideAsk();
    const td = $('#todos');
    if (td.children.length === 1 && td.firstChild.classList.contains('muted')) td.firstChild.textContent = 'No plan needed: the task was simple.';
    this.status(status);
    $('#btn-cancel').hidden = true;
    $('#btn-resume').hidden = status === 'done';
    if (result) {
      this.renderMetrics(result);
      if ($('#answer-card').hidden) this.answer(result);
      this.clock(result.seconds || 0);
    }
    if (S.runId) this.files(S.runId);
    if (error && error !== 'cancelled') this.push({ kind: 'error', data: { message: error } });
  },
};

// ------------------------------------------------------------------ runs ---
async function answer(text) {
  const card = $('#ask-card');
  const askId = card.dataset.askId;
  if (!askId || !S.runId) return;
  try {
    await postJSON('/api/runs/' + encodeURIComponent(S.runId) + '/answer', { ask_id: askId, answer: String(text) });
  } catch (e) { $('#ask-timer').textContent = e.message || 'could not send'; return; }
  Live.hideAsk();
}

async function launch(path, body, title, task) {
  const hint = $('#run-hint');
  hint.textContent = '';
  S.running = true; updateRunButton();
  try {
    const r = await fetch(apiUrl(path), {
      method: 'POST', headers: { 'Content-Type': 'application/json', ...authHeaders() }, body: JSON.stringify(body || {}),
    });
    const res = await r.json().catch(() => ({}));
    if (!r.ok) {
      const msg = typeof res.detail === 'string' ? res.detail : (typeof res.error === 'string' ? res.error : (r.status === 422 ? 'Invalid task.' : 'HTTP ' + r.status));
      hint.textContent = (r.status === 401 ? 'Access code missing or wrong. ' : '') + msg;
      S.running = false; updateRunButton();
      return;
    }
    S.runId = res.run_id;
    Live.reset(task, title);
    stream(res.run_id);
  } catch (e) {
    hint.textContent = 'Could not reach the backend: ' + (e && e.message ? e.message : e);
    S.running = false; updateRunButton();
  }
}

function startRun() {
  const task = $('#task').value.trim();
  if (!task || S.running) return;
  S.code = $('#code').value; lsSet('wf_code', S.code);
  launch('/api/run', { task }, 'Live run', task);
}

function resumeRun() {
  if (!S.runId || S.running) return;
  launch('/api/runs/' + encodeURIComponent(S.runId) + '/resume', {}, 'Resumed run', $('#live-task').textContent);
}

async function cancelRun() {
  if (!S.runId) return;
  try { await postJSON('/api/runs/' + encodeURIComponent(S.runId) + '/cancel'); } catch { /* finished already */ }
}

function endRun(result, status, error) {
  if (S.es) { S.es.close(); S.es = null; }
  S.running = false;
  Live.finish(result, status, error);
  updateRunButton();
  api('/api/health').then((h) => { S.health = h; renderConn(); }).catch(() => {});
}

function stream(runId) {
  const es = new EventSource(apiUrl('/api/runs/' + encodeURIComponent(runId) + '/events'));
  S.es = es;
  es.onmessage = (e) => { try { Live.push(JSON.parse(e.data)); } catch { /* ignore */ } };
  es.addEventListener('done', (e) => {
    let d = {}; try { d = JSON.parse(e.data); } catch { /* ignore */ }
    endRun(d.result, d.status || 'done', d.error);
  });
  es.onerror = async () => {
    if (!S.es) return;
    es.close(); S.es = null;           // do not auto-reconnect: the server would replay from the start
    try {
      const info = await api('/api/runs/' + encodeURIComponent(runId));
      if (info.status !== 'running') { endRun(info.result, info.status, info.error); return; }
    } catch { /* fall through */ }
    endRun(null, 'error', 'Connection to the backend was lost. The run is checkpointed: press Resume.');
  };
}

// ------------------------------------------------------------------- init ---
function init() {
  S.base = normBase(lsGet('wf_backend', ''));
  S.code = lsGet('wf_code', '');
  $('#backend').value = S.base;
  $('#code').value = S.code;

  for (const b of $$('#tabs button')) b.addEventListener('click', () => showTab(b.dataset.tab));
  window.addEventListener('hashchange', routeFromHash);
  $('#btn-connect').addEventListener('click', connect);
  $('#backend').addEventListener('keydown', (e) => { if (e.key === 'Enter') connect(); });
  $('#task').addEventListener('input', onTaskInput);
  $('#btn-run').addEventListener('click', startRun);
  $('#btn-cancel').addEventListener('click', cancelRun);
  $('#btn-resume').addEventListener('click', resumeRun);
  $('#btn-savekey').addEventListener('click', saveKey);
  $('#keyinput').addEventListener('keydown', (e) => { if (e.key === 'Enter') saveKey(); });
  $('#btn-forgetkey').addEventListener('click', forgetKey);
  $('#btn-secret').addEventListener('click', addSecret);
  $('#btn-ask-send').addEventListener('click', () => { const v = $('#ask-text').value.trim(); if (v) answer(v); });
  $('#ask-text').addEventListener('keydown', (e) => { if (e.key === 'Enter' && e.target.value.trim()) answer(e.target.value.trim()); });
  $('#code').addEventListener('input', () => { S.code = $('#code').value; updateRunButton(); });

  routeFromHash();
  renderChips();
  if (!$('#task').value) { $('#task').value = EXAMPLES[0][1]; }
  onTaskInput();
  connect();
}

document.addEventListener('DOMContentLoaded', init);
})();
