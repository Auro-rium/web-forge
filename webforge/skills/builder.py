"""Compile a successful browser trace into a reusable parameterized Skill."""
from __future__ import annotations

import json
import logging
import re
from typing import Optional
from urllib.parse import urlparse

from webforge.contracts import Skill, SkillStep, TraceStep
from webforge.skills.library import SkillLibrary
from webforge.skills.replay import run_skill

log = logging.getLogger("webforge.skills")

USEFUL_ACTIONS = {"goto", "click", "type", "select", "press"}
STEP_ACTIONS = USEFUL_ACTIONS | {"scroll", "wait"}
_TPL = re.compile(r"\{(\w+)\}")

BUILDER_SYSTEM = """You compile a successful web-browsing action trace into a reusable, parameterized skill that can be replayed WITHOUT an LLM.
Return ONLY one JSON object, no prose, no markdown fences:
{"name": "snake_case_name",
 "description": "what it does and what it returns",
 "params": ["category"],
 "example_params": {"category": "Poetry"},
 "steps": [
   {"action": "goto", "locator": null, "value": "https://books.toscrape.com/"},
   {"action": "click", "locator": {"role": "link", "name": "{category}"}, "value": null}
 ],
 "extract": {"type": "css", "selector": "article.product_pod", "fields": {"title": "h3 a@title", "price": ".price_color"}}}
Rules:
- actions: goto | click | type | select | press. goto uses "value" = url; type/select/press use "value".
- Task-specific literals (category name, search term, ...) become {param} templates in BOTH locators and values; list every param in "params" and give a realistic value for each in "example_params" (the values used in this task).
- Locators: dict with role+name (preferred), placeholder, text or css. Keep generic site structure; no one-off ids.
- Keep steps minimal: drop redundant/duplicate steps, start with a direct goto of the start URL.
- extract: if the output is a list of repeated items (cards, rows) use {"type":"css","selector":ROW_CSS,"fields":{"field":"sel@attr" | "sel" | "@attr"}}; otherwise {"type":"text"}.
- name must be snake_case and describe the task generically."""

BUILD_PROMPT = """TASK: {task}

SUCCESSFUL STEPS (action | args | locator | url-before):
{steps}

RESULT SUMMARY: {summary}

Return the skill JSON."""

REPAIR_SYSTEM = BUILDER_SYSTEM + "\nYou are fixing a skill whose replay failed. Return the complete corrected skill JSON (same format)."

REPAIR_PROMPT = """The skill below failed on replay.
SKILL JSON:
{skill}

FAILED STEP INDEX: {failed_step}
ERROR: {error}
EXAMPLE PARAMS: {params}
CURRENT PAGE (observation):
{observation}

Fix the failing step (e.g. a better role+name, text or css locator) and return the full corrected skill JSON."""


def _hostname(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower()
    except Exception:
        return ""


def _parse_json(text: str) -> Optional[dict]:
    if not text:
        return None
    t = re.sub(r"```(?:json)?", "", text)
    try:
        v = json.loads(t.strip())
        return v if isinstance(v, dict) else None
    except Exception:
        pass
    start = t.find("{")
    if start < 0:
        return None
    dec = json.JSONDecoder()
    while start >= 0:
        try:
            v, _ = dec.raw_decode(t[start:])
            if isinstance(v, dict):
                return v
        except Exception:
            pass
        start = t.find("{", start + 1)
    return None


def _snake(name: str) -> str:
    n = re.sub(r"[^0-9a-zA-Z]+", "_", str(name)).strip("_").lower()
    return n or ""


def _prefilter(trace: list[TraceStep]) -> list[TraceStep]:
    return [t for t in trace if t.ok and t.action in USEFUL_ACTIONS]


def _render_steps(steps: list[TraceStep]) -> str:
    lines = []
    for i, t in enumerate(steps):
        lines.append(f"{i}. {t.action} | {json.dumps(t.args, default=str)[:200]} | "
                     f"{json.dumps(t.locator) if t.locator else '-'} | {t.url}")
    return "\n".join(lines)


class SkillBuilder:
    def __init__(self, llm, library: SkillLibrary):
        self.llm = llm
        self.library = library
        self.last_error: str = ""

    # ------------------------------------------------------------ helpers --
    async def _ask(self, system: str, message: str) -> Optional[dict]:
        reply = await self.llm.chat(role="builder", message=message, system=system,
                                    tools=[], json_output=True)
        return _parse_json(getattr(reply, "text", "") or "")

    def _to_skill(self, d: dict, domain: Optional[str], fallback_url: str = "") -> tuple[Skill, dict]:
        name = _snake(d.get("name", ""))
        if not name:
            raise ValueError("missing name")
        raw_steps = d.get("steps")
        if not isinstance(raw_steps, list) or not raw_steps:
            raise ValueError("no steps")
        steps: list[SkillStep] = []
        for rs in raw_steps:
            if not isinstance(rs, dict):
                raise ValueError("bad step")
            action = str(rs.get("action", "")).lower()
            if action not in STEP_ACTIONS:
                raise ValueError(f"bad action {action!r}")
            loc = rs.get("locator")
            if loc is not None and not isinstance(loc, dict):
                raise ValueError("locator must be object or null")
            val = rs.get("value")
            steps.append(SkillStep(action=action, locator=loc or None,
                                   value=None if val is None else str(val)))
        extract = d.get("extract") or {"type": "text"}
        if not isinstance(extract, dict):
            extract = {"type": "text"}
        if extract.get("type") == "css" or "selector" in extract:
            sel = extract.get("selector") or extract.get("css")
            if not sel:
                extract = {"type": "text"}
            else:
                extract = {"type": "css", "selector": sel, "fields": extract.get("fields") or {}}
        else:
            extract = {"type": "text"}
        params = [str(p) for p in (d.get("params") or [])]
        for found in _TPL.findall(json.dumps([[s.locator, s.value] for s in steps]) + json.dumps(extract)):
            if found not in params:
                params.append(found)
        ex = d.get("example_params") or {}
        if not isinstance(ex, dict):
            ex = {}
        dom = ""
        for s in steps:
            if s.action == "goto" and s.value:
                dom = _hostname(s.value)
                break
        dom = dom or (domain or "") or _hostname(fallback_url)
        skill = Skill(name=name, domain=dom, description=str(d.get("description") or name),
                      params=params, steps=steps, extract=extract)
        return skill, ex

    def _clash(self, skill: Skill) -> bool:
        old = self.library.get(skill.name)
        return old is not None and old.domain != skill.domain

    async def _validate(self, skill: Skill, example: dict, browser) -> tuple[bool, int, str]:
        missing = [p for p in skill.params if p not in example]
        if missing:
            return False, -1, f"example_params missing {missing}"
        r = await run_skill(skill, example, browser)
        return r.ok, (r.failed_step if r.failed_step is not None else -1), r.message

    # -------------------------------------------------------------- build --
    async def build(self, task: str, trace: list[TraceStep], result_summary: str, browser,
                    domain: Optional[str] = None) -> Optional[Skill]:
        self.last_error = ""
        try:
            return await self._build(task, trace, result_summary, browser, domain)
        except Exception as e:
            self.last_error = f"builder exception: {e!r}"
            log.warning(self.last_error)
            return None

    async def _build(self, task, trace, result_summary, browser, domain):
        useful = _prefilter(trace)
        if len(useful) < 1:
            self.last_error = "no useful steps in trace"
            return None
        fallback_url = useful[0].url
        msg = BUILD_PROMPT.format(task=task, steps=_render_steps(useful), summary=result_summary)
        d = await self._ask(BUILDER_SYSTEM, msg)
        if d is None:
            self.last_error = "LLM returned unparseable output"
            return None
        skill, example = self._to_skill(d, domain, fallback_url)
        if self._clash(skill):
            self.last_error = f"name {skill.name!r} clashes with a skill from another domain"
            return None
        ok, idx, err = await self._validate(skill, example, browser)
        if not ok:
            first_err = f"{err}"
            obs = await self._observe(browser)
            msg = REPAIR_PROMPT.format(skill=json.dumps(d, indent=1), failed_step=idx, error=err,
                                       params=json.dumps(example), observation=obs)
            d2 = await self._ask(REPAIR_SYSTEM, msg)
            if d2 is None:
                self.last_error = f"replay failed ({first_err}); repair output unparseable"
                return None
            skill, example2 = self._to_skill(d2, domain, fallback_url)
            example = example2 or example
            if self._clash(skill):
                self.last_error = f"name {skill.name!r} clashes with a skill from another domain"
                return None
            ok, idx, err = await self._validate(skill, example, browser)
            if not ok:
                self.last_error = f"replay failed after repair: {err}"
                return None
        existing = self.library.get(skill.name)
        skill.version = (existing.version + 1) if existing else 1
        skill.uses = 0
        skill.successes = 0
        self.library.save(skill)
        return skill

    async def _observe(self, browser) -> str:
        try:
            return (await browser.observe()).render(max_elements=80, max_text=1500)
        except Exception as e:
            return f"(observation unavailable: {e})"

    # ------------------------------------------------------------- repair --
    async def repair(self, skill: Skill, failed_step: int, error: str, observation_text: str,
                     browser, example_params: dict) -> Optional[Skill]:
        self.last_error = ""
        try:
            msg = REPAIR_PROMPT.format(skill=json.dumps(skill.to_dict(), indent=1), failed_step=failed_step,
                                       error=error, params=json.dumps(example_params),
                                       observation=observation_text)
            d = await self._ask(REPAIR_SYSTEM, msg)
            if d is None:
                self.last_error = "repair: LLM returned unparseable output"
                return None
            d["name"] = skill.name  # a repair never renames
            new, ex = self._to_skill(d, skill.domain)
            new.domain = skill.domain or new.domain
            ok, idx, err = await self._validate(new, example_params or ex, browser)
            if not ok:
                self.last_error = f"repair replay failed: {err}"
                return None
            new.version = skill.version + 1
            new.uses, new.successes = skill.uses, skill.successes
            self.library.save(new)
            return new
        except Exception as e:
            self.last_error = f"repair exception: {e!r}"
            log.warning(self.last_error)
            return None
