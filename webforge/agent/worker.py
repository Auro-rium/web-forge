"""Browsing sub-agent: fresh LLM thread per subtask (context isolation)."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Optional
from urllib.parse import urlparse

from webforge.agent.prompts import WORKER_SYSTEM, WORKER_TOOLS
from webforge.contracts import TraceStep
from webforge.safety import check_action

ACTIONS = {"goto", "click", "type", "select", "press", "scroll", "back", "extract"}


@dataclass
class WorkerResult:
    success: bool
    summary: str
    data: Any = None
    steps: int = 0
    trace: list = field(default_factory=list)   # list[TraceStep]
    domain: str = ""


def _emit(on_event: Optional[Callable], kind: str, data: dict) -> None:
    if on_event:
        try:
            on_event(kind, data)
        except Exception:
            pass


def _domain_of(browser) -> str:
    try:
        return (urlparse(browser.page.url).hostname or "").lower()
    except Exception:
        return ""


def _jsonable(x: Any) -> Any:
    try:
        return json.loads(json.dumps(x, default=str))
    except Exception:
        return str(x)


async def _render(browser) -> tuple[str, list]:
    try:
        obs = await browser.observe()
        return obs.render(max_elements=100, max_text=2500), list(obs.elements)
    except Exception as e:  # noqa: BLE001
        return f"(observation failed: {e})", []


async def run_worker(subtask: str, start_url: Optional[str] = None, *, llm, browser, cfg,
                     on_event: Optional[Callable] = None, hints: str = "") -> WorkerResult:
    trace_start = len(browser.trace)
    steps = 0
    allowed = getattr(cfg, "allowed_domains", None)
    max_steps = int(getattr(cfg, "worker_max_steps", 25))
    elements: list = []

    def result(success: bool, summary: str, data: Any = None) -> WorkerResult:
        return WorkerResult(success=success, summary=summary, data=data, steps=steps,
                            trace=list(browser.trace[trace_start:]), domain=_domain_of(browser))

    async def do_action(name: str, args: dict) -> str:
        nonlocal steps, elements
        ok, reason = check_action(name, args, elements, allowed)
        if not ok:
            _emit(on_event, "action", {"action": name, "args": args, "ok": False, "message": "BLOCKED: " + reason})
            return "ERROR (blocked by safety policy): " + reason
        kw = dict(args)
        if name == "scroll":
            kw.setdefault("direction", "down")
        try:
            res = await browser.act(name, **kw)
        except Exception as e:  # noqa: BLE001
            return f"ERROR: {e!r}"
        steps += 1
        _emit(on_event, "action", {"action": name, "args": args, "ok": res.ok, "message": res.message[:300]})
        msg = ("OK: " if res.ok else "ERROR: ") + res.message
        if name == "extract" and res.data is not None:
            msg += "\nDATA: " + json.dumps(_jsonable(res.data))[:2500]
        return msg

    # ---- first message
    if start_url:
        out = await do_action("goto", {"url": start_url})
        if out.startswith("ERROR"):
            hints = (hints + f"\n(Initial goto failed: {out})").strip()
    text, elements = await _render(browser)
    _emit(on_event, "observe", {"url": getattr(getattr(browser, "page", None), "url", ""), "n_elements": len(elements)})
    first = f"SUBTASK: {subtask}\n"
    if hints:
        first += f"\nLESSONS / HINTS (from earlier runs):\n{hints}\n"
    first += f"\nCURRENT PAGE:\n{text}"
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
                                       message="Call a tool: continue browsing or call done(success, summary, data).")
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
                return result(bool(succ), str(a.get("summary", ""))[:2000], a.get("data"))
            if tc.name not in ACTIONS:
                out = f"ERROR: unknown tool '{tc.name}'"
            elif steps >= max_steps:
                return result(False, "step budget exhausted")
            else:
                out = await do_action(tc.name, tc.args or {})
            if i == len(calls) - 1:
                text, elements = await _render(browser)
                _emit(on_event, "observe", {"url": getattr(getattr(browser, "page", None), "url", ""),
                                            "n_elements": len(elements)})
                out += "\n\nCURRENT PAGE:\n" + text
            outputs.append({"tool_call_id": tc.id, "output": out})
        if steps >= max_steps:
            return result(False, "step budget exhausted")
        reply = await llm.submit(role="worker", thread_id=thread, outputs=outputs)
