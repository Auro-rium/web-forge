"""Async Playwright wrapper implementing BrowserAPI.

Browser owns the Chromium process and context; Tab owns one page with its own trace.
Browser delegates the BrowserAPI to a default tab, and new_tab() gives independent tabs
for parallel sub-agents.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, Optional
from urllib.parse import urlparse

from playwright.async_api import async_playwright

from webforge.contracts import ActionResult, Observation, TraceStep
from webforge.browser.actions import (FIND_JS, LINKS_JS, READ_JS, STRUCTURED_JS, TABLES_JS, extract_rows,
                                      normalize_url, preview, short_err)
from webforge.safety import Secrets, domain_allowed
from webforge.browser.observe import observe_page, page_text as _page_text

BADGE_JS = r"""
([label, text, waiting]) => {
  const colors = { w1: '#8fb8ff', w2: '#e0b26b', w3: '#9fd8a5', w4: '#d99bd6', w5: '#79d1d1' };
  let b = document.getElementById('wf-badge');
  if (!b) { b = document.createElement('div'); b.id = 'wf-badge'; document.documentElement.appendChild(b); }
  const c = waiting ? '#ffb454' : (colors[label] || '#8ea6ff');
  b.style.cssText = 'position:fixed;z-index:2147483647;right:12px;bottom:12px;max-width:60vw;padding:6px 10px;' +
    'border-radius:6px;font:600 12px/1.3 ui-monospace,Menlo,Consolas,monospace;color:#0a0b0e;background:' + c +
    ';box-shadow:0 4px 18px rgba(0,0,0,.45);pointer-events:none;opacity:.95;white-space:nowrap;overflow:hidden;text-overflow:ellipsis';
  b.textContent = 'WebForge · ' + (label || 'agent') + ' · ' + text;
  const base = document.title.replace(/^\[(w\d+|agent|read)\] /, '');
  document.title = '[' + (label || 'agent') + '] ' + base;
}
"""

CLEAR_MARKS_JS = r"""
() => {
  const b = document.getElementById('wf-badge'); if (b) b.remove();
  document.title = document.title.replace(/^\[(w\d+|agent|read)\] /, '');
  document.querySelectorAll('[data-wf-hl]').forEach(e => { e.style.outline = e.__wfo || ''; e.removeAttribute('data-wf-hl'); });
}
"""

HIGHLIGHT_JS = r"""
e => { e.setAttribute('data-wf-hl', '1'); e.__wfo = e.style.outline; e.style.outline = '3px solid #ffb454';
       e.style.outlineOffset = '2px'; e.scrollIntoView({ block: 'center', inline: 'center' }); }
"""

UNHIGHLIGHT_JS = r"""
e => { e.style.outline = e.__wfo || ''; e.style.outlineOffset = ''; e.removeAttribute('data-wf-hl'); }
"""


def _truthy(v: Any) -> bool:
    if isinstance(v, str):
        return v.strip().lower() not in ("", "false", "0", "no", "off", "unchecked")
    return bool(v)


UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/124.0.0.0 Safari/537.36")

READ_WINDOW = 6000


class Browser:
    def __init__(self, chromium_path: str = "", allowed_domains: Optional[list[str]] = None,
                 secrets: Optional[Secrets] = None):
        self.chromium_path = chromium_path
        self.allowed_domains = allowed_domains
        self.secrets = secrets or Secrets()
        self._pw = self._browser = self._ctx = None
        self._tab: Optional["Tab"] = None
        self._tabs: list["Tab"] = []
        self.external = False        # attached to a Chrome the user is running (CDP), not launched by us
        self.keep_tabs = False       # external mode: leave the agent's tabs open after the run (marks removed)
        self.mark: Optional[bool] = None   # badge/highlight on controlled pages; default: on when a human can see them
        self._visible = False        # a human is watching the real window: pause briefly so actions are visible

    # ------------------------------------------------------------ lifecycle ---
    async def start(self, headless: bool = True) -> None:
        self._visible = not headless
        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(
            executable_path=self.chromium_path or None, headless=headless, args=["--no-sandbox"])
        self._ctx = await self._browser.new_context(viewport={"width": 1280, "height": 900}, user_agent=UA)
        self._ctx.set_default_timeout(8000)
        self._tab = await self.new_tab()

    async def connect(self, cdp_url: str) -> None:
        """Attach to a running Chrome (started with --remote-debugging-port). Only NEW tabs are ever used."""
        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.connect_over_cdp(cdp_url)
        self._ctx = self._browser.contexts[0] if self._browser.contexts else await self._browser.new_context()
        self._ctx.set_default_timeout(8000)
        self.external, self._visible = True, True
        self._tab = await self.new_tab()

    async def new_tab(self) -> "Tab":
        """An independent tab (own page + trace) in the same browser context."""
        mark = self.mark if self.mark is not None else (self.external or self._visible)
        tab = Tab(await self._ctx.new_page(), self.allowed_domains, self.secrets, mark=mark, visible=self._visible)
        self._tabs.append(tab)
        return tab

    async def close(self) -> None:
        if self.external:
            # the user's Chrome keeps running and the user's own tabs are never touched
            for t in list(self._tabs):
                await (t.clear_marks() if self.keep_tabs else t.close())
            for obj, meth in ((self._browser, "close"), (self._pw, "stop")):
                try:
                    if obj:
                        await getattr(obj, meth)()
                except Exception:
                    pass
            self._ctx = self._browser = self._pw = None
            self._tab, self._tabs = None, []
            return
        for obj, meth in ((self._ctx, "close"), (self._browser, "close"), (self._pw, "stop")):
            try:
                if obj:
                    await getattr(obj, meth)()
            except Exception:
                pass
        self._ctx = self._browser = self._pw = None
        self._tab, self._tabs = None, []

    # ------------------------------------------- default-tab delegation ---
    @property
    def page(self):
        return self._tab.page if self._tab else None

    @property
    def trace(self) -> list[TraceStep]:
        return self._tab.trace if self._tab else []

    def __getattr__(self, name: str):
        # observe / act / act_locator / page_text / read / tables / ... go to the default tab
        tab = self.__dict__.get("_tab")
        if tab is not None and not name.startswith("_"):
            return getattr(tab, name)
        raise AttributeError(name)


class Tab:
    def __init__(self, page, allowed_domains: Optional[list[str]] = None, secrets: Optional[Secrets] = None,
                 mark: bool = False, visible: bool = False):
        self.page = page
        self.allowed_domains = allowed_domains
        self.secrets = secrets or Secrets(env=False)
        self.trace: list[TraceStep] = []
        self.label = ""              # agent id shown on the page badge and tab title ("w2")
        self.status = "ready"
        self.mark = mark             # draw the badge / outline on the page
        self.visible = visible       # a human watches this window: slow down just enough to be seen
        self._waiting = False
        self._owned = [page]         # every page this tab opened (a click may open popups); closed with the tab
        self._last_obs: Optional[Observation] = None
        self._new_page = None
        page.on("popup", lambda p: setattr(self, "_new_page", p))

    def identify(self, label: str) -> None:
        self.label = label

    async def new_tab(self) -> "Tab":
        return Tab(await self.page.context.new_page(), self.allowed_domains, self.secrets, self.mark, self.visible)

    async def close(self) -> None:
        for pg in self._owned:
            try:
                await pg.close()
            except Exception:
                pass

    # -------------------------------------------- visible control marks ---
    def _describe(self, action: str, a: dict) -> str:
        name = ""
        if "idx" in a and self._last_obs:
            name = next((e.name for e in self._last_obs.elements if e.idx == a["idx"]), "")
        if action == "goto":
            return "opening " + (urlparse(normalize_url(str(a.get("url", "")))).hostname or "page")
        if action == "click":
            return f"clicking '{name[:30]}'" if name else "clicking"
        if action == "type":
            return f"typing in '{name[:30]}'" if name else "typing"
        if action == "fill_form":
            return "filling the form"
        if action in ("select", "check"):
            return f"setting '{name[:30]}'" if name else "setting a field"
        if action in ("read", "tables", "links", "find", "structured", "extract"):
            return "reading the page"
        return action

    async def _mark(self, text: Optional[str] = None) -> None:
        if not self.mark:
            return
        if text is not None:
            self.status = text
        try:
            await self.page.evaluate(BADGE_JS, [self.label, "waiting for you" if self._waiting else self.status,
                                                 self._waiting])
        except Exception:
            pass   # error pages, closed tabs: the badge is decoration

    async def attention(self, waiting: bool) -> None:
        """The agent needs the human: bring this tab to the front and turn its badge amber."""
        self._waiting = waiting
        if self.mark:
            if waiting and self.visible:
                try:
                    await self.page.bring_to_front()
                except Exception:
                    pass
            await self._mark()

    async def clear_marks(self) -> None:
        for pg in self._owned:
            try:
                await pg.evaluate(CLEAR_MARKS_JS)
            except Exception:
                pass

    async def _highlight(self, el) -> None:
        if not self.mark:
            return
        try:
            await el.evaluate(HIGHLIGHT_JS)
            if self.visible:
                await asyncio.sleep(0.35)   # long enough for a person to see where the agent is working
        except Exception:
            pass

    @staticmethod
    async def _unhighlight(el) -> None:
        try:
            await el.evaluate(UNHIGHLIGHT_JS)
        except Exception:
            pass   # the click navigated away: the element is gone

    # ------------------------------------------------------------- observe ---
    async def observe(self) -> Observation:
        self._last_obs = await observe_page(self.page)
        return self._last_obs

    async def page_text(self, max_chars: int = 4000) -> str:
        return await _page_text(self.page, max_chars)

    # ----------------------------------------------------------------- act ---
    async def _settle(self) -> None:
        if self._new_page is not None:
            self.page, self._new_page = self._new_page, None
            self._owned.append(self.page)
            self.page.on("popup", lambda p: setattr(self, "_new_page", p))
        try:
            await self.page.wait_for_load_state("domcontentloaded", timeout=5000)
        except Exception:
            pass
        await asyncio.sleep(0.3)

    def _locator_for_idx(self, idx: Any) -> Optional[dict]:
        if self._last_obs:
            for e in self._last_obs.elements:
                if e.idx == idx:
                    return e.locator
        return None

    async def act(self, action: str, **args: Any) -> ActionResult:
        url_before = self.page.url if self.page else ""
        locator = self._locator_for_idx(args.get("idx")) if "idx" in args else None
        await self._mark(self._describe(action, args))
        try:
            res = await self._do(action, args)
        except Exception as e:  # never raise from act()
            res = ActionResult(False, short_err(e))
        await self._mark()   # navigation replaces the document: put the badge and title prefix back
        res.message = self.secrets.redact(res.message)
        self.trace.append(TraceStep(action=action, args=dict(args), locator=locator, url=url_before,
                                    ok=res.ok, result=res.message[:300]))
        return res

    async def _by_idx(self, idx: Any):
        el = self.page.locator(f'[data-wf-idx="{int(idx)}"]')
        return el.first if await el.count() else None

    async def _do(self, action: str, a: dict) -> ActionResult:
        p = self.page
        if action == "goto":
            return await self._goto(a.get("url", ""))
        if action in ("click", "type", "select", "check"):
            el = await self._by_idx(a.get("idx", -1))
            if el is None:
                return ActionResult(False, f"element idx {a.get('idx')} not found; observe again")
            if action == "check":
                return await self._set_checked(el, a.get("on", True))
            return await self._element_action(action, el, a.get("text", a.get("value")),
                                              bool(a.get("submit", False)))
        if action == "fill_form":
            return await self.fill_form(a.get("fields") or {})
        if action == "press":
            await p.keyboard.press(a["key"])
            await self._settle()
            return ActionResult(True, f"pressed {a['key']}")
        if action == "scroll":
            return await self._scroll(a.get("direction", "down"))
        if action == "back":
            await p.go_back(wait_until="domcontentloaded")
            await self._settle()
            return ActionResult(True, f"went back to {self.page.url}")
        if action == "wait":
            await asyncio.sleep(min(float(a.get("ms", 1000)), 15000) / 1000)
            return ActionResult(True, "waited")
        if action == "extract":
            return await self._extract(a.get("css"), a.get("fields"))
        if action == "read":
            return await self.read(int(a.get("offset", 0) or 0))
        if action == "tables":
            return await self.tables()
        if action == "links":
            return await self.links(a.get("filter") or "")
        if action == "find":
            return await self.find(str(a.get("text", "")))
        if action == "structured":
            return await self.structured()
        return ActionResult(False, f"unknown action '{action}'")

    async def _goto(self, url: str) -> ActionResult:
        url = normalize_url(url)
        if not domain_allowed(url, self.allowed_domains):
            return ActionResult(False, "domain not allowed")
        await self.page.goto(url, wait_until="domcontentloaded")
        await self._settle()
        return ActionResult(True, f"navigated to {self.page.url}")

    async def _element_action(self, action: str, el, value: Optional[str], submit: bool) -> ActionResult:
        await self._highlight(el)
        try:
            return await self._element_action_inner(action, el, value, submit)
        finally:
            await self._unhighlight(el)

    async def _element_action_inner(self, action: str, el, value: Optional[str], submit: bool) -> ActionResult:
        if action == "click":
            await el.click()
            await self._settle()
            return ActionResult(True, f"clicked; now at {self.page.url}")
        if action == "type":
            real, missing = self.secrets.resolve(value or "")
            if missing:
                return ActionResult(False, f"unknown secret(s) {missing}; available: {self.secrets.names()}")
            await el.fill(real)
            await self._touch(el)
            if submit:
                await el.press("Enter")
                await self._settle()
            return ActionResult(True, f"typed '{value}'" + (" and submitted" if submit else ""))
        try:
            await el.select_option(value=value)
        except Exception:
            await el.select_option(label=value)
        await self._touch(el)
        return ActionResult(True, f"selected '{value}'")

    @staticmethod
    async def _touch(el) -> None:
        try:   # lets the observation report native validation errors for fields the agent filled
            await el.evaluate("e => { e.dataset.wfTouched = '1'; }")
        except Exception:
            pass

    async def _set_checked(self, el, on: Any) -> ActionResult:
        on = _truthy(on)
        await self._highlight(el)
        try:
            await el.set_checked(on)
            await self._touch(el)
        finally:
            await self._unhighlight(el)
        return ActionResult(True, ("checked" if on else "unchecked"))

    def _match_field(self, key: Any):
        """Element for a fill_form key: idx, then exact label/name, then case-insensitive contains."""
        els = self._last_obs.elements if self._last_obs else []
        k = str(key).strip()
        if k.isdigit():
            return next((e for e in els if e.idx == int(k)), None)
        low = k.lower()
        fields = [e for e in els if e.tag in ("input", "select", "textarea")]
        return (next((e for e in fields if e.name.lower() == low), None)
                or next((e for e in fields if low in e.name.lower() or (e.name and e.name.lower() in low)), None))

    async def fill_form(self, fields: dict) -> ActionResult:
        """Fill many fields in one step. Keys are labels (or idx); values set by field type."""
        if not isinstance(fields, dict) or not fields:
            return ActionResult(False, "fields must be a non-empty {label_or_idx: value} object")
        if self._last_obs is None:
            await self.observe()
        results, ok_all = [], True
        for key, value in fields.items():
            meta = self._match_field(key)
            if meta is None:
                ok_all = False
                results.append(f"{key}: NO MATCHING FIELD")
                continue
            el = await self._by_idx(meta.idx)
            if el is None:
                ok_all = False
                results.append(f"{key}: element gone; observe again")
                continue
            try:
                kind = (meta.locator.get("role") if isinstance(meta.locator, dict) else "") or meta.role
                if meta.tag == "select":
                    r = await self._element_action("select", el, str(value), False)
                elif meta.role in ("checkbox", "radio") or kind in ("checkbox", "radio"):
                    r = await self._set_checked(el, value)
                else:
                    r = await self._element_action("type", el, str(value), False)
                ok_all &= r.ok
                results.append(f"{key} -> [{meta.idx}] {r.message if r.ok else 'ERROR ' + r.message}")
            except Exception as e:  # noqa: BLE001
                ok_all = False
                results.append(f"{key}: ERROR {short_err(e)}")
        return ActionResult(ok_all, "filled form: " + "; ".join(results))

    async def screenshot(self, width: int = 480, quality: int = 50) -> Optional[bytes]:
        """Small JPEG of the viewport for the live UI (None on failure)."""
        try:
            png = await self.page.screenshot(type="jpeg", quality=quality, scale="css",
                                             clip={"x": 0, "y": 0, "width": 1280, "height": 900})
        except Exception:
            return None
        try:
            from PIL import Image  # optional: shrink if Pillow is available
            import io
            im = Image.open(io.BytesIO(png))
            im.thumbnail((width, width))
            out = io.BytesIO()
            im.save(out, "JPEG", quality=quality)
            return out.getvalue()
        except Exception:
            return png

    async def _scroll(self, direction: str) -> ActionResult:
        sign = -1 if str(direction).lower() == "up" else 1
        await self.page.evaluate("(s) => window.scrollBy(0, s * window.innerHeight * 0.8)", sign)
        await asyncio.sleep(0.2)
        return ActionResult(True, f"scrolled {direction}")

    async def _extract(self, css: Optional[str], fields: Optional[dict]) -> ActionResult:
        if not css:
            t = await self.page_text(4000)
            return ActionResult(True, t[:500], t)
        res = await extract_rows(self.page, css, fields)
        if res.get("error"):
            return ActionResult(False, res["error"], [])
        rows = res.get("rows") or []
        if not rows:
            return ActionResult(False, f"no elements match '{css}'", [])
        msg = preview(rows)
        if res.get("bad"):
            msg = f"invalid selector for field(s) {', '.join(res['bad'])} (left empty); " + msg
        return ActionResult(True, msg, rows)

    # ------------------------------------------- generic extraction tools ---
    async def read(self, offset: int = 0, window: int = READ_WINDOW) -> ActionResult:
        """Readable main content (headings, lists, table rows), paginated."""
        try:
            text = await self.page.evaluate(READ_JS) or ""
        except Exception as e:
            return ActionResult(False, short_err(e))
        if not text.strip():
            text = await self.page_text(200000)
        offset = max(0, offset)
        chunk = text[offset:offset + window]
        nxt = offset + window
        more = f"\n...[{len(text) - nxt} more chars; read(offset={nxt})]" if nxt < len(text) else ""
        return ActionResult(bool(chunk), f"{len(text)} chars total, showing {offset}-{offset + len(chunk)}:\n"
                            + chunk + more, chunk)

    async def tables(self, max_rows: int = 60) -> ActionResult:
        try:
            tbs = await self.page.evaluate(TABLES_JS, max_rows)
        except Exception as e:
            return ActionResult(False, short_err(e), [])
        if not tbs:
            return ActionResult(False, "no data tables on this page", [])
        return ActionResult(True, f"{len(tbs)} table(s): " + json.dumps(tbs, ensure_ascii=False)[:3000], tbs)

    async def links(self, filter: str = "", max_links: int = 80) -> ActionResult:
        try:
            ls = await self.page.evaluate(LINKS_JS, [filter, max_links])
        except Exception as e:
            return ActionResult(False, short_err(e), [])
        if not ls:
            return ActionResult(False, f"no visible links{' matching ' + repr(filter) if filter else ''}", [])
        return ActionResult(True, preview(ls, 40), ls)

    async def find(self, text: str, context: int = 300) -> ActionResult:
        if not text:
            return ActionResult(False, "text is required")
        try:
            r = await self.page.evaluate(FIND_JS, [text, context])
        except Exception as e:
            return ActionResult(False, short_err(e))
        if not r.get("found"):
            return ActionResult(False, f"'{text}' not found on this page")
        return ActionResult(True, f"found {r['count']}x; context: {r['context']}", r)

    async def structured(self) -> ActionResult:
        """JSON-LD + OpenGraph/meta data (prices, dates, ratings) without selectors."""
        try:
            d = await self.page.evaluate(STRUCTURED_JS)
        except Exception as e:
            return ActionResult(False, short_err(e))
        if not d.get("jsonld") and not d.get("meta"):
            return ActionResult(False, "no structured data (JSON-LD/meta) on this page", d)
        return ActionResult(True, json.dumps(d, ensure_ascii=False, default=str)[:3000], d)


async def open_browser(cfg, secrets: Optional[Secrets] = None, headless: bool = True) -> Browser:
    """The browser the agent will use: the user's own Chrome when WEBFORGE_CDP_URL is set, else a launched Chromium."""
    b = Browser(chromium_path=cfg.chromium_path, allowed_domains=cfg.allowed_domains, secrets=secrets)
    b.mark = True
    if cfg.cdp_url:
        if cfg.approval == "auto" and not cfg.allow_auto_real:
            raise RuntimeError("WEBFORGE_APPROVAL=auto is refused while controlling your own Chrome: submitting and "
                               "logging in would run without asking. Use ask (default), or set "
                               "WEBFORGE_ALLOW_AUTO_REAL_BROWSER=1 if you really mean it.")
        b.keep_tabs = cfg.keep_tabs
        await b.connect(cfg.cdp_url)
    else:
        await b.start(headless=headless)
    return b
