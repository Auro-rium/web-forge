"""LLM-free replay of a compiled Skill.

Browser convention (BrowserAPI.act_locator):
  - step:    act_locator(step.action, locator, value)   (templates already filled)
  - extract: act_locator(action="extract", locator=None, value=None, extract=E) with
        E = {"type": "text"}                                   -> readable page text
        E = {"type": "css", "selector": S, "css": S, "fields": F} -> list of row dicts
    Both "selector" and "css" keys are passed because the browser agent reads `css`
    and `fields`, while the Skill contract names the key `selector`.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Optional

from webforge.contracts import Skill, fill_template


@dataclass
class SkillResult:
    ok: bool
    output: Any
    message: str
    failed_step: Optional[int] = None


def _jsonable(x: Any) -> Any:
    try:
        return json.loads(json.dumps(x, default=str))
    except Exception:
        return str(x)


def _extract_spec(extract: dict, params: dict) -> dict:
    e = fill_template(extract or {"type": "text"}, params)
    if e.get("type") == "css" or "selector" in e or "css" in e:
        sel = e.get("selector") or e.get("css")
        return {"type": "css", "selector": sel, "css": sel, "fields": e.get("fields") or {}}
    return {"type": "text"}


async def run_skill(skill: Skill, params: dict, browser) -> SkillResult:
    params = params or {}
    missing = [p for p in skill.params if p not in params or params[p] is None]
    if missing:
        return SkillResult(False, None, f"missing params: {', '.join(missing)}", None)

    for i, step in enumerate(skill.steps):
        locator = fill_template(step.locator, params) if step.locator else None
        value = fill_template(step.value, params) if step.value is not None else None
        try:
            r = await browser.act_locator(step.action, locator, value)
        except Exception as e:  # browser blew up
            return SkillResult(False, None, f"step {i} ({step.action}) raised: {e}", i)
        if not r.ok:
            return SkillResult(False, None, f"step {i} ({step.action} {locator or value}) failed: {r.message}", i)

    spec = _extract_spec(skill.extract, params)
    try:
        r = await browser.act_locator("extract", None, None, extract=spec)
    except Exception as e:
        return SkillResult(False, None, f"extract raised: {e}", len(skill.steps))
    if not r.ok:
        return SkillResult(False, None, f"extract failed: {r.message}", len(skill.steps))
    data = r.data if spec["type"] == "css" else (r.data if r.data is not None else r.message)
    return SkillResult(True, _jsonable(data), "ok", None)
