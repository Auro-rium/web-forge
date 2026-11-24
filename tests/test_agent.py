import functools
import http.server
import json
import re
import threading
from pathlib import Path

import pytest

from webforge.agent.planner import run_task
from webforge.browser.session import Browser
from webforge.config import Config
from webforge.contracts import El
from webforge.llm import ScriptedMock
from webforge.memory import LocalMemory
from webforge.safety import check_action, domain_allowed
from webforge.skills.library import SkillLibrary

SITE = Path(__file__).resolve().parent.parent / "fixtures" / "site"


@pytest.fixture(scope="module")
def base_url():
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(SITE))
    handler.log_message = lambda *a, **k: None
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


@pytest.fixture
async def br():
    b = Browser(Config().chromium_path)
    await b.start()
    yield b
    await b.close()


def test_check_action():
    els = [El(0, "a", "link", "Poetry", {}), El(1, "input", "textbox", "Password", {}),
           El(2, "button", "button", "Place Order", {}), El(3, "input", "textbox", "Search", {})]
    allowed = ["books.toscrape.com"]
    assert check_action("goto", {"url": "https://books.toscrape.com/x"}, els, allowed)[0]
    assert not check_action("goto", {"url": "https://evil.com/"}, els, allowed)[0]
    assert check_action("goto", {"url": "http://127.0.0.1:8000/"}, els, allowed)[0]
    assert not check_action("type", {"idx": 1, "text": "x"}, els, allowed)[0]
    assert check_action("type", {"idx": 3, "text": "x"}, els, allowed)[0]
    assert not check_action("click", {"idx": 2}, els, allowed)[0]
    assert check_action("click", {"idx": 0}, els, allowed)[0]
    assert domain_allowed("https://sub.books.toscrape.com", allowed)


def find_idx(message, role, name):
    m = re.search(r"\[(\d+)\] %s '%s'" % (role, re.escape(name)), message)
    assert m, message[:500]
    return int(m.group(1))


async def test_end_to_end_learn_then_reuse(base_url, br, tmp_path):
    cfg = Config()
    cfg.data_dir = tmp_path
    lib = SkillLibrary(tmp_path / "skills")
    mem = LocalMemory(cfg, path=tmp_path / "mem.jsonl")
    start = base_url + "/index.html"

    skill_json = {
        "name": "list_books_in_category", "description": "List books (title, price) in a category",
        "params": ["category"], "example_params": {"category": "Poetry"},
        "steps": [{"action": "goto", "locator": None, "value": start},
                  {"action": "click", "locator": {"role": "link", "name": "{category}"}, "value": None}],
        "extract": {"type": "css", "selector": "article.product_pod",
                    "fields": {"title": "h3 a@title", "price": ".price_color"}},
    }

    def worker_click(**kw):
        return {"tool_calls": [{"name": "click", "args": {"idx": find_idx(kw["message"], "link", "Poetry")}}]}

    llm = ScriptedMock({
        "planner": [
            {"tool_calls": [{"name": "write_todos", "args": {"todos": [{"task": "list poetry", "status": "doing"}]}},
                            {"name": "delegate_browse",
                             "args": {"subtask": "List books in the Poetry category", "start_url": start}}]},
            {"tool_calls": [{"name": "final_answer", "args": {"answer": "Poetry: Sharp Objects of Verse, ..."}}]},
        ],
        "worker": [
            worker_click,
            {"tool_calls": [{"name": "extract", "args": {"css": "article.product_pod",
                                                         "fields": {"title": "h3 a@title", "price": ".price_color"}}}]},
            {"tool_calls": [{"name": "done", "args": {"success": True, "summary": "found books", "data": []}}]},
        ],
        "builder": [json.dumps(skill_json)],
        "reflector": [json.dumps({"lessons": [{"domain": "127.0.0.1", "text": "Categories are links in the sidebar."}]})],
    })
    events = []
    r1 = await run_task("list books in the Poetry category", llm=llm, browser=br, library=lib, memory=mem,
                        cfg=cfg, on_event=lambda k, d: events.append(k), run_dir=tmp_path / "run1")
    assert r1.ok, r1.answer
    assert r1.skills_created == ["list_books_in_category"]
    assert lib.get("list_books_in_category") is not None
    assert any((m["metadata"].get("kind") == "lesson") for m in await mem.list())
    assert {"task_start", "todos", "delegate", "action", "skill_built", "lesson", "final"} <= set(events)
    assert r1.steps >= 3 and r1.llm_calls > 0
    n_worker_calls = sum(1 for e in llm.log if e["role"] == "worker")

    # ---- run 2: planner reuses the skill, no worker
    llm2 = ScriptedMock({
        "planner": [
            {"tool_calls": [{"name": "run_skill", "args": {"name": "list_books_in_category",
                                                           "params": {"category": "Travel"}}}]},
            lambda **kw: {"tool_calls": [{"name": "final_answer", "args": {"answer": kw["message"]}}]},
        ],
        "reflector": [json.dumps({"lessons": [{"domain": "127.0.0.1", "text": "Skill works."}]})],
    })
    r2 = await run_task("list books in the Travel category", llm=llm2, browser=br, library=lib, memory=mem,
                        cfg=cfg, learn=True)
    assert r2.ok
    assert r2.skills_used == ["list_books_in_category"]
    assert r2.steps < r1.steps
    assert not any(e["role"] == "worker" for e in llm2.log) and n_worker_calls > 0
    out = json.loads(r2.answer)[0]["output"]
    assert out.startswith("OK")
    assert out.count("title") == 3 and "Lonely Atlas" in out
    assert lib.get("list_books_in_category").uses == 1
