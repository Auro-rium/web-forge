import asyncio
import os

import pytest

from webforge.config import Config
from webforge.llm import BackboardLLM, MockLLM, ScriptedMock, make_llm
from webforge.memory import LocalMemory, make_memory


def run(c):
    return asyncio.run(c)


def test_mock_handler_roundtrip():
    def h(role, message, thread_id, tools, system, kind):
        if kind == "chat":
            return {"tool_calls": [{"name": "click", "args": {"idx": 3}}]}
        return "done"
    m = MockLLM(h)
    r = run(m.chat(role="worker", message="x" * 40, system="s", tools=[{"a": 1}]))
    assert r.status == "REQUIRES_ACTION" and r.tool_calls[0].name == "click"
    assert r.tool_calls[0].args == {"idx": 3} and r.tool_calls[0].id == "mock_call_1"
    assert r.thread_id.startswith("mock_thread_") and r.tokens == 10
    r2 = run(m.submit(role="worker", thread_id=r.thread_id, outputs=[{"tool_call_id": "mock_call_1", "output": "ok"}]))
    assert r2.status == "COMPLETED" and r2.text == "done" and r2.thread_id == r.thread_id
    assert m.usage()["calls"] == 2 and m.usage()["tokens"] > 10


def test_scripted_mock():
    m = ScriptedMock({"worker": [{"tool_calls": [("goto", {"url": "u"})]}, {"text": "fin"}]})
    r = run(m.chat(role="worker", message="hi", system=""))
    assert r.status == "REQUIRES_ACTION"
    r = run(m.submit(role="worker", thread_id=r.thread_id, outputs=[]))
    assert r.text == "fin"
    with pytest.raises(RuntimeError, match="worker"):
        run(m.chat(role="worker", message="again", system=""))
    with pytest.raises(RuntimeError, match="planner"):
        run(m.chat(role="planner", message="x", system=""))


def test_make_llm_mock():
    assert isinstance(make_llm(Config(), mock=True), MockLLM)


def test_local_memory(tmp_path):
    cfg = Config(data_dir=tmp_path)
    mem = make_memory(cfg, local_only=True)
    assert isinstance(mem, LocalMemory)

    async def go():
        await mem.add("books.toscrape.com category links are in the left sidebar", {"domain": "books", "kind": "lesson"})
        await mem.add("quotes pagination uses the Next button", {"domain": "quotes", "kind": "lesson"})
        hits = await mem.search("how to click category sidebar links", 5)
        assert hits and "sidebar" in hits[0]["content"] and hits[0]["score"] > 0
        assert set(hits[0]) == {"content", "metadata", "score"}
        assert len(await mem.list()) == 2
        assert "- [lesson]" in await mem.lessons_for("quotes pagination")
        await mem.reset()
        assert await mem.list() == [] and await mem.search("sidebar") == []
    run(go())


@pytest.mark.skipif(os.getenv("WEBFORGE_LIVE") != "1", reason="set WEBFORGE_LIVE=1")
@pytest.mark.live
def test_live_tool_roundtrip(tmp_path):
    cfg = Config(data_dir=tmp_path)
    cfg.api_key = cfg.api_key or "proxy-injected"
    tools = [{"type": "function", "function": {
        "name": "get_weather", "description": "Get weather for a city",
        "parameters": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}}}]

    async def go():
        llm = BackboardLLM(cfg)
        try:
            r = await llm.chat(role="worker", message="What's the weather in Paris? Use the tool.",
                               system="You are a helpful assistant. Always use tools when available.", tools=tools)
            assert r.status == "REQUIRES_ACTION" and r.tool_calls[0].name == "get_weather"
            r2 = await llm.submit(role="worker", thread_id=r.thread_id,
                                  outputs=[{"tool_call_id": r.tool_calls[0].id, "output": "Sunny, 21C"}])
            assert r2.status == "COMPLETED" and r2.text
            assert llm.usage()["calls"] == 2
        finally:
            await llm.aclose()
    run(go())


async def test_budget_cap_blocks_calls(tmp_path):
    import json
    from webforge.llm import BackboardLLM, BudgetExceeded
    cfg = Config(data_dir=tmp_path, ledger_file=tmp_path / "spend.json")
    cfg.budget_usd = 1.0
    (tmp_path / "spend.json").write_text(json.dumps({"total_usd": 1.2}))
    llm = BackboardLLM(cfg)
    with pytest.raises(BudgetExceeded):
        await llm.chat(role="planner", message="hi", system="s")


def test_allowed_domains_default_is_open_web(monkeypatch):
    monkeypatch.delenv("WEBFORGE_ALLOWED_DOMAINS", raising=False)
    assert Config().allowed_domains == []          # empty list = any public website
    monkeypatch.setenv("WEBFORGE_ALLOWED_DOMAINS", "*")
    assert Config().allowed_domains == []
    monkeypatch.setenv("WEBFORGE_ALLOWED_DOMAINS", "a.com, b.org")
    assert Config().allowed_domains == ["a.com", "b.org"]
