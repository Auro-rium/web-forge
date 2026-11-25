/* WebForge motion layer. Plain JS, no dependencies, no network.
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
const clamp = (v, a, b) => (v < a ? a : v > b ? b : v);
const hasIO = 'IntersectionObserver' in window;

// Calls cb(true/false) when el enters/leaves the viewport (assumes visible without IntersectionObserver).
function onVisible(el, cb, margin = '0px') {
  if (!hasIO) { cb(true); return; }
  new IntersectionObserver((es) => { for (const e of es) cb(e.isIntersecting); }, { rootMargin: margin }).observe(el);
}

// ------------------------------------------------------- background field ---
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
// --------------------------------------------- hero: the agent loop in ASCII ---
// One planner fans out to parallel workers, a gate asks the human, the verifier checks, files come out.
function Loop() {
  const pre = $('#loop');
  if (!pre) return;
  const AG = ['w1', 'w2', 'w3'];
  const JOBS = ['search: laptop reviews', 'read: 3 spec pages', 'fill: order form'];
  const bar = (k, n = 18) => '█'.repeat(Math.round(k * n)).padEnd(n, '·');
  const frame = (t) => {
    const p = (t % 9000) / 9000;                       // one cycle every 9 s
    const k = (i) => clamp((p - 0.12 - i * 0.04) / 0.45, 0, 1);
    const gate = p > 0.62 && p < 0.74, verify = p >= 0.74 && p < 0.86, done = p >= 0.86;
    const L = [];
    L.push(' task ──▸ PLANNER  todos ' + (p < 0.1 ? '[ ][ ][ ]' : '[' + (k(0) >= 1 ? 'x' : ' ') + '][' + (k(1) >= 1 ? 'x' : ' ') + '][' + (k(2) >= 1 ? 'x' : ' ') + ']'));
    L.push('            │');
    AG.forEach((a, i) => L.push('            ├─▸ ' + a + ' ' + bar(k(i)) + ' ' + JOBS[i].padEnd(24)));
    L.push('            │');
    L.push('            ├─▸ ' + (gate ? 'GATE  "submit order?" ─▸ you: approve ✓' : 'gate  allow · ask · block               '));
    L.push('            ├─▸ ' + (verify || done ? 'VERIFIER  every part covered? ✓          ' : 'verifier                                 '));
    L.push('            └─▸ ' + (done ? 'outputs/report.md  outputs/laptops.csv  ✓' : 'outputs/                                 '));
    return L.join('\n');
  };
  if (reduced()) { pre.textContent = frame(8600); return; }
  let raf = 0, inView = true;
  const t0 = performance.now();
  const tick = (now) => { pre.textContent = frame(now - t0); raf = (inView && !doc.hidden) ? requestAnimationFrame(tick) : 0; };
  const kick = () => { if (!raf && inView && !doc.hidden) raf = requestAnimationFrame(tick); };
  onVisible(pre, (v) => { inView = v; kick(); }, '40px');
  doc.addEventListener('visibilitychange', kick);
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
  Loop();
  Packets();
  FootType();
}
const safe = () => { try { boot(); } catch (e) { if (window.console) console.warn('fx disabled:', e); } };
if (doc.readyState === 'loading') doc.addEventListener('DOMContentLoaded', safe); else safe();
})();
