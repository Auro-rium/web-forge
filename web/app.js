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
const SVGNS = 'http://www.w3.org/2000/svg';
function svg(tag, attrs, ...kids) {
  const e = document.createElementNS(SVGNS, tag);
  for (const [k, v] of Object.entries(attrs || {})) if (v != null) e.setAttribute(k, String(v));
  for (const c of kids.flat()) if (c != null) e.append(c.nodeType ? c : document.createTextNode(String(c)));
  return e;
}
const clear = (n) => { while (n.firstChild) n.removeChild(n.firstChild); return n; };
const lsGet = (k, d = '') => { try { const v = localStorage.getItem(k); return v == null ? d : v; } catch { return d; } };
const lsSet = (k, v) => { try { localStorage.setItem(k, v); } catch { /* storage unavailable */ } };
const trunc = (s, n) => { s = String(s ?? ''); return s.length > n ? s.slice(0, n - 1) + '…' : s; };
const fmtNum = (n, d = 0) => Number(n || 0).toLocaleString(undefined, { minimumFractionDigits: d, maximumFractionDigits: d });
const fmtUsd = (n) => '$' + Number(n || 0).toFixed(Number(n) < 0.1 ? 4 : 2);

const S = { base: '', code: '', online: false, health: null, running: false, es: null, tasks: [], demo: [] };

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
const stat = (name) => fetchJSON('data/' + name, {}, 8000);
const authHeaders = () => (S.code ? { 'X-Access-Code': S.code } : {});

// ------------------------------------------------------------------- tabs ---
const TABS = ['run', 'learning', 'library', 'arch'];
function showTab(name, opts = {}) {
  if (!TABS.includes(name)) name = 'run';
  for (const t of TABS) $('#tab-' + t).hidden = t !== name;
  for (const b of $$('#tabs button')) b.setAttribute('aria-selected', String(b.dataset.tab === name));
  if (!opts.noHash && location.hash !== '#' + name) history.replaceState(null, '', '#' + name);
  if (!opts.noScroll) window.scrollTo({ top: 0 });
}
function routeFromHash() {
  const h = location.hash.replace('#', '');
  if (h === 'replay') {
    showTab('run', { noHash: true, noScroll: true });
    $('#replay-det').open = true;
    $('#replay').scrollIntoView({ behavior: 'smooth', block: 'center' });
  } else showTab(TABS.includes(h) ? h : 'run', { noHash: true, noScroll: true });
}

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
  await Promise.all([loadTasks(), loadBench(), loadLibrary()]);
}

function renderConn() {
  const h = S.health;
  const pill = $('#conn-pill');
  pill.classList.toggle('on', S.online);
  pill.classList.toggle('off', !S.online);
  $('#conn-text').textContent = S.online ? (h.mock ? 'connected (mock LLM)' : 'connected') : 'offline demo';
  $('#offline-banner').hidden = S.online;
  $('#conn-summary').textContent = S.online ? '- ' + (S.base || 'this site') : '- not connected';
  $('#conn-card').open = !S.online && !!S.base;
  $('#code-row').hidden = !(S.online && h.needs_code);
  $('#btn-reset-mem').hidden = !S.online;
  const d = $('#health-detail');
  d.textContent = S.online
    ? `${h.provider} / ${h.model}. Spend ${fmtUsd(h.spend_usd)} of ${fmtUsd(h.budget_usd)}. Browser ${h.browser ? 'available' : 'NOT available'}.` +
      (h.needs_code ? ' Access code required to run.' : '')
    : (S.base ? 'Could not reach ' + S.base + ' (is it running, and does WEBFORGE_CORS_ORIGINS include this site?).' : '');
  $('#replay-det').open = !S.online || $('#replay-det').open;
  renderKeyCard();
  updateRunButton();
}

// ----------------------------------------------------- backboard key setup ---
const needsKey = () => !!(S.online && S.health && S.health.needs_key);
function renderKeyCard() {
  $('#keycard').hidden = !needsKey();
  $('#btn-forgetkey').hidden = !(S.online && S.health && S.health.key_source === 'saved');
}
function setKeyStatus(text) { $('#key-status').textContent = text || ''; }
async function saveKey() {
  const input = $('#keyinput');
  const key = input.value.trim();
  if (!key) { setKeyStatus('Paste your Backboard API key first.'); return; }
  const btn = $('#btn-savekey');
  btn.disabled = true;
  setKeyStatus('Checking the key with Backboard...');
  try {
    await api('/api/settings/key', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...authHeaders() },
      body: JSON.stringify({ api_key: key }),
    }, 30000);
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

function updateRunButton() {
  const b = $('#btn-run');
  const hint = $('#run-hint');
  b.disabled = S.running || !S.online || ($('#task').value.trim() === '');
  b.textContent = S.running ? 'Running...' : 'Run agent';
  if (needsKey()) { b.disabled = true; hint.textContent = 'Add your Backboard key first'; }
  else if (!S.online) hint.textContent = 'Connect a backend to run the agent live. Until then, use the replay below.';
  else if (S.health && S.health.browser === false) hint.textContent = 'The backend reports no browser; runs will fail.';
  else if (S.health && S.health.needs_code && !S.code) hint.textContent = 'This backend needs an access code (see Connection).';
  else hint.textContent = '';
}

// ------------------------------------------------------------------ tasks ---
async function loadTasks() {
  let tasks = [];
  try { if (S.online) tasks = (await api('/api/tasks')).tasks || []; } catch { /* fall through */ }
  if (!tasks.length) { try { tasks = (await stat('tasks.json')).tasks || []; } catch { /* none */ } }
  S.tasks = tasks;
  const box = clear($('#chips'));
  for (const t of tasks) {
    box.append(el('button', { class: 'chip', type: 'button', title: t.prompt, on: { click: () => {
      $('#task').value = t.prompt; onTaskInput();
    } } }, t.id.replace(/_/g, ' ')));
  }
  if (!$('#task').value && tasks[0]) { $('#task').value = tasks[0].prompt; onTaskInput(); }
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
const fmtArgs = (a) => { try { return trunc(JSON.stringify(a ?? {}), 90); } catch { return ''; } };

function describe(kind, d) {
  d = d || {};
  switch (kind) {
    case 'task_start': return trunc(d.task, 200);
    case 'delegate': return 'Delegate: ' + trunc(d.subtask, 160) + (d.start_url ? ' (start ' + d.start_url + ')' : '');
    case 'skill_run': return 'Run skill ' + d.name + ' ' + fmtArgs(d.params);
    case 'skill_result': return d.name + ': ' + (d.ok ? 'ok' : 'FAILED') + (d.message ? ' - ' + trunc(d.message, 160) : '');
    case 'observe': return 'Look at ' + trunc(d.url || 'page', 90) + ' (' + (d.n_elements ?? '?') + ' elements)';
    case 'action': return d.action + ' ' + fmtArgs(d.args) + (d.ok === false ? '  FAILED: ' : '  ') + trunc(d.message, 140);
    case 'memory_hits': return 'Recalled lessons from memory';
    case 'skill_built': return 'Built skill ' + d.name + (d.repaired ? ' (repaired, v' + d.version + ')' : '');
    case 'lesson': return trunc(d.text, 200);
    case 'final': return 'Final answer ready';
    case 'warn': case 'error': return trunc(d.message, 240);
    default: return trunc(JSON.stringify(d), 160);
  }
}

const Live = {
  t0: 0, timer: null, mode: 'live', n: { actions: 0 }, used: new Map(), built: new Set(),
  reset(task, title, mode) {
    this.mode = mode; this.n = { actions: 0 }; this.used = new Map(); this.built = new Set();
    $('#live').hidden = false;
    $('#live-title').textContent = title;
    $('#live-task').textContent = task;
    this.status('running');
    clear($('#todos')).append(el('li', { class: 'muted' }, 'waiting for the plan...'));
    clear($('#timeline'));
    clear($('#skill-chips')).append(el('span', { class: 'muted small' }, 'none yet'));
    const mh = clear($('#mem-hits')); mh.textContent = 'no memory recalled'; mh.className = 'mem-hits muted small';
    clear($('#lessons'));
    $('#answer-card').hidden = true;
    this.renderMetrics();
    this.stopClock();
    this.t0 = performance.now();
    $('#live-clock').textContent = '0.0s';
    if (mode === 'live') this.timer = setInterval(() => this.clock((performance.now() - this.t0) / 1000), 200);
    $('#live').scrollIntoView({ behavior: 'smooth', block: 'start' });
  },
  stopClock() { if (this.timer) { clearInterval(this.timer); this.timer = null; } },
  clock(sec) { $('#live-clock').textContent = sec.toFixed(1) + 's'; },
  status(s) { const b = $('#live-status'); b.textContent = s; b.className = 'badge ' + (s === 'running' ? 'running' : s === 'done' ? 'done' : s === 'error' ? 'error' : ''); },
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
  chips() {
    const box = clear($('#skill-chips'));
    if (!this.used.size && !this.built.size) { box.append(el('span', { class: 'muted small' }, 'none yet')); return; }
    for (const [n, ok] of this.used) box.append(el('span', { class: 'tag ' + (ok === false ? 'fail' : 'used'), title: 'skill used' }, '▶ ' + n));
    for (const n of this.built) box.append(el('span', { class: 'tag built', title: 'skill built from this run' }, '+ ' + n));
  },
  push(ev) {
    if (!ev || !ev.kind) return;
    const { kind, data } = ev; const d = data || {};
    if (typeof ev.t === 'number' && this.mode === 'replay') this.clock(ev.t);
    switch (kind) {
      case 'todos': this.renderTodos(Array.isArray(d.todos) ? d.todos : []); return;
      case 'action': this.n.actions++; break;
      case 'skill_run': this.used.set(d.name, null); this.chips(); break;
      case 'skill_result': this.used.set(d.name, !!d.ok); this.chips(); break;
      case 'skill_built': this.built.add(d.name); this.chips(); break;
      case 'memory_hits': { const m = clear($('#mem-hits')); m.textContent = String(d.text || ''); m.className = 'mem-hits has small'; break; }
      case 'lesson': $('#lessons').append(el('li', null, String(d.text || ''))); break;
      case 'final': this.answer(d.answer, d.ok); break;
      default: break;
    }
    const fail = (kind === 'action' && d.ok === false) || (kind === 'skill_result' && !d.ok);
    const tl = $('#timeline');
    const stick = tl.scrollTop + tl.clientHeight >= tl.scrollHeight - 30;
    tl.append(el('li', { class: 'k-' + kind + (fail ? ' fail' : '') },
      el('span', { class: 'ts' }, (typeof ev.t === 'number' ? ev.t.toFixed(1) : '') + 's'),
      el('span', { class: 'kd' }, kind.replace(/_/g, ' ')),
      el('span', { class: 'tx' }, describe(kind, d))));
    if (stick) tl.scrollTop = tl.scrollHeight;
    this.renderMetrics();
  },
  answer(text, ok) {
    $('#answer-card').hidden = false;
    $('#answer').textContent = String(text || '(no answer)');
    const b = $('#answer-badge'); b.textContent = ok === false ? 'incomplete' : 'complete'; b.className = 'badge ' + (ok === false ? 'bad' : 'ok');
  },
  renderMetrics(res) {
    const m = clear($('#metrics'));
    const items = res
      ? [['Steps', fmtNum(res.steps)], ['LLM calls', fmtNum(res.llm_calls)], ['Tokens', fmtNum(res.tokens)],
         ['Cost', fmtUsd(res.cost_usd)], ['Seconds', fmtNum(res.seconds, 1)],
         ['Skills used', fmtNum((res.skills_used || []).length)], ['Skills built', fmtNum((res.skills_created || []).length)]]
      : [['Actions', fmtNum(this.n.actions)], ['Skills used', fmtNum(this.used.size)], ['Skills built', fmtNum(this.built.size)]];
    for (const [l, v] of items) m.append(el('div', { class: 'metric' }, el('div', { class: 'v' }, v), el('div', { class: 'l' }, l)));
  },
  finish(result, status, error) {
    this.stopClock();
    this.status(status === 'done' ? 'done' : 'error');
    if (result) {
      this.renderMetrics(result);
      if ($('#answer-card').hidden) this.answer(result.answer, result.ok);
      if (this.mode === 'live') this.clock(result.seconds || 0);
      for (const n of result.skills_used || []) if (!this.used.has(n)) this.used.set(n, true);
      for (const n of result.skills_created || []) this.built.add(n);
      this.chips();
    }
    if (error) this.push({ t: undefined, kind: 'error', data: { message: error } });
  },
};

// --------------------------------------------------------------- live run ---
async function startRun() {
  const task = $('#task').value.trim();
  if (!task || S.running) return;
  S.code = $('#code').value; lsSet('wf_code', S.code);
  const hint = $('#run-hint');
  hint.textContent = '';
  S.running = true; updateRunButton();
  Player.pause();
  try {
    const r = await fetch(apiUrl('/api/run'), {
      method: 'POST', headers: { 'Content-Type': 'application/json', ...authHeaders() },
      body: JSON.stringify({ task, learn: $('#opt-learn').checked, use_skills: $('#opt-skills').checked,
                             use_memory: $('#opt-memory').checked }),
    });
    const body = await r.json().catch(() => ({}));
    if (!r.ok) {
      const msg = typeof body.detail === 'string' ? body.detail : (typeof body.error === 'string' ? body.error : (r.status === 422 ? 'Invalid task.' : 'HTTP ' + r.status));
      hint.textContent = (r.status === 401 ? 'Access code missing or wrong. ' : '') + msg;
      S.running = false; updateRunButton();
      return;
    }
    Live.reset(task, 'Live run', 'live');
    stream(body.run_id);
  } catch (e) {
    hint.textContent = 'Could not reach the backend: ' + (e && e.message ? e.message : e);
    S.running = false; updateRunButton();
  }
}

function endRun(result, status, error) {
  if (S.es) { S.es.close(); S.es = null; }
  Live.finish(result, status, error);
  S.running = false; updateRunButton();
  refreshAfterRun();
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
    endRun(null, 'error', 'Connection to the backend was lost.');
  };
}

async function refreshAfterRun() {
  try { S.health = await api('/api/health'); renderConn(); } catch { /* ignore */ }
  loadLibrary(); loadBench();
}

// ----------------------------------------------------------------- replay ---
const Player = {
  run: null, i: 0, timer: null, playing: false, started: false,
  select(idx) {
    this.pause();
    this.run = S.demo[idx] || null; this.i = 0; this.started = false;
    $('#demo-note').textContent = this.run ? (this.run.recorded ? 'Recorded from a real run.' : (this.run.note || '')) : '';
    $('#btn-play').textContent = 'Play';
    if (this.run) this.begin();
  },
  begin() {
    this.i = 0; this.started = true;
    Live.reset(this.run.task, 'Replay: ' + (this.run.title || 'recorded run'), 'replay');
    Live.clock(0);
  },
  speed() { return Number($('#demo-speed').value) || 1; },
  play() {
    if (!this.run) return;
    if (!this.started || this.i >= this.run.events.length) this.begin();
    this.playing = true; $('#btn-play').textContent = 'Pause';
    this.step();
  },
  pause() {
    this.playing = false; clearTimeout(this.timer); this.timer = null;
    $('#btn-play').textContent = this.run && this.i >= (this.run.events || []).length && this.i > 0 ? 'Replay' : 'Play';
  },
  step() {
    if (!this.playing) return;
    const evs = this.run.events;
    if (this.i >= evs.length) {
      this.playing = false;
      Live.finish(this.run.result, 'done');
      $('#btn-play').textContent = 'Replay';
      return;
    }
    const prev = this.i ? evs[this.i - 1].t : 0;
    const wait = Math.min(Math.max((evs[this.i].t - prev) * 1000, 120), 1800) / this.speed();
    this.timer = setTimeout(() => { const ev = evs[this.i++]; Live.push(ev); this.step(); }, wait);
  },
};

async function loadDemo() {
  try { S.demo = (await stat('demo_runs.json')).runs || []; } catch { S.demo = []; }
  const sel = clear($('#demo-select'));
  S.demo.forEach((r, i) => sel.append(el('option', { value: i }, r.title || r.id)));
  if (!S.demo.length) $('#demo-note').textContent = 'No recorded runs available.';
}

// ------------------------------------------------------------------ bench ---
const NUM = ['steps', 'llm_calls', 'tokens', 'seconds', 'cost_usd'];
const mean = (xs) => (xs.length ? xs.reduce((a, b) => a + b, 0) / xs.length : 0);
function agg(rows) {
  const o = { n: rows.length, success: rows.length ? rows.filter((r) => r.correct).length / rows.length * 100 : 0 };
  for (const k of NUM) o[k] = mean(rows.map((r) => Number(r[k]) || 0));
  return o;
}
const reduction = (c, l) => (c ? (c - l) / c * 100 : 0);

function barChart(title, groups, fmt, unit) {
  const W = 260, H = 190, top = 34, bot = 34, padX = 8;
  const plotH = H - top - bot;
  const max = Math.max(...groups.flatMap((g) => [g.cold, g.learned]), 0) * 1.12 || 1;
  const gw = (W - padX * 2) / groups.length;
  const bw = Math.min(24, gw * 0.38);
  const s = svg('svg', { viewBox: `0 0 ${W} ${H}`, role: 'img',
    'aria-label': title + ': ' + groups.map((g) => `${g.label} cold ${fmt(g.cold)}, learned ${fmt(g.learned)}`).join('; ') });
  s.append(svg('text', { x: padX, y: 16, class: 'ttl' }, title + (unit ? ' (' + unit + ')' : '')));
  s.append(svg('line', { x1: padX, x2: W - padX, y1: top + plotH, y2: top + plotH, class: 'ax' }));
  groups.forEach((g, i) => {
    const cx = padX + gw * i + gw / 2;
    const grp = svg('g', null, svg('title', null, `${g.label}: cold ${fmt(g.cold)} (n=${g.coldN}), learned ${fmt(g.learned)} (n=${g.learnedN})`));
    for (const [side, val, n, x] of [['cold', g.cold, g.coldN, cx - bw - 1], ['learned', g.learned, g.learnedN, cx + 1]]) {
      const h = n ? Math.max(val / max * plotH, val > 0 ? 1.5 : 0) : 0;
      grp.append(svg('rect', { x, y: top + plotH - h, width: bw, height: h, rx: 3, class: 'bar-' + side }));
      grp.append(svg('text', { x: x + bw / 2, y: top + plotH - h - 4, 'text-anchor': 'middle', class: 'vl' }, n ? fmt(val) : '–'));
    }
    grp.append(svg('text', { x: cx, y: top + plotH + 15, 'text-anchor': 'middle', class: 'lb' }, g.short));
    s.append(grp);
  });
  return el('div', { class: 'chart' }, s);
}
const kfmt = (v) => (v >= 10000 ? (v / 1000).toFixed(0) + 'k' : v >= 1000 ? (v / 1000).toFixed(1) + 'k' : v.toFixed(0));

async function loadBench() {
  let data = null, fromApi = false;
  if (S.online) {
    try { const d = await api('/api/bench'); if ((d.cold || []).length && (d.learned || []).length) { data = d; fromApi = true; } } catch { /* use static */ }
  }
  if (!data) { try { data = await stat('bench.json'); } catch { data = null; } }
  renderBench(data, fromApi);
}

function renderBench(data, fromApi) {
  const banner = $('#bench-banner');
  const hero = clear($('#bench-hero')), charts = clear($('#bench-charts')), table = clear($('#bench-table'));
  if (!data || !data.cold || !data.learned || !data.cold.length || !data.learned.length) {
    banner.hidden = false; banner.textContent = 'No benchmark results available yet.';
    return;
  }
  banner.hidden = false;
  banner.className = 'banner info';
  banner.textContent = data.sample
    ? 'Sample data: ' + (data.note || 'a single cold/learned pair. The full benchmark replaces this.')
    : `Benchmark results (${fromApi ? 'live from the backend' : 'exported snapshot'}): ${data.cold_file || 'cold'} vs ${data.learned_file || 'learned'}.`;

  const cold = data.cold, learned = data.learned;
  const fams = [...new Set([...cold, ...learned].map((r) => r.family))];
  const groups = [{ label: 'Overall', short: 'Overall', c: cold, l: learned },
    ...fams.map((f) => ({ label: f.replace(/_/g, ' '), short: trunc(f.split('_')[0], 9), c: cold.filter((r) => r.family === f), l: learned.filter((r) => r.family === f) }))]
    .map((g) => ({ ...g, ca: agg(g.c), la: agg(g.l) }));
  const all = groups[0];

  // headline numbers
  const heroItem = (label, c, l, fmt) => {
    const r = reduction(c, l);
    return el('div', { class: 'stat' },
      el('div', { class: 'big' + (r < -0.5 ? ' neg' : '') }, Math.abs(r) < 0.5 ? '0%' : (r >= 0 ? '−' : '+') + Math.abs(r).toFixed(0) + '%'),
      el('div', { class: 'l' }, label), el('div', { class: 'sub' }, 'from ' + fmt(c) + ' to ' + fmt(l) + ' per task'));
  };
  hero.append(heroItem('Steps', all.ca.steps, all.la.steps, (v) => v.toFixed(1)),
    heroItem('Tokens', all.ca.tokens, all.la.tokens, kfmt),
    heroItem('Time', all.ca.seconds, all.la.seconds, (v) => v.toFixed(1) + 's'),
    heroItem('Cost', all.ca.cost_usd, all.la.cost_usd, (v) => '$' + v.toFixed(4)),
    el('div', { class: 'stat' }, el('div', { class: 'big' + (all.la.success < all.ca.success ? ' neg' : '') }, all.la.success.toFixed(0) + '%'),
      el('div', { class: 'l' }, 'Success (learned)'), el('div', { class: 'sub' }, 'cold: ' + all.ca.success.toFixed(0) + '%')));

  charts.append(el('div', { class: 'legend', style: null }, el('span', null, el('i', { style: 'background:var(--cold)' }), 'Cold'),
    el('span', null, el('i', { style: 'background:var(--learned)' }), 'Learned')));
  charts.lastChild.style.gridColumn = '1 / -1'; charts.lastChild.style.margin = '0';
  const mk = (title, key, fmt, unit) => el('div', { class: 'card' }, barChart(title, groups.map((g) => ({
    label: g.label, short: g.short, cold: g.ca[key], learned: g.la[key], coldN: g.ca.n, learnedN: g.la.n })), fmt, unit));
  charts.append(mk('Steps per task', 'steps', (v) => v.toFixed(1)), mk('Tokens per task', 'tokens', kfmt),
    mk('Seconds per task', 'seconds', (v) => v.toFixed(1), 's'), mk('Success rate', 'success', (v) => v.toFixed(0), '%'));

  // per-task table
  const ids = [...new Set([...cold, ...learned].map((r) => r.id))];
  const pick = (rows, id) => rows.filter((r) => r.id === id);
  const head1 = el('tr', { class: 'grp' }, el('th', { colspan: 2 }), el('th', { colspan: 4 }, 'Cold'), el('th', { colspan: 5 }, 'Learned'));
  const head2 = el('tr', null, ...['Task', 'Family', 'Steps', 'Tokens', 'Sec', 'Correct', 'Steps', 'Tokens', 'Sec', 'Correct', 'Skills used'].map((h) => el('th', null, h)));
  table.append(el('thead', null, head1, head2));
  const body = el('tbody');
  const okText = (rows) => { const c = rows.filter((r) => r.correct).length; return rows.length === 1 ? (c ? 'yes' : 'no') : `${c}/${rows.length}`; };
  for (const id of ids) {
    const c = pick(cold, id), l = pick(learned, id);
    const ca = agg(c), la = agg(l);
    const fam = (c[0] || l[0] || {}).family || '';
    const cmp = (k, fmt) => (c.length && l.length && la[k] < ca[k] ? 'good' : (c.length && l.length && la[k] > ca[k] ? 'badc' : ''));
    const cell = (rows, a, k, fmt, cls) => el('td', { class: cls || '' }, rows.length ? fmt(a[k]) : '–');
    body.append(el('tr', null, el('td', null, id), el('td', null, fam.replace(/_/g, ' ')),
      cell(c, ca, 'steps', (v) => v.toFixed(1)), cell(c, ca, 'tokens', (v) => fmtNum(v)), cell(c, ca, 'seconds', (v) => v.toFixed(1)),
      el('td', null, c.length ? okText(c) : '–'),
      cell(l, la, 'steps', (v) => v.toFixed(1), cmp('steps')), cell(l, la, 'tokens', (v) => fmtNum(v), cmp('tokens')),
      cell(l, la, 'seconds', (v) => v.toFixed(1), cmp('seconds')), el('td', null, l.length ? okText(l) : '–'),
      el('td', null, l.length ? ([...new Set(l.flatMap((r) => r.skills_used || []))].join(', ') || '-') : '–')));
  }
  table.append(body);
}

// ---------------------------------------------------------------- library ---
let MEM = [];
const withParams = (str) => String(str ?? '').split(/(\{\w+\})/).map((p) => (/^\{\w+\}$/.test(p) ? el('span', { class: 'p' }, p) : p));
function locDesc(loc) {
  if (!loc) return '';
  if (loc.role) return `${loc.role} "${loc.name ?? ''}"`;
  if (loc.placeholder) return `field "${loc.placeholder}"`;
  if (loc.text) return `text "${loc.text}"`;
  if (loc.css) return `element ${loc.css}`;
  return JSON.stringify(loc);
}
function stepNode(s) {
  const v = s.value, l = locDesc(s.locator);
  const a = (t) => el('span', { class: 'act' }, t);
  switch (s.action) {
    case 'goto': return el('li', null, a('goto'), ' ', withParams(v));
    case 'click': return el('li', null, a('click'), ' ', withParams(l));
    case 'type': return el('li', null, a('type'), ' "', withParams(v), '" into ', withParams(l));
    case 'select': return el('li', null, a('select'), ' "', withParams(v), '" in ', withParams(l));
    case 'press': return el('li', null, a('press'), ' ', withParams(v));
    default: return el('li', null, a(s.action), ' ', withParams(v || l));
  }
}
function extractNode(x) {
  if (!x) return null;
  if (x.type === 'css') {
    const f = Object.keys(x.fields || {}).join(', ');
    return el('li', null, el('span', { class: 'act' }, 'extract'), ' every ', withParams(x.selector), f ? ', fields: ' + f : '');
  }
  return el('li', null, el('span', { class: 'act' }, 'extract'), ' page text');
}

function skillCard(sk) {
  const rate = sk.uses ? Math.round(sk.successes / sk.uses * 100) : null;
  const det = el('details', { class: 'sk-steps' });
  const list = el('ol', { class: 'steps' });
  let loaded = false;
  const fill = (full) => {
    clear(list);
    (full.steps || []).forEach((s) => list.append(stepNode(s)));
    const ex = extractNode(full.extract); if (ex) list.append(ex);
  };
  det.append(el('summary', { class: 'small' }, `Steps (${sk.n_steps ?? (sk.steps || []).length})`), list);
  det.addEventListener('toggle', async () => {
    if (!det.open || loaded) return;
    loaded = true;
    if (sk.steps) { fill(sk); return; }
    list.append(el('li', { class: 'muted' }, 'loading...'));
    try { fill(await api('/api/skills/' + encodeURIComponent(sk.name))); }
    catch { clear(list).append(el('li', { class: 'muted' }, 'could not load steps')); loaded = false; }
  });
  const card = el('div', { class: 'card' },
    el('h4', { class: 'sk-name' }, sk.name),
    el('div', { class: 'sk-meta' }, el('span', { class: 'tag dom' }, sk.domain || 'any site'), el('span', { class: 'tag' }, 'v' + (sk.version || 1)),
      ...(sk.params || []).map((p) => el('span', { class: 'tag used' }, '{' + p + '}')), sk.demoted ? el('span', { class: 'tag fail' }, 'demoted') : null),
    el('p', { class: 'sk-desc' }, sk.description || ''),
    el('div', { class: 'small' }, rate == null ? 'Not reused yet' : `Reused ${sk.uses}×, ${sk.successes} succeeded (${rate}%)`),
    el('div', { class: 'rate' }, el('i', { style: 'width:' + (rate || 0) + '%' })),
    det);
  if (S.online) {
    card.append(el('div', { class: 'row', style: 'margin-top:10px' }, el('button', { class: 'btn ghost danger small', type: 'button', on: { click: async () => {
      if (!confirm('Delete skill ' + sk.name + '?')) return;
      try {
        await api('/api/skills/' + encodeURIComponent(sk.name), { method: 'DELETE', headers: authHeaders() });
        loadLibrary();
      } catch (e) { alert('Could not delete: ' + e.message); }
    } } }, 'Delete')));
  }
  return card;
}

function renderMemory() {
  const f = $('#mem-filter').value;
  const box = clear($('#memory'));
  const items = MEM.filter((m) => !f || (m.metadata || {}).kind === f);
  $('#mem-count').textContent = '(' + items.length + ')';
  if (!items.length) { box.append(el('div', { class: 'empty' }, 'No memories yet. Run a task with learning on.')); return; }
  for (const m of items) {
    const md = m.metadata || {};
    box.append(el('div', { class: 'card mem-card' },
      el('div', { class: 'sk-meta' }, el('span', { class: 'tag ' + (md.kind === 'failure' ? 'fail' : md.kind === 'skill' ? 'used' : 'built') }, md.kind || 'note'),
        md.domain ? el('span', { class: 'tag dom' }, md.domain) : null),
      el('p', null, m.content || ''),
      md.task ? el('div', { class: 'muted small' }, 'from task: ' + trunc(md.task, 140)) : null));
  }
}

async function loadLibrary() {
  let skills = null, memory = null;
  const banner = $('#lib-banner');
  if (S.online) {
    try { skills = (await api('/api/skills')).skills; } catch { /* static */ }
    try { memory = (await api('/api/memory')).memories; } catch { /* static */ }
  }
  const live = skills != null || memory != null;
  if (skills == null) { try { skills = (await stat('skills.json')).skills; } catch { skills = []; } }
  if (memory == null) { try { memory = (await stat('memory.json')).memories; } catch { memory = []; } }
  banner.hidden = live;
  if (!live) banner.textContent = 'Showing an exported snapshot. Connect a backend to see its live library.';
  const box = clear($('#skills'));
  $('#skills-count').textContent = '(' + skills.length + ')';
  if (!skills.length) box.append(el('div', { class: 'empty' }, 'No skills yet. Run a task with learning on; the agent builds one when it succeeds.'));
  for (const s of skills) box.append(skillCard(s));
  MEM = memory || [];
  renderMemory();
}

// ------------------------------------------------------------------- init ---
function init() {
  S.base = normBase(lsGet('wf_backend', ''));
  S.code = lsGet('wf_code', '');
  $('#backend').value = S.base;
  $('#code').value = S.code;

  for (const b of $$('#tabs button')) b.addEventListener('click', () => showTab(b.dataset.tab));
  window.addEventListener('hashchange', routeFromHash);
  $('#to-replay').addEventListener('click', () => setTimeout(routeFromHash, 0));
  $('#btn-connect').addEventListener('click', connect);
  $('#backend').addEventListener('keydown', (e) => { if (e.key === 'Enter') connect(); });
  $('#task').addEventListener('input', onTaskInput);
  $('#btn-run').addEventListener('click', startRun);
  $('#btn-savekey').addEventListener('click', saveKey);
  $('#keyinput').addEventListener('keydown', (e) => { if (e.key === 'Enter') saveKey(); });
  $('#btn-forgetkey').addEventListener('click', forgetKey);
  $('#code').addEventListener('input', () => { S.code = $('#code').value; updateRunButton(); });
  $('#mem-filter').addEventListener('change', renderMemory);
  $('#btn-play').addEventListener('click', () => (Player.playing ? Player.pause() : Player.play()));
  $('#btn-restart').addEventListener('click', () => { if (!Player.run) return; Player.pause(); Player.begin(); Player.play(); });
  $('#demo-select').addEventListener('change', (e) => Player.select(Number(e.target.value)));
  $('#demo-speed').addEventListener('change', () => { if (Player.playing) { clearTimeout(Player.timer); Player.step(); } });
  $('#btn-reset-mem').addEventListener('click', async () => {
    if (!confirm('Erase all agent memory?')) return;
    try { await api('/api/memory/reset', { method: 'POST', headers: authHeaders() }); loadLibrary(); }
    catch (e) { alert('Could not reset: ' + e.message); }
  });

  routeFromHash();
  onTaskInput();
  loadDemo().then(() => { if (S.demo.length) { Player.run = S.demo[0]; $('#demo-note').textContent = S.demo[0].note || ''; } });
  // optional deploy-time default backend (web/data/config.json: {"backend": "https://..."})
  const boot = S.base ? Promise.resolve() : stat('config.json').then((c) => { if (c && c.backend) { S.base = normBase(c.backend); $('#backend').value = S.base; } }).catch(() => {});
  boot.then(connect);
}

document.addEventListener('DOMContentLoaded', init);
})();
