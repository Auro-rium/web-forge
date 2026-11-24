"""Safety guards for the browsing worker: domain allowlist, sensitive inputs, risky clicks."""
from __future__ import annotations

import re
from typing import Any, Optional
from urllib.parse import urlparse

ALWAYS_ALLOWED = {"localhost", "127.0.0.1"}

_SENSITIVE_INPUT = re.compile(
    r"pass(word|wd|code)?\b|credit.?card|card.?number|\bcvv\b|\bcvc\b|security.?code|\bssn\b|social.?security",
    re.I)
_RISKY_CLICK = re.compile(
    r"check\s?out|pay\s?now|place\s+(your\s+)?order|buy\s+now|delete\s+(my\s+)?account|sign\s?in|log\s?in|"
    r"complete\s+purchase|confirm\s+(order|purchase)", re.I)


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


def _find(elements: Optional[list], idx: Any):
    for e in elements or []:
        if getattr(e, "idx", None) == idx or str(getattr(e, "idx", "")) == str(idx):
            return e
    return None


def check_action(action: str, args: dict, elements: Optional[list], allowed_domains: Optional[list[str]]) -> tuple[bool, str]:
    """Return (allowed, reason). reason is "" when allowed."""
    args = args or {}
    if action == "goto":
        url = str(args.get("url", ""))
        if not domain_allowed(url, allowed_domains):
            return False, f"navigation to '{url}' blocked: domain not in allowed list {allowed_domains}"
        return True, ""
    if action in ("click", "type", "select"):
        el = _find(elements, args.get("idx"))
        if el is None:
            return True, ""   # browser will report the missing element
        label = f"{getattr(el, 'name', '')} {getattr(el, 'role', '')}"
        if action in ("type", "select"):
            hay = f"{label} {getattr(el, 'locator', '')}"
            if "password" in label.lower() or _SENSITIVE_INPUT.search(hay):
                return False, f"typing into sensitive field '{el.name}' is blocked"
        if action == "click" and _RISKY_CLICK.search(getattr(el, "name", "") or ""):
            return False, f"clicking '{el.name}' is blocked (checkout/payment/login/account deletion)"
    return True, ""


def redact(text: Any, limit: int = 0) -> str:
    """Mask obvious secrets (card numbers, bearer tokens, api keys, passwords) in text."""
    s = str(text)
    s = re.sub(r"\b(?:\d[ -]?){13,19}\b", "[REDACTED-NUMBER]", s)
    s = re.sub(r"(?i)\b(bearer|token|api[_-]?key|password|secret)\b(\s*[:=]\s*)\S+", r"\1\2[REDACTED]", s)
    s = re.sub(r"\b(sk|pk|rk)-[A-Za-z0-9_-]{12,}\b", "[REDACTED-KEY]", s)
    return s[:limit] if limit else s
