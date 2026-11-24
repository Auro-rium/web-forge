"""Persistent skill library: one JSON file per skill (`<name>.json`)."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from webforge.contracts import Skill

DEMOTE_MIN_USES = 3
DEMOTE_RATE = 0.5


def is_demoted(skill: Skill) -> bool:
    return skill.uses >= DEMOTE_MIN_USES and (skill.successes / skill.uses) < DEMOTE_RATE


def _hostname(url: str) -> str:
    try:
        return (urlparse(url if "//" in url else "//" + url).hostname or "").lower()
    except Exception:
        return ""


class SkillLibrary:
    def __init__(self, dir: Path):
        self.dir = Path(dir)
        self.dir.mkdir(parents=True, exist_ok=True)

    def _path(self, name: str) -> Path:
        safe = "".join(c for c in name if c.isalnum() or c in "_-.") or "skill"
        return self.dir / f"{safe}.json"

    def save(self, skill: Skill) -> None:
        p = self._path(skill.name)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(skill.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(p)

    def get(self, name: str) -> Optional[Skill]:
        p = self._path(name)
        if not p.exists():
            return None
        try:
            return Skill.from_dict(json.loads(p.read_text(encoding="utf-8")))
        except Exception:
            return None

    def list(self, domain: Optional[str] = None, include_demoted: bool = False) -> list[Skill]:
        out: list[Skill] = []
        for p in sorted(self.dir.glob("*.json")):
            try:
                s = Skill.from_dict(json.loads(p.read_text(encoding="utf-8")))
            except Exception:
                continue
            if domain and not self._domain_match(s.domain, domain):
                continue
            if not include_demoted and is_demoted(s):
                continue
            out.append(s)
        return out

    @staticmethod
    def _domain_match(skill_domain: str, wanted: str) -> bool:
        a = skill_domain.lower().removeprefix("www.")
        b = (_hostname(wanted) or wanted).lower().removeprefix("www.")
        return a == b or b.endswith("." + a) or a.endswith("." + b)

    def delete(self, name: str) -> bool:
        p = self._path(name)
        if p.exists():
            p.unlink()
            return True
        return False

    def record_use(self, name: str, success: bool) -> Optional[Skill]:
        s = self.get(name)
        if s is None:
            return None
        s.uses += 1
        if success:
            s.successes += 1
        self.save(s)
        return s

    def describe(self, domain: Optional[str] = None) -> str:
        lines = []
        for s in self.list(domain):
            lines.append(
                f"{s.name}({', '.join(s.params)}) @{s.domain} - {s.description} [ok {s.successes}/{s.uses}]"
            )
        return "\n".join(lines)

