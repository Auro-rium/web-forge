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
