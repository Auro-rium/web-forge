"""Safety policy, worker approvals and the planner's autonomy features (offline: MockLLM + fixture site)."""
import asyncio
import json
import re

from webforge.agent.planner import load_checkpoint, run_task
from webforge.agent.worker import run_worker
from webforge.contracts import El
from webforge.llm import MockLLM, ScriptedMock
from webforge.safety import ALLOW, ASK, BLOCK, Secrets, check_action, domain_allowed


def final(**kw):
    return {"tool_calls": [{"name": "final_answer", "args": {"answer": kw["message"]}}]}


def idx_of(message: str, name: str) -> int:
    m = re.search(r"\[(\d+)\] \w+ '%s'" % re.escape(name), message)
    assert m, message[:800]
    return int(m.group(1))


# ----------------------------------------------------------------- safety ---

def test_safety_verdicts():
    els = [El(0, "a", "link", "Docs", {}), El(1, "input", "textbox", "Password", {"css": "#pw"}),
           El(2, "button", "button", "Place order", {}), El(3, "input", "searchbox", "Search", {}),
           El(4, "button", "button", "Submit registration", {"role": "button", "submit": True}),
           El(5, "input", "textbox", "Card number", {}), El(6, "input", "textbox", "Full name", {})]
    assert check_action("click", {"idx": 0}, els, None)[0] == ALLOW
    assert check_action("click", {"idx": 4}, els, None)[0] == ASK
    assert check_action("click", {"idx": 2}, els, None)[0] == BLOCK
    assert check_action("type", {"idx": 5, "text": "4111"}, els, None)[0] == BLOCK
    assert check_action("type", {"idx": 1, "text": "x"}, els, None)[0] == ASK
    assert check_action("type", {"idx": 3, "text": "cats", "submit": True}, els, None)[0] == ALLOW
    assert check_action("type", {"idx": 6, "text": "Ada", "submit": True}, els, None)[0] == ASK
    assert check_action("fill_form", {"fields": {"Full name": "Ada"}}, els, None)[0] == ALLOW
    assert check_action("fill_form", {"fields": {"Full name": "Ada", "Card number": "4111"}}, els, None)[0] == BLOCK
    assert check_action("goto", {"url": "https://evil.com"}, els, ["example.com"])[0] == BLOCK
    assert domain_allowed("https://sub.example.com/x", ["example.com"]) and domain_allowed("https://a.b", [])


def test_secrets_resolve_and_redact():
    s = Secrets({"Site_PW": "hunter2-secret"}, env=False)
    assert s.names() == ["site_pw"]
    assert s.resolve("{{secret:site_pw}}") == ("hunter2-secret", [])
    assert s.resolve("{{ secret:nope }}")[1] == ["nope"]
    assert s.redact("pw is hunter2-secret") == "pw is {{secret:site_pw}}"


# ------------------------------------------------------------------ worker ---

def _register_worker(base_url, submit_answers):
    """Scripted worker: fill the registration form, click submit, report the confirmation."""
    def handler(role, message, thread_id, tools, system, kind):
        if "Thanks, Ada" in message:
            return {"tool_calls": [{"name": "done", "args": {"success": True, "summary": "registered",
                                                             "data": re.search(r"Thanks.*?WF-\d+", message).group(0)}}]}
        if "not approve" in message or "needs the user's approval" in message:
            return {"tool_calls": [{"name": "done", "args": {"success": False, "summary": "submission not approved"}}]}
        if "value='Ada Lovelace'" in message:
            return {"tool_calls": [{"name": "click", "args": {"idx": idx_of(message, "Submit registration")}}]}
        return {"tool_calls": [{"name": "fill_form", "args": {"fields": {
            "Full name": "Ada Lovelace", "Email": "ada@example.org", "Ticket type": "VIP", "terms": True}}}]}
    asked = []

    async def ask(q, options):
        asked.append(q)
        return submit_answers.pop(0) if submit_answers else ""
    return MockLLM(handler), ask, asked


async def test_worker_fills_form_and_submits_after_approval(base_url, br, cfg):
    llm, ask, asked = _register_worker(base_url, ["approve"])
    wr = await run_worker("register Ada", base_url + "/forms/register.html", llm=llm, browser=br, cfg=cfg, ask=ask)
    assert wr.success and "Registration confirmed: vip ticket" in wr.data
    assert len(asked) == 1 and "Submit registration" in asked[0] and "Ada Lovelace" in asked[0]


async def test_worker_respects_denial(base_url, br, cfg):
    llm, ask, asked = _register_worker(base_url, ["deny"])
    wr = await run_worker("register Ada", base_url + "/forms/register.html", llm=llm, browser=br, cfg=cfg, ask=ask)
    assert not wr.success and "not approved" in wr.summary and len(asked) == 1
    assert "Thanks" not in await br.page_text()


async def test_worker_auto_and_deny_modes(base_url, br, cfg):
    cfg.approval = "auto"
    llm, ask, asked = _register_worker(base_url, [])
    wr = await run_worker("register", base_url + "/forms/register.html", llm=llm, browser=br, cfg=cfg, ask=ask)
    assert wr.success and asked == []
    cfg.approval = "deny"
    llm, ask, asked = _register_worker(base_url, [])
    wr = await run_worker("register", base_url + "/forms/register.html", llm=llm, browser=br, cfg=cfg, ask=ask)
    assert not wr.success and asked == []


async def test_worker_never_types_card_numbers(base_url, br, cfg):
    cfg.approval = "auto"
    calls = {"n": 0}

    def handler(role, message, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"tool_calls": [{"name": "type", "args": {"idx": idx_of(message, "Card number"), "text": "4111111111111111"}}]}
        assert "never allowed" in message
        return {"tool_calls": [{"name": "done", "args": {"success": False, "summary": "blocked"}}]}
    wr = await run_worker("pay", base_url + "/forms/register.html", llm=MockLLM(handler), browser=br, cfg=cfg)
    assert not wr.success
    assert await br.page.input_value("#card") == ""


# ----------------------------------------------------------------- planner ---

async def test_parallel_browse_runs_workers_concurrently(base_url, br, cfg):
    pages = [base_url + "/index.html", base_url + "/wiki/index.html", base_url + "/data/specs.html"]
    live = {"now": 0, "max": 0}
    orig_new_tab = br.new_tab

    async def counting_new_tab():
        tab = await orig_new_tab()
        live["now"] += 1
        live["max"] = max(live["max"], live["now"])
        orig_close = tab.close

        async def close():
            live["now"] -= 1
            await orig_close()
        tab.close = close
        return tab
    br.new_tab = counting_new_tab

    def handler(role, message, thread_id, tools, system, kind):
        if role == "planner" and kind == "chat":
            return {"tool_calls": [{"name": "parallel_browse", "args": {"subtasks": [
                {"subtask": f"title of page {i}", "start_url": u} for i, u in enumerate(pages)]}}]}
        if role == "planner":
            return final(message=message)
        if role == "worker":
            title = re.search(r"TITLE: (.*)", message).group(1)
            return {"tool_calls": [{"name": "done", "args": {"success": True, "summary": "ok", "data": title}}]}
        return '{"complete": true}'

    events = []
    r = await run_task("compare three pages", llm=MockLLM(handler), browser=br, cfg=cfg,
                       on_event=lambda k, d: events.append((k, d)))
    assert r.ok and "3/3 subtasks succeeded" in r.answer
    assert "Mini Wiki" in r.answer and "Specs - Mini Data" in r.answer
    assert {d["agent"] for k, d in events if k == "delegate"} == {"w1", "w2", "w3"}
    assert live["max"] == 3 and live["now"] == 0   # three tabs open at once, all closed afterwards
    assert br.page.url == "about:blank"   # default tab untouched; workers used their own tabs


async def test_read_urls_saves_pages(base_url, br, cfg):
    llm = ScriptedMock({"planner": [
        {"tool_calls": [{"name": "read_urls", "args": {"urls": [base_url + "/data/specs.html", base_url + "/wiki/index.html"]}}]},
        final], "verifier": ['{"complete": true}']})
    r = await run_task("read two pages", llm=llm, browser=br, cfg=cfg)
    assert r.ok and "Planet data" in r.answer
    pages = [n for n in r.notes if n.startswith("pages/")]
    assert len(pages) == 2 and any("| Mars | 2 | 24.6 hours |" in r.notes[n] for n in pages)


async def test_large_output_offloaded_and_paged(cfg):
    big = "x" * 9000 + "THE-END"
    llm = ScriptedMock({
        "planner": [{"tool_calls": [{"name": "web_search", "args": {"queries": ["big"]}}]},
                    {"tool_calls": [{"name": "read_file", "args": {"name": "results/search-1.md", "offset": 8990}}]},
                    final],
        "searcher": [{"text": big}], "verifier": ['{"complete": true}']})
    r = await run_task("q", llm=llm, browser=None, cfg=cfg)
    assert r.notes["results/search-1.md"] == big and "THE-END" in r.answer
    first = json.loads(next(e for e in llm.log if e["role"] == "planner" and e["kind"] == "submit")["message"])
    assert "saved to file 'results/search-1.md'" in first[0]["output"] and len(first[0]["output"]) < 3000


async def test_parallel_web_search(cfg):
    events = []
    llm = ScriptedMock({"planner": [
        {"tool_calls": [{"name": "web_search", "args": {"queries": ["python", "node", "go"]}}]}, final],
        "searcher": [{"text": "A"}, {"text": "B"}, {"text": "C"}], "verifier": ['{"complete": true}']})
    r = await run_task("versions", llm=llm, browser=None, cfg=cfg, on_event=lambda k, d: events.append((k, d)))
    assert all(f"### {q}" in r.answer for q in ("python", "node", "go"))
    assert sum(1 for k, _ in events if k == "search") == 3


async def test_verifier_rejects_then_accepts(cfg):
    llm = ScriptedMock({
        "planner": [{"tool_calls": [{"name": "final_answer", "args": {"answer": "Python 3.15"}}]},
                    lambda **kw: {"tool_calls": [{"name": "final_answer", "args": {
                        "answer": "Python 3.15; Go 1.27" + (" (fixed)" if "missing" in kw["message"] else "")}}]}],
        "verifier": ['{"complete": false, "missing": ["Go version"], "unsupported_claims": [], "verdict": "no Go"}',
                     '{"complete": true, "missing": [], "verdict": "ok"}']})
    events = []
    r = await run_task("Python and Go versions", llm=llm, browser=None, cfg=cfg,
                       on_event=lambda k, d: events.append((k, d)))
    assert r.ok and r.answer == "Python 3.15; Go 1.27 (fixed)"
    assert r.verification["complete"] is True
    assert [d["complete"] for k, d in events if k == "verdict"] == [False, True]


async def test_verifier_failure_never_traps_a_run(cfg):
    llm = ScriptedMock({"planner": [{"tool_calls": [{"name": "final_answer", "args": {"answer": "done"}}]}],
                        "verifier": ["not json at all"]})
    r = await run_task("t", llm=llm, browser=None, cfg=cfg)
    assert r.ok and r.answer == "done"


async def test_analyze_in_separate_context_and_deliverable(cfg):
    corpus = "laptop A $900 good battery; laptop B $700 weak battery"
    llm = ScriptedMock({
        "planner": [
            {"tool_calls": [{"name": "write_file", "args": {"name": "notes/laptops.md", "content": corpus}}]},
            {"tool_calls": [{"name": "analyze", "args": {"files": ["notes/laptops.md"], "instruction": "CSV of name,price",
                                                         "save_as": "laptops.csv"}}]},
            final],
        "analyst": [lambda **kw: "name,price\nA,900\nB,700" if corpus in kw["message"] else "MISSING"],
        "verifier": ['{"complete": true}']})
    events = []
    r = await run_task("compare laptops, save a CSV", llm=llm, browser=None, cfg=cfg,
                       on_event=lambda k, d: events.append((k, d)))
    assert r.outputs == ["outputs/laptops.csv"] and r.notes["outputs/laptops.csv"].startswith("name,price")
    assert any(k == "output" and d["name"] == "outputs/laptops.csv" for k, d in events)
    analyst = [e for e in llm.log if e["role"] == "analyst"]
    assert len(analyst) == 1 and analyst[0]["thread_id"] not in {e["thread_id"] for e in llm.log if e["role"] == "planner"}


async def test_ask_user_with_and_without_answer(cfg):
    asked = []

    async def on_ask(q, options):
        asked.append((q, options))
        return "the VIP plan"
    def script():
        return ScriptedMock({"planner": [{"tool_calls": [{"name": "ask_user", "args": {
            "question": "Which plan?", "options": ["basic", "VIP"]}}]}, final], "verifier": ['{"complete": true}']})
    r = await run_task("pick a plan", llm=script(), browser=None, cfg=cfg, on_ask=on_ask)
    assert asked == [("Which plan?", ["basic", "VIP"])] and "USER ANSWER: the VIP plan" in r.answer
    r = await run_task("pick a plan", llm=script(), browser=None, cfg=cfg)
    assert "No answer from the user" in r.answer


async def test_budget_wraps_up_then_refuses_research(cfg):
    cfg.run_deadline_s = 0.2
    searches = [{"tool_calls": [{"name": "web_search", "args": {"queries": [f"q{i}"]}}]} for i in range(4)]

    llm = ScriptedMock({"planner": searches + [final], "searcher": [{"text": "r"}] * 8,
                        "verifier": ['{"complete": true}']})
    orig = llm.web_search

    async def slow_search(q):
        await asyncio.sleep(0.08)
        return await orig(q)
    llm.web_search = slow_search
    await run_task("q", llm=llm, browser=None, cfg=cfg)
    msgs = [e["message"] for e in llm.log if e["role"] == "planner" and e["kind"] == "submit"]
    assert any("[budget]" in m for m in msgs)
    assert any("budget exhausted" in m for m in msgs)
    assert sum(1 for e in llm.log if e["role"] == "searcher") < 4


async def test_checkpoint_and_resume(cfg):
    run_dir = cfg.runs_dir / "r1"
    llm = ScriptedMock({"planner": [
        {"tool_calls": [{"name": "write_todos", "args": {"todos": [{"task": "collect", "status": "done"},
                                                                   {"task": "report", "status": "pending"}]}}]},
        {"tool_calls": [{"name": "write_file", "args": {"name": "notes/data.md", "content": "collected facts"}}]},
        lambda **kw: (_ for _ in ()).throw(RuntimeError("worker died"))]})
    r = await run_task("collect then report", llm=llm, browser=None, cfg=cfg, run_dir=run_dir)
    assert not r.ok
    cp = load_checkpoint(run_dir)
    assert cp["status"] == "error" and cp["workspace"]["notes/data.md"] == "collected facts" and cp["rounds"] == 2

    llm2 = ScriptedMock({"planner": [
        lambda **kw: {"tool_calls": [{"name": "write_file", "args": {
            "name": "outputs/report.md", "content": "report from: " + ("notes/data.md" if "notes/data.md" in kw["message"] else "?")}}]},
        final], "verifier": ['{"complete": true}']})
    events = []
    r2 = await run_task(cp["task"], llm=llm2, browser=None, cfg=cfg, run_dir=run_dir, resume=cp,
                        on_event=lambda k, d: events.append((k, d)))
    assert r2.ok and r2.notes["outputs/report.md"] == "report from: notes/data.md"
    assert r2.outputs == ["outputs/report.md"] and (run_dir / "outputs" / "report.md").exists()
    assert ("resume", {"files": 1, "todos": 2}) in events
    assert "RESUMING" in llm2.log[0]["message"]
    assert load_checkpoint(run_dir)["status"] == "done"


async def test_planner_is_told_not_to_retry_a_denied_action(base_url, br, cfg):
    llm, ask, asked = _register_worker(base_url, ["deny"])
    wr = await run_worker("register Ada", base_url + "/forms/register.html", llm=llm, browser=br, cfg=cfg, ask=ask)
    assert wr.denied and "Submit registration" in wr.denied[0]

    def handler(role, message, thread_id, tools, system, kind):
        if role == "planner" and kind == "chat":
            return {"tool_calls": [{"name": "delegate_browse", "args": {"subtask": "register Ada", "start_url": base_url + "/forms/register.html"}}]}
        if role == "planner":
            return final(message=message)
        if role == "worker":
            return _register_worker(base_url, [])[0].handler(role, message, thread_id, tools, system, kind)
        return '{"complete": true}'
    cfg.approval = "deny"
    r = await run_task("register Ada", llm=MockLLM(handler), browser=br, cfg=cfg)
    assert "USER DID NOT APPROVE" in r.answer and "Do not retry" in r.answer
