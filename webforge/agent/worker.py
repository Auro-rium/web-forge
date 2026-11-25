"""Browsing worker sub-agent: one focused subtask, a fresh LLM thread and its own browser tab.

Every action goes through the safety policy: allowed actions run, consequential ones (submit, send,
login, delete, typing a secret) wait for the human's approval via `ask`, payments are always blocked.
"""
from __future__ import annotations

import base64
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional
from urllib.parse import urlparse

from webforge.agent.prompts import WORKER_SYSTEM, WORKER_TOOLS
from webforge.safety import ALLOW, BLOCK, approval_question, check_action, strip_userinfo

ACTIONS = {"goto", "click", "type", "select", "check", "fill_form", "press", "scroll", "back", "extract",
           "read", "tables", "links", "find", "structured"}
DATA_ACTIONS = {"extract"}   # the other readers already put their data in the message
PAGE_CHANGING = {"goto", "click", "type", "select", "check", "fill_form", "press", "back"}
SCREENSHOT_EVERY_S = 1.5
MAX_QUESTIONS = 3        # distinct questions one sub-agent may put to the human
MAX_ASK_CALLS = 8        # ask_user calls (including repeats) before the sub-agent is stopped


def _similar(a: str, b: str) -> float:
    """Word overlap (Jaccard) between two questions: same question reworded scores high."""
    ta = {w for w in re.findall(r"[a-z0-9@.]+", a.lower()) if len(w) > 2}
    tb = {w for w in re.findall(r"[a-z0-9@.]+", b.lower()) if len(w) > 2}
    return len(ta & tb) / len(ta | tb) if ta and tb else 0.0
NO_USER = "No user is available to answer. Proceed with your best judgment and state the assumption."


@dataclass
class WorkerResult:
    success: bool
    summary: str
    data: Any = None
    steps: int = 0
    trace: list = field(default_factory=list)   # list[TraceStep]
    domain: str = ""
    denied: list = field(default_factory=list)  # actions the user (or the approval mode) refused
    answered: list = field(default_factory=list)  # (question, answer) pairs the user already answered


def _emit(on_event: Optional[Callable], kind: str, data: dict) -> None:
    if on_event:
        try:
            on_event(kind, data)
        except Exception:
            pass


def _url(browser) -> str:
    return getattr(getattr(browser, "page", None), "url", "") or ""


def _jsonable(x: Any) -> Any:
    try:
        return json.loads(json.dumps(x, default=str))
    except Exception:
        return str(x)


async def _render(browser, max_text: int) -> tuple[str, list]:
    try:
        obs = await browser.observe()
        return obs.render(max_elements=100, max_text=max_text), list(obs.elements)
    except Exception as e:  # noqa: BLE001
        return f"(observation failed: {e})", []


def _approved(answer: str) -> bool:
    return (answer or "").strip().lower().startswith(("approve", "yes", "y", "ok", "allow"))


async def run_worker(subtask: str, start_url: Optional[str] = None, *, llm, browser, cfg,
                     on_event: Optional[Callable] = None,
                     ask: Optional[Callable[..., Awaitable[str]]] = None, agent: str = "planner") -> WorkerResult:
    trace_start = len(browser.trace)
    steps = 0
    allowed = getattr(cfg, "allowed_domains", None)
    max_steps = int(getattr(cfg, "worker_max_steps", 25))
    obs_chars = int(getattr(cfg, "obs_text_chars", 4000))
    mode = str(getattr(cfg, "approval", "ask"))
    shots = bool(getattr(cfg, "screenshots", False)) and hasattr(browser, "screenshot")
    secrets = getattr(browser, "secrets", None)
    elements: list = []
    last_shot = [0.0]
    denied: list[str] = []
    ask_calls = 0
    answered: list[tuple[str, str]] = []
    agent_id = [agent]

    def redact(text: str) -> str:
        return secrets.redact(text) if secrets is not None else text

    def redact_fields(fields: dict) -> dict:
        return {k: redact(str(v)) for k, v in (fields or {}).items()}

    def result(success: bool, summary: str, data: Any = None) -> WorkerResult:
        return WorkerResult(success=success, summary=summary, data=data, steps=steps, denied=list(denied),
                            trace=list(browser.trace[trace_start:]), answered=list(answered),
                            domain=(urlparse(_url(browser)).hostname or "").lower())

    async def screenshot(force: bool = False) -> None:
        if not shots or (not force and time.monotonic() - last_shot[0] < SCREENSHOT_EVERY_S):
            return
        last_shot[0] = time.monotonic()
        img = await browser.screenshot()
        if img and len(img) < 120_000:
            _emit(on_event, "screenshot", {"jpeg_b64": base64.b64encode(img).decode(), "url": _url(browser)})

    async def ask_human(question: str, options: list, last: str, meta: Optional[dict] = None) -> str:
        """Ask the human with the tab flagged for attention and agent_state waiting/running around it."""
        att = getattr(browser, "attention", None)
        _emit(on_event, "agent_state", {"state": "waiting", "last": last})
        if att:
            try:
                await att(True)
            except Exception:
                pass
        try:
            return (await (ask(question, options, agent_id[0], meta) if meta else ask(question, options, agent_id[0]))) or ""
        finally:
            if att:
                try:
                    await att(False)
                except Exception:
                    pass
            _emit(on_event, "agent_state", {"state": "running", "last": "answer received"})

    async def gate(name: str, args: dict) -> Optional[str]:
        """None if the action may run, else the error text for the model."""
        verdict, reason = check_action(name, args, elements, allowed)
        if verdict == ALLOW:
            return None
        if verdict == BLOCK:
            _emit(on_event, "action", {"action": name, "args": args, "ok": False, "message": "BLOCKED: " + reason})
            return "ERROR (blocked by safety policy, never allowed): " + reason
        if mode == "auto":
            return None
        if mode == "deny" or ask is None:
            _emit(on_event, "action", {"action": name, "args": args, "ok": False, "message": "NOT APPROVED: " + reason})
            denied.append(reason)
            return f"ERROR: '{reason}' needs the user's approval and approval is not available. Do not retry it."
        await screenshot(force=True)
        fields = args.get("fields") or {e.name or f"[{e.idx}]": e.value for e in elements
                                         if e.tag in ("input", "select", "textarea") and e.value}
        shown = redact_fields(fields)
        # the consent screen must show the REAL host: a page can sit at https://school.org-...@evil.com/
        safe_url, had_userinfo = strip_userinfo(_url(browser))
        # structured copy for the UI: it must never have to parse the question text (the button name is page-controlled)
        meta = {"kind": "approval", "reason": reason, "url": safe_url, "fields": shown, "userinfo": had_userinfo}
        answer = await ask_human(approval_question(reason, safe_url, shown),
                                 ["approve", "deny"], f"waiting for approval: {reason}"[:120], meta)
        if _approved(answer):
            return None
        _emit(on_event, "action", {"action": name, "args": args, "ok": False, "message": "DENIED by user: " + reason})
        denied.append(reason)
        return f"ERROR: the user did not approve '{reason}' (answer: {answer[:120]}). Do not retry it."

    async def do_action(name: str, args: dict) -> str:
        nonlocal steps
        refused = await gate(name, args)
        if refused:
            return refused
        kw = dict(args)
        if name == "scroll":
            kw.setdefault("direction", "down")
        try:
            res = await browser.act(name, **kw)
        except Exception as e:  # noqa: BLE001
            return f"ERROR: {redact(repr(e))}"
        steps += 1
        _emit(on_event, "action", {"action": name, "args": args, "ok": res.ok, "message": res.message[:300]})
        if name in PAGE_CHANGING:
            await screenshot()
        msg = ("OK: " if res.ok else "ERROR: ") + res.message
        if name in DATA_ACTIONS and res.data is not None:
            msg += "\nDATA: " + redact(json.dumps(_jsonable(res.data))[:4000])
        return msg

    async def page() -> str:
        nonlocal elements
        text, elements = await _render(browser, obs_chars)
        _emit(on_event, "observe", {"url": _url(browser), "n_elements": len(elements)})
        return redact(text)

    # ---- first message
    note = ""
    if start_url:
        out = await do_action("goto", {"url": start_url})
        if out.startswith("ERROR"):
            note = f"\n(Initial goto failed: {out})"
    first = f"SUBTASK: {subtask}\n"
    if secrets is not None and secrets.names():
        first += f"\nSECRETS available (type them as {{{{secret:NAME}}}}): {', '.join(secrets.names())}\n"
    first += f"{note}\nCURRENT PAGE:\n{await page()}"
    await screenshot(force=True)
    reply = await llm.chat(role="worker", message=first, system=WORKER_SYSTEM, tools=WORKER_TOOLS)
    thread = reply.thread_id
    nudged = False

    while True:
        if reply.status == "FAILED":
            return result(False, "LLM call failed: " + (reply.text or "")[:200])
        if reply.status != "REQUIRES_ACTION" or not reply.tool_calls:
            if not nudged:
                nudged = True
                reply = await llm.chat(role="worker", thread_id=thread, system="", tools=WORKER_TOOLS,
                                       message="Call a tool: continue, or call done(success, summary, data).")
                continue
            return result(False, (reply.text or "ended without calling done()")[:500])

        outputs: list[dict] = []
        calls = reply.tool_calls
        for i, tc in enumerate(calls):
            if tc.name == "done":
                a = tc.args or {}
                succ = a.get("success", True)
                if isinstance(succ, str):
                    succ = succ.strip().lower() not in ("false", "0", "no")
                return result(bool(succ), redact(str(a.get("summary", "")))[:2000], a.get("data"))
            if tc.name == "ask_user":
                ask_calls += 1
                if ask_calls > MAX_ASK_CALLS:   # ask_user is not a browser step, so bound it explicitly
                    return result(False, "stopped: the sub-agent kept asking questions instead of acting")
                a = tc.args or {}
                q = str(a.get("question", "")).strip()
                if not q:
                    out = "ERROR: question is required"
                elif ask is None:
                    out = NO_USER
                elif (prev := next((ans for pq, ans in answered if _similar(q, pq) >= 0.5), None)) is not None:
                    # models sometimes re-ask an answered question; never pester the human twice for the same thing
                    out = (f"USER ANSWER (already answered; do not ask again): {prev}. Apply it now with fill_form / "
                           "type, then continue with the subtask.")
                elif len(answered) >= MAX_QUESTIONS:
                    out = (f"You have reached the question limit ({MAX_QUESTIONS}) for this subtask. Proceed with "
                           "what you have, or call done(success=false) explaining what is missing.")
                else:
                    opts = [str(o) for o in (a.get("options") or [])][:6]
                    ans = redact(await ask_human(q, opts, "asked the user: " + q[:100]))
                    answered.append((q[:200], ans[:200]))
                    out = "USER ANSWER: " + ans
            elif tc.name not in ACTIONS:
                out = f"ERROR: unknown tool '{tc.name}'"
            elif steps >= max_steps:
                return result(False, "step budget exhausted")
            else:
                out = await do_action(tc.name, tc.args or {})
            if i == len(calls) - 1:
                out += "\n\nCURRENT PAGE:\n" + await page()
            outputs.append({"tool_call_id": tc.id, "output": out})
        if steps >= max_steps:
            return result(False, "step budget exhausted")
        reply = await llm.submit(role="worker", thread_id=thread, outputs=outputs)
