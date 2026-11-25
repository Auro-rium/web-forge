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
const finite = (n) => (n != null && n !== '' && Number.isFinite(Number(n)));
const num = (n) => (finite(n) ? Number(n) : 0);
const fmtNum = (n, d = 0) => num(n).toLocaleString(undefined, { minimumFractionDigits: d, maximumFractionDigits: d });
const fmtUsd = (n) => { const v = num(n); return '$' + v.toFixed(v > 0 && v < 0.1 ? 4 : 2); };
const arr = (x) => (Array.isArray(x) ? x : []);
const fmtArgs = (a) => { try { return trunc(JSON.stringify(a ?? {}), 90); } catch { return ''; } };

const S = { launchErr: '', wd: null, finishInfo: null, seen: new Set(), base: '', code: '', online: false, health: null, running: false, es: null, runId: null, lastN: 0, retry: 0, rt: null, reconnecting: false };

const EXAMPLES = [
  ['class registration', 'Register the four students in this list for the science fair at http://127.0.0.1:8765/forms/register.html (student tickets, morning session, they accept the terms): Ada Lovelace ada.lovelace@school.org; Alan Turing alan.turing@school.org; Maya Rao maya@school; Grace Hopper grace.hopper@school.org. Save outputs/registrations.csv with name, email and confirmation code.'],
  ['compare laptops', 'Compare 3 good student laptops under $1000 (price, battery, weight), recommend one and save outputs/laptops.csv'],
  ['fill a form', 'Go to https://httpbin.org/forms/post and order a large pizza with bacon and mushrooms for Ada Lovelace (phone 555-0100, ada@example.org), delivery at 19:00, then tell me what the server received'],
  ['HN digest', 'Summarize the top 3 stories on Hacker News right now, with the top comment of each, and save outputs/hn.md'],
  ['research + decide', 'Which is cheaper per month for 1 TB of cloud storage: Google One, iCloud+ or Dropbox? Show the prices with sources and recommend one'],
];

function normBase(v) {
  v = (v || '').trim().replace(/\/+$/, '');
  if (!v) return '';
  if (!/^[a-z][a-z0-9+.-]*:\/\//i.test(v)) {
    if (/^[a-z][a-z0-9+.-]*:/i.test(v) && !/^[^/]+:\d+(\/|$)/.test(v)) return '';   // javascript:, data:, ... (but host:port is fine)
    v = (/^(localhost|127\.|\[::1\])/.test(v) ? 'http://' : 'https://') + v;
  }
  return /^https?:\/\//i.test(v) ? v : '';
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
  renderControl();
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
  else if (S.launchErr) hint.textContent = S.launchErr;
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
  S.launchErr = '';
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
const laneIdx = (a) => parseInt(String(a || 'w0').slice(1), 10) % 6 || 0;
const laneTag = (a) => el('span', { class: 'lane lane-' + laneIdx(a) }, a || 'agent');

function describe(kind, d) {
  d = d || {};
  switch (kind) {
    case 'task_start': return trunc(d.task, 200);
    case 'resume': return 'Resuming from checkpoint: ' + d.files + ' file(s), ' + d.todos + ' todo(s)';
    case 'search': return 'Web search: ' + trunc(d.query, 160);
    case 'parallel': return 'Launching ' + trunc(d.n, 8) + ' sub-agents in parallel: ' + trunc(arr(d.agents).join(', '), 160);
    case 'read_urls': return 'Reading ' + arr(d.urls).length + ' page(s) in parallel: ' + trunc(arr(d.urls).join(', '), 160);
    case 'delegate': return 'Delegate: ' + trunc(d.subtask, 160) + (d.start_url ? ' (start ' + d.start_url + ')' : '');
    case 'observe': return 'Look at ' + trunc(d.url || 'page', 90) + ' (' + (d.n_elements ?? '?') + ' elements)';
    case 'action': return d.action + ' ' + fmtArgs(d.args) + (d.ok === false ? '  FAILED: ' : '  ') + trunc(d.message, 140);
    case 'ask': return 'Question for you: ' + trunc(d.question, 200);
    case 'answer': return /^No answer from the user/i.test(String(d.answer || '')) ? 'No answer (timed out)' : 'Your answer: ' + trunc(d.answer, 160);
    case 'analyze': return 'Analyst: ' + trunc(d.instruction, 140) + ' (' + arr(d.files).length + ' file(s))';
    case 'output': return 'Deliverable saved: ' + trunc(d.name, 120);
    case 'verdict': return d.complete ? 'Verifier: complete' : 'Verifier: incomplete, missing ' + trunc(arr(d.missing).join('; '), 160);
    case 'final': return 'Result ready';
    case 'agent_state': return 'state ' + d.state + (d.subtask ? ': ' + trunc(d.subtask, 120) : '') + (d.steps != null ? ' (' + d.steps + ' steps)' : '');
    case 'nudge': case 'budget': case 'verify': return trunc(d.message, 200);
    case 'warn': case 'error': return trunc(d.message, 240);
    default: return trunc(JSON.stringify(d), 160);
  }
}
const QUIET = new Set(['screenshot', 'ask_id', 'metrics', 'agent_state']);   // not in the Activity timeline
const NOLOG = new Set(['screenshot', 'ask_id', 'metrics']);                  // not in the Monitor log

// ---- approvals / questions queue, keyed by ask_id --------------------------
// Everything shown on an approval card is derived from the structured `meta` (reason, url, fields). The host is taken
// from the PARSED url (never from page-controlled text); the page-controlled button text is only shown as a quoted label.
// Approvals without usable meta are never grouped and show the full question text (see `legacy`).
const titleOf = (reason) => { const m = String(reason).match(/^click\s+['"](.+)['"]\s*$/i); return m ? m[1] : null; };
function urlInfo(u) {
  u = String(u || '');
  try {
    const x = new URL(u);
    if (x.protocol !== 'http:' && x.protocol !== 'https:') return { ok: false, raw: u, host: '', origin: 'raw:' + u };
    return { ok: true, raw: u, host: x.host, origin: x.origin, base: x.protocol + '//' + x.host, ui: !!(x.username || x.password),
      path: x.pathname + x.search + x.hash };
  } catch { return { ok: false, raw: u, host: '', origin: 'raw:' + u }; }
}
// host first, never shortened; only the path is cut. Userinfo (user:pass@) is dropped from the text and flagged.
function urlText(i) {
  if (!i.ok) return i.raw ? 'not a recognised web address: ' + trunc(i.raw, 120) : '';
  return i.base + trunc(i.path === '/' ? '' : i.path, 100) + (i.ui ? '  (the address has a login part before the host; it is ignored)' : '');
}
let askSeq = 0;
const FIELDS_SHOWN = 12, VALUE_MAX = 200, NOTE_MS = 8000;

const Asks = {
  map: new Map(),      // ask_id -> ask
  cards: new Map(),    // group key -> {root, ...refs}
  box() { return $('#asks'); },
  say(text) { const s = $('#sr-status'); if (s) s.textContent = text; },
  note(text) {
    const box = $('#ask-notes'); if (!box) return;
    const n = el('p', { class: 'small ask-note' }, text);
    box.append(n);
    while (box.childElementCount > 6) box.firstElementChild.remove();
    setTimeout(() => n.remove(), NOTE_MS);
    this.say(text);
  },
  add(d) {
    if (!d || (typeof d.ask_id !== 'string' && typeof d.ask_id !== 'number') || d.ask_id === '' || this.map.has(String(d.ask_id))) return;
    const q = String(d.question == null ? '' : d.question);
    const meta = d.meta && typeof d.meta === 'object' && !Array.isArray(d.meta) ? d.meta : {};
    const wantsApproval = (d.kind || meta.kind) === 'approval' || /^Approve:/.test(q);
    const options = Array.isArray(d.options) ? d.options.map(String) : [];
    const a = { id: String(d.ask_id), q, options, kind: 'question', agent: d.agent ? String(d.agent) : '',
      deadline: Date.now() + (Number(d.timeout_s) || 90) * 1000, err: '', busy: false, legacy: false, title: 'question' };
    const fieldsOk = meta.fields == null || (typeof meta.fields === 'object' && !Array.isArray(meta.fields));
    if (wantsApproval && meta.kind === 'approval' && typeof meta.reason === 'string' && typeof meta.url === 'string' && fieldsOk) {
      a.kind = 'approval';
      a.reason = meta.reason; a.url = meta.url;
      a.info = urlInfo(meta.url);
      a.fields = Object.entries(meta.fields || {}).map(([k, v]) => [String(k), typeof v === 'string' ? v : JSON.stringify(v)]);
      a.label = titleOf(a.reason);
      a.title = 'Action on ' + (a.info.ok ? a.info.host : 'an unrecognised address');
      a.gkey = 'g:' + a.reason + '\n' + a.info.origin + '\n' + (a.info.ui ? 'ui' : '');
    } else {
      a.gkey = 'q:' + a.id;            // never grouped
      if (wantsApproval) { a.legacy = true; a.title = 'approval'; if (!a.options.length) a.options = ['approve', 'deny']; }
    }
    this.map.set(a.id, a);
    this.render(true);
  },
  remove(id) { if (this.map.delete(id)) this.render(); },
  clear() { this.map.clear(); this.render(); },
  hasAgent(agent) { for (const a of this.map.values()) if (a.agent === agent) return true; return false; },
  onAnswer(d) {
    d = d || {};
    const timedOut = /^No answer from the user/i.test(String(d.answer || ''));
    let a = d.ask_id != null ? this.map.get(String(d.ask_id)) : null;
    if (!a && d.ask_id == null && d.agent) for (const x of this.map.values()) if (x.agent === d.agent) { a = x; break; }   // untagged: oldest ask of that agent
    if (!a) return;
    if (timedOut) this.note('Timed out: "' + trunc(a.label || a.title, 60) + '". The agent moved on.');
    this.remove(a.id);
  },
  groups() {
    const g = new Map();
    for (const a of this.map.values()) { if (!g.has(a.gkey)) g.set(a.gkey, []); g.get(a.gkey).push(a); }
    return g;
  },
  async send(ids, text, all) {
    if (!S.runId || !ids.length) return;
    const items = ids.map((i) => this.map.get(i)).filter(Boolean);
    if (!items.length || items.some((a) => a.busy)) return;             // double submit guard
    let focusId = null;
    if (!all) {
      const list = this.groups().get(items[0].gkey) || [];
      const i = list.findIndex((a) => a.id === items[0].id);
      const nxt = list[i + 1] || list[i - 1];
      focusId = nxt ? nxt.id : null;
    }
    const gkey = items[0].gkey;
    for (const a of items) { a.busy = true; a.err = ''; }
    this.render();
    try {
      const run = '/api/runs/' + encodeURIComponent(S.runId);
      if (all) {
        const res = await postJSON(run + '/answer_all', { answer: String(text), ask_ids: items.map((a) => a.id) });
        for (const a of items) this.map.delete(a.id);
        if (res && typeof res.resolved === 'number' && res.resolved < items.length) {
          this.note((items.length - res.resolved) + ' of ' + items.length + ' had already expired or been answered, so your "' + text + '" did not reach the agent for them.');
        }
      } else {
        await postJSON(run + '/answer', { ask_id: ids[0], answer: String(text) });
        for (const a of items) this.map.delete(a.id);
      }
    } catch (e) {
      const gone = e && (e.status === 404 || e.status === 409 || e.status === 410);
      const unknownRun = gone && /unknown run/i.test(String(e.message));
      for (const a of items) {
        a.busy = false;
        if (gone) {
          this.map.delete(a.id);
          this.note(unknownRun ? 'The backend no longer knows this run (it may have restarted). Press Resume.'
            : '"' + trunc(a.label || a.title, 60) + '" was already answered or had expired; nothing was sent.');
        } else a.err = (e && e.message) || 'could not send';
      }
    }
    this.render();
    this.refocus(gkey, focusId);
  },
  refocus(gkey, itemId) {
    let t = null;
    const c = this.cards.get(gkey);
    if (c && c.approval) {
      const li = itemId && c.items.get(itemId);
      t = (li && !li.ok.disabled ? li.ok : null) || (!c.bAll.disabled ? c.bAll : null);
    } else if (c && c.input && !c.input.disabled && !c.input.hidden) t = c.input;
    if (!t) { const first = $('#asks .ask button:not(:disabled)'); t = first || $('#ask-pill:not([hidden])'); }
    if (t) t.focus({ preventScroll: true });
  },
  buildCard(gkey, first) {
    const approval = first.kind === 'approval';
    const c = { approval, items: new Map(), n: 0, sig: '', lock: 0, confirmed: [] };
    const tid = 'ask-t-' + (++askSeq);
    c.title = el('span', { class: 'a-title', id: tid });
    c.timer = el('span', { class: 'muted mono small a-timer', 'aria-hidden': 'true' });
    c.sub = el('p', { class: 'small muted a-sub' });
    c.label = el('p', { class: 'small a-label' });
    c.err = el('p', { class: 'small a-err', role: 'status' });
    c.root = el('div', { class: 'card ask' + (approval ? ' approval' : first.legacy ? ' legacy' : ''), role: 'group', 'aria-labelledby': tid },
      el('h3', null, c.title, ' ', c.timer), c.sub, c.label);
    if (approval) {
      c.list = el('ul', { class: 'ask-items' });
      // "all" acts on the ids that were on screen and stable (c.confirmed), never on whatever joined a moment ago
      const ids = () => c.confirmed.filter((id) => this.map.has(id));
      c.bAll = el('button', { class: 'btn primary', type: 'button', on: { click: () => this.send(ids(), 'approve', true) } });
      c.dAll = el('button', { class: 'btn danger', type: 'button', on: { click: () => this.send(ids(), 'deny', true) } }, 'Deny all');
      c.root.append(c.list, el('div', { class: 'row wrap-r ask-actions' }, c.bAll, c.dAll), c.err);
    } else {
      c.q = el('p', { class: 'ask-q' });
      c.opts = el('div', { class: 'row wrap-r' });
      c.input = el('input', { class: 'grow', autocomplete: 'off', placeholder: 'or type an answer', 'aria-label': 'your answer' });
      const send = () => { const v = c.input.value.trim(); if (v && !c.input.disabled) this.send([first.id], v, false); };
      c.input.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); send(); } });
      c.btn = el('button', { class: 'btn primary', type: 'button', on: { click: send } }, 'Send');
      c.inRow = el('div', { class: 'row' }, c.input, c.btn);
      c.inRow.hidden = !!first.legacy;     // an approval whose details could not be verified: only the explicit options
      c.root.append(c.q, c.opts, c.inRow, c.err);
    }
    return c;
  },
  buildFields(a) {
    const box = el('div', { class: 'iv mono' });
    if (!a.fields.length) { box.append(el('div', { class: 'f muted' }, '(no form values)')); return box; }
    const row = ([k, v]) => {
      const long = v.length > VALUE_MAX;
      const vs = el('span', { class: 'fv' }, long ? v.slice(0, VALUE_MAX) + '…' : v);
      const r = el('div', { class: 'f' }, k ? el('span', { class: 'fk' }, k + ': ') : null, vs);
      if (long) {
        const b = el('button', { class: 'more', type: 'button', 'aria-expanded': 'false' }, 'show all ' + v.length + ' characters');
        b.addEventListener('click', () => {
          const open = b.getAttribute('aria-expanded') === 'true';
          vs.textContent = open ? v.slice(0, VALUE_MAX) + '…' : v;
          b.setAttribute('aria-expanded', String(!open)); b.textContent = open ? 'show all ' + v.length + ' characters' : 'show less';
        });
        r.append(' ', b);
      }
      return r;
    };
    for (const f of a.fields.slice(0, FIELDS_SHOWN)) box.append(row(f));
    if (a.fields.length > FIELDS_SHOWN) {
      const rest = el('div', { class: 'f-rest' }); rest.hidden = true;
      for (const f of a.fields.slice(FIELDS_SHOWN)) rest.append(row(f));
      const n = a.fields.length - FIELDS_SHOWN;
      const b = el('button', { class: 'more', type: 'button', 'aria-expanded': 'false' }, '+' + n + ' more field' + (n === 1 ? '' : 's'));
      b.addEventListener('click', () => {
        rest.hidden = !rest.hidden; b.setAttribute('aria-expanded', String(!rest.hidden));
        b.textContent = rest.hidden ? '+' + n + ' more field' + (n === 1 ? '' : 's') : 'show fewer fields';
      });
      box.append(rest, b);
    }
    return box;
  },
  buildItem(a) {
    const li = { url: el('div', { class: 'iu mono small' }), ok: null, no: null };
    li.fields = this.buildFields(a);
    li.ok = el('button', { class: 'btn small', type: 'button', on: { click: () => this.send([a.id], 'approve', false) } }, 'approve');
    li.no = el('button', { class: 'btn small danger', type: 'button', on: { click: () => this.send([a.id], 'deny', false) } }, 'deny');
    li.root = el('li', null, laneTag(a.agent), el('div', { class: 'ibody' }, li.url, li.fields), el('span', { class: 'ib' }, li.ok, li.no));
    return li;
  },
  render(isNew) {
    const box = this.box(); if (!box) return;
    const groups = this.groups();
    for (const [k, c] of this.cards) if (!groups.has(k)) { c.root.remove(); this.cards.delete(k); }
    let fresh = null;
    for (const [k, list] of groups) {
      let c = this.cards.get(k);
      const n = list.length, first = list[0];
      if (!c) { c = this.buildCard(k, first); this.cards.set(k, c); box.append(c.root); fresh = c; }
      c.err.textContent = list.map((a) => a.err).find(Boolean) || '';
      const busy = list.some((a) => a.busy);
      if (c.approval) {
        const sig = list.map((a) => a.id).join(',');
        if (sig !== c.sig) { c.sig = sig; c.lock = Date.now() + 1000; setTimeout(() => this.render(), 1050); }   // group changed: "all" is locked for ~1 s
        const locked = Date.now() < c.lock;
        if (!locked) c.confirmed = list.map((a) => a.id);
        const urls = new Set(list.map((a) => a.url));
        const same = urls.size <= 1;
        c.title.textContent = first.title + (n > 1 ? ' ×' + n : '');
        c.sub.textContent = same ? (first.url ? 'on ' + urlText(first.info) : 'no address given') : 'on ' + (first.info.ok ? first.info.host : 'several addresses') + ' (the pages differ, each is listed below)';
        c.sub.title = same ? first.url : '';
        c.label.textContent = first.label != null ? 'Click button “' + trunc(first.label, 60) + '”' : 'Action “' + trunc(first.reason, 60) + '”';
        c.label.title = first.reason;
        c.bAll.textContent = n > 1 ? 'Approve all (' + n + ')' : 'Approve';
        c.dAll.textContent = n > 1 ? 'Deny all' : 'Deny';
        c.bAll.disabled = c.dAll.disabled = busy || locked;
        const seen = new Set();
        for (const a of list) {
          seen.add(a.id);
          let li = c.items.get(a.id);
          if (!li) { li = this.buildItem(a); c.items.set(a.id, li); c.list.append(li.root); }
          li.url.textContent = same ? '' : urlText(a.info); li.url.title = a.url; li.url.hidden = same;
          li.ok.disabled = li.no.disabled = a.busy;
        }
        for (const [id, li] of c.items) if (!seen.has(id)) { li.root.remove(); c.items.delete(id); }
        c.list.hidden = n === 1 && !list[0].fields.length && same;
        if (c.n !== n) this.say('New approval on ' + (first.info.ok ? first.info.host : 'an unrecognised address') + ': ' + trunc(first.label || first.reason, 60) + ' (' + n + ')');
      } else {
        c.title.textContent = (first.legacy ? 'Approval needed' : 'WebForge needs you') + (first.agent ? ' (' + first.agent + ')' : '');
        c.sub.textContent = first.legacy ? 'This approval has no verified details. Read the full text below.' : ''; c.sub.hidden = !c.sub.textContent;
        c.label.hidden = true;
        c.q.textContent = first.q;
        if (!c.opts.childElementCount && first.options.length) {
          for (const o of first.options) {
            c.opts.append(el('button', { class: 'btn' + (o === 'approve' ? ' primary' : o === 'deny' ? ' danger' : ''), type: 'button',
              on: { click: () => this.send([first.id], String(o), false) } }, String(o)));
          }
        }
        for (const b of $$('button', c.root)) b.disabled = busy;
        c.input.disabled = busy;
        if (!c.n) this.say((first.legacy ? 'Approval needed from ' : 'Question from ') + (first.agent || 'the agent'));
      }
      c.n = n;
    }
    if (fresh && isNew && this.map.size === 1) fresh.root.scrollIntoView({ behavior: 'smooth', block: 'start' });
    const pill = $('#ask-pill');
    if (pill) {
      const total = this.map.size, onlyAppr = Array.from(this.map.values()).every((a) => a.kind === 'approval' || a.legacy);
      pill.hidden = total === 0;
      pill.textContent = total + (onlyAppr ? (total === 1 ? ' approval' : ' approvals') : (total === 1 ? ' question' : ' questions')) + ' waiting ↑';
      $('#live').classList.toggle('has-pill', total > 0);
    }
    this.tick();
    Live.updateStatus();
    Agents.refresh();
  },
  tick() {
    const now = Date.now();
    let expired = false;
    for (const a of Array.from(this.map.values())) {
      if (a.deadline - now <= 0) { this.map.delete(a.id); expired = true; this.note('Timed out: "' + trunc(a.label || a.title, 60) + '". The agent moved on.'); }
    }
    if (expired) { this.render(); return; }
    for (const [k, c] of this.cards) {
      const list = this.groups().get(k);
      if (!list) continue;
      const left = Math.max(0, (Math.min(...list.map((a) => a.deadline)) - now) / 1000);
      c.timer.textContent = Math.ceil(left) + 's left';
    }
  },
};

// ---- per-agent state: tiles + table ----------------------------------------
const STATE_LABEL = Object.assign(Object.create(null), { skipped: 'not started', queued: 'queued', running: 'running', waiting: 'waiting for you', done: 'done', failed: 'failed', stopped: 'stopped' });
const Agents = {
  map: new Map(),
  get(id, create, state) {
    if (typeof id !== 'string' && typeof id !== 'number') return null;
    id = String(id); if (!id) return null;
    let a = this.map.get(id);
    if (!a && create) {
      a = { id, subtask: '', state: state || 'running', steps: 0, last: '', lastAct: Date.now(), url: '', hasShot: false };
      this.build(a); this.map.set(id, a);
      $('#tiles-empty').hidden = true;
      $('#mission').hidden = false;
      Mon.addAgentOption(id);
    }
    return a;
  },
  build(a) {
    a.tile = el('div', { class: 'tile', 'data-agent': a.id });
    a.chip = el('span', { class: 'chip-state' });
    a.sub = el('p', { class: 'tile-sub' });
    a.meta = el('p', { class: 'tile-meta mono small' });
    a.last_ = el('p', { class: 'tile-last mono small' });
    a.shotBox = el('div', { class: 'tile-shot' });
    a.ph = el('span', { class: 'ph small' });
    a.shotBox.append(a.ph);
    a.tile.append(el('div', { class: 'tile-head' }, laneTag(a.id), a.chip), a.sub, a.meta, a.last_, a.shotBox);
    $('#tiles').append(a.tile);
    a.row = el('tr', null);
    a.cAgent = el('td'); a.cAgent.append(laneTag(a.id));
    a.cSubTx = el('div', { class: 'clamp2' }); a.cSub = el('td', { class: 'c-sub' }, a.cSubTx); a.cState = el('td'); a.cSteps = el('td', { class: 'num' });
    a.cLastTx = el('div', { class: 'clamp2' }); a.cLast = el('td', { class: 'c-last' }, a.cLastTx); a.cIdle = el('td', { class: 'num' });
    a.row.append(a.cAgent, a.cSub, a.cState, a.cSteps, a.cLast, a.cIdle);
    $('#agent-rows').append(a.row);
  },
  eff(a) { return (a.state === 'running' && Asks.hasAgent(a.id)) ? 'waiting' : a.state; },
  paint(a) {
    const st = this.eff(a);
    a.tile.className = 'tile st-' + st;
    a.chip.className = 'chip-state st-' + st;
    a.chip.textContent = STATE_LABEL[st] || st;
    a.sub.textContent = a.subtask || '(no subtask yet)';
    a.meta.textContent = a.steps + ' step' + (a.steps === 1 ? '' : 's') + (a.url ? ' · ' + trunc(a.url, 48) : '');
    a.last_.textContent = a.last ? '› ' + a.last : '› (nothing yet)';
    if (!a.hasShot) a.ph.textContent = S.health && S.health.real_browser ? 'working in your Chrome window' : 'no screenshot yet';
    a.cSubTx.textContent = a.subtask || ''; a.cSubTx.title = a.subtask || '';
    a.cState.textContent = STATE_LABEL[st] || st; a.cState.className = 'st-' + st;
    a.cSteps.textContent = String(a.steps);
    a.cLastTx.textContent = a.last || ''; a.cLastTx.title = a.last || '';
    this.paintIdle(a);
  },
  paintIdle(a) {
    const st = this.eff(a);
    a.cIdle.textContent = (st === 'done' || st === 'failed' || st === 'stopped' || st === 'skipped') ? '-' : Math.max(0, Math.round((Date.now() - a.lastAct) / 1000)) + 's';
  },
  refresh() { for (const a of this.map.values()) this.paint(a); this.counts(); },
  tickIdle() { for (const a of this.map.values()) this.paintIdle(a); },
  counts() {
    let run = 0, wait = 0, done = 0;
    for (const a of this.map.values()) { const s = this.eff(a); if (s === 'running' || s === 'queued') run++; else if (s === 'waiting') wait++; else done++; }
    return { run, wait, done };
  },
  // run is over: nothing may stay running / queued / pulsing "waiting for you"
  settle(status) {
    for (const a of this.map.values()) {
      if (a.state === 'queued') a.state = 'skipped';          // never got to run
      else if (a.state === 'running' || a.state === 'waiting') {
        a.state = status === 'done' ? 'done' : (status === 'cancelled' ? 'stopped' : 'failed');
      }
    }
    this.refresh();
  },
  touch(d) {
    const a = this.get(d && d.agent, false);
    if (a) { a.lastAct = Date.now(); if (a.state === 'queued') a.state = 'running'; }
    return a;
  },
  onEvent(kind, d) {
    d = d || {};
    switch (kind) {
      case 'parallel':
        for (const id of (Array.isArray(d.agents) ? d.agents : [])) { const a = this.get(String(id), true, 'queued'); if (a) { a.lastAct = Date.now(); this.paint(a); } }
        break;
      case 'delegate': {
        const a = this.get(d.agent, true); if (!a) break;
        if (d.subtask) a.subtask = String(d.subtask);
        if (a.state === 'queued') a.state = 'running';
        a.lastAct = Date.now(); this.paint(a); break;
      }
      case 'agent_state': {
        const a = this.get(d.agent, true); if (!a) break;
        if (typeof d.state === 'string' && STATE_LABEL[d.state]) a.state = d.state;
        else if (a.state === 'queued') a.state = 'running';
        if (d.subtask) a.subtask = String(d.subtask);
        if (d.steps != null && !isNaN(Number(d.steps))) a.steps = Number(d.steps);
        if (d.last) a.last = trunc(String(d.last), 110);
        a.lastAct = Date.now(); this.paint(a); break;
      }
      case 'action': {
        const a = this.touch(d); if (!a) break;
        if (d.ok !== false) a.steps += 1;
        a.last = trunc(describe('action', d), 110); this.paint(a); break;
      }
      case 'observe': {
        const a = this.touch(d); if (!a) break;
        if (d.url) a.url = String(d.url);
        this.paint(a); break;
      }
      case 'screenshot': {
        const a = this.get(d.agent, true); if (!a) break;
        a.lastAct = Date.now();
        if (/^[A-Za-z0-9+/=]+$/.test(d.jpeg_b64 || '')) {
          if (!a.img) { a.img = el('img', { alt: 'browser view of ' + a.id }); clear(a.shotBox).append(a.img); }
          a.img.src = 'data:image/jpeg;base64,' + d.jpeg_b64; a.hasShot = true;
        }
        if (d.url) a.url = String(d.url);
        this.paint(a); break;
      }
      default: this.touch(d);
    }
  },
  reset() {
    this.map.clear(); $('#mission').hidden = true;
    clear($('#tiles')).append(el('span', { class: 'muted small', id: 'tiles-empty' }, 'no sub-agents yet')); clear($('#agent-rows'));
  },
};

// ---- monitor: gauges, log, sparkline ---------------------------------------
const LOG_CAP = 600;
const TL_CAP = 400;
const Mon = {
  g: {}, buckets: new Array(40).fill(0), cur: 0, paused: false, lines: [], timer: null, agentsSeen: new Set(), seen: false, fallbackAt: 0,
  init() {
    const box = $('#gauges');
    const bar = (id, label) => {
      const v = el('span', { class: 'gv mono' }, '-'); const fill = el('i');
      const root = el('div', { class: 'gauge' }, el('div', { class: 'gl' }, el('span', null, label), v), el('div', { class: 'bar' }, fill));
      this.g[id] = { root, v, fill }; box.append(root);
    };
    const stat = (id, label) => {
      const v = el('span', { class: 'sv' }, '-');
      this.g[id] = { v, root: el('div', { class: 'gstat' }, v, el('span', { class: 'gl2' }, label)) }; box.append(this.g[id].root);
    };
    bar('elapsed', 'Elapsed / deadline'); bar('cost', 'Run cost / budget'); bar('global', 'Global spend / cap');
    stat('llm', 'LLM calls'); stat('tokens', 'Tokens'); stat('steps', 'Browser steps'); stat('agents', 'Agents active / waiting / done');
  },
  setBar(id, val, max, text) {
    const g = this.g[id]; if (!g) return;
    g.v.textContent = text;
    const f = max > 0 && finite(val) ? Math.min(1, Math.max(0, val / max)) : 0;
    g.fill.style.width = (f * 100).toFixed(1) + '%';
    g.root.classList.toggle('warn', f > 0.7 && f <= 0.9);
    g.root.classList.toggle('crit', f > 0.9);
  },
  dash() {
    for (const id of ['elapsed', 'cost', 'global']) this.setBar(id, 0, 0, '-');
    for (const id of ['llm', 'tokens', 'steps', 'agents']) this.g[id].v.textContent = '-';
  },
  agentsGauge(counts) { this.g.agents.v.textContent = counts.run + ' / ' + counts.wait + ' / ' + counts.done; },
  // metrics events are live-only: a late or reconnecting client (or an older backend) may never get one
  metrics(m) {
    m = m && typeof m === 'object' ? m : {};
    this.seen = true;
    const n = (x) => Math.max(0, num(x));
    const has = (x) => finite(x);
    const lim = (x) => (has(x) && Number(x) > 0 ? Number(x) : 0);
    this.setBar('elapsed', n(m.elapsed), lim(m.deadline_s), has(m.elapsed) ? fmtNum(n(m.elapsed), 0) + 's' + (lim(m.deadline_s) ? ' / ' + fmtNum(m.deadline_s) + 's' : '') : '-');
    this.setBar('cost', n(m.run_cost), lim(m.run_budget), has(m.run_cost) ? fmtUsd(n(m.run_cost)) + (lim(m.run_budget) ? ' / ' + fmtUsd(m.run_budget) : '') : '-');
    this.setBar('global', n(m.global_spend), lim(m.global_cap), has(m.global_spend) ? fmtUsd(n(m.global_spend)) + (lim(m.global_cap) ? ' / ' + fmtUsd(m.global_cap) : '') : '-');
    this.g.llm.v.textContent = has(m.llm_calls) ? fmtNum(n(m.llm_calls)) : '-';
    this.g.tokens.v.textContent = has(m.tokens) ? fmtNum(n(m.tokens)) : '-';
    this.g.steps.v.textContent = has(m.steps) ? fmtNum(n(m.steps)) : '-';
    if (m.active && typeof m.active === 'object') {
      let run = 0, wait = 0, done = 0;
      for (const [id, s0] of Object.entries(m.active)) {
        const s = s0 === 'running' && Asks.hasAgent(id) ? 'waiting' : s0;   // the server does not know about open approval cards
        if (s === 'running' || s === 'queued') run++; else if (s === 'waiting') wait++; else done++;
      }
      this.agentsGauge({ run, wait, done });
    } else this.agentsGauge(Agents.counts());
  },
  // no metrics events (older backend / replay): local clock and local agent counts
  local() {
    if (this.seen) return;
    const el_ = (performance.now() - Live.t0) / 1000;
    this.setBar('elapsed', 0, 0, fmtNum(el_, 0) + 's');
    this.g.steps.v.textContent = fmtNum(Live.actions);
    this.agentsGauge(Agents.counts());
  },
  addAgentOption(id) {
    if (this.agentsSeen.has(id)) return;
    this.agentsSeen.add(id);
    $('#lf-agent').append(el('option', { value: id }, id));
  },
  level(kind, d) {
    if (kind === 'action') return d.ok === false ? 'warn' : 'action';
    if (kind === 'ask' || kind === 'answer') return 'ask';
    if (kind === 'warn') return 'warn';
    if (kind === 'error') return 'error';
    if (kind === 'verdict') return d.complete ? 'info' : 'warn';
    if (kind === 'agent_state' && d.state === 'failed') return 'warn';
    return 'info';
  },
  log(ev) {
    const d = ev.data || {};
    if (d.agent) this.addAgentOption(String(d.agent));
    const level = this.level(ev.kind, d);
    const time = typeof ev.t === 'number' ? ev.t.toFixed(1) + 's' : '';
    const msg = describe(ev.kind, d);
    const agent = d.agent ? String(d.agent) : '';
    const text = [time, level, agent, ev.kind, msg].filter(Boolean).join(' · ');
    const row = el('div', { class: 'll lv-' + level },
      el('span', { class: 'lt' }, time), el('span', { class: 'lv' }, level),
      el('span', { class: 'la' }, agent ? laneTag(agent) : ''), el('span', { class: 'lk' }, ev.kind), el('span', { class: 'lm' }, msg));
    row.dataset.level = level; row.dataset.agent = agent; row.dataset.text = text.toLowerCase(); row.dataset.plain = text;
    const box = $('#loglines');
    box.append(row);
    while (box.childElementCount > LOG_CAP) box.firstElementChild.remove();
    row.hidden = !this.match(row);
    if (!this.paused) box.scrollTop = box.scrollHeight;
  },
  match(row) {
    const lv = $('#lf-level').value, ag = $('#lf-agent').value, q = $('#lf-text').value.trim().toLowerCase();
    return (!lv || row.dataset.level === lv) && (!ag || row.dataset.agent === ag) && (!q || row.dataset.text.includes(q));
  },
  refilter() {
    for (const r of $$('#loglines .ll')) r.hidden = !this.match(r);
    if (!this.paused) { const b = $('#loglines'); b.scrollTop = b.scrollHeight; }
  },
  spark() {
    const pts = this.buckets.map((v, i) => i * (200 / 39) + ',' + 38).join(' ');
    const max = Math.max(3, ...this.buckets);
    $('#spark-line').setAttribute('points', this.buckets.map((v, i) => (i * (200 / 39)).toFixed(1) + ',' + (38 - (v / max) * 36).toFixed(1)).join(' ') || pts);
    $('#spark-now').textContent = (this.buckets[this.buckets.length - 2] || 0) + '/s';
  },
  second() {
    this.buckets.push(this.cur); this.buckets.shift(); this.cur = 0;
    this.spark(); Agents.tickIdle(); Asks.tick();
    if (!this.seen && Date.now() >= this.fallbackAt) this.local();
  },
  start() { this.stop(); this.seen = false; this.fallbackAt = Date.now() + 3000; this.timer = setInterval(() => this.second(), 1000); },
  stop() { if (this.timer) { clearInterval(this.timer); this.timer = null; } },
  reset() {
    this.stop();
    this.buckets.fill(0); this.cur = 0; this.paused = false;
    $('#lf-pause').textContent = 'Pause scroll';
    clear($('#loglines'));
    this.agentsSeen.clear();
    clear($('#lf-agent')).append(el('option', { value: '' }, 'all'));
    $('#lf-level').value = ''; $('#lf-text').value = '';
    this.seen = false;
    this.dash();
    this.spark();
  },
  async copy() {
    const text = $$('#loglines .ll').filter((r) => !r.hidden).map((r) => r.dataset.plain).join('\n');
    const b = $('#lf-copy');
    try { await navigator.clipboard.writeText(text); b.textContent = 'Copied'; }
    catch {
      try { const ta = el('textarea', { style: 'position:fixed;opacity:0' }); ta.value = text; document.body.append(ta); ta.select(); document.execCommand('copy'); ta.remove(); b.textContent = 'Copied'; }
      catch { b.textContent = 'Copy failed'; }
    }
    setTimeout(() => { b.textContent = 'Copy'; }, 1500);
  },
};

function renderControl() {
  const h = S.health, line = $('#ctl-line'), note = $('#ctl-note');
  const mode = h && h.browser_mode;
  line.hidden = !mode;
  line.textContent = mode ? 'Controlling: ' + mode : '';
  note.hidden = !(h && h.real_browser);
  for (const a of Agents.map.values()) Agents.paint(a);
}

const Live = {
  t0: 0, timer: null, actions: 0, lastErr: '',
  reset(task, title) {
    this.actions = 0; this.lastErr = '';
    $('#metrics').hidden = false;
    clear($('#ask-notes')); $('#live-reason').textContent = ''; $('#live-err').textContent = ''; $('#live').classList.remove('has-pill');
    $('#live').hidden = false;
    $('#live-title').textContent = title;
    $('#live-task').textContent = task;
    clear($('#todos')).append(el('li', { class: 'muted' }, 'waiting for the plan...'));
    clear($('#timeline'));
    Agents.reset();
    Mon.reset();
    Mon.start();
    renderControl();
    $('#lf-dl').href = apiUrl('/api/runs/' + encodeURIComponent(S.runId) + '/log');
    $('#answer-card').hidden = true;
    $('#verify-badge').hidden = true;
    $('#verify-note').hidden = true;
    clear($('#files')).hidden = true;
    Asks.clear();
    this.status('running');
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
    b.className = 'badge ' + (s === 'running' ? 'running' : s === 'done' ? 'done' : s === 'waiting for you' ? 'ask' : (s === 'cancelled' || s === 'reconnecting') ? '' : 'error');
  },
  updateStatus() { if (S.running && !S.reconnecting) this.status(Asks.map.size ? 'waiting for you' : 'running'); },
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
  push(ev) {
    if (!ev || typeof ev !== 'object' || ev.kind == null || ev.kind === '') return;
    const kind = String(ev.kind); const d = ev.data && typeof ev.data === 'object' && !Array.isArray(ev.data) ? ev.data : {};
    if (kind !== ev.kind) ev = { ...ev, kind };
    if (kind === 'metrics') { Mon.metrics(d); $('#metrics').hidden = true; return; }
    this.lastErr = kind === 'error' ? String(d.message || '') : '\0';
    Mon.cur += 1;
    Agents.onEvent(kind, d);
    switch (kind) {
      case 'todos': this.renderTodos(Array.isArray(d.todos) ? d.todos : []); return;
      case 'ask_id': Asks.add(d); return;
      case 'answer': Asks.onAnswer(d); break;
      case 'action': if (d.ok !== false) this.actions++; break;
      case 'final': this.answer(d); break;
      default: break;
    }
    if (!NOLOG.has(kind)) Mon.log(ev);
    if (QUIET.has(kind)) return;
    const fail = (kind === 'action' && d.ok === false) || (kind === 'verdict' && !d.complete);
    const tl = $('#timeline');
    const stick = tl.scrollTop + tl.clientHeight >= tl.scrollHeight - 30;
    tl.append(el('li', { class: 'k-' + String(kind).replace(/[^\w-]/g, '_') + (fail ? ' fail' : '') },
      el('span', { class: 'ts' }, typeof ev.t === 'number' ? ev.t.toFixed(1) + 's' : ''),
      el('span', { class: 'kd' }, kind.replace(/_/g, ' ')),
      el('span', { class: 'tx' }, d.agent ? laneTag(d.agent) : '', describe(kind, d))));
    while (tl.childElementCount > TL_CAP) tl.firstElementChild.remove();
    if (stick) tl.scrollTop = tl.scrollHeight;
    if (!Mon.seen) this.renderMetrics();
  },
  answer(d) {
    $('#answer-card').hidden = false;
    $('#answer').textContent = String(d.answer || '(no answer)');
    const b = $('#answer-badge'); b.textContent = d.ok === false ? 'incomplete' : 'complete'; b.className = 'badge ' + (d.ok === false ? 'bad' : 'ok');
    const v = d.verification && typeof d.verification === 'object' ? d.verification : {};
    if (Object.keys(v).length) {
      const list = (x) => (Array.isArray(x) ? x.map(String) : []);
      const miss = list(v.missing), uns = list(v.unsupported_claims);
      const vb = $('#verify-badge'); vb.hidden = false;
      vb.textContent = v.complete ? '✓ verified' : '⚠ verifier: incomplete';
      vb.className = 'badge ' + (v.complete ? 'ok' : 'bad');
      const note = $('#verify-note');
      const parts = [v.verdict != null ? String(v.verdict) : '', miss.length ? 'Missing: ' + miss.join('; ') : '',
        uns.length ? 'Unsupported: ' + uns.join('; ') : ''].filter(Boolean);
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
    Mon.stop();
    Asks.clear();
    const td = $('#todos');
    if (td.children.length === 1 && td.firstChild.classList.contains('muted')) {
      td.firstChild.textContent = status === 'done' ? 'No plan needed: the task was simple.' : '(the run ended before a plan was made)';
    }
    Agents.settle(status);
    for (const li of $$('#todos li.doing')) li.className = status === 'cancelled' ? 'halted' : (status === 'done' ? 'done' : 'failed');
    if (error && error !== 'cancelled' && status !== 'done') $('#live-reason').textContent = trunc(String(error), 200);
    Asks.say(status === 'done' ? 'Run finished' : status === 'cancelled' ? 'Run stopped' : 'Run failed' + (error ? ': ' + trunc(String(error), 120) : ''));
    if (!Mon.seen) {
      if (result) Mon.metrics({ elapsed: result.seconds, run_cost: result.cost_usd, llm_calls: result.llm_calls, tokens: result.tokens, steps: result.steps });
      else Mon.local();
    }
    Mon.agentsGauge(Agents.counts());
    this.status(status);
    $('#metrics').hidden = false;
    this.renderMetrics();
    $('#btn-cancel').hidden = true;
    $('#btn-resume').hidden = status === 'done';
    if (result) {
      this.renderMetrics(result);
      if ($('#answer-card').hidden) this.answer(result);
      this.clock(result.seconds || 0);
    }
    if (S.runId) this.files(S.runId);
    if (error && error !== 'cancelled' && this.lastErr !== String(error)) this.push({ kind: 'error', data: { message: String(error) } });
  },
};

// ------------------------------------------------------------------ runs ---

async function launch(path, body, title, task) {
  setLaunchErr('');
  S.running = true; updateRunButton();
  try {
    const r = await fetch(apiUrl(path), {
      method: 'POST', headers: { 'Content-Type': 'application/json', ...authHeaders() }, body: JSON.stringify(body || {}),
    });
    const res = await r.json().catch(() => ({}));
    if (!r.ok) {
      const msg = typeof res.detail === 'string' ? res.detail : (typeof res.error === 'string' ? res.error : (r.status === 422 ? 'Invalid task.' : 'HTTP ' + r.status));
      S.running = false;
      setLaunchErr((r.status === 401 ? 'Access code missing or wrong. ' : '') + msg);
      return;
    }
    S.runId = res.run_id; S.retry = 0; S.reconnecting = false; S.finishInfo = null; S.seen = new Set(); S.lastN = 0; clearTimeout(S.rt);
    Live.reset(task, title);
    stream(res.run_id);
  } catch (e) {
    S.running = false;
    setLaunchErr('Could not reach the backend: ' + (e && e.message ? e.message : e));
  }
}
// launch/resume errors live in their own field: updateRunButton must not blank them (cleared on the next launch or edit)
function setLaunchErr(msg) {
  S.launchErr = msg || '';
  updateRunButton();
  const le = $('#live-err');
  if (le) { le.textContent = S.launchErr; }
  if (S.launchErr) Asks.say(S.launchErr);
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
  clearTimeout(S.rt); S.rt = null; clearTimeout(S.wd); S.wd = null; S.reconnecting = false; S.finishInfo = null;
  S.running = false;
  Live.finish(result, status, error);
  updateRunButton();
  api('/api/health').then((h) => { S.health = h; renderConn(); }).catch(() => {});
}

// SSE. Events carry a strictly increasing `n` (a bounded set of seen n's drops replays and duplicates, so a reopened
// stream, which the server replays from the start, is harmless). A dropped or silent connection must not end the run
// in the UI while the server run continues: reopen with backoff; end only when the server says the run is over.
const RETRY_MS = [1000, 1500, 2000, 3000, 4000];
const SILENCE_MS = 10000;      // the backend sends a `metrics` event about every second
const SEEN_MAX = 5000;
function arm(runId, es) {
  clearTimeout(S.wd);
  S.wd = setTimeout(() => {
    if (S.es !== es || !S.running || S.runId !== runId) return;
    es.close(); S.es = null;
    if (S.finishInfo) { const i = S.finishInfo; endRun(i.result, i.status, i.error); return; }
    reconnect(runId, true);
  }, SILENCE_MS);
}
function stream(runId) {
  if (S.es) { S.es.close(); S.es = null; }
  const es = new EventSource(apiUrl('/api/runs/' + encodeURIComponent(runId) + '/events'));
  S.es = es;
  arm(runId, es);
  es.onmessage = (e) => {
    if (S.es !== es) return;
    let ev; try { ev = JSON.parse(e.data); } catch { return; }
    if (!ev || typeof ev !== 'object') return;
    arm(runId, es);
    let isNew = true;
    if (typeof ev.n === 'number') {
      if (S.seen.has(ev.n)) return;
      isNew = ev.n > S.lastN;
      S.seen.add(ev.n);
      if (S.seen.size > SEEN_MAX) S.seen.delete(S.seen.values().next().value);
      if (isNew) S.lastN = ev.n;
    }
    if (isNew) {                                  // a genuinely new event: the connection really works
      S.retry = 0;
      if (S.reconnecting) { S.reconnecting = false; Live.updateStatus(); }
    }
    Live.push(ev);
  };
  es.addEventListener('done', (e) => {
    if (S.es !== es) return;
    let d = null; try { d = JSON.parse(e.data); } catch { /* unparsable */ }
    if (d && typeof d === 'object' && typeof d.status === 'string') { endRun(d.result, d.status, d.error); return; }
    es.close(); S.es = null; recheck(runId);      // do not guess "done": ask the server
  });
  es.onerror = () => {
    if (S.es !== es) return;
    es.close(); S.es = null;
    if (S.finishInfo) { const i = S.finishInfo; endRun(i.result, i.status, i.error); return; }
    reconnect(runId);
  };
}

async function recheck(runId) {
  try {
    const info = await api('/api/runs/' + encodeURIComponent(runId), {}, 5000);
    if (!S.running || S.runId !== runId) return;
    if (info.status === 'running') { reconnect(runId, true); return; }
    replayThenEnd(runId, info);
  } catch (e) {
    if (!S.running || S.runId !== runId) return;
    endRun(null, 'error', e && e.status === 404 ? 'The backend no longer knows this run. Press Resume.' : 'Connection to the backend was lost. The run is checkpointed: press Resume.');
  }
}
// the run is over on the server: reopen the stream once so the events missed while disconnected are replayed
// (the server then sends `done`); if that fails, settle from /api/runs
function replayThenEnd(runId, info) {
  S.finishInfo = info;
  stream(runId);
}

function reconnect(runId, quiet) {
  if (!S.running || S.runId !== runId) return;
  let delay = 300;
  if (!quiet) {
    if (S.retry >= RETRY_MS.length) {
      endRun(null, 'error', 'Connection to the backend was lost. The run is checkpointed: press Resume.');
      return;
    }
    delay = RETRY_MS[S.retry++];
    S.reconnecting = true;
    Live.status('reconnecting');
  }
  clearTimeout(S.rt);
  S.rt = setTimeout(async () => {
    if (!S.running || S.runId !== runId) return;
    try {
      const info = await api('/api/runs/' + encodeURIComponent(runId), {}, 5000);
      if (!S.running || S.runId !== runId) return;
      if (info.status !== 'running') { replayThenEnd(runId, info); return; }
    } catch (e) {
      if (e && e.status === 404) { endRun(null, 'error', 'The backend no longer knows this run. Press Resume.'); return; }
      if (quiet) S.retry++;          // unreachable right now: this counts like any other failed attempt
    }
    if (S.running && S.runId === runId) stream(runId);
  }, delay);
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
  Mon.init();
  $('#lf-level').addEventListener('change', () => Mon.refilter());
  $('#lf-agent').addEventListener('change', () => Mon.refilter());
  $('#lf-text').addEventListener('input', () => Mon.refilter());
  $('#lf-pause').addEventListener('click', () => {
    Mon.paused = !Mon.paused;
    $('#lf-pause').textContent = Mon.paused ? 'Resume scroll' : 'Pause scroll';
    if (!Mon.paused) { const b = $('#loglines'); b.scrollTop = b.scrollHeight; }
  });
  $('#lf-copy').addEventListener('click', () => Mon.copy());
  $('#ask-pill').addEventListener('click', () => {
    const first = $('#asks .ask');
    (first || $('#asks')).scrollIntoView({ behavior: 'smooth', block: 'start' });
    const b = $('#asks .ask button:not(:disabled)'); if (b) b.focus({ preventScroll: true });
  });
  $('#code').addEventListener('input', () => { S.code = $('#code').value; updateRunButton(); });

  routeFromHash();
  renderChips();
  if (!$('#task').value) { $('#task').value = EXAMPLES[0][1]; }
  onTaskInput();
  connect();
}

document.addEventListener('DOMContentLoaded', init);
})();
