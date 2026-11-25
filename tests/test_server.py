import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from webforge import server
from webforge.config import Config


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    c = Config()
    c.data_dir = tmp_path / "data"
    c.runs_dir = tmp_path / "runs"
    c.ledger_file = c.data_dir / "spend.json"
    c.budget_usd = 1.0
    c.api_key = "env-test-key-0000"
    c.runs_dir.mkdir()
    c.data_dir.mkdir()
    monkeypatch.setattr(server, "get_cfg", lambda: c)
    monkeypatch.delenv("WEBFORGE_ACCESS_CODE", raising=False)
    monkeypatch.delenv("WEBFORGE_MOCK", raising=False)
    monkeypatch.delenv("WEBFORGE_LOCAL", raising=False)
    server._KEY_ATTEMPTS.clear()
    server.STATE.runs.clear()
    server.STATE.active = None
    server.STATE.secrets = None
    return c


@pytest.fixture
def client(cfg):
    with TestClient(server.app) as c:
        yield c


def _stub(release: asyncio.Event | None = None):
    async def fake(run, cfg):
        await server._push(run, "task_start", {"task": run.req.task})
        if release is not None:
            await release.wait()
        await server._push(run, "final", {"answer": "42", "ok": True})
        run.result = {"task": run.req.task, "answer": "42", "ok": True, "steps": 1}
        await server._finish(run, "done")
    return fake


def test_health_and_spend(client, cfg):
    h = client.get("/api/health").json()
    assert h["ok"] is True and h["budget_usd"] == 1.0 and "browser" in h and h["mock"] is False
    assert "api_key" not in json.dumps(h).lower()
    assert client.get("/api/spend").json()["budget_usd"] == 1.0
    for gone in ("/api/tasks", "/api/skills", "/api/memory", "/api/bench"):
        assert client.get(gone).status_code in (404, 405)


def test_run_validation(client, monkeypatch):
    monkeypatch.setattr(server, "_execute", _stub())
    assert client.post("/api/run", json={"task": "   "}).status_code == 422
    assert client.post("/api/run", json={"task": "x" * 501}).status_code == 422
    assert client.post("/api/run", json={"task": "bad\x00task"}).status_code == 422


def test_budget_exhausted(client, cfg, monkeypatch):
    monkeypatch.setattr(server, "_execute", _stub())
    (cfg.data_dir / "spend.json").write_text(json.dumps({"total_usd": 5.0}))
    assert client.post("/api/run", json={"task": "hello"}).status_code == 402


def test_access_code_required(client, monkeypatch):
    monkeypatch.setenv("WEBFORGE_ACCESS_CODE", "sesame")
    monkeypatch.setattr(server, "_execute", _stub())
    assert client.get("/api/health").json()["needs_code"] is True
    assert client.post("/api/run", json={"task": "hi"}).status_code == 401
    assert client.post("/api/run", json={"task": "hi"}, headers={"X-Access-Code": "nope"}).status_code == 401
    r = client.post("/api/run", json={"task": "hi"}, headers={"X-Access-Code": "sesame"})
    assert r.status_code == 202 and r.json()["run_id"]
    rid = r.json()["run_id"]
    assert client.post(f"/api/runs/{rid}/answer", json={"ask_id": "q", "answer": "approve"}).status_code == 401
    assert client.post(f"/api/runs/{rid}/cancel").status_code == 401
    assert client.post(f"/api/runs/{rid}/resume").status_code == 401
    assert client.post("/api/secrets", json={"name": "a", "value": "b"}).status_code == 403


def test_run_events_sse_and_info(client, monkeypatch):
    monkeypatch.setattr(server, "_execute", _stub())
    rid = client.post("/api/run", json={"task": "say 42"}).json()["run_id"]
    with client.stream("GET", f"/api/runs/{rid}/events") as r:
        assert r.headers["content-type"].startswith("text/event-stream")
        body = "".join(r.iter_text())
    assert '"kind": "task_start"' in body and "event: done" in body and '"answer": "42"' in body
    info = client.get(f"/api/runs/{rid}").json()
    assert info["status"] == "done" and info["result"]["answer"] == "42" and info["n_events"] == 2
    assert client.get("/api/runs/nope/events").status_code == 404


def test_concurrent_run_409(client, monkeypatch):
    # the stub blocks until released, so the second request must be refused
    ev = asyncio.Event()
    monkeypatch.setattr(server, "_execute", _stub(ev))
    r1 = client.post("/api/run", json={"task": "first"})
    assert r1.status_code == 202
    r2 = client.post("/api/run", json={"task": "second"})
    assert r2.status_code == 409 and "in progress" in r2.json()["detail"]
    ev.set()
    rid = r1.json()["run_id"]
    with client.stream("GET", f"/api/runs/{rid}/events") as r:
        "".join(r.iter_text())
    assert client.post("/api/run", json={"task": "third"}).status_code == 202


def test_run_persists_result(client, cfg, monkeypatch):
    monkeypatch.setattr(server, "_execute", _stub())
    rid = client.post("/api/run", json={"task": "persist me"}).json()["run_id"]
    with client.stream("GET", f"/api/runs/{rid}/events") as r:
        "".join(r.iter_text())
    for _ in range(50):
        if (cfg.runs_dir / rid / "result.json").exists():
            break
        import time
        time.sleep(0.05)
    assert json.loads((cfg.runs_dir / rid / "result.json").read_text())["answer"] == "42"
    assert (cfg.runs_dir / rid / "events.json").exists()


def _wait(pred, n=100):
    import time
    for _ in range(n):
        if pred():
            return True
        time.sleep(0.03)
    return False


def test_ask_answer_roundtrip(client, monkeypatch):
    async def fake(run, cfg):
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        run.pending["q1"] = fut
        await server._push(run, "ask_id", {"ask_id": "q1", "question": "Approve?", "options": ["approve", "deny"]})
        ans = await fut
        run.result = {"answer": "user said " + ans, "ok": True}
        await server._finish(run, "done")
    monkeypatch.setattr(server, "_execute", fake)
    rid = client.post("/api/run", json={"task": "needs approval"}).json()["run_id"]
    assert _wait(lambda: "q1" in client.get(f"/api/runs/{rid}").json()["pending"])
    assert client.post(f"/api/runs/{rid}/answer", json={"ask_id": "nope", "answer": "x"}).status_code == 404
    assert client.post(f"/api/runs/{rid}/answer", json={"ask_id": "q1", "answer": "approve"}).json() == {"ok": True}
    assert _wait(lambda: client.get(f"/api/runs/{rid}").json()["status"] == "done")
    assert client.get(f"/api/runs/{rid}").json()["result"]["answer"] == "user said approve"


def test_cancel_run(client, monkeypatch):
    monkeypatch.setattr(server, "_execute", _stub(asyncio.Event()))   # never released
    rid = client.post("/api/run", json={"task": "long"}).json()["run_id"]
    assert client.post(f"/api/runs/{rid}/cancel").json() == {"ok": True}
    assert _wait(lambda: client.get(f"/api/runs/{rid}").json()["status"] == "cancelled")
    assert client.post(f"/api/runs/{rid}/cancel").status_code == 409


def test_secrets_names_only_and_redacted_from_events(client, monkeypatch):
    r = client.post("/api/secrets", json={"name": "site_pw", "value": "hunter2-secret"})
    assert r.json() == {"names": ["site_pw"]}
    assert client.get("/api/secrets").json() == {"names": ["site_pw"]}
    assert client.post("/api/secrets", json={"name": "bad name!", "value": "x"}).status_code == 422

    async def leaky(run, cfg):
        await server._push(run, "action", {"message": "typed hunter2-secret"})
        await server._finish(run, "done")
    monkeypatch.setattr(server, "_execute", leaky)
    rid = client.post("/api/run", json={"task": "t"}).json()["run_id"]
    with client.stream("GET", f"/api/runs/{rid}/events") as resp:
        body = "".join(resp.iter_text())
    assert "hunter2" not in body and "{{secret:site_pw}}" in body


def test_files_endpoint_serves_outputs_only(client, cfg):
    out = cfg.runs_dir / "web_x" / "outputs"
    out.mkdir(parents=True)
    (out / "report.md").write_text("# hi")
    (cfg.runs_dir / "web_x" / "checkpoint.json").write_text("{}")
    assert client.get("/api/runs/web_x/files").json() == {"files": [{"name": "report.md", "size": 4}]}
    assert client.get("/api/runs/web_x/files/report.md").text == "# hi"
    assert client.get("/api/runs/web_x/files/..%2Fcheckpoint.json").status_code == 404
    assert client.get("/api/runs/web_x/files/../checkpoint.json").status_code == 404
    assert client.get("/api/runs/bad..id/files").status_code == 404


def test_resume_endpoint(client, cfg, monkeypatch):
    seen = {}

    async def fake(run, cfg_):
        seen["resume"] = run.resume
        await server._finish(run, "done")
    monkeypatch.setattr(server, "_execute", fake)
    d = cfg.runs_dir / "web_old"
    d.mkdir()
    assert client.post("/api/runs/web_old/resume").status_code == 404
    (d / "checkpoint.json").write_text(json.dumps({"task": "finish me", "status": "error", "workspace": {"a": "b"}}))
    r = client.post("/api/runs/web_old/resume")
    assert r.status_code == 202 and r.json()["run_id"] == "web_old"
    assert _wait(lambda: seen.get("resume", {}).get("workspace") == {"a": "b"})
    (d / "checkpoint.json").write_text(json.dumps({"task": "x", "status": "done"}))
    assert _wait(lambda: server.STATE.active is None)
    assert client.post("/api/runs/web_old/resume").status_code == 409


def test_cors_defaults(monkeypatch):
    monkeypatch.delenv("WEBFORGE_CORS_ORIGINS", raising=False)
    monkeypatch.delenv("WEBFORGE_ACCESS_CODE", raising=False)
    assert server._cors_origins() == ["*"]
    monkeypatch.setenv("WEBFORGE_ACCESS_CODE", "x")
    assert server._cors_origins() == []
    monkeypatch.setenv("WEBFORGE_CORS_ORIGINS", "https://a.vercel.app/, https://b.dev")
    assert server._cors_origins() == ["https://a.vercel.app", "https://b.dev"]


# ------------------------------------------------------------ key settings ---

GOOD_KEY = "bb_test_1234567890abcdef"


def _ok_verify(monkeypatch):
    async def fake(key, cfg):
        return None
    monkeypatch.setattr(server, "verify_key", fake)


def test_health_key_fields_never_leak(client, cfg):
    h = client.get("/api/health").json()
    assert h["has_key"] is True and h["key_source"] == "env" and h["needs_key"] is False
    assert "local_mode" in h and cfg.api_key not in json.dumps(h)
    cfg.api_key = ""
    h = client.get("/api/health").json()
    assert (h["has_key"], h["key_source"], h["needs_key"]) == (False, "none", True)
    cfg.api_key = "proxy-injected"
    h = client.get("/api/health").json()
    assert h["key_source"] == "proxy" and h["needs_key"] is False


def test_save_key_writes_0600_and_health_reports_saved(client, cfg, monkeypatch):
    cfg.api_key = ""
    _ok_verify(monkeypatch)
    r = client.post("/api/settings/key", json={"api_key": "  " + GOOD_KEY + "\n"})
    assert r.status_code == 200 and r.json() == {"ok": True, "key_source": "saved"}
    f = cfg.data_dir / "backboard_key"
    assert f.read_text() == GOOD_KEY and (f.stat().st_mode & 0o777) == 0o600
    h = client.get("/api/health").json()
    assert h["key_source"] == "saved" and h["has_key"] and not h["needs_key"]
    assert GOOD_KEY not in json.dumps(h) and GOOD_KEY not in r.text


def test_key_resolution_order(cfg):
    (cfg.data_dir / "backboard_key").write_text("saved-key-0000000")
    cfg.api_key = "proxy-injected"
    assert server.resolve_key(cfg) == ("saved-key-0000000", "saved")   # saved beats the placeholder
    cfg.api_key = "real-env-key-0000"
    assert server.resolve_key(cfg) == ("real-env-key-0000", "env")     # real env key beats saved
    assert server._cfg_with_key(cfg).api_key == "real-env-key-0000"


def test_save_key_invalid_shape(client, cfg, monkeypatch):
    _ok_verify(monkeypatch)
    for bad in ("", "short", "has space inside key", "x" * 301, "ctrl\x01char-in-key-1"):
        assert client.post("/api/settings/key", json={"api_key": bad}).status_code == 422
    assert not (cfg.data_dir / "backboard_key").exists()


def test_save_key_rejected_and_unreachable_not_saved(client, cfg, monkeypatch):
    async def rejected(key, cfg):
        raise server.KeyRejected()
    monkeypatch.setattr(server, "verify_key", rejected)
    r = client.post("/api/settings/key", json={"api_key": GOOD_KEY})
    assert r.status_code == 400 and r.json()["error"] == "Backboard rejected this key"

    async def down(key, cfg):
        raise server.KeyUnverifiable(f"ConnectError: boom {key}")
    monkeypatch.setattr(server, "verify_key", down)
    r = client.post("/api/settings/key", json={"api_key": GOOD_KEY})
    assert r.status_code == 502 and GOOD_KEY not in r.text
    assert not (cfg.data_dir / "backboard_key").exists()


def test_key_endpoints_need_access_code_when_set(client, cfg, monkeypatch):
    _ok_verify(monkeypatch)
    monkeypatch.setenv("WEBFORGE_ACCESS_CODE", "sesame")
    assert client.post("/api/settings/key", json={"api_key": GOOD_KEY}).status_code == 403
    assert client.post("/api/settings/key", json={"api_key": GOOD_KEY},
                       headers={"X-Access-Code": "nope"}).status_code == 403
    assert client.delete("/api/settings/key").status_code == 403
    assert client.post("/api/settings/key", json={"api_key": GOOD_KEY},
                       headers={"X-Access-Code": "sesame"}).status_code == 200


def test_key_endpoint_remote_client_needs_local_mode(cfg, monkeypatch):
    _ok_verify(monkeypatch)
    with TestClient(server.app, client=("203.0.113.9", 5555)) as c:
        assert c.post("/api/settings/key", json={"api_key": GOOD_KEY}).status_code == 403
        monkeypatch.setenv("WEBFORGE_LOCAL", "1")
        assert c.post("/api/settings/key", json={"api_key": GOOD_KEY}).status_code == 200


def test_delete_key_and_rate_limit(client, cfg, monkeypatch):
    cfg.api_key = ""
    _ok_verify(monkeypatch)
    client.post("/api/settings/key", json={"api_key": GOOD_KEY})
    r = client.delete("/api/settings/key")
    assert r.status_code == 200 and r.json()["key_source"] == "none"
    assert not (cfg.data_dir / "backboard_key").exists()
    assert client.delete("/api/settings/key").status_code == 200   # idempotent
    for _ in range(server.KEY_ATTEMPTS_PER_MIN):
        client.post("/api/settings/key", json={"api_key": "short"})
    assert client.post("/api/settings/key", json={"api_key": GOOD_KEY}).status_code == 429


def test_run_without_key_is_428(client, cfg, monkeypatch):
    monkeypatch.setattr(server, "_execute", _stub())
    cfg.api_key = ""
    r = client.post("/api/run", json={"task": "hello"})
    assert r.status_code == 428 and r.json() == {"error": "Backboard API key not set", "needs_key": True}
    assert server.STATE.active is None
    monkeypatch.setenv("WEBFORGE_MOCK", "1")      # mock mode never needs a key
    assert client.post("/api/run", json={"task": "hello"}).status_code == 202


def test_sse_does_not_skip_events_when_screenshots_replace_each_other(client, monkeypatch):
    """Dropping an older screenshot must not shift the stream position and swallow the next event (e.g. the ask card)."""
    async def fake(run, cfg):
        for i in range(6):
            await server._push(run, "screenshot", {"agent": "w1", "jpeg_b64": f"frame{i}"})
            await server._push(run, "action", {"message": f"step {i}"})
            await asyncio.sleep(0.02)
        await server._push(run, "ask_id", {"ask_id": "q1", "question": "Approve?"})
        await server._finish(run, "done")
    monkeypatch.setattr(server, "_execute", fake)
    rid = client.post("/api/run", json={"task": "t"}).json()["run_id"]
    with client.stream("GET", f"/api/runs/{rid}/events") as resp:
        body = "".join(resp.iter_text())
    assert body.count('"step ') == 6 and '"ask_id": "q1"' in body
    assert "frame5" in body


def test_sse_survives_event_cap(client, monkeypatch):
    monkeypatch.setattr(server, "MAX_EVENTS", 10)

    async def fake(run, cfg):
        for i in range(40):
            await server._push(run, "action", {"message": f"s{i}"})
        await server._push(run, "final", {"answer": "ok", "ok": True})
        await server._finish(run, "done")
    monkeypatch.setattr(server, "_execute", fake)
    rid = client.post("/api/run", json={"task": "t"}).json()["run_id"]
    with client.stream("GET", f"/api/runs/{rid}/events") as resp:
        body = "".join(resp.iter_text())
    assert '"answer": "ok"' in body
