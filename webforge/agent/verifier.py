"""Verifier sub-agent: checks a candidate final answer against the task before the run may finish."""
from __future__ import annotations

import json

from webforge.agent.prompts import VERIFIER_SYSTEM


def _parse(text: str) -> dict:
    t = (text or "").strip()
    a, b = t.find("{"), t.rfind("}")
    for cand in (t, t[a:b + 1] if 0 <= a < b else ""):
        try:
            v = json.loads(cand)
            if isinstance(v, dict):
                return v
        except Exception:
            continue
    return {}


async def verify(task: str, answer: str, evidence: list[str], files: list[str], llm) -> dict:
    """Return {complete, missing, unsupported_claims, verdict}. Fails open (complete=True) on errors,
    so a broken verifier can never trap a run."""
    msg = (f"TASK:\n{task}\n\nCANDIDATE FINAL ANSWER:\n{answer[:6000]}\n\n"
           f"EVIDENCE (tool calls and short results, latest last):\n" + "\n".join(evidence[-60:])[:6000] +
           f"\n\nWORKSPACE FILES: {', '.join(files) or '(none)'}\n\nReturn the verdict JSON.")
    try:
        reply = await llm.chat(role="verifier", message=msg, system=VERIFIER_SYSTEM, tools=[], json_output=True)
        v = _parse(reply.text)
    except Exception as e:  # noqa: BLE001
        return {"complete": True, "missing": [], "unsupported_claims": [], "verdict": f"verifier unavailable: {e!r}"}
    if not v:
        return {"complete": True, "missing": [], "unsupported_claims": [], "verdict": "verifier gave no verdict"}
    missing = [str(x) for x in (v.get("missing") or [])][:8]
    unsupported = [str(x) for x in (v.get("unsupported_claims") or [])][:8]
    complete = bool(v.get("complete", not missing))
    return {"complete": complete and not missing, "missing": missing, "unsupported_claims": unsupported,
            "verdict": str(v.get("verdict", ""))[:400]}
