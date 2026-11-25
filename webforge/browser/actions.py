"""Action helpers: locator resolution, extraction, domain checks."""
from __future__ import annotations

import json
from typing import Any, Optional
from webforge.safety import domain_allowed  # noqa: F401  (re-exported for the browser session)


EXTRACT_JS = r"""
([css, fields]) => {
  const norm = s => (s || '').replace(/\s+/g, ' ').trim();
  const read = (el, attr) => {
    if (!el) return null;
    if (attr) return (attr === 'href' || attr === 'src') && el[attr] ? el[attr] : el.getAttribute(attr);
    return norm(el.textContent);
  };
  let nodes;
  try { nodes = [...document.querySelectorAll(css)]; }
  catch (e) { return { rows: [], bad: [], error: 'invalid CSS selector: ' + css }; }
  const bad = new Set();
  const rows = nodes.map(row => {
    if (!fields) return norm(row.textContent);
    const out = {};
    for (const [k, spec] of Object.entries(fields)) {
      const s = String(spec ?? '');
      const i = s.lastIndexOf('@');
      const sel = i >= 0 ? s.slice(0, i).trim() : s.trim();
      const attr = i >= 0 ? s.slice(i + 1).trim() : '';
      try { out[k] = read(sel ? row.querySelector(sel) : row, attr); }
      catch (e) { out[k] = null; bad.add(k); }   // one malformed field must not sink the whole extraction
    }
    return out;
  });
  return { rows, bad: [...bad], error: '' };
}
"""


def short_err(e: Exception, limit: int = 200) -> str:
    msg = str(e).strip().splitlines()[0] if str(e).strip() else type(e).__name__
    return f"{type(e).__name__}: {msg}"[:limit]


def normalize_url(url: str) -> str:
    url = (url or "").strip()
    return url if "://" in url or url.startswith(("about:", "data:")) else "https://" + url


def locator_candidates(page, loc: dict) -> list:
    """Playwright locators in resolution order: role+name, placeholder, text, css."""
    c = []
    if loc.get("role"):
        kw = {"name": loc["name"], "exact": True} if loc.get("name") else {}
        c.append(page.get_by_role(loc["role"], **kw))
    if loc.get("placeholder"):
        c.append(page.get_by_placeholder(loc["placeholder"]))
    if loc.get("text"):
        c.append(page.get_by_text(loc["text"], exact=True))
    if loc.get("css"):
        c.append(page.locator(loc["css"]))
    return c


async def resolve_locator(page, loc: Optional[dict]):
    """First candidate with a visible match (its first visible element), else None."""
    for cand in locator_candidates(page, loc or {}):
        try:
            n = await cand.count()
            for i in range(min(n, 10)):
                el = cand.nth(i)
                if await el.is_visible():
                    return el
        except Exception:
            continue
    return None


async def extract_rows(page, css: Optional[str], fields: Optional[dict]) -> dict:
    """Returns {"rows": [...], "bad": [field names with invalid selectors], "error": str}."""
    if not css:
        return {"rows": [], "bad": [], "error": ""}
    return await page.evaluate(EXTRACT_JS, [css, fields or None])


def preview(rows: Any, n: int = 15) -> str:
    shown = rows[:n] if isinstance(rows, list) else rows
    s = json.dumps(shown, ensure_ascii=False)
    more = f" (+{len(rows) - n} more)" if isinstance(rows, list) and len(rows) > n else ""
    return f"{len(rows)} rows: {s}{more}" if isinstance(rows, list) else s


# ------------------------------------------------------- generic extraction ---
# Selector-free readers that work on most websites (no per-site knowledge needed).

READ_JS = r"""
() => {
  const SKIP = new Set(['SCRIPT', 'STYLE', 'NOSCRIPT', 'SVG', 'TEMPLATE', 'IFRAME', 'NAV', 'FOOTER', 'ASIDE', 'FORM']);
  const pick = () => {
    const cands = [...document.querySelectorAll('main, article, [role=main], #content, #main, .content, .main')];
    let best = null, n = 0;
    for (const c of cands) { const l = (c.innerText || '').length; if (l > n) { n = l; best = c; } }
    const body = document.body;
    return best && n > 0.25 * ((body && body.innerText) || '').length ? best : body;
  };
  const root = pick();
  if (!root) return '';
  const out = [];
  const norm = s => (s || '').replace(/\s+/g, ' ').trim();
  const walk = (node) => {
    for (const el of node.children) {
      if (SKIP.has(el.tagName) && el !== root) continue;
      const st = getComputedStyle(el);
      if (st.display === 'none' || st.visibility === 'hidden') continue;
      const t = el.tagName;
      if (/^H[1-6]$/.test(t)) { out.push('\n' + '#'.repeat(+t[1]) + ' ' + norm(el.innerText)); continue; }
      if (t === 'LI') { out.push('- ' + norm(el.innerText)); continue; }
      if (t === 'P' || t === 'BLOCKQUOTE' || t === 'PRE' || t === 'DT' || t === 'DD' || t === 'FIGCAPTION') {
        const s = norm(el.innerText); if (s) out.push(s); continue; }
      if (t === 'TR') { const cells = [...el.children].map(c => norm(c.innerText)); out.push('| ' + cells.join(' | ') + ' |'); continue; }
      if (el.children.length) walk(el);
      else { const s = norm(el.innerText); if (s) out.push(s); }
    }
  };
  walk(root);
  const seen = new Set();
  return out.filter(l => { if (!l.trim()) return false; if (seen.has(l) && l.length < 200) return false; seen.add(l); return true; }).join('\n');
}
"""

TABLES_JS = r"""
(maxRows) => {
  const norm = s => (s || '').replace(/\s+/g, ' ').trim();
  return [...document.querySelectorAll('table')].map((tb, ti) => {
    const rows = [...tb.querySelectorAll('tr')];
    if (!rows.length) return null;
    let head = rows[0].querySelectorAll('th').length ? [...rows[0].children].map(c => norm(c.innerText)) : null;
    const body = head ? rows.slice(1) : rows;
    if (!head) head = [...(rows[0].children)].map((_, i) => 'col' + (i + 1));
    head = head.map((h, i) => h || 'col' + (i + 1));
    const data = body.slice(0, maxRows).map(r => {
      const cells = [...r.children].map(c => norm(c.innerText));
      const o = {}; cells.forEach((c, i) => { o[head[i] || 'col' + (i + 1)] = c; }); return o;
    }).filter(o => Object.values(o).some(v => v));
    const cap = tb.querySelector('caption');
    return { index: ti, caption: cap ? norm(cap.innerText) : '', columns: head, rows: data, total_rows: body.length };
  }).filter(t => t && t.rows.length);
}
"""

LINKS_JS = r"""
([filter, max]) => {
  const norm = s => (s || '').replace(/\s+/g, ' ').trim();
  const f = (filter || '').toLowerCase();
  const seen = new Set(); const out = [];
  for (const a of document.querySelectorAll('a[href]')) {
    const r = a.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0) continue;
    const text = norm(a.innerText || a.getAttribute('aria-label') || a.title);
    const href = a.href;
    if (!text || !href || href.startsWith('javascript:') || seen.has(href)) continue;
    if (f && !(text.toLowerCase().includes(f) || href.toLowerCase().includes(f))) continue;
    seen.add(href); out.push({ text: text.slice(0, 120), href });
    if (out.length >= max) break;
  }
  return out;
}
"""

FIND_JS = r"""
([needle, ctx]) => {
  const text = (document.body ? document.body.innerText : '');
  const i = text.toLowerCase().indexOf(String(needle).toLowerCase());
  if (i < 0) return { found: false, context: '' };
  const w = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  let n; while ((n = w.nextNode())) {
    if (n.textContent.toLowerCase().includes(String(needle).toLowerCase()) && n.parentElement) {
      n.parentElement.scrollIntoView({ block: 'center' }); break; }
  }
  const s = Math.max(0, i - ctx), e = Math.min(text.length, i + String(needle).length + ctx);
  const count = text.toLowerCase().split(String(needle).toLowerCase()).length - 1;
  return { found: true, count, context: text.slice(s, e).replace(/\s+/g, ' ').trim() };
}
"""

STRUCTURED_JS = r"""
() => {
  const out = { jsonld: [], meta: {} };
  for (const s of document.querySelectorAll('script[type="application/ld+json"]')) {
    try { const v = JSON.parse(s.textContent); (Array.isArray(v) ? v : [v]).forEach(x => out.jsonld.push(x)); } catch (e) {}
  }
  for (const m of document.querySelectorAll('meta[property], meta[name], meta[itemprop]')) {
    const k = m.getAttribute('property') || m.getAttribute('name') || m.getAttribute('itemprop');
    const v = m.getAttribute('content');
    if (k && v && /^(og:|article:|product:|twitter:|description|author|keywords|price|priceCurrency|datePublished)/i.test(k))
      out.meta[k] = v.slice(0, 300);
  }
  return out;
}
"""
