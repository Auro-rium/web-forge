"""Shared interfaces for WebForge. Every module codes against these types.

DO NOT change signatures here without updating all modules.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional, Protocol


# ---------------------------------------------------------------- browser ---

@dataclass
class El:
    """One interactive element on the page, as shown to the LLM."""
    idx: int                 # index the LLM refers to: click(idx=3)
    tag: str                 # a, button, input, select, textarea, ...
    role: str                # ARIA-ish role: link, button, textbox, combobox, checkbox, ...
    name: str                # accessible name / visible text / placeholder (trimmed, <=80 chars)
    locator: dict            # robust locator, see LOCATOR below
    value: str = ""          # current value for inputs/selects
    href: str = ""           # for links
    info: str = ""           # form details: type, required, options=[...], INVALID, submit


# LOCATOR dict (recorded in the trace for each element action). Any subset of:
#   {"role": "link", "name": "Poetry"}   -> page.get_by_role(role, name=name, exact=True)
#   {"text": "Poetry"}                   -> page.get_by_text(text, exact=True)
#   {"css": "div.side_categories a:nth-of-type(3)"} -> page.locator(css)
#   {"placeholder": "Search"}            -> page.get_by_placeholder(placeholder)


@dataclass
class Observation:
    url: str
    title: str
    elements: list[El]
    text: str                # readable page text (whitespace-collapsed)
    errors: list = field(default_factory=list)   # visible form validation / alert messages

    def render(self, max_elements: int = 120, max_text: int = 4000) -> str:
        """Compact text form given to the LLM."""
        lines = [f"URL: {self.url}", f"TITLE: {self.title}", "INTERACTIVE ELEMENTS:"]
        for e in self.elements[:max_elements]:
            extra = (f" value='{e.value}'" if e.value else "") + (f" ({e.info})" if e.info else "")
            lines.append(f"[{e.idx}] {e.role} '{e.name}'{extra}")
        if len(self.elements) > max_elements:
            lines.append(f"... {len(self.elements) - max_elements} more elements (scroll or use extract)")
        if self.errors:
            lines.append("FORM ERRORS / ALERTS: " + " | ".join(self.errors))
        t = self.text[:max_text]
        lines.append("PAGE TEXT:\n" + t + ("\n...[truncated]" if len(self.text) > max_text else ""))
        return "\n".join(lines)


@dataclass
class ActionResult:
    ok: bool
    message: str             # short human/LLM readable outcome
    data: Any = None         # e.g. extracted rows


@dataclass
class TraceStep:
    """One executed browser action (the run's audit trail)."""
    action: str              # goto | click | type | select | press | scroll | back | wait | extract
    args: dict               # args as the LLM gave them (idx, text, url, ...)
    locator: Optional[dict]  # resolved robust locator of the target element (None for goto/scroll/...)
    url: str                 # page url BEFORE the action
    ok: bool
    result: str


class BrowserAPI(Protocol):
    """One browser tab. Browser (the process) exposes the same API for its default tab plus new_tab()."""
    trace: list[TraceStep]

    async def observe(self) -> Observation: ...
    # action in: goto(url) click(idx) type(idx,text,submit=False) select(idx,value) check(idx,on=True)
    #            fill_form(fields={label_or_idx: value}) press(key) scroll(direction="down"|"up") back() wait(ms)
    #            extract(css, fields) read(offset=0) tables() links(filter="") find(text) structured()
    async def act(self, action: str, **args: Any) -> ActionResult: ...
    async def new_tab(self) -> "BrowserAPI": ...
    async def close(self) -> None: ...


# --------------------------------------------------------------------- llm ---

@dataclass
class ToolCall:
    id: str
    name: str
    args: dict


@dataclass
class LLMReply:
    thread_id: str
    status: str              # "COMPLETED" | "REQUIRES_ACTION" | "FAILED"
    text: str                # assistant text (may be "")
    tool_calls: list[ToolCall] = field(default_factory=list)
    tokens: int = 0          # total tokens for this round trip
    cost_usd: float = 0.0


class LLM(Protocol):
    """role selects the Backboard assistant: planner | worker | searcher | analyst | verifier."""
    async def chat(self, *, role: str, message: str, system: str, tools: list[dict],
                   thread_id: Optional[str] = None, json_output: bool = False) -> LLMReply: ...
    async def submit(self, *, role: str, thread_id: str, outputs: list[dict]) -> LLMReply: ...
    # outputs: [{"tool_call_id": str, "output": str}]
    async def web_search(self, query: str) -> LLMReply: ...   # one-shot answer grounded by web search


# ------------------------------------------------------------------ agent ---

@dataclass
class RunResult:
    """Returned by webforge.agent.planner.run_task()."""
    task: str
    answer: str                      # final answer text from the planner
    ok: bool                         # planner called final_answer (not budget exhausted / crashed)
    steps: int = 0                   # total browser actions executed by workers
    llm_calls: int = 0
    tokens: int = 0
    cost_usd: float = 0.0
    seconds: float = 0.0
    outputs: list = field(default_factory=list)          # deliverable files (workspace paths under outputs/)
    verification: dict = field(default_factory=dict)     # verifier verdict {complete, missing, unsupported_claims, verdict}
    notes: dict = field(default_factory=dict)            # workspace files {path: content}


# Event callback used for the live UIs. Signature: on_event(kind: str, data: dict) -> None
# Worker events carry "agent" (w1, w2, ...). Kinds:
#   task_start {task}  resume {files, todos}  todos {todos}  search {query}  read_urls {urls}
#   parallel {n, agents}  delegate {subtask, start_url}  observe {url, n_elements}  action {action, args, ok, message}
#   screenshot {jpeg_b64}  ask {ask_id?, question, options}  answer {answer}  analyze {files, instruction}
#   output {name, chars}  verify {message}  verdict {complete, missing, ...}  budget {message}  nudge {message}
#   final {answer, ok, outputs, verification}  warn {message}
