"""Async Playwright wrapper implementing BrowserAPI."""
from __future__ import annotations

import asyncio
from typing import Any, Optional

from playwright.async_api import async_playwright

from webforge.contracts import ActionResult, Observation, TraceStep
from webforge.browser.actions import (domain_allowed, extract_rows, normalize_url, preview,
                                      resolve_locator, short_err)
from webforge.browser.observe import observe_page, page_text as _page_text

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/124.0.0.0 Safari/537.36")


class Browser:
    def __init__(self, chromium_path: str = "", allowed_domains: Optional[list[str]] = None):
        self.chromium_path = chromium_path
        self.allowed_domains = allowed_domains
        self.trace: list[TraceStep] = []
        self.page = None
        self._pw = self._browser = self._ctx = None
        self._last_obs: Optional[Observation] = None
        self._new_page = None

    # ------------------------------------------------------------ lifecycle ---
    async def start(self, headless: bool = True) -> None:
        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(
            executable_path=self.chromium_path or None, headless=headless, args=["--no-sandbox"])
        self._ctx = await self._browser.new_context(viewport={"width": 1280, "height": 900}, user_agent=UA)
        self._ctx.set_default_timeout(8000)
        self._ctx.on("page", lambda p: setattr(self, "_new_page", p))
        self.page = await self._ctx.new_page()
        self._new_page = None

    async def close(self) -> None:
        for obj, meth in ((self._ctx, "close"), (self._browser, "close"), (self._pw, "stop")):
            try:
                if obj:
                    await getattr(obj, meth)()
            except Exception:
                pass
        self.page = self._ctx = self._browser = self._pw = None

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
        try:
            res = await self._do(action, args)
        except Exception as e:  # never raise from act()
            res = ActionResult(False, short_err(e))
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
        if action in ("click", "type", "select"):
            el = await self._by_idx(a.get("idx", -1))
            if el is None:
                return ActionResult(False, f"element idx {a.get('idx')} not found; observe again")
            return await self._element_action(action, el, a.get("text", a.get("value")),
                                              bool(a.get("submit", False)))
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
        return ActionResult(False, f"unknown action '{action}'")

    async def _goto(self, url: str) -> ActionResult:
        url = normalize_url(url)
        if not domain_allowed(url, self.allowed_domains):
            return ActionResult(False, "domain not allowed")
        await self.page.goto(url, wait_until="domcontentloaded")
        await self._settle()
        return ActionResult(True, f"navigated to {self.page.url}")

    async def _element_action(self, action: str, el, value: Optional[str], submit: bool) -> ActionResult:
        if action == "click":
            await el.click()
            await self._settle()
            return ActionResult(True, f"clicked; now at {self.page.url}")
        if action == "type":
            await el.fill(value or "")
            if submit:
                await el.press("Enter")
                await self._settle()
            return ActionResult(True, f"typed '{value}'" + (" and submitted" if submit else ""))
        try:
            await el.select_option(value=value)
        except Exception:
            await el.select_option(label=value)
        return ActionResult(True, f"selected '{value}'")

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

    # --------------------------------------------------------- replay (skills) ---
    async def act_locator(self, action: str, locator: Optional[dict], value: Optional[str] = None,
                          extract: Optional[dict] = None) -> ActionResult:
        """Replay by locator. Does not touch self.trace."""
        try:
            if action == "goto":
                return await self._goto(value or "")
            if action == "scroll":
                return await self._scroll(value or "down")
            if action == "wait":
                await asyncio.sleep(min(float(value or 1000), 15000) / 1000)
                return ActionResult(True, "waited")
            if action == "press":
                await self.page.keyboard.press(value or "Enter")
                await self._settle()
                return ActionResult(True, f"pressed {value}")
            if action == "extract":
                ex = extract or {"type": "text"}
                if ex.get("type", "css") == "text":
                    t = await self.page_text(4000)
                    return ActionResult(True, t[:500], t)
                return await self._extract(ex.get("selector") or ex.get("css"), ex.get("fields"))
            if action in ("click", "type", "select"):
                el = await resolve_locator(self.page, locator)
                if el is None:
                    return ActionResult(False, f"locator not found: {locator}")
                return await self._element_action(action, el, value, False)
            return ActionResult(False, f"unknown action '{action}'")
        except Exception as e:
            return ActionResult(False, short_err(e))
