import asyncio
import json

from webforge.contracts import ActionResult, LLMReply, Observation, Skill, SkillStep, TraceStep
from webforge.skills.builder import SkillBuilder
from webforge.skills.library import SkillLibrary
from webforge.skills.replay import run_skill


def run(c):
    return asyncio.run(c)


class FakeBrowser:
    def __init__(self, fail_names=()):
        self.calls = []
        self.fail_names = set(fail_names)

    async def act_locator(self, action, locator, value=None, extract=None):
        self.calls.append((action, locator, value, extract))
        if locator and locator.get("name") in self.fail_names:
            return ActionResult(False, "no match")
        if action == "extract":
            if extract and extract.get("type") == "css":
                return ActionResult(True, "2 rows", [{"title": "A"}, {"title": "B"}])
            return ActionResult(True, "text", "hello page")
        return ActionResult(True, "ok")

    async def observe(self):
        return Observation(url="u", title="t", elements=[], text="page")


class FakeLLM:
    def __init__(self, replies):
        self.replies = list(replies)
        self.msgs = []

    async def chat(self, *, role, message, system, tools, thread_id=None, json_output=False):
        self.msgs.append((role, message, json_output))
        t = self.replies.pop(0)
        return LLMReply(thread_id="t", status="COMPLETED", text=t)


def skill(name="s", domain="books.toscrape.com", **kw):
    return Skill(name=name, domain=domain, description="d", params=["category"],
                 steps=[SkillStep("goto", None, "https://books.toscrape.com/"),
                        SkillStep("click", {"role": "link", "name": "{category}"})], **kw)


GOOD = {
    "name": "List Books In Category", "description": "lists books", "params": ["category"],
    "example_params": {"category": "Poetry"},
    "steps": [{"action": "goto", "locator": None, "value": "https://books.toscrape.com/"},
              {"action": "click", "locator": {"role": "link", "name": "{category}"}, "value": None}],
    "extract": {"type": "css", "selector": "article.product_pod", "fields": {"title": "h3 a@title"}},
}

TRACE = [
    TraceStep("goto", {"url": "https://books.toscrape.com/"}, None, "about:blank", True, "ok"),
    TraceStep("scroll", {}, None, "https://books.toscrape.com/", True, "ok"),
    TraceStep("click", {"idx": 3}, {"role": "link", "name": "Travel"}, "https://books.toscrape.com/", False, "x"),
    TraceStep("click", {"idx": 4}, {"role": "link", "name": "Poetry"}, "https://books.toscrape.com/", True, "ok"),
    TraceStep("extract", {}, None, "https://books.toscrape.com/catalogue/category/poetry", True, "ok"),
]


def test_library(tmp_path):
    lib = SkillLibrary(tmp_path / "sk")
    lib.save(skill("a"))
    lib.save(skill("b", domain="other.com"))
    assert lib.get("a").domain == "books.toscrape.com"
    assert lib.get("zzz") is None
    assert [s.name for s in lib.list("books.toscrape.com")] == ["a"]
    lib.record_use("a", True)
    for _ in range(3):
        lib.record_use("a", False)
    s = lib.get("a")
    assert (s.uses, s.successes) == (4, 1)
    assert [x.name for x in lib.list()] == ["b"]
    assert {x.name for x in lib.list(include_demoted=True)} == {"a", "b"}
    assert "b(category) @other.com - d [ok 0/0]" in lib.describe()
    assert lib.delete("b") and lib.get("b") is None


def test_run_skill_ok_and_templates():
    b = FakeBrowser()
    r = run(run_skill(skill(), {"category": "Poetry"}, b))
    assert r.ok and r.output == "hello page"
    assert b.calls[1][1] == {"role": "link", "name": "Poetry"}
    s = skill(extract={"type": "css", "selector": "div", "fields": {"t": "a"}})
    r = run(run_skill(s, {"category": "X"}, b))
    assert r.output == [{"title": "A"}, {"title": "B"}]
    ex = b.calls[-1][3]
    assert ex["selector"] == "div" and ex["css"] == "div" and ex["fields"] == {"t": "a"}


def test_run_skill_missing_param_and_failed_step():
    b = FakeBrowser()
    r = run(run_skill(skill(), {}, b))
    assert not r.ok and "category" in r.message and b.calls == []
    r = run(run_skill(skill(), {"category": "Nope"}, FakeBrowser(fail_names={"Nope"})))
    assert not r.ok and r.failed_step == 1


def test_builder_builds(tmp_path):
    lib = SkillLibrary(tmp_path)
    llm = FakeLLM(["```json\n" + json.dumps(GOOD) + "\n```"])
    b = FakeBrowser()
    bld = SkillBuilder(llm, lib)
    s = run(bld.build("books in Poetry", TRACE, "found 2", b))
    assert s is not None, bld.last_error
    assert s.params == ["category"] and s.steps[1].locator["name"] == "{category}"
    assert s.domain == "books.toscrape.com" and s.version == 1 and s.name == "list_books_in_category"
    assert lib.get(s.name) is not None
    assert b.calls[1][1]["name"] == "Poetry"
    assert llm.msgs[0][0] == "builder" and llm.msgs[0][2] is True
    assert "Travel" not in llm.msgs[0][1]  # failed step filtered


def test_builder_repair_round(tmp_path):
    bad = json.loads(json.dumps(GOOD))
    bad["steps"][1]["locator"]["name"] = "{category} books"
    lib = SkillLibrary(tmp_path)
    llm = FakeLLM([json.dumps(bad), "here: " + json.dumps(GOOD)])
    b = FakeBrowser(fail_names={"Poetry books"})
    bld = SkillBuilder(llm, lib)
    s = run(bld.build("t", TRACE, "ok", b))
    assert s is not None and s.steps[1].locator["name"] == "{category}"
    assert len(llm.msgs) == 2


def test_builder_garbage_and_no_steps(tmp_path):
    bld = SkillBuilder(FakeLLM(["not json at all"]), SkillLibrary(tmp_path))
    assert run(bld.build("t", TRACE, "ok", FakeBrowser())) is None and bld.last_error
    bld = SkillBuilder(FakeLLM([]), SkillLibrary(tmp_path))
    assert run(bld.build("t", [TRACE[1]], "ok", FakeBrowser())) is None


def test_builder_fails_twice_and_name_clash(tmp_path):
    lib = SkillLibrary(tmp_path)
    bad = json.loads(json.dumps(GOOD))
    bad["steps"][1]["locator"]["name"] = "Zzz"
    bld = SkillBuilder(FakeLLM([json.dumps(bad)] * 2), lib)
    assert run(bld.build("t", TRACE, "ok", FakeBrowser(fail_names={"Zzz"}))) is None
    assert lib.list() == []
    lib.save(skill("list_books_in_category", domain="other.com"))
    bld = SkillBuilder(FakeLLM([json.dumps(GOOD)]), lib)
    assert run(bld.build("t", TRACE, "ok", FakeBrowser())) is None
    assert "clash" in bld.last_error


def test_repair(tmp_path):
    lib = SkillLibrary(tmp_path)
    old = skill("fixme")
    lib.save(old)
    fixed = json.loads(json.dumps(GOOD))
    fixed["name"] = "whatever"
    bld = SkillBuilder(FakeLLM([json.dumps(fixed)]), lib)
    new = run(bld.repair(old, 1, "no match", "obs", FakeBrowser(), {"category": "Poetry"}))
    assert new is not None and new.name == "fixme" and new.version == 2
    assert lib.get("fixme").version == 2
