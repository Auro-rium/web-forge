"""Post-run reflection: distill general lessons into long-term memory."""
from __future__ import annotations

import json
import re
from typing import Callable, Optional

from webforge.agent.prompts import REFLECTOR_SYSTEM


def _parse(text: str) -> list[dict]:
    if not text:
        return []
    t = re.sub(r"```(?:json)?", "", text)
    cands = [t.strip()]
    a, b = t.find("{"), t.rfind("}")
    if 0 <= a < b:
        cands.append(t[a:b + 1])
    for c in cands:
        try:
            v = json.loads(c)
        except Exception:
            continue
        if isinstance(v, dict):
            v = v.get("lessons", [])
        if isinstance(v, list):
            return [x for x in v if isinstance(x, (dict, str))]
    return []


# Lessons about connectivity are environment-specific (proxy, firewall, outage), not facts about the site.
_TRANSIENT = re.compile(r"unreachable|ERR_[A-Z_]+|net::|tunnel|timed? ?out|connection (?:failed|refused|reset)|"
                        r"could not (?:be )?(?:reach|load|connect)|failed to (?:load|connect|navigate)|"
                        r"navigation (?:to .{0,60})?failed|blocked in this|network error", re.I)


def is_transient(text: str) -> bool:
    return bool(_TRANSIENT.search(text or ""))


async def reflect(task: str, result_summary: str, ok: bool, trace_digest: str, llm, memory,
                  on_event: Optional[Callable] = None) -> list[str]:
    """Store 1-3 lessons in memory. Never raises. Returns the stored lesson texts."""
    stored: list[str] = []
    try:
        msg = (f"TASK: {task}\nOUTCOME: {'success' if ok else 'FAILED'}\nRESULT: {str(result_summary)[:1500]}\n"
               f"WHAT HAPPENED:\n{str(trace_digest)[:3000]}\n\nReturn the lessons JSON.")
        reply = await llm.chat(role="reflector", message=msg, system=REFLECTOR_SYSTEM, tools=[], json_output=True)
        for item in _parse(getattr(reply, "text", "") or "")[:3]:
            if isinstance(item, str):
                item = {"text": item}
            text = str(item.get("text", "")).strip()
            if not text or is_transient(text):
                continue
            domain = str(item.get("domain", "") or "")
            await memory.add(text, {"domain": domain, "kind": "lesson" if ok else "failure", "task": task})
            stored.append(text)
            if on_event:
                try:
                    on_event("lesson", {"text": text, "domain": domain})
                except Exception:
                    pass
    except Exception as e:  # noqa: BLE001
        if on_event:
            try:
                on_event("warn", {"message": f"reflection failed: {e!r}"})
            except Exception:
                pass
    return stored
