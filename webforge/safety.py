"""Safety policy for browser actions: allow, ask the user for approval, or block.

- block: payment data and paying. Never allowed, whatever the approval mode.
- ask:   consequential actions (submitting, sending, logging in, deleting, typing a password). They run
         only after the human approves (WEBFORGE_APPROVAL=ask), always (auto) or never (deny).
- allow: everything else (navigation, reading, typing into ordinary fields, selecting options).

Secrets: the model writes {{secret:NAME}}; the browser substitutes the value at typing time and every
string shown to the model or the UI is passed through redact().
"""
from __future__ import annotations

import os
import re
from typing import Any, Optional
from urllib.parse import urlparse

ALWAYS_ALLOWED = {"localhost", "127.0.0.1"}
ALLOW, ASK, BLOCK = "allow", "ask", "block"

_PAYMENT_FIELD = re.compile(
    r"credit.?card|card.?(number|no\b|holder)|\bcc.?(num|number)\b|\bcvv\b|\bcvc\b|\bcsc\b|security.?code|"
    r"expir(y|ation)|\biban\b|routing.?number|account.?number|\bssn\b|social.?security", re.I)
_PAY_CLICK = re.compile(
    r"\bpay\b|pay\s?now|buy\s+now|complete\s+(purchase|payment)|place\s+(your\s+)?order|confirm\s+(order|purchase|payment)|"
    r"checkout|check\s?out|subscribe\s+now|start\s+(free\s+)?trial", re.I)
_CONSEQUENTIAL_CLICK = re.compile(
    r"submit|send|post\b|publish|sign\s?up|register|create\s+account|sign\s?in|log\s?in|confirm|delete|remove|"
    r"unsubscribe|book\b|reserve|apply|save\s+changes|transfer|approve|accept", re.I)
_PASSWORD = re.compile(r"pass(word|wd|code)|\bpin\b|one.?time|otp", re.I)
SECRET_RE = re.compile(r"\{\{\s*secret:([A-Za-z0-9_\-]+)\s*\}\}")


# ------------------------------------------------------------------ domains ---
def domain_allowed(url: str, allowed: Optional[list[str]]) -> bool:
    """True if url's host is on the allowlist (subdomains included). Empty allowlist = allow all."""
    if not allowed:
        return True
    u = (url or "").strip()
    if "://" not in u:
        u = "https://" + u
    host = (urlparse(u).hostname or "").lower()
    if host in ALWAYS_ALLOWED:
        return True
    return any(host == d.lower() or host.endswith("." + d.lower()) for d in allowed)


# ------------------------------------------------------------------ secrets ---
class Secrets:
    """Named secret values. Loaded from WEBFORGE_SECRET_<NAME> env vars, plus values set at runtime (UI)."""

    def __init__(self, values: Optional[dict[str, str]] = None, env: bool = True):
        self._v: dict[str, str] = {}
        if env:
            for k, v in os.environ.items():
                if k.startswith("WEBFORGE_SECRET_") and v:
                    self._v[k[len("WEBFORGE_SECRET_"):].lower()] = v
        for k, v in (values or {}).items():
            self.set(k, v)

    def set(self, name: str, value: str) -> None:
        name = re.sub(r"[^a-z0-9_\-]", "", (name or "").strip().lower())
        if name and value:
            self._v[name] = value

    def names(self) -> list[str]:
        return sorted(self._v)

    def resolve(self, text: str) -> tuple[str, list[str]]:
        """Replace {{secret:NAME}} with values. Returns (text, missing_names)."""
        missing: list[str] = []

        def sub(m: re.Match) -> str:
            v = self._v.get(m.group(1).lower())
            if v is None:
                missing.append(m.group(1))
                return m.group(0)
            return v
        return SECRET_RE.sub(sub, text or ""), missing

    def redact(self, text: Any) -> Any:
        if not isinstance(text, str) or not self._v:
            return text
        for name, v in self._v.items():
            if len(v) >= 3:
                text = text.replace(v, f"{{{{secret:{name}}}}}")
        return text


# ------------------------------------------------------------------ policy ---
def _find(elements: Optional[list], idx: Any):
    for e in elements or []:
        if getattr(e, "idx", None) == idx or str(getattr(e, "idx", "")) == str(idx):
            return e
    return None


def _label(el) -> str:
    return f"{getattr(el, 'name', '')} {getattr(el, 'role', '')} {getattr(el, 'locator', '')}"


def _is_password(el) -> bool:
    return "password" in str(getattr(el, "locator", "")).lower() or bool(_PASSWORD.search(getattr(el, "name", "") or ""))


def _is_search(el) -> bool:
    if el is None:
        return True
    hay = f"{getattr(el, 'name', '')} {getattr(el, 'role', '')} {getattr(el, 'locator', '')}".lower()
    return "search" in hay or "query" in hay or hay.strip().startswith("q ")


def _field_check(el, value: str) -> tuple[str, str]:
    if el is None:
        return ALLOW, ""
    if _PAYMENT_FIELD.search(_label(el)):
        return BLOCK, f"entering payment/identity data into '{el.name}' is never allowed"
    if _is_password(el) or SECRET_RE.search(value or ""):
        return ASK, f"type a password/secret into '{el.name}'"
    return ALLOW, ""


def check_action(action: str, args: dict, elements: Optional[list],
                 allowed_domains: Optional[list[str]]) -> tuple[str, str]:
    """Return (verdict, reason): verdict is allow | ask | block. reason describes the action for ask/block."""
    args = args or {}
    if action == "goto":
        url = str(args.get("url", ""))
        if not domain_allowed(url, allowed_domains):
            return BLOCK, f"navigation to '{url}' blocked: domain not in allowed list {allowed_domains}"
        return ALLOW, ""
    if action in ("type", "select"):
        el = _find(elements, args.get("idx"))
        verdict, reason = _field_check(el, str(args.get("text", args.get("value", ""))))
        if verdict != ALLOW:
            return verdict, reason
        if action == "type" and args.get("submit") and not _is_search(el):
            return ASK, f"submit the form by pressing Enter in '{getattr(el, 'name', 'field')}'"
        return ALLOW, ""
    if action == "fill_form":
        worst, reasons = ALLOW, []
        for key, value in (args.get("fields") or {}).items():
            el = _find(elements, key) or next((e for e in elements or [] if (getattr(e, "name", "") or "").strip().lower()
                                               == str(key).strip().lower()), None)
            label = el if el is not None else type("E", (), {"name": str(key), "role": "", "locator": ""})()
            verdict, reason = _field_check(label, str(value))
            if verdict == BLOCK:
                return BLOCK, reason
            if verdict == ASK:
                worst = ASK
                reasons.append(reason)
        return worst, "; ".join(reasons)
    if action in ("click", "check"):
        el = _find(elements, args.get("idx"))
        if el is None:
            return ALLOW, ""   # the browser reports the missing element
        name = getattr(el, "name", "") or ""
        if action == "click" and _PAY_CLICK.search(name):
            return BLOCK, f"clicking '{name}' would pay or purchase; payments are never allowed"
        submit_like = "submit" in str(getattr(el, "locator", "")).lower()
        if action == "click" and (_CONSEQUENTIAL_CLICK.search(name) or submit_like):
            return ASK, f"click '{name}'"
    return ALLOW, ""


def approval_question(reason: str, url: str, fields: Optional[dict] = None) -> str:
    q = f"Approve: {reason} on {url}?"
    if fields:
        shown = {k: ("•••" if SECRET_RE.search(str(v)) else str(v)[:60]) for k, v in fields.items()}
        q += f" Form values: {shown}"
    return q
