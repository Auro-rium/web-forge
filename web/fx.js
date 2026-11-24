/* WebForge motion layer ("Trace"). Plain JS, no dependencies, no network except data/bench.json.
   Everything here is decoration on top of app.js: if this file fails, the site still works.
   Rules: dynamic text only via textContent; honour prefers-reduced-motion (static end states, no loops);
   loops pause when the tab is hidden or their element is off-screen. */
(() => {
'use strict';

const doc = document;
const root = doc.documentElement;
const $ = (s, r = doc) => r.querySelector(s);
const RM = window.matchMedia ? matchMedia('(prefers-reduced-motion: reduce)') : { matches: false };
const reduced = () => RM.matches;
const ease = (x) => 1 - Math.pow(1 - x, 3);
const clamp = (v, a, b) => (v < a ? a : v > b ? b : v);
const hasIO = 'IntersectionObserver' in window;

// Calls cb(true/false) when el enters/leaves the viewport (assumes visible without IntersectionObserver).
function onVisible(el, cb, margin = '0px') {
  if (!hasIO) { cb(true); return; }
  new IntersectionObserver((es) => { for (const e of es) cb(e.isIntersecting); }, { rootMargin: margin }).observe(el);
}

// --------------------------------------------------------------- count-up ---
// Animates the first number inside `final` (e.g. "−29%", "27/27", "1,583") from 0, then sets `final` exactly.
const NUM_RE = /^(\D*?)(\d[\d,]*(?:\.\d+)?)([\s\S]*)$/;
function zeroOf(final) {
  const m = NUM_RE.exec(final);
  if (!m) return final;
  const dec = (m[2].split('.')[1] || '').length;
  return m[1] + (0).toFixed(dec) + m[3];
}
function countUp(el, final, dur = 1100, done) {
  const m = NUM_RE.exec(final);
  const target = m ? parseFloat(m[2].replace(/,/g, '')) : 0;
  if (!m || reduced() || !target) { el.textContent = final; if (done) done(); return; }
  const dec = (m[2].split('.')[1] || '').length;
  const comma = m[2].includes(',');
  const fmt = (v) => (comma ? v.toLocaleString('en-US', { minimumFractionDigits: dec, maximumFractionDigits: dec }) : v.toFixed(dec));
  const t0 = performance.now();
  const tick = (now) => {
    const p = clamp((now - t0) / dur, 0, 1);
    if (p >= 1) { el.textContent = final; if (done) done(); return; }
    el.textContent = m[1] + fmt(target * ease(p)) + m[3];
    requestAnimationFrame(tick);
  };
  requestAnimationFrame(tick);
}

// ---------------------------------------------------------- ASCII field ---
// A slow drifting field of characters from layered sine waves, drawn from a pre-rendered glyph atlas.
function Field(canvas) {
  if (!canvas || !canvas.getContext) return null;
  const ctx = canvas.getContext('2d');
  const RAMP = ' ·:;+=x#';
  const ALPHA = [0, 0.08, 0.1, 0.12, 0.14, 0.16, 0.19, 0.22];
  const MAX_CELLS = 4200, FPS = 12;
  let cols = 0, rows = 0, cw = 0, ch = 0, dpr = 1, atlas = null;
  let colA, rowA, diagA;
  let t = 0, last = 0, timer = 0, raf = 0;
  let inView = true, scrollVis = 1;

  function atlasBuild() {
    const rgb = (getComputedStyle(root).getPropertyValue('--field') || '142, 166, 255').trim();
    const gw = Math.ceil(cw * dpr), gh = Math.ceil(ch * dpr);
    atlas = doc.createElement('canvas');
    atlas.width = gw * RAMP.length; atlas.height = gh;
    const a = atlas.getContext('2d');
    a.font = `400 ${Math.round(ch * 0.6 * dpr)}px 'JetBrains Mono', ui-monospace, Menlo, Consolas, monospace`;
    a.textAlign = 'center'; a.textBaseline = 'middle';
    for (let i = 1; i < RAMP.length; i++) {
      a.fillStyle = `rgba(${rgb}, ${ALPHA[i]})`;
      a.fillText(RAMP[i], i * gw + gw / 2, gh / 2);
    }
  }
  function size() {
    const w = canvas.clientWidth || innerWidth, h = canvas.clientHeight || innerHeight;
    dpr = Math.min(window.devicePixelRatio || 1, 2);
    cw = 15; ch = 22;
    const n = (w / cw) * (h / ch);
    if (n > MAX_CELLS) { const s = Math.sqrt(n / MAX_CELLS); cw *= s; ch *= s; }
    cols = Math.ceil(w / cw); rows = Math.ceil(h / ch);
    canvas.width = Math.round(w * dpr); canvas.height = Math.round(h * dpr);
    colA = new Float32Array(cols); rowA = new Float32Array(rows); diagA = new Float32Array(cols + rows);
    atlasBuild();
  }
  function draw() {
    const W = canvas.width, H = canvas.height;
    ctx.clearRect(0, 0, W, H);
    for (let x = 0; x < cols; x++) colA[x] = Math.sin(x * 0.11 + t * 0.32) + 0.7 * Math.sin(x * 0.037 - t * 0.19);
    for (let y = 0; y < rows; y++) rowA[y] = Math.sin(y * 0.19 - t * 0.27) + 0.6 * Math.sin(y * 0.06 + t * 0.13);
    for (let d = 0; d < cols + rows; d++) diagA[d] = Math.sin(d * 0.075 + t * 0.45);
    const gw = Math.ceil(cw * dpr), gh = Math.ceil(ch * dpr), L = RAMP.length;
    for (let y = 0; y < rows; y++) {
      const ry = rowA[y], py = Math.round(y * ch * dpr);
      for (let x = 0; x < cols; x++) {
        const v = (colA[x] + ry + diagA[x + y]) / 4.3;      // about -1..1
        const i = Math.floor((v - 0.12) * L * 1.2);         // lower half of the waves stays empty
        if (i < 1) continue;
        const k = i >= L ? L - 1 : i;
        ctx.drawImage(atlas, k * gw, 0, gw, gh, Math.round(x * cw * dpr), py, gw, gh);
      }
    }
  }
  function frame(now) {
    raf = 0;
    if (!running()) return;
    t += Math.min((now - (last || now)) / 1000, 0.2);
    last = now;
    draw();
    timer = setTimeout(() => { timer = 0; raf = requestAnimationFrame(frame); }, 1000 / FPS);
  }
  const running = () => !reduced() && !doc.hidden && inView && scrollVis > 0;
  function kick() {
    if (!running() || raf || timer) return;
    last = 0; raf = requestAnimationFrame(frame);
  }
  function stop() { if (timer) clearTimeout(timer); if (raf) cancelAnimationFrame(raf); timer = 0; raf = 0; }
  function onScroll() {
    // fade the field out as the hero scrolls away; stop drawing once it is invisible
    scrollVis = clamp(1 - scrollY / (innerHeight * 1.1), 0, 1);
    canvas.style.setProperty('--fv', scrollVis.toFixed(3));
    if (scrollVis > 0) kick(); else stop();
  }

  size();
  draw();                                   // one static frame (this is all reduced motion gets)
  requestAnimationFrame(() => canvas.classList.add('on'));
  let rs = 0;
  addEventListener('resize', () => { clearTimeout(rs); rs = setTimeout(() => { size(); draw(); }, 150); }, { passive: true });
  let sq = 0;
  addEventListener('scroll', () => { if (!sq) sq = requestAnimationFrame(() => { sq = 0; onScroll(); }); }, { passive: true });
  doc.addEventListener('visibilitychange', () => (doc.hidden ? stop() : kick()));
  if (RM.addEventListener) RM.addEventListener('change', () => (reduced() ? stop() : kick()));
  onScroll();
  kick();
  return { retheme() { atlasBuild(); draw(); } };
}

// ------------------------------------------------------------ hero stage ---
// Two ASCII browsers. Left: a first-time run explores (8 steps). Right: the learned skill replays (2 steps).
const W = 46, H = 19;
const CATS = ['Poetry', 'Mystery', 'Travel', 'History', 'Science', 'Fiction'];
const CARDS = [['Lima', '£23.91'], ['Oslo', '£41.07'], ['Kyoto', '£18.64'], ['Cairo', '£52.30'], ['Nepal', '£34.12'], ['Fjords', '£27.85']];
const URL = 'books.toscrape.com/';
const START = [1, 3];
const SPEED = 38;          // cursor cells per second
const LEAD = 700;          // blank page before the first step
const HOLD = 2600;         // pause on the end state

const catRow = (name) => 6 + CATS.indexOf(name);
const cardPos = (i) => [6 + (i >= 3 ? 5 : 0), 12 + (i % 3) * 11];

function initState() {
  return { url: 'about:blank', load: 0, crumb: 'Home / All books', cat: {}, mark: '', cards: [], scan: -1, flash: false };
}
const goto = (S, dt) => {
  S.url = URL.slice(0, Math.floor(dt / 24));
  S.load = clamp(Math.floor((dt - URL.length * 24) / 32), 0, 14);
};
const COLD = [
  { label: 'goto ' + URL, res: 'page loaded', rk: 't', to: [1, 6], dwell: 1150, fx: goto },
  { label: 'click "Mystery"', res: 'wrong category for this task', rk: 'bad', to: [catRow('Mystery'), 4], dwell: 900,
    fx: (S) => { S.cat.Mystery = 'bad'; S.mark = 'Mystery'; S.crumb = 'Home / Mystery'; } },
  { label: 'back', res: 'returned to the home page', rk: 't', to: [1, 2], dwell: 650,
    fx: (S) => { S.mark = ''; S.crumb = 'Home / All books'; } },
  { label: 'click "Travel"', res: 'category page', rk: 't', to: [catRow('Travel'), 4], dwell: 800,
    fx: (S) => { S.cat.Travel = 'hit'; S.mark = 'Travel'; S.crumb = 'Home / Travel'; } },
  { label: 'observe', res: 'reading elements and text', rk: 't', to: null, dwell: 1300,
    fx: (S, dt) => { S.scan = dt < 1250 ? 5 + Math.floor(dt / 1250 * 11) : -1; } },
  { label: 'extract ".book-title"', res: '0 rows: selector matched nothing', rk: 'bad', to: [7, 15], dwell: 1000,
    fx: (S, dt) => { S.flash = dt < 640 && Math.floor(dt / 160) % 2 === 0; } },
  { label: 'extract "article.product_pod"', res: '6 rows', rk: 'ok', to: [12, 15], dwell: 1150,
    fx: (S, dt) => { for (let i = 0; i < 6; i++) S.cards[i] = dt > i * 110; } },
  { label: 'think', res: 'rows match the task', rk: 't', to: null, dwell: 1000 },
  { label: 'done', lk: 'ok', res: 'answer returned', rk: 'ok', to: null, dwell: 0, count: false },
];
const LEARNED = [
  { label: 'goto ' + URL, res: 'page loaded', rk: 't', to: [1, 6], dwell: 1150, fx: goto },
  { label: 'run_skill list_books(category="Travel")', res: 'replayed, no browsing LLM calls', rk: 'ok', to: [catRow('Travel'), 4], jump: true, dwell: 1200,
    fx: (S, dt) => { S.cat.Travel = 'hit'; S.mark = 'Travel'; S.crumb = 'Home / Travel'; for (let i = 0; i < 6; i++) S.cards[i] = dt > 40 + i * 35; } },
  { label: 'done', lk: 'ok', res: 'answer returned', rk: 'ok', to: null, dwell: 0, count: false },
];

function manhattan(a, b) {
  const out = [];
  let [r, c] = a;
  while (c !== b[1]) { c += Math.sign(b[1] - c); out.push([r, c]); }
  while (r !== b[0]) { r += Math.sign(b[0] - r); out.push([r, c]); }
  return out;
}
function compile(steps) {
  let t = LEAD, pos = START;
  const out = steps.map((s) => {
    const path = s.to && !s.jump ? manhattan(pos, s.to) : [];
    const walk = path.length ? Math.max(200, path.length / SPEED * 1000) : (s.jump ? 160 : 0);
    const st = { ...s, count: s.count !== false, t0: t, ta: t + walk, t1: t + walk + s.dwell, path, from: pos };
    if (s.to) pos = s.to;
    t = st.t1;
    return st;
  });
  return { steps: out, end: t };
}

function Pane(pre, counter, script) {
  const N = W * H;
  const chars = new Array(N), cls = new Array(N), pc = new Array(N).fill(''), pk = new Array(N).fill('');
  const put = (r, c, s, k) => { for (let i = 0; i < s.length && c + i < W; i++) { chars[r * W + c + i] = s[i]; cls[r * W + c + i] = k; } };
  const set = (r, c, k) => { cls[r * W + c] = k; };
  let lastN = -1;

  function page(S) {
    chars.fill(' '); cls.fill('');
    const side = (r) => { put(r, 0, '│', 'f'); put(r, W - 1, '│', 'f'); };
    put(0, 0, '┌' + '─'.repeat(W - 2) + '┐', 'f');
    side(1); put(1, 2, '< >', 't'); put(1, 6, S.url, 'lk');
    put(2, 0, '├' + '─'.repeat(W - 2) + '┤', 'f');
    for (let r = 3; r <= 15; r++) side(r);
    const shown = (r) => r - 3 < S.load;
    if (S.load > 0) {
      put(3, 2, 'BOOKS TO SCRAPE', 't'); put(3, 29, '[ search      ]', 'f');
      if (shown(4)) put(4, 0, '├' + '─'.repeat(9) + '┬' + '─'.repeat(W - 12) + '┤', 'f');
      if (shown(5)) { put(5, 2, 'genres', 't'); put(5, 12, S.crumb, 't'); }
      for (let r = 5; r <= 15; r++) if (shown(r)) put(r, 10, '│', 'f');
      CATS.forEach((n, i) => {
        const r = 6 + i;
        if (!shown(r)) return;
        const k = S.cat[n] || 'lk';
        put(r, 2, S.mark === n ? '>' : ' ', k); put(r, 3, n, k);
      });
      CARDS.forEach(([title, price], i) => {
        const [r0, c0] = cardPos(i);
        const hit = S.cards[i];
        const fk = hit ? 'hit' : (S.flash ? 'bad' : 'f');
        const lines = [['┌────────┐', fk], ['│ ░░░░░░ │', hit ? 'hit' : 'cv'], ['│ ' + title.padEnd(6) + ' │', hit ? 'hit' : 't'], ['│ ' + price + ' │', hit ? 'hit' : 'lk'], ['└────────┘', fk]];
        lines.forEach(([s, k], j) => {
          if (!shown(r0 + j)) return;
          put(r0 + j, c0, s, k);
          if (!hit && j > 0 && j < 4) { set(r0 + j, c0, fk); set(r0 + j, c0 + 9, fk); }
        });
      });
      if (S.scan > 0) for (let c = 11; c < W - 1; c++) cls[S.scan * W + c] = 'scan';
    }
    put(16, 0, S.load >= 14 ? '└' + '─'.repeat(9) + '┴' + '─'.repeat(W - 12) + '┘' : '└' + '─'.repeat(W - 2) + '┘', 'f');
  }

  function render(P, tc) {
    const S = initState();
    let pos = START, n = 0, cur = null;
    const trail = [];
    for (const st of P.steps) {
      if (tc < st.t0) break;
      cur = st;
      if (st.count) n++;
      if (st.path.length) {
        const k = tc >= st.ta ? st.path.length : Math.floor((tc - st.t0) / (st.ta - st.t0) * st.path.length);
        for (let i = 0; i < k; i++) trail.push(st.path[i]);
        if (k) pos = st.path[k - 1];
      } else if (st.to && tc >= st.ta) pos = st.to;
      if (tc >= st.ta && st.fx) st.fx(S, tc - st.ta);
    }
    page(S);
    for (const [r, c] of trail) { const i = r * W + c; if (chars[i] === ' ') { chars[i] = '·'; cls[i] = 'tr'; } }
    // two-line log under the page
    if (cur) {
      put(17, 0, '>', 'pr'); put(17, 2, cur.label, cur.lk || 'lg');
      if (tc >= cur.ta) put(18, 2, cur.res, cur.rk);
    } else { put(17, 0, '>', 'pr'); put(17, 2, '_', 'pr'); }
    set(pos[0], pos[1], 'cu');
    if (n !== lastN) { lastN = n; if (counter) counter.textContent = 'steps ' + n; }
    paint();
  }

  function paint() {
    let changed = false;
    for (let i = 0; i < N; i++) if (chars[i] !== pc[i] || cls[i] !== pk[i]) { changed = true; break; }
    if (!changed) return;
    for (let i = 0; i < N; i++) { pc[i] = chars[i]; pk[i] = cls[i]; }
    const frag = doc.createDocumentFragment();
    for (let r = 0; r < H; r++) {
      let run = '', k = cls[r * W];
      const flush = () => {
        if (!run) return;
        if (k) { const s = doc.createElement('span'); s.className = k; s.textContent = run; frag.append(s); }
        else frag.append(doc.createTextNode(run));
        run = '';
      };
      for (let c = 0; c < W; c++) {
        const i = r * W + c;
        if (cls[i] !== k) { flush(); k = cls[i]; }
        run += chars[i];
      }
      flush();
      if (r < H - 1) frag.append(doc.createTextNode('\n'));
    }
    pre.replaceChildren(frag);
  }
  return { render, P: compile(script) };
}

function Stage() {
  const stage = $('#stage'), pc = $('#pane-cold'), pl = $('#pane-learned');
  if (!stage || !pc || !pl) return;
  const cold = Pane(pc, $('#cold-n'), COLD), learned = Pane(pl, $('#learned-n'), LEARNED);
  const cycle = Math.max(cold.P.end, learned.P.end) + HOLD;

  // size the grid so 46 columns always fit the pane (no horizontal scroll on a 390px phone)
  const probe = doc.createElement('span');
  probe.setAttribute('aria-hidden', 'true');
  probe.style.cssText = 'position:absolute;visibility:hidden;white-space:pre;font:400 100px var(--mono);left:-9999px;top:0';
  probe.textContent = '─'.repeat(20);
  doc.body.append(probe);
  function fit() {
    const cs = getComputedStyle(pc);
    const avail = pc.clientWidth - parseFloat(cs.paddingLeft) - parseFloat(cs.paddingRight);
    const ratio = (probe.getBoundingClientRect().width / 20) / 100 || 0.6;
    if (avail <= 0) return;
    const fs = Math.floor(Math.min(14, avail / (W * ratio)) * 10) / 10;
    stage.style.setProperty('--pf', fs + 'px');
  }
  fit();
  if ('ResizeObserver' in window) new ResizeObserver(fit).observe(stage); else addEventListener('resize', fit);
  if (doc.fonts && doc.fonts.ready) doc.fonts.ready.then(fit);

  const draw = (tc) => { cold.render(cold.P, tc); learned.render(learned.P, tc); };
  if (reduced()) { draw(cycle - 1); return; }

  let clock = 0, last = 0, raf = 0, inView = true;
  const frame = (now) => {
    raf = 0;
    if (doc.hidden || !inView || reduced()) { last = 0; if (reduced()) draw(cycle - 1); return; }
    if (!last) last = now;
    const dt = now - last;
    if (dt >= 33) {                    // ~30 fps is plenty for a character grid
      clock = (clock + Math.min(dt, 100)) % cycle;
      last = now;
      draw(clock);
    }
    raf = requestAnimationFrame(frame);
  };
  const kick = () => { if (!raf && !doc.hidden && inView) raf = requestAnimationFrame(frame); };
  draw(0);
  onVisible(stage, (v) => { inView = v; kick(); }, '40px');
  doc.addEventListener('visibilitychange', kick);
  if (RM.addEventListener) RM.addEventListener('change', kick);
  kick();
}

// ------------------------------------------------------- headline decode ---
function Decode() {
  const h1 = $('#hero-title');
  if (!h1 || reduced()) return;
  const GL = 'abcdeghknoprsuxyz#%&*+=?<>0123456789';
  const rnd = () => GL[(Math.random() * GL.length) | 0];
  const lines = Array.from(h1.querySelectorAll('[data-t]'));
  const cells = [];
  let total = 0;
  for (const ln of lines) total += ln.dataset.t.length;
  let idx = 0;
  for (const ln of lines) {
    const text = ln.dataset.t;
    ln.textContent = '';
    for (const chr of text) {
      const s = doc.createElement('span');
      s.className = 'dc';
      s.textContent = chr;
      if (chr !== ' ') {
        s.classList.add('s');
        s.dataset.g = rnd();
        cells.push({ s, at: 140 + (idx / total) * 760 + Math.random() * 90 });
      }
      ln.append(s);
      idx++;
    }
  }
  const t0 = performance.now();
  let lastSwap = 0;
  const tick = (now) => {
    const t = now - t0;
    const swap = now - lastSwap > 55;
    if (swap) lastSwap = now;
    let left = 0;
    for (const c of cells) {
      if (c.done) continue;
      if (t >= c.at) { c.s.classList.remove('s'); c.done = true; continue; }
      left++;
      if (swap) c.s.dataset.g = rnd();
    }
    if (left) { requestAnimationFrame(tick); return; }
    for (const ln of lines) ln.textContent = ln.dataset.t;   // restore plain text
    h1.classList.add('decoded');
  };
  requestAnimationFrame(tick);
}

// ------------------------------------------------------ measured strip ---
async function Strip() {
  const box = $('#strip');
  if (!box) return;
  let d;
  try {
    const r = await fetch('data/bench.json', { cache: 'no-cache' });
    if (!r.ok) return;
    d = await r.json();
  } catch { return; }
  const cold = d && Array.isArray(d.cold) ? d.cold : [], learned = d && Array.isArray(d.learned) ? d.learned : [];
  if (!d || d.sample || !cold.length || !learned.length) return;     // never present sample data as a measurement

  const mean = (rows, k) => rows.reduce((a, r) => a + (Number(r[k]) || 0), 0) / rows.length;
  const right = (rows) => rows.filter((r) => r.correct).length;
  const pct = (c, l) => (c ? (l - c) / c * 100 : 0);
  const pctText = (p) => (Math.abs(p) < 0.5 ? '0%' : (p < 0 ? '−' : '+') + Math.abs(p).toFixed(0) + '%');
  const n0 = (v) => Math.round(v).toLocaleString('en-US');

  const cells = [];
  const cell = (n, t, s, bad) => cells.push({ n, t, s, bad });
  const rc = right(cold), rl = right(learned);
  cell(`${rl}/${learned.length}`, 'correct, learned', `first time: ${rc}/${cold.length}`, rl < rc);
  for (const [k, label, f] of [['steps', 'steps per task', (v) => v.toFixed(2)], ['tokens', 'tokens per task', n0], ['seconds', 'time per task', (v) => v.toFixed(1) + 's']]) {
    const c = mean(cold, k), l = mean(learned, k), p = pct(c, l);
    cell(pctText(p), label, `${f(c)} → ${f(l)} mean`, p >= 0.5);       // more is worse for all three
  }

  // where the gain comes from, per family (steps)
  const fams = [...new Set(cold.map((r) => r.family))].filter(Boolean);
  const gain = [], none = [];
  for (const f of fams) {
    const c = cold.filter((r) => r.family === f), l = learned.filter((r) => r.family === f);
    if (!c.length || !l.length) continue;
    (pct(mean(c, 'steps'), mean(l, 'steps')) <= -5 ? gain : none).push(f.replace(/_/g, ' '));
  }
  const list = (xs) => (xs.length < 2 ? xs.join('') : xs.slice(0, -1).join(', ') + ' and ' + xs[xs.length - 1]);
  const runs = cold.length === learned.length ? `${cold.length} runs per mode` : `${cold.length} first-time and ${learned.length} learned runs`;
  let src = `measured: ${runs}, ${d.cold_file || 'cold'} vs ${d.learned_file || 'learned'}.`;
  if (gain.length && none.length) src += ` Fewer steps only in ${list(gain)}; ${list(none)} show${none.length === 1 ? 's' : ''} no step saving.`;

  const nodes = [];
  box.replaceChildren();
  for (const c of cells) {
    const n = doc.createElement('div'); n.className = 'n' + (c.bad ? ' bad' : ''); n.textContent = reduced() ? c.n : zeroOf(c.n);
    const t = doc.createElement('div'); t.className = 't'; t.textContent = c.t;
    const s = doc.createElement('div'); s.className = 's'; s.textContent = c.s;
    const wrap = doc.createElement('div'); wrap.append(n, t, s);
    box.append(wrap);
    nodes.push([n, c.n]);
  }
  const p = doc.createElement('p'); p.className = 'src'; p.textContent = src;
  box.append(p);
  if (reduced()) return;
  box.setAttribute('aria-busy', 'true');           // announce the final numbers, not every frame
  let fired = false;
  onVisible(box, (v) => {
    if (!v || fired) return;
    fired = true;
    let left = nodes.length;
    nodes.forEach(([n, final], i) => setTimeout(() => countUp(n, final, 1200, () => { if (!--left) box.removeAttribute('aria-busy'); }), i * 110));
  });
}

// ------------------------------------- count-up for the Learning tab stats ---
function StatCounts() {
  const host = $('#bench-hero');
  if (!host || !window.MutationObserver) return;
  const io = hasIO ? new IntersectionObserver((es) => {
    for (const e of es) {
      if (!e.isIntersecting) continue;
      io.unobserve(e.target);
      countUp(e.target, e.target.dataset.final || e.target.textContent, 1100);
    }
  }, { threshold: 0.4 }) : null;
  const scan = () => {
    for (const b of host.querySelectorAll('.stat .big')) {
      if (b.dataset.final != null) continue;
      b.dataset.final = b.textContent;
      if (reduced() || !io) continue;
      b.textContent = zeroOf(b.dataset.final);
      io.observe(b);
    }
  };
  new MutationObserver(scan).observe(host, { childList: true, subtree: true });
  scan();
}

// --------------------------------------------- architecture: data packets ---
function Packets() {
  const svgEl = $('#arch-svg'), tab = $('#tab-arch');
  if (!svgEl || !tab || reduced()) return;
  const NS = 'http://www.w3.org/2000/svg';
  let built = false;
  const build = () => {
    built = true;
    const edges = svgEl.querySelector('.edges');
    if (!edges) return;
    const layer = doc.createElementNS(NS, 'g');
    layer.setAttribute('class', 'pkts');
    layer.setAttribute('aria-hidden', 'true');
    Array.from(edges.querySelectorAll('path')).forEach((p, i) => {
      const two = p.hasAttribute('marker-start');
      const dur = two ? 4.8 : 2.4;
      const c = doc.createElementNS(NS, 'circle');
      c.setAttribute('r', '3');
      c.setAttribute('class', 'pkt' + (p.classList.contains('learn') ? ' l' : ''));
      const am = doc.createElementNS(NS, 'animateMotion');
      am.setAttribute('path', p.getAttribute('d'));
      am.setAttribute('dur', dur + 's');
      am.setAttribute('begin', (-((i * 0.61) % dur)).toFixed(2) + 's');   // negative begin: already moving, never parked at 0,0
      am.setAttribute('repeatCount', 'indefinite');
      if (two) { am.setAttribute('keyPoints', '0;1;0'); am.setAttribute('keyTimes', '0;0.5;1'); am.setAttribute('calcMode', 'linear'); }
      c.append(am);
      layer.append(c);
    });
    edges.after(layer);
  };
  const sync = () => {
    if (tab.hidden) { if (built && svgEl.pauseAnimations) svgEl.pauseAnimations(); return; }
    if (!built) build();
    if (svgEl.unpauseAnimations) svgEl.unpauseAnimations();
  };
  new MutationObserver(sync).observe(tab, { attributes: true, attributeFilter: ['hidden'] });
  sync();
}

// ---------------------------------------------------- footer: typed trace ---
function FootType() {
  const pre = $('.ascii-foot');
  if (!pre || reduced()) return;
  const lines = pre.textContent.split('\n');
  const len = Math.max(...lines.map((l) => l.length));
  const blank = () => lines.map((l) => ' '.repeat(l.length)).join('\n');
  pre.textContent = blank();
  let done = false;
  onVisible(pre, (v) => {
    if (!v || done) return;
    done = true;
    const t0 = performance.now(), dur = 1400;
    const tick = (now) => {
      const k = Math.ceil(clamp((now - t0) / dur, 0, 1) * len);
      pre.textContent = lines.map((l) => l.slice(0, k).padEnd(l.length)).join('\n');
      if (k < len) requestAnimationFrame(tick);
    };
    requestAnimationFrame(tick);
  });
}

// ------------------------------------------------------------------ boot ---
function boot() {
  root.classList.add('fx');
  Decode();
  const field = Field($('#field'));
  if (field) {
    const re = () => requestAnimationFrame(() => field.retheme());
    const mq = window.matchMedia ? matchMedia('(prefers-color-scheme: dark)') : null;
    if (mq && mq.addEventListener) mq.addEventListener('change', re);
    new MutationObserver(re).observe(root, { attributes: true, attributeFilter: ['data-theme'] });
  }
  Stage();
  Strip();
  StatCounts();
  Packets();
  FootType();
}
const safe = () => { try { boot(); } catch (e) { if (window.console) console.warn('fx disabled:', e); } };
if (doc.readyState === 'loading') doc.addEventListener('DOMContentLoaded', safe); else safe();
})();
