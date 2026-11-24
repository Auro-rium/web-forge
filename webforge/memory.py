"""Memory layer: BackboardMemory (semantic + local mirror), LocalMemory (JSONL only)."""
from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any, Optional

from .config import Config
from .llm import _load_state, _retry, _save_state

_WORD = re.compile(r"[a-z0-9]+")


def _tokens(s: str) -> set[str]:
    return set(_WORD.findall((s or "").lower()))


class LocalMemory:
    """JSONL-backed memory with keyword-overlap search."""

    def __init__(self, cfg: Optional[Config] = None, path: Optional[Path] = None):
        self.cfg = cfg or Config()
        self.path = Path(path or self.cfg.memory_mirror)

    def _read(self) -> list[dict]:
        out = []
        try:
            for line in self.path.read_text().splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                    if isinstance(d, dict) and d.get("content"):
                        d.setdefault("metadata", {})
                        out.append(d)
                except Exception:
                    continue
        except FileNotFoundError:
            pass
        return out

    async def add(self, content: str, metadata: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a") as f:
            f.write(json.dumps({"content": content, "metadata": dict(metadata or {})}) + "\n")

    def _rank(self, query: str, limit: int) -> list[dict]:
        q = _tokens(query)
        scored = []
        for e in self._read():
            doc = _tokens(e["content"]) | _tokens(" ".join(str(v) for v in (e.get("metadata") or {}).values()))
            if not q or not doc:
                continue
            ov = len(q & doc)
            if ov:
                scored.append({"content": e["content"], "metadata": e["metadata"], "score": ov / len(q)})
        scored.sort(key=lambda x: -x["score"])
        return scored[:limit]

    async def search(self, query: str, limit: int = 5) -> list[dict]:
        return self._rank(query, limit)

    async def list(self) -> list[dict]:
        return [{"content": e["content"], "metadata": e["metadata"], "score": 0.0} for e in self._read()]

    async def reset(self) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass

    async def lessons_for(self, domain_or_task: str, limit: int = 5) -> str:
        return _format(await self.search(domain_or_task, limit))

    async def aclose(self) -> None:
        return None


def _format(hits: list[dict]) -> str:
    lines = []
    for h in hits:
        kind = (h.get("metadata") or {}).get("kind")
        lines.append(f"- [{kind}] {h['content']}" if kind else f"- {h['content']}")
    return "\n".join(lines)


class BackboardMemory(LocalMemory):
    """Semantic memory on assistant 'webforge-knowledge'; JSONL mirror is the fallback."""

    def __init__(self, cfg: Optional[Config] = None):
        super().__init__(cfg)
        self._client = None
        self._aid: Optional[str] = None

    @property
    def client(self):
        if self._client is None:
            from backboard import BackboardClient
            self._client = BackboardClient(api_key=self.cfg.api_key or "proxy-injected",
                                           base_url=self.cfg.base_url, timeout=120)
        return self._client

    async def _assistant_id(self) -> str:
        if self._aid:
            return self._aid
        state = _load_state(self.cfg.state_file)
        aid = state.get("knowledge")
        if not aid:
            name = "webforge-knowledge"
            try:
                for a in await _retry(self.client.list_assistants, name=name, limit=1) or []:
                    if getattr(a, "name", name) == name:
                        aid = str(a.assistant_id)
                        break
            except Exception:
                aid = None
            if not aid:
                a = await _retry(self.client.create_assistant, name=name,
                                 system_prompt="WebForge knowledge base: lessons, skills and failures.")
                aid = str(a.assistant_id)
            state["knowledge"] = aid
            _save_state(self.cfg.state_file, state)
        self._aid = aid
        return aid

    async def add(self, content: str, metadata: dict) -> None:
        await super().add(content, metadata)   # mirror first: source of truth
        try:
            aid = await self._assistant_id()
            await _retry(self.client.add_memory, aid, content, metadata=dict(metadata or {}))
        except Exception:
            pass

    async def search(self, query: str, limit: int = 5) -> list[dict]:
        try:
            aid = await self._assistant_id()
            r = await _retry(self.client.search_memories, aid, query, limit=limit)
            items = r.get("memories", []) if isinstance(r, dict) else getattr(r, "memories", [])
            out = [x for x in (_norm(i) for i in items or []) if x]
            if out:
                meta = {e["content"]: e["metadata"] for e in self._read()}
                for o in out:   # QUIRK: search results carry no metadata; restore from mirror
                    if not o["metadata"]:
                        o["metadata"] = meta.get(o["content"], {})
                out.sort(key=lambda x: -x["score"])
                return out[:limit]
        except Exception:
            pass
        return self._rank(query, limit)

    async def list(self) -> list[dict]:
        merged = await super().list()
        seen = {e["content"] for e in merged}
        try:
            aid = await self._assistant_id()
            r = await _retry(self.client.get_memories, aid)
            for i in getattr(r, "memories", None) or []:
                n = _norm(i)
                if n and n["content"] not in seen:
                    seen.add(n["content"])
                    merged.append(n)
        except Exception:
            pass
        return merged

    async def reset(self) -> None:
        await super().reset()
        try:
            aid = await self._assistant_id()
            await _retry(self.client.reset_memories, aid)
        except Exception:
            pass

    async def aclose(self) -> None:
        c, self._client = self._client, None
        if c is not None:
            for attr in ("aclose", "close"):
                fn = getattr(c, attr, None)
                if fn:
                    try:
                        r = fn()
                        if asyncio.iscoroutine(r):
                            await r
                    except Exception:
                        pass
                    break


def _norm(i: Any) -> Optional[dict]:
    if isinstance(i, dict):
        content, meta, score = i.get("content"), i.get("metadata"), i.get("score")
    else:
        content, meta, score = (getattr(i, "content", None), getattr(i, "metadata", None),
                                getattr(i, "score", None))
    if not content:
        return None
    return {"content": str(content), "metadata": meta if isinstance(meta, dict) else {},
            "score": float(score) if isinstance(score, (int, float)) else 0.0}


def make_memory(cfg: Optional[Config] = None, local_only: bool = False):
    cfg = cfg or Config()
    return LocalMemory(cfg) if local_only else BackboardMemory(cfg)
