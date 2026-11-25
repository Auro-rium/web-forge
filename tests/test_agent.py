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


async def test_planner_answers_general_question_with_web_search(tmp_path):
    """General questions (weather, news, recommendations) go to Backboard's web search, not the browser."""
    events = []
    llm = ScriptedMock({
        "planner": [
            {"tool_calls": [{"name": "web_search", "args": {"query": "weather in London today"}}]},
            lambda **kw: {"tool_calls": [{"name": "final_answer",
                                            "args": {"answer": json.loads(kw["message"])[0]["output"]}}]},
        ],
        "searcher": [{"text": "Overcast, 8°C. Source: https://weather.metoffice.gov.uk/"}],
    })
    cfg = Config(data_dir=tmp_path, runs_dir=tmp_path / "runs", ledger_file=tmp_path / "spend.json")
    r = await run_task("What's the weather in London today?", llm=llm, browser=None,
                       library=SkillLibrary(tmp_path / "sk"), memory=LocalMemory(path=tmp_path / "m.jsonl"),
                       cfg=cfg, learn=False, on_event=lambda k, d: events.append((k, d)))
    assert r.ok and "8°C" in r.answer and "metoffice" in r.answer
    assert r.steps == 0                                   # no browser actions were needed
    assert ("search", {"query": "weather in London today"}) in events


def test_reflector_drops_transient_network_lessons():
    from webforge.agent.reflector import is_transient
    assert is_transient("python.org may be unreachable from some browsers")
    assert is_transient("Direct goto to Met Office failed with net::ERR_TUNNEL_CONNECTION_FAILED")
    assert is_transient("Direct navigation to Best Buy search URLs failed; use web search")
    assert not is_transient("Use the Wikipedia infobox 'Stable release' field for the latest version")
    assert not is_transient("Books are listed as article.product_pod; price in .price_color")


def _cfg(tmp_path, **kw):
    return Config(data_dir=tmp_path, runs_dir=tmp_path / "runs", ledger_file=tmp_path / "spend.json", **kw)


async def test_parallel_browse_runs_workers_concurrently_in_own_tabs(base_url, br, tmp_path):
    """Three independent subtasks -> three workers, three tabs, results merged for the planner."""
    import asyncio
    from webforge.llm import MockLLM
    live = {"now": 0, "peak": 0}
    pages = {"w-books": base_url + "/index.html", "w-wiki": base_url + "/wiki/index.html",
             "w-data": base_url + "/data/specs.html"}

    def handler(role, message, thread_id, tools, system, kind):
        if role == "planner" and kind == "chat":
            return {"tool_calls": [{"name": "parallel_browse", "args": {"subtasks": [
                {"subtask": f"report title of {k}", "start_url": u} for k, u in pages.items()]}}]}
        if role == "planner":
            return {"tool_calls": [{"name": "final_answer", "args": {"answer": message}}]}
        if role == "worker":
            title = re.search(r"TITLE: (.*)", message).group(1)
            return {"tool_calls": [{"name": "done", "args": {"success": True, "summary": "ok", "data": {"title": title}}}]}
        return "{}"

    llm = MockLLM(handler)
    orig_new_tab = br.new_tab

    async def counting_new_tab():
        tab = await orig_new_tab()
        live["now"] += 1
        live["peak"] = max(live["peak"], live["now"])
        orig_close = tab.close

        async def close():
            await asyncio.sleep(0)
            live["now"] -= 1
            await orig_close()
        tab.close = close
        return tab
    br.new_tab = counting_new_tab
    events = []
    r = await run_task("compare three pages", llm=llm, browser=br, library=SkillLibrary(tmp_path / "sk"),
                       memory=LocalMemory(path=tmp_path / "m.jsonl"), cfg=_cfg(tmp_path), learn=False,
                       on_event=lambda k, d: events.append((k, d)))
    assert r.ok and "3/3 subtasks succeeded" in r.answer
    for t in ("Mini Wiki", "Specs - Mini Data"):
        assert t in r.answer
    assert live["peak"] == 3 and live["now"] == 0            # three tabs open at once, all closed after
    agents = {d.get("agent") for k, d in events if k == "delegate"}
    assert agents == {"w1", "w2", "w3"}
    assert br.page.url == "about:blank"                       # default tab untouched


async def test_read_urls_parallel_and_offloads_to_files(base_url, br, tmp_path):
    llm = ScriptedMock({"planner": [
        {"tool_calls": [{"name": "read_urls", "args": {"urls": [base_url + "/data/specs.html",
                                                                 base_url + "/wiki/index.html"]}}]},
        lambda **kw: {"tool_calls": [{"name": "final_answer", "args": {"answer": kw["message"]}}]},
    ]})
    r = await run_task("read two pages", llm=llm, browser=br, library=SkillLibrary(tmp_path / "sk"),
                       memory=LocalMemory(path=tmp_path / "m.jsonl"), cfg=_cfg(tmp_path), learn=False)
    assert r.ok and "Planet data" in r.answer
    files = [n for n in r.notes if n.startswith("pages/")]
    assert len(files) == 2 and any("| Mars | 2 | 24.6 hours |" in r.notes[n] for n in files)


async def test_large_output_offloaded_and_paged(tmp_path):
    big = "x" * 9000 + "THE-END"
    llm = ScriptedMock({
        "planner": [
            {"tool_calls": [{"name": "web_search", "args": {"queries": ["big"]}}]},
            {"tool_calls": [{"name": "read_file", "args": {"name": "results/search-1.md", "offset": 8990}}]},
            lambda **kw: {"tool_calls": [{"name": "final_answer", "args": {"answer": kw["message"]}}]},
        ],
        "searcher": [{"text": big}],
    })
    r = await run_task("q", llm=llm, browser=None, library=SkillLibrary(tmp_path / "sk"),
                       memory=LocalMemory(path=tmp_path / "m.jsonl"), cfg=_cfg(tmp_path), learn=False)
    assert r.notes["results/search-1.md"] == big
    submit = next(e for e in llm.log if e["role"] == "planner" and e["kind"] == "submit")
    first_output = json.loads(submit["message"])[0]["output"]
    assert "saved to file 'results/search-1.md'" in first_output and len(first_output) < 3000
    assert "THE-END" in r.answer


async def test_web_search_many_queries_in_one_call(tmp_path):
    events = []
    llm = ScriptedMock({
        "planner": [
            {"tool_calls": [{"name": "web_search", "args": {"queries": ["python latest", "node latest", "go latest"]}}]},
            lambda **kw: {"tool_calls": [{"name": "final_answer", "args": {"answer": kw["message"]}}]},
        ],
        "searcher": [{"text": "A"}, {"text": "B"}, {"text": "C"}],
    })
    r = await run_task("versions", llm=llm, browser=None, library=SkillLibrary(tmp_path / "sk"),
                       memory=LocalMemory(path=tmp_path / "m.jsonl"), cfg=_cfg(tmp_path), learn=False,
                       on_event=lambda k, d: events.append((k, d)))
    assert r.ok and all(f"### {q}" in r.answer for q in ("python latest", "node latest", "go latest"))
    assert sum(1 for k, _ in events if k == "search") == 3


async def test_research_budget_forces_an_answer(tmp_path):
    """A planner that keeps searching is told to answer after the soft budget and refused 3 rounds later."""
    searches = [{"tool_calls": [{"name": "web_search", "args": {"queries": [f"q{i}"]}}]} for i in range(10)]
    llm = ScriptedMock({
        "planner": searches[:6] + [
            lambda **kw: {"tool_calls": [{"name": "web_search", "args": {"queries": ["again"]}}]}] * 3 + [
            lambda **kw: {"tool_calls": [{"name": "final_answer", "args": {"answer": kw["message"][-300:]}}]}],
        "searcher": [{"text": f"result {i}"} for i in range(20)],
    })
    cfg = _cfg(tmp_path)
    cfg.planner_soft_rounds = 3
    r = await run_task("q", llm=llm, browser=None, library=SkillLibrary(tmp_path / "sk"),
                       memory=LocalMemory(path=tmp_path / "m.jsonl"), cfg=cfg, learn=False)
    msgs = [e["message"] for e in llm.log if e["role"] == "planner" and e["kind"] == "submit"]
    assert any("[budget]" in m for m in msgs)
    assert "research budget exhausted" in r.answer
    assert sum(1 for e in llm.log if e["role"] == "searcher") == 5   # rounds 1-5 searched, 6+ refused
