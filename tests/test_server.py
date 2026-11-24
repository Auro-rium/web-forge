import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from webforge import server
from webforge.config import Config
from webforge.contracts import Skill, SkillStep
from webforge.skills.library import SkillLibrary


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


def test_health_and_tasks(client, cfg):
    h = client.get("/api/health").json()
    assert h["ok"] is True and h["budget_usd"] == 1.0 and "browser" in h and h["mock"] is False
    assert "api_key" not in json.dumps(h).lower()
    t = client.get("/api/tasks").json()["tasks"]
    assert any(x["id"] == "books_poetry" for x in t) and all(x["prompt"] for x in t)
    assert client.get("/api/spend").json()["budget_usd"] == 1.0


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
    assert client.delete("/api/skills/x").status_code == 401
    assert client.post("/api/memory/reset").status_code == 401


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


def test_skills_endpoints(client, cfg):
    lib = SkillLibrary(cfg.skills_dir)
    lib.save(Skill(name="list_books", domain="books.toscrape.com", description="d", params=["category"],
                   steps=[SkillStep("goto", None, "https://books.toscrape.com/"),
                          SkillStep("click", {"role": "link", "name": "{category}"})], uses=2, successes=2))
    lst = client.get("/api/skills").json()["skills"]
    assert lst[0]["name"] == "list_books" and lst[0]["n_steps"] == 2 and lst[0]["uses"] == 2
    full = client.get("/api/skills/list_books").json()
    assert len(full["steps"]) == 2
    assert client.get("/api/skills/..%2Fetc").status_code == 404
    assert client.get("/api/skills/missing").status_code == 404
    assert client.delete("/api/skills/list_books").json() == {"deleted": "list_books"}
    assert client.get("/api/skills").json()["skills"] == []


def test_memory_endpoints(client, cfg, monkeypatch):
    monkeypatch.setenv("WEBFORGE_MOCK", "1")
    cfg.memory_mirror.write_text("\n".join(json.dumps({"content": f"lesson {i}", "metadata": {"kind": "lesson"}})
                                           for i in range(3)) + "\n")
    m = client.get("/api/memory").json()
    assert m["total"] == 3 and m["memories"][0]["content"] == "lesson 2"
    assert client.post("/api/memory/reset").json() == {"reset": True}
    assert client.get("/api/memory").json()["total"] == 0


def test_bench_endpoint(client, cfg):
    assert client.get("/api/bench").json()["cold"] == []
    row = {"id": "t1", "family": "f", "mode": "cold", "correct": True, "steps": 6, "tokens": 100, "seconds": 10.0,
           "cost_usd": 0.01}
    (cfg.runs_dir / "bench_cold_20250101_000000.jsonl").write_text(json.dumps(row) + "\n")
    (cfg.runs_dir / "bench_cold_20250102_000000.jsonl").write_text(json.dumps({**row, "steps": 8}) + "\n")
    (cfg.runs_dir / "bench_learned_20250102_000000.jsonl").write_text(
        json.dumps({**row, "mode": "learned", "steps": 4}) + "\n")
    b = client.get("/api/bench").json()
    assert b["cold_file"] == "bench_cold_20250102_000000.jsonl" and b["cold"][0]["steps"] == 8
    assert b["learned"][0]["steps"] == 4
    assert b["aggregate"]["overall"]["reduction"]["steps"] == pytest.approx(50.0)


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
