"""WebForge API server (FastAPI): run the agent from a browser, stream events live, answer its questions and
approvals, manage secrets, download deliverables, cancel and resume runs.

Run:  python -m webforge serve [--port 8000]      or      uvicorn webforge.server:app

Env:
  WEBFORGE_ACCESS_CODE   if set, starting/controlling runs and setting secrets need header X-Access-Code
  WEBFORGE_CORS_ORIGINS  comma list of allowed browser origins ("*" allowed). Default "*" only when no access code
                         is set; with an access code and no list, cross-origin browsers are refused.
  WEBFORGE_MOCK=1        use the offline MockLLM (no Backboard key / spend)
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


class AnswerRequest(BaseModel):
    ask_id: str
    answer: str


class AnswerAllRequest(BaseModel):
    answer: str
    ask_ids: list[str]


class SecretRequest(BaseModel):
    name: str
    value: str


class Run:
    def __init__(self, run_id: str, req: RunRequest, resume: Optional[dict] = None, parent: Optional[str] = None):
        self.id = run_id
        self.req = req
        self.resume = resume
        self.parent = parent
        self.status = "running"          # running | done | error | cancelled
        self.events: list[dict] = []     # each carries a monotonically increasing "n"; old entries may be dropped
        self.seq = 0
        self.result: Optional[dict] = None
        self.error: Optional[str] = None
        self.started = time.time()
        self.cond = asyncio.Condition()
        self.task: Optional[asyncio.Task] = None
        self.pending: dict[str, asyncio.Future] = {}   # ask_id -> future resolved by POST /answer
        self.agent_states: dict[str, str] = {}         # agent id -> running|waiting|done|failed (from agent_state)
        self.steps = 0                                  # count of "action" events so far

    def public(self) -> dict:
        return {"run_id": self.id, "status": self.status, "task": self.req.task, "resumed_from": self.parent,
                "n_events": len(self.events), "pending": list(self.pending), "result": self.result,
                "error": self.error}


class State:
    def __init__(self) -> None:
        self.runs: "OrderedDict[str, Run]" = OrderedDict()
        self.active: Optional[str] = None
        self.secrets = None   # webforge.safety.Secrets, created lazily (env WEBFORGE_SECRET_* + UI-set values)

    def get_secrets(self):
        if self.secrets is None:
            from webforge.safety import Secrets
            self.secrets = Secrets()
        return self.secrets


STATE = State()


def _jsonable(x: Any) -> Any:
    return json.loads(json.dumps(x, default=str))


async def _push(run: Run, kind: str, data: dict) -> None:
    data = _jsonable(data or {})
    sec = STATE.get_secrets()
    data = json.loads(sec.redact(json.dumps(data, ensure_ascii=False)))
    run.seq += 1
    ev = {"n": run.seq, "t": round(time.time() - run.started, 2), "kind": kind, "data": data}
    if kind == "action":
        run.steps += 1
    elif kind == "agent_state" and data.get("agent"):
        run.agent_states[str(data["agent"])] = str(data.get("state", "running"))
    if kind not in ("screenshot", "metrics"):
        _append_log(run, ev)
    if kind == "screenshot":   # live-only: replace the previous frame of the same agent instead of piling up
        run.events = [e for e in run.events
                      if not (e["kind"] == "screenshot" and e["data"].get("agent") == data.get("agent"))]
    elif kind == "metrics":    # live-only gauges: only the latest frame is worth keeping (and must not crowd out real events)
        run.events = [e for e in run.events if e["kind"] != "metrics"]
    if len(run.events) >= MAX_EVENTS:
        run.events.pop(1 if len(run.events) > 1 else 0)
    run.events.append(ev)
    async with run.cond:
        run.cond.notify_all()


def _append_log(run: Run, ev: dict) -> None:
    """One JSON line per event in runs/<id>/log.jsonl (already redacted). IO errors are ignored."""
    try:
        d = Path(get_cfg().runs_dir) / run.id
        d.mkdir(parents=True, exist_ok=True)
        with open(d / "log.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(ev, ensure_ascii=False, default=str) + "\n")
    except Exception:
        pass


async def _metrics_ticker(run: Run, cfg: Config, llm: Any, interval: float = 1.0) -> None:
    """Push a `metrics` event every `interval` seconds until cancelled. Never raises (except cancellation)."""
    def usage() -> dict:
        try:
            return llm.usage() or {}
        except Exception:
            return {}

    base_cost = float(usage().get("cost_usd", 0) or 0)
    done_cost = float((run.resume or {}).get("spent", 0) or 0)       # spent before this (resumed) run started
    done_secs = float((run.resume or {}).get("elapsed", 0) or 0)
    while True:
        try:
            u = usage()
            try:
                spend = _spend(cfg)
            except Exception:
                spend = 0.0
            await _push(run, "metrics", {
                "elapsed": round(done_secs + time.time() - run.started, 1), "deadline_s": cfg.run_deadline_s,
                "run_cost": round(done_cost + max(float(u.get("cost_usd", 0) or 0) - base_cost, 0.0), 6),
                "run_budget": cfg.run_budget_usd, "global_spend": spend, "global_cap": cfg.budget_usd,
                "llm_calls": int(u.get("calls", 0) or 0), "tokens": int(u.get("tokens", 0) or 0),
                "steps": run.steps, "active": dict(run.agent_states)})
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        await asyncio.sleep(interval)


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
        events = [e for e in run.events if e["kind"] not in ("screenshot", "metrics")]
        (d / "events.json").write_text(json.dumps(
            {"run_id": run.id, "task": run.req.task, "events": events, "result": run.result, "error": run.error},
            ensure_ascii=False, default=str), encoding="utf-8")
    except Exception:
        pass


async def _execute(run: Run, cfg: Config) -> None:
    """Build everything, run the agent, store the result. Replaced by a stub in tests."""
    from webforge.agent.planner import run_task
    from webforge.browser.session import open_browser
    from webforge.llm import make_llm

    llm = browser = ticker = None
    loop = asyncio.get_running_loop()

    def on_event(kind: str, data: dict) -> None:
        # planner/worker call this synchronously from the event loop thread
        loop.create_task(_push(run, kind, data))

    async def on_ask(question: str, options: list, agent: str = "planner", meta: Optional[dict] = None) -> str:
        ask_id = secrets.token_hex(4)
        fut: asyncio.Future = loop.create_future()
        run.pending[ask_id] = fut
        await _push(run, "ask_id", {"ask_id": ask_id, "question": question, "options": options,
                                    "timeout_s": cfg.ask_timeout_s, "agent": agent,
                                    "kind": "approval" if (meta or {}).get("kind") == "approval" else "question",
                                    "meta": meta or {}})
        try:
            return await fut
        finally:
            run.pending.pop(ask_id, None)

    try:
        llm = make_llm(cfg, mock=_mock())
        browser = await open_browser(cfg, STATE.get_secrets(), headless=os.getenv("WEBFORGE_HEADED") != "1")
        ticker = asyncio.create_task(_metrics_ticker(run, cfg, llm))
        res = await run_task(run.req.task, llm=llm, browser=browser, cfg=cfg, on_event=on_event, on_ask=on_ask,
                             run_dir=Path(cfg.runs_dir) / run.id, resume=run.resume)
        run.result = _jsonable(dataclasses.asdict(res))
        run.result.pop("notes", None)   # workspace stays on disk; the UI gets file names via /files
    except asyncio.CancelledError:
        run.error = "cancelled"
        raise
    except RuntimeError as e:
        run.error = _scrub(str(e)[:500], cfg.api_key)
        await _push(run, "error", {"message": run.error})
    except Exception as e:  # noqa: BLE001
        run.error = _scrub(f"{type(e).__name__}: {str(e)[:300]}", cfg.api_key)
        await _push(run, "error", {"message": run.error})
    finally:
        if ticker is not None:
            ticker.cancel()
        for closer in (getattr(browser, "close", None), getattr(llm, "aclose", None)):
            if closer:
                try:
                    await closer()
                except Exception:
                    pass


async def _run_wrapper(run: Run, cfg: Config) -> None:
    status = None
    try:
        await _execute(run, cfg)  # browser/LLM are closed before we announce "done"
        await asyncio.sleep(0)    # let queued on_event pushes land first
    except asyncio.CancelledError:
        status = "cancelled"
    except BaseException as e:  # noqa: BLE001
        run.error = run.error or _scrub(f"{type(e).__name__}: {str(e)[:300]}", cfg.api_key)
    finally:
        for fut in run.pending.values():
            if not fut.done():
                fut.cancel()
        _persist(cfg, run)
        if run.status == "running":
            await _finish(run, status or ("error" if run.error else "done"))
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
    mode = ("your Chrome (remote debugging)" if cfg.cdp_url else "own Chromium (window)"
            if os.getenv("WEBFORGE_HEADED") == "1" else "own Chromium (headless, live thumbnails)")
    return {"ok": True, "mock": _mock(), "real_browser": bool(cfg.cdp_url), "browser_mode": mode, "model": cfg.model, "provider": cfg.provider,
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


def _validate_task(task: str) -> str:
    task = (task or "").strip()
    if not task:
        raise HTTPException(422, "Task must not be empty.")
    if len(task) > MAX_TASK_CHARS:
        raise HTTPException(422, f"Task is too long (max {MAX_TASK_CHARS} characters).")
    if any(ord(c) < 32 and c not in "\n\t" for c in task):
        raise HTTPException(422, "Task contains control characters.")
    return task


def _start(req: RunRequest, resume: Optional[dict] = None, parent: Optional[str] = None) -> JSONResponse:
    cfg = get_cfg()
    if _needs_key(cfg):
        return JSONResponse({"error": "Backboard API key not set", "needs_key": True}, status_code=428)
    cfg = _cfg_with_key(cfg)
    if not _mock() and _spend(cfg) >= cfg.budget_usd:
        raise HTTPException(402, f"Budget exhausted (${_spend(cfg):.2f} of ${cfg.budget_usd:.2f}).")
    if STATE.active is not None:  # single-threaded event loop: check+set is atomic (no await between)
        raise HTTPException(409, "Another run is in progress. Wait for it to finish.")
    run_id = parent if resume else f"web_{time.strftime('%Y%m%d_%H%M%S')}_{secrets.token_hex(2)}"
    run = Run(run_id, req, resume=resume, parent=parent)
    STATE.active = run_id
    STATE.runs.pop(run_id, None)
    STATE.runs[run_id] = run
    while len(STATE.runs) > KEEP_RUNS:
        STATE.runs.popitem(last=False)
    run.task = asyncio.create_task(_run_wrapper(run, cfg))
    return JSONResponse({"run_id": run_id}, status_code=202)


@app.post("/api/run")
async def start_run(req: RunRequest, x_access_code: Optional[str] = Header(default=None)) -> JSONResponse:
    _require_code(x_access_code)
    return _start(req.model_copy(update={"task": _validate_task(req.task)}))


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
        sent = 0   # highest event number already sent: list edits (dropped screenshots, the event cap) cannot shift it
        last = time.monotonic()
        while True:
            fresh = [e for e in list(run.events) if e["n"] > sent]
            for e in fresh:
                yield _sse(e)
                sent = e["n"]
                last = time.monotonic()
            if run.status != "running":
                if not any(e["n"] > sent for e in run.events):
                    yield _sse({"status": run.status, "result": run.result, "error": run.error}, event="done")
                    return
                continue
            if fresh:
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


@app.post("/api/runs/{run_id}/answer")
async def run_answer(run_id: str, body: AnswerRequest, x_access_code: Optional[str] = Header(default=None)) -> dict:
    _require_code(x_access_code)
    fut = _get_run(run_id).pending.get(body.ask_id)
    if fut is None or fut.done():
        raise HTTPException(404, "No open question with that id (it may have timed out).")
    fut.set_result((body.answer or "")[:2000])
    return {"ok": True}


@app.post("/api/runs/{run_id}/answer_all")
async def run_answer_all(run_id: str, body: AnswerAllRequest,
                         x_access_code: Optional[str] = Header(default=None)) -> dict:
    _require_code(x_access_code)
    run = _get_run(run_id)
    n = 0
    for aid in body.ask_ids:
        fut = run.pending.get(aid)
        if fut is not None and not fut.done():
            fut.set_result((body.answer or "")[:2000])
            n += 1
    return {"resolved": n}


@app.get("/api/runs/{run_id}/log")
async def run_log(run_id: str):
    from fastapi.responses import FileResponse
    f = _run_dir(run_id) / "log.jsonl"
    if not f.is_file():
        raise HTTPException(404, "No log for this run.")
    return FileResponse(f, media_type="application/x-ndjson",
                        headers={"Content-Disposition": f'attachment; filename="{run_id}.jsonl"'})


@app.post("/api/runs/{run_id}/cancel")
async def run_cancel(run_id: str, x_access_code: Optional[str] = Header(default=None)) -> dict:
    _require_code(x_access_code)
    run = _get_run(run_id)
    if run.status != "running" or run.task is None:
        raise HTTPException(409, "Run is not running.")
    run.task.cancel()
    return {"ok": True}


def _run_dir(run_id: str) -> Path:
    if not _RUN_ID_RE.match(run_id):
        raise HTTPException(404, "Unknown run.")
    return Path(get_cfg().runs_dir) / run_id


@app.post("/api/runs/{run_id}/resume")
async def run_resume(run_id: str, x_access_code: Optional[str] = Header(default=None)) -> JSONResponse:
    _require_code(x_access_code)
    from webforge.agent.planner import load_checkpoint
    cp = load_checkpoint(_run_dir(run_id))
    if not cp:
        raise HTTPException(404, "This run has no checkpoint to resume from.")
    if cp.get("status") == "done":
        raise HTTPException(409, "This run already finished.")
    return _start(RunRequest(task=cp["task"]), resume=cp, parent=run_id)


@app.get("/api/runs/{run_id}/files")
async def run_files(run_id: str) -> dict:
    out = _run_dir(run_id) / "outputs"
    files = sorted(str(p.relative_to(out)) for p in out.rglob("*") if p.is_file()) if out.is_dir() else []
    return {"files": [{"name": f, "size": (out / f).stat().st_size} for f in files]}


@app.get("/api/runs/{run_id}/files/{path:path}")
async def run_file(run_id: str, path: str):
    from fastapi.responses import FileResponse
    out = (_run_dir(run_id) / "outputs").resolve()
    f = (out / path).resolve()
    if out not in f.parents or not f.is_file():
        raise HTTPException(404, "No such file.")
    return FileResponse(f, filename=f.name)


@app.get("/api/secrets")
async def secrets_list() -> dict:
    return {"names": STATE.get_secrets().names()}


@app.post("/api/secrets")
async def secrets_set(body: SecretRequest, request: Request,
                      x_access_code: Optional[str] = Header(default=None)) -> dict:
    _require_key_admin(request, x_access_code)
    if not _NAME_RE.match(body.name or "") or not body.value or len(body.value) > 500:
        raise HTTPException(422, "Give a name (letters, digits, _ - .) and a value (max 500 chars).")
    STATE.get_secrets().set(body.name, body.value)   # process memory only: never written to disk or events
    return {"names": STATE.get_secrets().names()}


# ---------------------------------------------------------------- static ---

if WEB_DIR.is_dir():
    app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="web")


def serve(host: str = "127.0.0.1", port: int = 8000) -> None:
    import uvicorn
    uvicorn.run(app, host=host, port=port, log_level="info")
