"""Shared interfaces for WebForge. Every module codes against these types.

DO NOT change signatures here without updating all modules.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict
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


# LOCATOR dict (used by trace + skill replay). Any subset of:
#   {"role": "link", "name": "Poetry"}   -> page.get_by_role(role, name=name, exact=True)
#   {"text": "Poetry"}                   -> page.get_by_text(text, exact=True)
#   {"css": "div.side_categories a:nth-of-type(3)"} -> page.locator(css)
#   {"placeholder": "Search"}            -> page.get_by_placeholder(placeholder)
# Resolution order on replay: role+name, placeholder, text, css. First that matches >=1 visible element wins (.first).
# Any string value may contain {param} templates (filled by fill_template).


@dataclass
class Observation:
    url: str
    title: str
    elements: list[El]
    text: str                # readable page text (whitespace-collapsed)

    def render(self, max_elements: int = 120, max_text: int = 4000) -> str:
        """Compact text form given to the LLM."""
        lines = [f"URL: {self.url}", f"TITLE: {self.title}", "INTERACTIVE ELEMENTS:"]
        for e in self.elements[:max_elements]:
            extra = f" value='{e.value}'" if e.value else ""
            lines.append(f"[{e.idx}] {e.role} '{e.name}'{extra}")
        if len(self.elements) > max_elements:
            lines.append(f"... {len(self.elements) - max_elements} more elements (scroll or use extract)")
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
    """One executed browser action, recorded for skill building."""
    action: str              # goto | click | type | select | press | scroll | back | wait | extract
    args: dict               # args as the LLM gave them (idx, text, url, ...)
    locator: Optional[dict]  # resolved robust locator of the target element (None for goto/scroll/...)
    url: str                 # page url BEFORE the action
    ok: bool
    result: str

    def to_dict(self) -> dict:
        return asdict(self)


class BrowserAPI(Protocol):
    trace: list[TraceStep]

    async def start(self, headless: bool = True) -> None: ...
    async def close(self) -> None: ...
    async def observe(self) -> Observation: ...
    # action in: goto(url) click(idx) type(idx,text,submit=False) select(idx,value) press(key)
    #            scroll(direction="down"|"up") back() wait(ms) extract(css=None, fields=None)
    #            read(offset=0) tables() links(filter="") find(text) structured()
    async def act(self, action: str, **args: Any) -> ActionResult: ...
    # replay helpers (no idx; used by skills): action in click|type|select|press|goto|scroll|wait|extract
    async def act_locator(self, action: str, locator: Optional[dict], value: Optional[str] = None,
                          extract: Optional[dict] = None) -> ActionResult: ...
    # generic extraction (also reachable as act("read"|"tables"|"links"|"find"|"structured", ...))
    async def new_tab(self) -> "BrowserAPI": ...   # independent tab (own page + trace) for parallel workers


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
    """role selects the Backboard assistant: planner | worker | builder | reflector."""
    async def chat(self, *, role: str, message: str, system: str, tools: list[dict],
                   thread_id: Optional[str] = None, json_output: bool = False) -> LLMReply: ...
    async def submit(self, *, role: str, thread_id: str, outputs: list[dict]) -> LLMReply: ...
    # outputs: [{"tool_call_id": str, "output": str}]
    async def web_search(self, query: str) -> LLMReply: ...   # one-shot answer grounded by web search


class Memory(Protocol):
    async def add(self, content: str, metadata: dict) -> None: ...
    async def search(self, query: str, limit: int = 5) -> list[dict]: ...   # [{"content","metadata","score"}]
    async def list(self) -> list[dict]: ...
    async def reset(self) -> None: ...


# ------------------------------------------------------------------ skills ---

@dataclass
class SkillStep:
    action: str                      # goto | click | type | select | press | scroll | wait
    locator: Optional[dict] = None   # LOCATOR dict, values may contain {param}
    value: Optional[str] = None      # url for goto, text for type, option for select, key for press; may contain {param}


@dataclass
class Skill:
    name: str                        # snake_case, e.g. list_books_in_category
    domain: str                      # e.g. books.toscrape.com
    description: str                 # what it does + what it returns
    params: list[str]                # e.g. ["category"]
    steps: list[SkillStep]
    # how to produce the output after the steps:
    #   {"type": "text"}  -> readable page text (truncated)
    #   {"type": "css", "selector": "article.product_pod",
    #    "fields": {"title": "h3 a@title", "price": ".price_color"}}  ("sel@attr" reads attribute, "sel" reads text,
    #    "@href" reads attr of the row element itself)
    extract: dict = field(default_factory=lambda: {"type": "text"})
    uses: int = 0
    successes: int = 0
    version: int = 1

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "Skill":
        d = dict(d)
        d["steps"] = [s if isinstance(s, SkillStep) else SkillStep(**s) for s in d.get("steps", [])]
        return Skill(**d)


_TPL = re.compile(r"\{(\w+)\}")


def fill_template(value: Any, params: dict) -> Any:
    """Recursively replace {param} in strings / dicts / lists."""
    if isinstance(value, str):
        return _TPL.sub(lambda m: str(params.get(m.group(1), m.group(0))), value)
    if isinstance(value, dict):
        return {k: fill_template(v, params) for k, v in value.items()}
    if isinstance(value, list):
        return [fill_template(v, params) for v in value]
    return value


# ------------------------------------------------------------------ agent ---

@dataclass
class RunResult:
    """Returned by webforge.agent.planner.run_task()."""
    task: str
    answer: str                      # final answer text from the planner
    ok: bool                         # planner called final_answer (not budget exhausted / crashed)
    steps: int = 0                   # total browser actions executed (worker act() calls + skill steps)
    llm_calls: int = 0
    tokens: int = 0
    cost_usd: float = 0.0
    seconds: float = 0.0
    skills_used: list = field(default_factory=list)      # names of skills executed via run_skill
    skills_created: list = field(default_factory=list)   # names of skills built after this run
    notes: dict = field(default_factory=dict)            # workspace files {filename: content}


# Event callback used for the live UI. Signature: on_event(kind: str, data: dict) -> None
# kinds: "task_start", "todos" {todos}, "delegate" {subtask}, "skill_run" {name, params}, "skill_result" {name, ok, message},
#        "observe" {url, n_elements}, "action" {action, args, ok, message}, "memory_hits" {text}, "search" {query},
#        "skill_built" {name}, "lesson" {text}, "final" {answer}, "warn" {message}
