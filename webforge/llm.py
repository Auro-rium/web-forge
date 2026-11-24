"""LLM layer: BackboardLLM (real), MockLLM / ScriptedMock (offline), make_llm factory."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Callable, Optional, Union

from .config import Config
from .contracts import LLMReply, ToolCall

ROLES = ("planner", "worker", "builder", "reflector")


# ------------------------------------------------------------------ helpers ---

def _retryable(exc: Exception) -> bool:
    """Retry network/5xx/429; never retry other 4xx (auth, validation, 404)."""
    status = getattr(exc, "status_code", None)
    if status is not None:
        return status >= 500 or status == 429
    return True


async def _retry(fn: Callable, *args, tries: int = 3, **kw):
    delay = 1.0
    for attempt in range(tries):
        try:
            return await fn(*args, **kw)
        except Exception as e:  # noqa: BLE001
            if attempt == tries - 1 or not _retryable(e):
                raise
            await asyncio.sleep(delay)
            delay *= 2


def _load_state(path: Path) -> dict:
    try:
        return json.loads(Path(path).read_text())
    except Exception:
        return {}


def _save_state(path: Path, state: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2))


def _tool_calls(resp: Any) -> list[ToolCall]:
    out: list[ToolCall] = []
    for tc in getattr(resp, "tool_calls", None) or []:
        fn = getattr(tc, "function", None)
        name = getattr(fn, "name", "") or ""
        args: Any = None
        try:
            args = fn.parsed_arguments
        except Exception:
            args = None
        if not isinstance(args, dict):
            try:
                args = json.loads(getattr(fn, "arguments", "") or "{}")
            except Exception:
                args = {}
        if not isinstance(args, dict):
            args = {}
        out.append(ToolCall(id=str(getattr(tc, "id", "") or ""), name=name, args=args))
    return out


def _usage_from(resp: Any) -> tuple[int, float]:
    try:
        msgs = getattr(resp, "messages", None) or []
        last = msgs[-1] if msgs else {}
        if not isinstance(last, dict):
            last = last.model_dump() if hasattr(last, "model_dump") else dict(getattr(last, "__dict__", {}))
        tokens = last.get("total_tokens")
        if tokens is None:
            tokens = (last.get("input_tokens") or 0) + (last.get("output_tokens") or 0)
        return int(tokens or 0), float(last.get("cost_usd") or 0.0)
    except Exception:
        return 0, 0.0



# ------------------------------------------------------------ spend cap ---

class BudgetExceeded(RuntimeError):
    """Raised before a Backboard call when cumulative spend reached the hard cap."""


def _ledger_path(cfg: Config) -> Path:
    return cfg.ledger_file


def ledger_total(cfg: Config) -> float:
    try:
        return float(json.loads(_ledger_path(cfg).read_text()).get("total_usd", 0.0))
    except Exception:
        return 0.0


def _ledger_add(cfg: Config, cost: float) -> float:
    """Atomically add cost to the global ledger (flock: parallel processes must not lose updates)."""
    import fcntl
    path = _ledger_path(cfg)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a+") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            fh.seek(0)
            try:
                total = float(json.loads(fh.read() or "{}").get("total_usd", 0.0))
            except Exception:
                total = 0.0
            total += cost
            fh.seek(0)
            fh.truncate()
            fh.write(json.dumps({"total_usd": round(total, 6), "cap_usd": cfg.budget_usd}))
        return total
    except Exception:
        return ledger_total(cfg) + cost


class BackboardLLM:
    """Real LLM via Backboard. One assistant per role, ids cached in cfg.state_file."""

    def __init__(self, cfg: Optional[Config] = None):
        self.cfg = cfg or Config()
        self._client = None
        self._ids: dict[str, str] = {}
        self._lock: Optional[asyncio.Lock] = None
        self._systems: dict[str, str] = {}   # thread_id -> system prompt
        self.total_tokens = 0
        self.total_cost = 0.0
        self.calls = 0

    @property
    def client(self):
        if self._client is None:
            from backboard import BackboardClient
            self._client = BackboardClient(api_key=self.cfg.api_key or "proxy-injected",
                                           base_url=self.cfg.base_url, timeout=120)
        return self._client

    async def _assistant_id(self, role: str, system: str = "", tools: Optional[list] = None) -> str:
        if role in self._ids:
            return self._ids[role]
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            if role in self._ids:
                return self._ids[role]
            state = _load_state(self.cfg.state_file)
            aid = (state.get("assistants") or {}).get(role)
            if not aid:
                name = f"webforge-{role}"
                try:
                    found = await _retry(self.client.list_assistants, name=name, limit=1)
                    for a in found or []:
                        if getattr(a, "name", name) == name:
                            aid = str(a.assistant_id)
                            break
                except Exception:
                    aid = None
                if not aid:
                    a = await _retry(self.client.create_assistant, name=name,
                                     system_prompt=f"You are the WebForge {role}.")
                    aid = str(a.assistant_id)
                state.setdefault("assistants", {})[role] = aid
                _save_state(self.cfg.state_file, state)
            self._ids[role] = aid
            return aid

    def _guard(self) -> None:
        spent = ledger_total(self.cfg)
        if spent >= self.cfg.budget_usd:
            raise BudgetExceeded(f"Backboard spend cap reached: ${spent:.3f} >= ${self.cfg.budget_usd:.2f}")

    def _account(self, resp: Any) -> LLMReply:
        tokens, cost = _usage_from(resp)
        self.calls += 1
        self.total_tokens += tokens
        self.total_cost += cost
        _ledger_add(self.cfg, cost)
        status = str(getattr(resp, "status", "") or "")
        # QUIRK: COMPLETED responses after submit still echo the old tool_calls; trust status.
        tcs = _tool_calls(resp) if status in ("", "REQUIRES_ACTION") else []
        if tcs:
            status = "REQUIRES_ACTION"
        elif not status:
            status = "COMPLETED"
        return LLMReply(thread_id=str(getattr(resp, "thread_id", "") or ""), status=status,
                        text=getattr(resp, "content", None) or "", tool_calls=tcs,
                        tokens=tokens, cost_usd=cost)

    async def chat(self, *, role: str, message: str, system: str, tools: Optional[list[dict]] = None,
                   thread_id: Optional[str] = None, json_output: bool = False) -> LLMReply:
        self._guard()
        aid = await self._assistant_id(role)
        if thread_id is not None and not system:
            system = self._systems.get(thread_id, "")
        kw: dict[str, Any] = dict(assistant_id=aid, thread_id=thread_id, tools=tools or None,
                                  llm_provider=self.cfg.provider, model_name=self.cfg.model,
                                  system_prompt=system or None, memory=None)
        if json_output:
            kw["json_output"] = True
        resp = await _retry(self.client.send_message, message, **kw)
        reply = self._account(resp)
        if reply.thread_id and system:
            self._systems[reply.thread_id] = system
        return reply

    async def submit(self, *, role: str, thread_id: str, outputs: list[dict]) -> LLMReply:
        self._guard()
        outs = [{"tool_call_id": str(o["tool_call_id"]), "output": str(o.get("output", ""))} for o in outputs]
        resp = await _retry(self.client.submit_tool_outputs_simple, thread_id, outs)
        reply = self._account(resp)
        if not reply.thread_id:
            reply.thread_id = thread_id
        return reply

    def usage(self) -> dict:
        return {"calls": self.calls, "tokens": self.total_tokens, "cost_usd": self.total_cost}

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


# ---------------------------------------------------------------- MockLLM ---

class MockLLM:
    """Offline LLM driven by a handler.

    handler(role, message, thread_id, tools, system, kind) -> LLMReply | dict | str
      kind is "chat" or "submit" (for submit, message = json.dumps(outputs), system = "").
    dict form: {"text": str, "tool_calls": [{"name": str, "args": dict}] }
      (tool_calls may also be (name, args) tuples). status = REQUIRES_ACTION if tool_calls
      else COMPLETED (override with "status"). str form = final text.
    Thread ids auto-generated as mock_thread_N when None; tool ids mock_call_N.
    """

    def __init__(self, handler: Optional[Callable] = None):
        self.handler = handler or (lambda **kw: {"text": "ok"})
        self.total_tokens = 0
        self.total_cost = 0.0
        self.calls = 0
        self.log: list[dict] = []     # every call, for assertions
        self._tid = 0
        self._cid = 0
        self._threads_by_role: dict[str, str] = {}

    def _to_reply(self, r: Any, thread_id: str, tokens: int) -> LLMReply:
        if isinstance(r, LLMReply):
            if not r.thread_id:
                r.thread_id = thread_id
            if not r.tokens:
                r.tokens = tokens
            return r
        if isinstance(r, str):
            r = {"text": r}
        tcs: list[ToolCall] = []
        for t in r.get("tool_calls") or []:
            if isinstance(t, ToolCall):
                tcs.append(t)
                continue
            if isinstance(t, (tuple, list)):
                name, args = t[0], (t[1] if len(t) > 1 else {})
            else:
                name, args = t["name"], t.get("args", {})
            self._cid += 1
            tcs.append(ToolCall(id=(t.get("id") if isinstance(t, dict) and t.get("id") else f"mock_call_{self._cid}"),
                                name=name, args=dict(args or {})))
        status = r.get("status") or ("REQUIRES_ACTION" if tcs else "COMPLETED")
        return LLMReply(thread_id=thread_id, status=status, text=r.get("text", "") or "",
                        tool_calls=tcs, tokens=tokens, cost_usd=0.0)

    def _call(self, kind: str, role: str, message: str, thread_id: Optional[str],
              tools: Optional[list], system: str) -> LLMReply:
        if not thread_id:
            self._tid += 1
            thread_id = f"mock_thread_{self._tid}"
        self.log.append({"kind": kind, "role": role, "message": message, "thread_id": thread_id,
                         "tools": tools, "system": system})
        r = self.handler(role=role, message=message, thread_id=thread_id, tools=tools or [],
                         system=system, kind=kind)
        tokens = max(1, len(message) // 4)
        reply = self._to_reply(r, thread_id, tokens)
        self.calls += 1
        self.total_tokens += reply.tokens
        self.total_cost += reply.cost_usd
        return reply

    async def chat(self, *, role: str, message: str, system: str = "", tools: Optional[list[dict]] = None,
                   thread_id: Optional[str] = None, json_output: bool = False) -> LLMReply:
        return self._call("chat", role, message, thread_id, tools, system)

    async def submit(self, *, role: str, thread_id: str, outputs: list[dict]) -> LLMReply:
        return self._call("submit", role, json.dumps(outputs), thread_id, None, "")

    def usage(self) -> dict:
        return {"calls": self.calls, "tokens": self.total_tokens, "cost_usd": self.total_cost}

    async def aclose(self) -> None:
        return None


class ScriptedMock(MockLLM):
    """Pops the next step for a role on every chat()/submit() of that role.

    steps_by_role = {"worker": [{"tool_calls": [{"name": "click", "args": {"idx": 3}}]}, {"text": "done"}]}
    Steps are anything MockLLM's handler may return (dict / str / LLMReply) or a callable(**call) -> same.
    """

    def __init__(self, steps_by_role: dict[str, list]):
        self.steps = {k: list(v) for k, v in steps_by_role.items()}
        super().__init__(self._next)

    def _next(self, *, role, message, thread_id, tools, system, kind):
        q = self.steps.get(role)
        if not q:
            raise RuntimeError(f"ScriptedMock: no scripted steps left for role '{role}' "
                               f"(kind={kind}, message={message[:80]!r})")
        step = q.pop(0)
        if callable(step):
            return step(role=role, message=message, thread_id=thread_id, tools=tools,
                        system=system, kind=kind)
        return step


def make_llm(cfg: Optional[Config] = None, mock: bool = False):
    """Real BackboardLLM, or an (unscripted, always-'ok') MockLLM when mock=True."""
    if mock:
        return MockLLM()
    return BackboardLLM(cfg or Config())
