"""WebForge API server (FastAPI): run the agent from a browser, stream events live, browse skills/memory/bench.

Run:  python -m webforge serve [--port 8000]      or      uvicorn webforge.server:app

Env:
  WEBFORGE_ACCESS_CODE   if set, POST /api/run, DELETE /api/skills/*, POST /api/memory/reset need header X-Access-Code
  WEBFORGE_CORS_ORIGINS  comma list of allowed browser origins ("*" allowed). Default "*" only when no access code
                         is set; with an access code and no list, cross-origin browsers are refused.
  WEBFORGE_MOCK=1        use the offline MockLLM + local memory (no Backboard key / spend)
  WEBFORGE_LOCAL=1       trust the client (set by docker-compose, which publishes the port on 127.0.0.1 only):
                         lets the browser save the Backboard key without an access code
Backboard key resolution: env BACKBOARD_API_KEY > key saved from the web UI (<data_dir>/backboard_key) > none.
Everything else comes from webforge.config.Config. API keys are never returned or logged.
"""
from __future__ import annotations

import asyncio
import copy
import dataclasses
import hmac
import json
import os
import re
import secrets
import time
from collections import OrderedDict, deque
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from webforge.config import ROOT, Config

MAX_TASK_CHARS = 500
MAX_EVENTS = 500
KEEP_RUNS = 20
KEEPALIVE_S = 15.0
_NAME_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,100}$")
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")
WEB_DIR = ROOT / "web"


def get_cfg() -> Config:
    """Fresh Config per call (tests monkeypatch this)."""
    return Config()


def _mock() -> bool:
    return os.getenv("WEBFORGE_MOCK", "") == "1"


def _access_code() -> str:
    return os.getenv("WEBFORGE_ACCESS_CODE", "")


def _cors_origins() -> list[str]:
    raw = os.getenv("WEBFORGE_CORS_ORIGINS", "").strip()
    if raw:
        return [o.strip().rstrip("/") for o in raw.split(",") if o.strip()]
    return [] if _access_code() else ["*"]


# ------------------------------------------------------------------ auth ---

def _check_code(provided: Optional[str]) -> bool:
    code = _access_code()
    if not code:
        return True
    return hmac.compare_digest((provided or "").encode(), code.encode())


def _require_code(provided: Optional[str]) -> None:
    if not _check_code(provided):
        raise HTTPException(401, "Access code required (header X-Access-Code).")


def _require_admin(request: Request, provided: Optional[str]) -> None:
    """Destructive endpoints: access code if configured, otherwise loopback clients only."""
    if _access_code():
        _require_code(provided)
        return
    host = request.client.host if request.client else ""
    if host not in ("127.0.0.1", "::1", "localhost", "testclient"):
        raise HTTPException(403, "Set WEBFORGE_ACCESS_CODE to allow this action from a remote client.")


# ------------------------------------------------------------ backboard key ---

PLACEHOLDER_KEY = "proxy-injected"   # sandbox network proxy injects the real key; the code sends this placeholder
KEY_FILE_NAME = "backboard_key"
_KEY_ATTEMPTS: "deque[float]" = deque()
KEY_ATTEMPTS_PER_MIN = 10


def _local_mode() -> bool:
    return os.getenv("WEBFORGE_LOCAL", "") == "1"


def _key_path(cfg: Config) -> Path:
    return Path(cfg.data_dir) / KEY_FILE_NAME


def _read_saved_key(cfg: Config) -> str:
    try:
        return _key_path(cfg).read_text(encoding="utf-8").strip()
    except Exception:
        return ""


def resolve_key(cfg: Config) -> tuple[str, str]:
    """(key, source). source: env | saved | proxy | none. Order: real env key > saved file > proxy placeholder."""
    env = (cfg.api_key or "").strip()
    if env and env != PLACEHOLDER_KEY:
        return env, "env"
    saved = _read_saved_key(cfg)
    if saved:
        return saved, "saved"
    if env == PLACEHOLDER_KEY:
        return env, "proxy"
    return "", "none"


def _cfg_with_key(cfg: Config) -> Config:
    """Per-run copy of cfg carrying the resolved key (global env is never mutated)."""
    key, _ = resolve_key(cfg)
    c = copy.copy(cfg)
    c.api_key = key
    return c


def _needs_key(cfg: Config) -> bool:
    return (not _mock()) and resolve_key(cfg)[1] == "none"


def _scrub(text: str, *secrets_: str) -> str:
    for sec in secrets_:
        if sec:
            text = text.replace(sec, "[redacted]")
    return text


def _require_key_admin(request: Request, provided: Optional[str]) -> None:
    if _access_code():
        if not _check_code(provided):
            raise HTTPException(403, "Access code required (header X-Access-Code).")
        return
    host = request.client.host if request.client else ""
    if _local_mode() or host in ("127.0.0.1", "::1", "localhost", "testclient"):
        return
    raise HTTPException(403, "Saving a key is only allowed locally (WEBFORGE_LOCAL=1) or with WEBFORGE_ACCESS_CODE set.")


def _rate_limit_key_attempts() -> None:
    now = time.monotonic()
    while _KEY_ATTEMPTS and now - _KEY_ATTEMPTS[0] > 60:
        _KEY_ATTEMPTS.popleft()
    if len(_KEY_ATTEMPTS) >= KEY_ATTEMPTS_PER_MIN:
        raise HTTPException(429, "Too many key attempts. Wait a minute and try again.")
    _KEY_ATTEMPTS.append(now)


class KeyRejected(Exception):
    """Backboard answered 401/403 for this key."""


class KeyUnverifiable(Exception):
    """Could not reach Backboard (network, timeout, 5xx)."""


async def verify_key(key: str, cfg: Config) -> None:
    """Cheap authenticated Backboard call. Returns None if accepted; raises KeyRejected / KeyUnverifiable."""
    from backboard import BackboardClient
    client = BackboardClient(api_key=key, base_url=cfg.base_url, timeout=20)
    try:
        await client.list_assistants(limit=1)
    except Exception as e:  # noqa: BLE001
        status = getattr(e, "status_code", None)
        if status in (401, 403):
            raise KeyRejected() from None
        raise KeyUnverifiable(_scrub(f"{type(e).__name__}: {str(e)[:200]}", key)) from None
    finally:
        try:
            await client.aclose()
        except Exception:
            pass


class KeyRequest(BaseModel):
    api_key: str


def _valid_key_shape(k: str) -> bool:
    return 10 <= len(k) <= 300 and not any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in k)



# ------------------------------------------------------------------ runs ---

class RunRequest(BaseModel):
    task: str
    learn: bool = True
    use_skills: bool = True
    use_memory: bool = True


class Run:
    def __init__(self, run_id: str, req: RunRequest):
        self.id = run_id
        self.req = req
        self.status = "running"          # running | done | error
        self.events: list[dict] = []
        self.result: Optional[dict] = None
        self.error: Optional[str] = None
        self.started = time.time()
        self.cond = asyncio.Condition()
        self.task: Optional[asyncio.Task] = None

    def public(self) -> dict:
        return {"run_id": self.id, "status": self.status, "task": self.req.task, "learn": self.req.learn,
                "use_skills": self.req.use_skills, "use_memory": self.req.use_memory,
                "n_events": len(self.events), "result": self.result, "error": self.error}


class State:
    def __init__(self) -> None:
        self.runs: "OrderedDict[str, Run]" = OrderedDict()
        self.active: Optional[str] = None


STATE = State()


def _jsonable(x: Any) -> Any:
    return json.loads(json.dumps(x, default=str))


async def _push(run: Run, kind: str, data: dict) -> None:
    ev = {"t": round(time.time() - run.started, 2), "kind": kind, "data": _jsonable(data or {})}
    if len(run.events) >= MAX_EVENTS:
        # keep the first/last events useful: drop the oldest non-terminal one
        run.events.pop(1 if len(run.events) > 1 else 0)
    run.events.append(ev)
    async with run.cond:
        run.cond.notify_all()


async def _finish(run: Run, status: str) -> None:
    run.status = status
    async with run.cond:
        run.cond.notify_all()


def _persist(cfg: Config, run: Run) -> None:
    try:
        d = Path(cfg.runs_dir) / run.id
        d.mkdir(parents=True, exist_ok=True)
        if run.result is not None:
            (d / "result.json").write_text(json.dumps(run.result, indent=2, ensure_ascii=False, default=str),
                                           encoding="utf-8")
        (d / "events.json").write_text(json.dumps(
            {"run_id": run.id, "task": run.req.task, "recorded": True, "events": run.events,
             "result": run.result, "error": run.error}, ensure_ascii=False, default=str), encoding="utf-8")
    except Exception:
        pass


async def _execute(run: Run, cfg: Config) -> None:
    """Build everything, run the agent, store the result. Replaced by a stub in tests."""
    from webforge.agent.planner import run_task
    from webforge.browser.session import Browser
    from webforge.llm import make_llm
    from webforge.memory import make_memory
    from webforge.skills.library import SkillLibrary

    mock = _mock()
    llm = memory = browser = None
    loop = asyncio.get_running_loop()

    def on_event(kind: str, data: dict) -> None:
        # planner/worker call this synchronously from the event loop thread
        loop.create_task(_push(run, kind, data))

    try:
        llm = make_llm(cfg, mock=mock)
        memory = make_memory(cfg, local_only=mock)
        library = SkillLibrary(cfg.skills_dir)
        browser = Browser(chromium_path=cfg.chromium_path, allowed_domains=cfg.allowed_domains)
        await browser.start(headless=os.getenv("WEBFORGE_HEADED") != "1")
        run_dir = Path(cfg.runs_dir) / run.id
        res = await run_task(run.req.task, llm=llm, browser=browser, library=library, memory=memory, cfg=cfg,
                             on_event=on_event, learn=run.req.learn, use_memory=run.req.use_memory,
                             use_skills=run.req.use_skills, run_dir=run_dir)
        run.result = _jsonable(dataclasses.asdict(res))
    except asyncio.CancelledError:
        run.error = "cancelled"
        raise
    except Exception as e:  # noqa: BLE001
        run.error = _scrub(f"{type(e).__name__}: {str(e)[:300]}", cfg.api_key)
        await _push(run, "error", {"message": run.error})
    finally:
        for closer in (getattr(browser, "close", None), getattr(llm, "aclose", None),
                       getattr(memory, "aclose", None)):
            if closer:
                try:
                    await closer()
                except Exception:
                    pass


async def _run_wrapper(run: Run, cfg: Config) -> None:
    try:
        await _execute(run, cfg)  # browser/LLM are closed before we announce "done"
        await asyncio.sleep(0)    # let queued on_event pushes land first
    except BaseException as e:  # noqa: BLE001
        run.error = run.error or _scrub(f"{type(e).__name__}: {str(e)[:300]}", cfg.api_key)
    finally:
        _persist(cfg, run)
        if run.status == "running":
            await _finish(run, "error" if run.error else "done")
        if STATE.active == run.id:
            STATE.active = None


# ------------------------------------------------------------------- app ---

app = FastAPI(title="WebForge API", docs_url="/api/docs", openapi_url="/api/openapi.json")

_origins = _cors_origins()
app.add_middleware(CORSMiddleware, allow_origins=_origins, allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
                   allow_headers=["*"], allow_credentials=False, max_age=600)


def _browser_available(cfg: Config) -> bool:
    if cfg.chromium_path and Path(cfg.chromium_path).exists():
        return True
    try:
        import importlib.util
        return importlib.util.find_spec("playwright") is not None
    except Exception:
        return False


def _spend(cfg: Config) -> float:
    from webforge.llm import ledger_total
    return round(ledger_total(cfg), 6)


@app.get("/api/health")
async def health() -> dict:
    cfg = get_cfg()
    _, key_source = resolve_key(cfg)
    return {"ok": True, "mock": _mock(), "model": cfg.model, "provider": cfg.provider,
            "has_key": key_source != "none", "key_source": key_source,
            "needs_key": _needs_key(cfg), "local_mode": _local_mode(),
            "spend_usd": _spend(cfg), "budget_usd": cfg.budget_usd, "browser": _browser_available(cfg),
            "needs_code": bool(_access_code()), "busy": STATE.active is not None,
            "allowed_domains": list(cfg.allowed_domains)}

@app.post("/api/settings/key")
async def settings_key_set(body: KeyRequest, request: Request,
                           x_access_code: Optional[str] = Header(default=None)) -> dict:
    _require_key_admin(request, x_access_code)
    _rate_limit_key_attempts()
    key = (body.api_key or "").strip()
    if not _valid_key_shape(key):
        raise HTTPException(422, "That does not look like an API key (10-300 characters, no spaces).")
    cfg = get_cfg()
    try:
        await verify_key(key, cfg)
    except KeyRejected:
        return JSONResponse({"error": "Backboard rejected this key"}, status_code=400)
    except KeyUnverifiable as e:
        return JSONResponse({"error": "Could not reach Backboard to check the key (" + _scrub(str(e), key) +
                                      "). Check your internet connection and try again; nothing was saved."},
                            status_code=502)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": "Could not verify the key: " + _scrub(type(e).__name__, key)},
                            status_code=502)
    path = _key_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(key)
    except BaseException:
        try:
            tmp.unlink()
        except Exception:
            pass
        raise
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    return {"ok": True, "key_source": resolve_key(cfg)[1]}


@app.delete("/api/settings/key")
async def settings_key_delete(request: Request, x_access_code: Optional[str] = Header(default=None)) -> dict:
    _require_key_admin(request, x_access_code)
    try:
        _key_path(get_cfg()).unlink()
    except FileNotFoundError:
        pass
    return {"ok": True, "key_source": resolve_key(get_cfg())[1]}



@app.get("/api/spend")
async def spend() -> dict:
    cfg = get_cfg()
    s = _spend(cfg)
    return {"spend_usd": s, "budget_usd": cfg.budget_usd, "remaining_usd": round(max(cfg.budget_usd - s, 0), 6)}


@app.get("/api/tasks")
async def tasks() -> dict:
    try:
        import yaml
        data = yaml.safe_load((ROOT / "bench" / "tasks.yaml").read_text(encoding="utf-8"))
        items = data["tasks"] if isinstance(data, dict) else data
        return {"tasks": [{"id": t["id"], "family": t.get("family", ""), "prompt": t["prompt"]} for t in items]}
    except Exception:
        return {"tasks": []}


@app.post("/api/run")
async def start_run(req: RunRequest, x_access_code: Optional[str] = Header(default=None)) -> JSONResponse:
    _require_code(x_access_code)
    task = (req.task or "").strip()
    if not task:
        raise HTTPException(422, "Task must not be empty.")
    if len(task) > MAX_TASK_CHARS:
        raise HTTPException(422, f"Task is too long (max {MAX_TASK_CHARS} characters).")
    if any(ord(c) < 32 and c not in "\n\t" for c in task):
        raise HTTPException(422, "Task contains control characters.")
    cfg = get_cfg()
    if _needs_key(cfg):
        return JSONResponse({"error": "Backboard API key not set", "needs_key": True}, status_code=428)
    cfg = _cfg_with_key(cfg)
    if not _mock() and _spend(cfg) >= cfg.budget_usd:
        raise HTTPException(402, f"Budget exhausted (${_spend(cfg):.2f} of ${cfg.budget_usd:.2f}).")
    if STATE.active is not None:  # single-threaded event loop: check+set is atomic (no await between)
        raise HTTPException(409, "Another run is in progress. Wait for it to finish.")
    run_id = f"web_{time.strftime('%Y%m%d_%H%M%S')}_{secrets.token_hex(2)}"
    req = req.model_copy(update={"task": task})
    run = Run(run_id, req)
    STATE.active = run_id
    STATE.runs[run_id] = run
    while len(STATE.runs) > KEEP_RUNS:
        STATE.runs.popitem(last=False)
    run.task = asyncio.create_task(_run_wrapper(run, cfg))
    return JSONResponse({"run_id": run_id}, status_code=202)


def _get_run(run_id: str) -> Run:
    run = STATE.runs.get(run_id)
    if not run:
        raise HTTPException(404, "Unknown run.")
    return run


def _sse(data: Any, event: Optional[str] = None) -> str:
    out = f"event: {event}\n" if event else ""
    return out + "data: " + json.dumps(data, ensure_ascii=False, default=str) + "\n\n"


@app.get("/api/runs/{run_id}/events")
async def run_events(run_id: str) -> StreamingResponse:
    run = _get_run(run_id)

    async def gen():
        i = 0
        last = time.monotonic()
        while True:
            while i < len(run.events):
                yield _sse(run.events[i])
                i += 1
                last = time.monotonic()
            if run.status != "running":
                if i >= len(run.events):
                    yield _sse({"status": run.status, "result": run.result, "error": run.error}, event="done")
                    return
                continue
            try:
                async with run.cond:
                    await asyncio.wait_for(run.cond.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                pass
            if time.monotonic() - last >= KEEPALIVE_S:
                yield ": keepalive\n\n"
                last = time.monotonic()

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/runs/{run_id}")
async def run_info(run_id: str) -> dict:
    return _get_run(run_id).public()


# ---------------------------------------------------------------- skills ---

def _library():
    from webforge.skills.library import SkillLibrary
    return SkillLibrary(get_cfg().skills_dir)


@app.get("/api/skills")
async def skills_list() -> dict:
    from webforge.skills.library import is_demoted
    out = []
    for s in _library().list(include_demoted=True):
        out.append({"name": s.name, "domain": s.domain, "description": s.description, "params": s.params,
                    "uses": s.uses, "successes": s.successes, "version": s.version, "n_steps": len(s.steps),
                    "demoted": is_demoted(s)})
    return {"skills": out}


@app.get("/api/skills/{name}")
async def skill_get(name: str) -> dict:
    if not _NAME_RE.match(name):
        raise HTTPException(404, "No such skill.")
    s = _library().get(name)
    if not s:
        raise HTTPException(404, "No such skill.")
    return s.to_dict()


@app.delete("/api/skills/{name}")
async def skill_delete(name: str, request: Request, x_access_code: Optional[str] = Header(default=None)) -> dict:
    _require_admin(request, x_access_code)
    if not _NAME_RE.match(name) or not _library().delete(name):
        raise HTTPException(404, "No such skill.")
    return {"deleted": name}


# ---------------------------------------------------------------- memory ---

@app.get("/api/memory")
async def memory_list() -> dict:
    from webforge.memory import LocalMemory
    items = await LocalMemory(get_cfg()).list()   # local mirror holds everything the agent wrote
    items = [{"content": i["content"], "metadata": i.get("metadata", {})} for i in reversed(items)]
    return {"total": len(items), "memories": items[:200]}


@app.post("/api/memory/reset")
async def memory_reset(request: Request, x_access_code: Optional[str] = Header(default=None)) -> dict:
    _require_admin(request, x_access_code)
    from webforge.memory import make_memory
    mem = make_memory(_cfg_with_key(get_cfg()), local_only=_mock())
    try:
        await mem.reset()
    finally:
        if hasattr(mem, "aclose"):
            try:
                await mem.aclose()
            except Exception:
                pass
    return {"reset": True}


# ----------------------------------------------------------------- bench ---

def _latest(runs_dir: Path, mode: str) -> Optional[Path]:
    files = sorted(p for p in Path(runs_dir).glob(f"bench_{mode}_*.jsonl") if p.is_file())
    return files[-1] if files else None


@app.get("/api/bench")
async def bench() -> dict:
    cfg = get_cfg()
    out: dict[str, Any] = {"sample": False, "cold": [], "learned": [], "cold_file": None, "learned_file": None,
                           "aggregate": None}
    try:
        from bench.run_bench import compare, load_jsonl
    except Exception:
        compare = load_jsonl = None  # type: ignore
    for mode in ("cold", "learned"):
        p = _latest(cfg.runs_dir, mode)
        if p and load_jsonl:
            try:
                out[mode] = load_jsonl(p)
                out[f"{mode}_file"] = p.name
            except Exception:
                pass
    if compare and out["cold"] and out["learned"]:
        try:
            out["aggregate"] = compare(out["cold"], out["learned"])
        except Exception:
            pass
    return out


# ---------------------------------------------------------------- static ---

if WEB_DIR.is_dir():
    app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="web")


def serve(host: str = "127.0.0.1", port: int = 8000) -> None:
    import uvicorn
    uvicorn.run(app, host=host, port=port, log_level="info")
