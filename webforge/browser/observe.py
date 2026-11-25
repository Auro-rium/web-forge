"""Page observation: injected JS that indexes interactive elements and builds robust locators."""
from __future__ import annotations

from webforge.contracts import El, Observation

MAX_ELEMENTS = 150
MAX_TEXT = 20000

OBSERVE_JS = r"""
(maxEl) => {
  const SEL = 'a[href],button,input:not([type=hidden]),select,textarea,[role=button],[onclick]';
  document.querySelectorAll('[data-wf-idx]').forEach(e => e.removeAttribute('data-wf-idx'));
  const norm = s => (s || '').replace(/\s+/g, ' ').trim();
  const vis = e => {
    const r = e.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0) return false;
    const s = getComputedStyle(e);
    return s.visibility !== 'hidden' && s.display !== 'none';
  };
  const textual = ['text', 'search', 'email', 'tel', 'url', 'password', 'number', ''];
  const roleOf = e => {
    const tag = e.tagName.toLowerCase(), t = (e.getAttribute('type') || '').toLowerCase();
    const explicit = e.getAttribute('role');
    let role, lrole;
    if (tag === 'a') role = 'link';
    else if (tag === 'button') role = 'button';
    else if (tag === 'select') role = 'combobox';
    else if (tag === 'textarea') role = 'textbox';
    else if (tag === 'input') {
      if (t === 'checkbox') role = 'checkbox';
      else if (t === 'radio') role = 'radio';
      else if (['submit', 'button', 'reset', 'image'].includes(t)) role = 'button';
      else if (t === 'range') role = 'slider';
      else role = 'textbox';
      lrole = t === 'search' ? 'searchbox' : t === 'number' ? 'spinbutton' : role;
    } else role = explicit || 'button';
    if (explicit && tag !== 'input' && tag !== 'select' && tag !== 'textarea') role = explicit;
    return [role, lrole || role];
  };
  const nameOf = e => {
    const tag = e.tagName.toLowerCase();
    const field = (tag === 'input' && !['submit', 'button', 'reset', 'image'].includes((e.type || '').toLowerCase()))
                  || tag === 'select' || tag === 'textarea';
    const lbl = e.labels && e.labels.length ? e.labels[0].innerText : '';
    const cands = field ? [
      ['aria', e.getAttribute('aria-label')],
      ['label', lbl],
      ['placeholder', e.getAttribute('placeholder')],
      ['title', e.getAttribute('title')],
      ['attr', e.getAttribute('name')],
    ] : [
      ['aria', e.getAttribute('aria-label')],
      ['text', e.innerText],
      ['title', e.getAttribute('title')],
      ['alt', e.getAttribute('alt') || (e.querySelector('img[alt]') || {}).alt],
      ['value', e.value],
      ['label', lbl],
    ];
    for (const [src, v] of cands) { const n = norm(v); if (n) return [n, src]; }
    return ['', ''];
  };
  const gen = c => /\d{3,}/.test(c) || c.length > 25;
  const stableId = id => id && /^[A-Za-z][\w-]*$/.test(id) && !/\d{3,}/.test(id);
  const cssPath = e => {
    const parts = [];
    for (let n = e; n && n.nodeType === 1 && n !== document.body && n !== document.documentElement; n = n.parentElement) {
      if (stableId(n.id)) { parts.unshift('#' + CSS.escape(n.id)); break; }
      let seg = n.tagName.toLowerCase();
      seg += [...n.classList].filter(c => !gen(c)).slice(0, 2).map(c => '.' + CSS.escape(c)).join('');
      const same = [...n.parentElement.children].filter(x => x.tagName === n.tagName);
      if (same.length > 1) seg += ':nth-of-type(' + (same.indexOf(n) + 1) + ')';
      parts.unshift(seg);
      try {
        const m = document.querySelectorAll(parts.join(' > '));
        if (m.length === 1 && m[0] === e) break;
      } catch (_) {}
    }
    return parts.join(' > ');
  };
  const infoOf = e => {
    const tag = e.tagName.toLowerCase(), t = (e.getAttribute('type') || '').toLowerCase(), bits = [];
    if (tag === 'input' && t && !['text', 'submit', 'button'].includes(t)) bits.push(t);
    if (e.required || e.getAttribute('aria-required') === 'true') bits.push('required');
    if (e.disabled) bits.push('disabled');
    if (tag === 'select') bits.push('options=' + JSON.stringify([...e.options].slice(0, 15).map(o => norm(o.text))));
    if (e.getAttribute('aria-invalid') === 'true' || (e.willValidate && e.value && !e.checkValidity())) bits.push('INVALID');
    if (tag === 'button' && (e.type || '') === 'submit' || (tag === 'input' && t === 'submit')) bits.push('submit');
    return bits.join(', ');
  };
  const nodes = [...document.querySelectorAll(SEL)].filter(vis).slice(0, maxEl);
  const items = nodes.map((e, i) => {
    e.setAttribute('data-wf-idx', i);
    const tag = e.tagName.toLowerCase();
    const [role, lrole] = roleOf(e);
    const [full, src] = nameOf(e);
    const name = full.length > 80 ? full.slice(0, 79) + '…' : full;
    return { idx: i, tag, role, lrole, name, full, src, el: e,
             value: tag === 'input' && ['checkbox', 'radio'].includes((e.type || '')) ? (e.checked ? 'checked' : '')
                    : ((e.type || '') === 'password' ? (e.value ? '•••' : '') : (e.value || '')),
             info: infoOf(e),
             href: tag === 'a' ? (e.href || '') : '' };
  });
  const count = (f) => { const m = {}; items.forEach(it => { const k = f(it); m[k] = (m[k] || 0) + 1; }); return m; };
  const trunc = it => it.full.length > 80;
  const byRole = count(it => it.lrole + '|' + it.full);
  const byText = count(it => it.full);
  const elements = items.map(it => {
    let loc;
    const input = ['input', 'textarea'].includes(it.tag);
    const roleOk = it.full && !trunc(it) && !(input && textual.includes((it.el.type || '')) && it.src === 'value')
                   && byRole[it.lrole + '|' + it.full] === 1;
    if (roleOk) loc = { role: it.lrole, name: it.full };
    else if (input && it.el.getAttribute('placeholder')) loc = { placeholder: it.el.getAttribute('placeholder') };
    else if (it.src === 'text' && !trunc(it) && byText[it.full] === 1) loc = { text: it.full };
    else loc = { css: cssPath(it.el) };
    if (it.info.includes('submit')) loc = { ...loc, submit: true };
    return { idx: it.idx, tag: it.tag, role: it.role, name: it.name, locator: loc, value: it.value.slice(0, 80),
             href: it.href, info: it.info };
  });
  const errSel = '[role=alert], .error, .errors, .invalid-feedback, .field-error, .help-block.error, [aria-live=assertive]';
  const errors = [...document.querySelectorAll(errSel)].filter(vis).map(x => norm(x.innerText)).filter(Boolean);
  document.querySelectorAll('input, select, textarea').forEach(x => {
    if (x.willValidate && x.dataset.wfTouched === '1' && !x.checkValidity() && x.validationMessage)
      errors.push((nameOf(x)[0] || x.name || 'field') + ': ' + x.validationMessage);
  });
  return { url: location.href, title: document.title, elements, errors: [...new Set(errors)].slice(0, 12),
           text: norm(document.body ? document.body.innerText : '') };
}
"""


async def observe_page(page) -> Observation:
    d = await page.evaluate(OBSERVE_JS, MAX_ELEMENTS)
    return Observation(url=d["url"], title=d["title"], elements=[El(**e) for e in d["elements"]],
                       text=d["text"][:MAX_TEXT], errors=d.get("errors") or [])


async def page_text(page, max_chars: int = 4000) -> str:
    t = await page.evaluate("() => (document.body ? document.body.innerText : '').replace(/\\s+/g,' ').trim()")
    return t[:max_chars]
