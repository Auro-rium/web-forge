"""Planner: the deep-agent orchestrator (todos, scratch files, skills, delegated browsing)."""
from __future__ import annotations

import asyncio
import json
import time
from datetime import date
from pathlib import Path
from typing import Any, Callable, Optional

from webforge.agent.prompts import PLANNER_SYSTEM, PLANNER_TOOLS
from webforge.agent.reflector import reflect
from webforge.agent.worker import run_worker
from webforge.contracts import RunResult
from webforge.safety import domain_allowed
from webforge.skills.builder import SkillBuilder
from webforge.skills.replay import run_skill

MAX_OUT = 3000
RESEARCH_TOOLS = {"web_search", "read_urls", "delegate_browse", "parallel_browse", "run_skill"}
OFFLOAD_PREVIEW = 1800


def _clip(s: Any, n: int = MAX_OUT) -> str:
    s = s if isinstance(s, str) else json.dumps(s, default=str)
    return s if len(s) <= n else s[:n] + f"...[truncated {len(s) - n} chars]"


async def run_task(task: str, *, llm, browser, library, memory, cfg, on_event: Optional[Callable] = None,
                   learn: bool = True, use_memory: bool = True, use_skills: bool = True,
                   run_dir: Optional[Path] = None) -> RunResult:
    t0 = time.monotonic()
    u0 = llm.usage()

    def emit(kind: str, data: dict) -> None:
        if on_event:
            try:
                on_event(kind, data)
            except Exception:
                pass

    workspace: dict[str, str] = {}
    todos: list = []
    stats = {"steps": 0}
    skills_used: list[str] = []
    skills_created: list[str] = []
    digest: list[str] = []
    if run_dir:
        try:
            Path(run_dir).mkdir(parents=True, exist_ok=True)
        except Exception:
            run_dir = None

    emit("task_start", {"task": task})
    builder = SkillBuilder(llm, library)

    counters: dict[str, int] = {}

    def offload(kind: str, text: str, ext: str = "md") -> str:
        """Keep big outputs out of the planner's context: full text goes to a workspace file."""
        if len(text) <= MAX_OUT:
            return text
        counters[kind] = counters.get(kind, 0) + 1
        name = f"results/{kind}-{counters[kind]}.{ext}"
        save(name, text)
        return (text[:OFFLOAD_PREVIEW] + f"\n...[{len(text) - OFFLOAD_PREVIEW} more chars saved to file '{name}'; "
                f"use read_file(name='{name}', offset={OFFLOAD_PREVIEW}) to see more]")

    def save(name: str, content: str) -> None:
        workspace[name] = content
        if run_dir:
            try:
                f = Path(run_dir) / name
                f.parent.mkdir(parents=True, exist_ok=True)
                f.write_text(content)
            except Exception:
                pass

    sem = asyncio.Semaphore(max(1, int(getattr(cfg, "max_parallel", 4))))
    agent_n = {"n": 0}

    def tagged(agent: str) -> Callable:
        def _ev(kind: str, data: dict) -> None:
            emit(kind, {**(data or {}), "agent": agent})
        return _ev

    async def maybe_build(subtask, wr, tab=None) -> None:
        if not (learn and wr.success and wr.trace):
            return
        try:
            skill = await builder.build(subtask, wr.trace, wr.summary, tab or browser, wr.domain or None)
            if skill is None:
                emit("warn", {"message": f"skill not built: {builder.last_error}"})
                return
            if skill.name not in skills_created:
                skills_created.append(skill.name)
            emit("skill_built", {"name": skill.name, "params": skill.params, "description": skill.description})
            try:
                await memory.add(f"Skill {skill.name}({', '.join(skill.params)}): {skill.description}",
                                 {"domain": skill.domain, "kind": "skill"})
            except Exception as e:  # noqa: BLE001
                emit("warn", {"message": f"memory.add failed: {e!r}"})
        except Exception as e:  # noqa: BLE001
            emit("warn", {"message": f"skill building failed: {e!r}"})

    # ---------------------------------------------------------- tool impls --
    async def _browse(subtask: str, start_url, tab, agent: str):
        hints = ""
        if use_memory:
            try:
                hints = await memory.lessons_for(subtask + " " + (start_url or ""))
            except Exception:
                hints = ""
        wr = await run_worker(subtask, start_url, llm=llm, browser=tab, cfg=cfg, on_event=tagged(agent), hints=hints)
        stats["steps"] += wr.steps
        digest.append(f"[{agent}] subtask '{subtask}' -> {'ok' if wr.success else 'FAILED'} in {wr.steps} steps: "
                      f"{wr.summary[:200]}")
        for st in wr.trace[:25]:
            digest.append(f"  {st.action} {json.dumps(st.args, default=str)[:80]} {st.locator or ''} ok={st.ok}")
        return wr

    def _report(agent: str, wr) -> str:
        data = "" if wr.data is None else json.dumps(wr.data, default=str, ensure_ascii=False)
        head = (f"[{agent}] {'SUCCESS' if wr.success else 'FAILED'} ({wr.steps} steps, {wr.domain})\n"
                f"summary: {wr.summary}\n")
        if len(data) > 1500:
            counters["data"] = counters.get("data", 0) + 1
            name = f"results/{agent}-data-{counters['data']}.json"
            save(name, data)
            return head + f"data (first 1500 chars; full data in file '{name}'): {data[:1500]}"
        return head + f"data: {data}"

    def _next_agent() -> str:
        agent_n["n"] += 1
        return f"w{agent_n['n']}"

    async def t_delegate(a: dict) -> str:
        subtask = str(a.get("subtask", "")).strip()
        if not subtask:
            return "ERROR: subtask is required"
        start_url = a.get("start_url") or None
        agent = _next_agent()
        emit("delegate", {"subtask": subtask, "start_url": start_url, "agent": agent})
        wr = await _browse(subtask, start_url, browser, agent)
        await maybe_build(subtask, wr)
        return offload("browse", _report(agent, wr))

    async def t_parallel(a: dict) -> str:
        items = a.get("subtasks") or []
        jobs = []
        for it in items[:8]:
            if isinstance(it, str):
                it = {"subtask": it}
            if isinstance(it, dict) and str(it.get("subtask", "")).strip():
                jobs.append((str(it["subtask"]).strip(), it.get("start_url") or None, _next_agent()))
        if not jobs:
            return "ERROR: subtasks must be a non-empty list of {subtask, start_url}"
        emit("parallel", {"n": len(jobs), "agents": [j[2] for j in jobs]})

        async def one(job):
            subtask, start_url, agent = job
            async with sem:
                tab = await browser.new_tab()
                emit("delegate", {"subtask": subtask, "start_url": start_url, "agent": agent})
                try:
                    return job, tab, await _browse(subtask, start_url, tab, agent)
                except Exception as e:  # noqa: BLE001
                    from webforge.agent.worker import WorkerResult
                    return job, tab, WorkerResult(False, f"worker crashed: {e!r}")

        done = await asyncio.gather(*(one(j) for j in jobs))
        reports = []
        for (subtask, _u, agent), tab, wr in done:
            await maybe_build(subtask, wr, tab)   # sequential: no races on the skill library
            reports.append(_report(agent, wr))
            try:
                await tab.close()
            except Exception:
                pass
        ok = sum(1 for _j, _t, wr in done if wr.success)
        return offload("parallel", f"{ok}/{len(done)} subtasks succeeded\n\n" + "\n\n".join(reports))

    async def t_read_urls(a: dict) -> str:
        urls = [str(u).strip() for u in (a.get("urls") or ([a["url"]] if a.get("url") else [])) if str(u).strip()]
        urls = urls[:8]
        if not urls:
            return "ERROR: urls must be a non-empty list"
        emit("read_urls", {"urls": urls})

        async def one(i: int, url: str) -> str:
            if not domain_allowed(url, cfg.allowed_domains):
                return f"## {url}\nBLOCKED: domain not in allowed list"
            async with sem:
                tab = await browser.new_tab()
                try:
                    g = await tab.act("goto", url=url)
                    if not g.ok:
                        return f"## {url}\nFAILED to open: {g.message}"
                    r = await tab.read(0, window=60000)
                    title = await tab.page.title()
                    text = r.data or ""
                    host = (tab.page.url.split("//")[-1].split("/")[0] or "page").replace(":", "_")
                    name = f"pages/{host}-{i + 1}.md"
                    save(name, f"# {title}\nURL: {tab.page.url}\n\n{text}")
                    return f"## {title} ({tab.page.url})\nsaved to '{name}' ({len(text)} chars). Preview:\n{text[:900]}"
                except Exception as e:  # noqa: BLE001
                    return f"## {url}\nFAILED: {e!r}"
                finally:
                    await tab.close()

        parts = await asyncio.gather(*(one(i, u) for i, u in enumerate(urls)))
        digest.append(f"read_urls {urls}")
        return offload("pages", "\n\n".join(parts))

    async def t_run_skill(a: dict) -> str:
        if not use_skills:
            return "ERROR: skills are disabled; use delegate_browse"
        name = str(a.get("name") or a.get("skill") or "")
        params = a.get("params") or {}
        skill = library.get(name)
        if skill is None:
            return f"ERROR: unknown skill '{name}'. Use list_skills."
        if not isinstance(params, dict):
            params = {}
        emit("skill_run", {"name": name, "params": params})
        res = await run_skill(skill, params, browser)
        stats["steps"] += len(skill.steps)
        if (not res.ok) and learn and res.failed_step is not None:
            try:
                page = await browser.page_text()
                new = await builder.repair(skill, res.failed_step, res.message, page, browser, params)
                if new is not None:
                    emit("skill_built", {"name": new.name, "repaired": True, "version": new.version})
                    res = await run_skill(new, params, browser)
                    stats["steps"] += len(new.steps)
                    skill = new
                else:
                    emit("warn", {"message": f"skill repair failed: {builder.last_error}"})
            except Exception as e:  # noqa: BLE001
                emit("warn", {"message": f"skill repair crashed: {e!r}"})
        try:
            library.record_use(name, res.ok)
        except Exception:
            pass
        emit("skill_result", {"name": name, "ok": res.ok, "message": res.message})
        if res.ok:
            if name not in skills_used:
                skills_used.append(name)
            digest.append(f"skill {name}({params}) ok")
            return _clip("OK. result: " + json.dumps(res.output, default=str))
        digest.append(f"skill {name}({params}) FAILED: {res.message}")
        return _clip(f"FAILED: {res.message}. Fall back to delegate_browse.")

    async def t_web_search(a: dict) -> str:
        qs = a.get("queries") or ([a["query"]] if a.get("query") else [])
        qs = [str(q).strip() for q in (qs if isinstance(qs, list) else [qs]) if str(q).strip()][:6]
        if not qs:
            return "ERROR: queries is required"

        async def one(q: str) -> str:
            emit("search", {"query": q})
            async with sem:
                try:
                    r = await llm.web_search(q)
                    return r.text or "(no results)"
                except Exception as e:  # noqa: BLE001
                    return f"ERROR: search failed: {e!r}"

        answers = await asyncio.gather(*(one(q) for q in qs))
        for q, ans in zip(qs, answers):
            digest.append(f"web_search '{q}' -> {ans[:200]}")
        if len(qs) == 1:
            return offload("search", answers[0])
        return offload("search", "\n\n".join(f"### {q}\n{ans}" for q, ans in zip(qs, answers)))

    def t_todos(a: dict) -> str:
        todos[:] = a.get("todos") or []
        emit("todos", {"todos": todos})
        return f"todos saved ({len(todos)} items)"

    def t_write_file(a: dict) -> str:
        name = str(a.get("name", "notes.txt")).replace("..", "_").strip("/") or "notes.txt"
        save(name, str(a.get("content", "")))
        return f"saved {name} ({len(workspace[name])} chars)"

    def t_read_file(a: dict) -> str:
        name = str(a.get("name", ""))
        if name not in workspace:
            return f"ERROR: no file '{name}'. Files: {list(workspace)}"
        text = workspace[name]
        off = max(0, int(a.get("offset", 0) or 0))
        lim = max(200, min(int(a.get("limit", MAX_OUT) or MAX_OUT), 8000))
        chunk = text[off:off + lim]
        more = f"\n...[{len(text) - off - lim} more chars; read_file(name='{name}', offset={off + lim})]" \
            if off + lim < len(text) else ""
        return chunk + more

    def t_list_skills(a: dict) -> str:
        return _clip(library.describe() or "(no skills yet)") if use_skills else "(skills disabled)"

    # ------------------------------------------------------------ first msg --
    catalog = library.describe() if use_skills else ""
    mem_text = ""
    if use_memory:
        try:
            mem_text = await memory.lessons_for(task)
        except Exception:
            mem_text = ""
    if mem_text:
        emit("memory_hits", {"text": mem_text})
    first = (f"TASK: {task}\n\nToday: {date.today().isoformat()}. "
             f"Allowed domains: {', '.join(cfg.allowed_domains) or 'any public website'}. Max {cfg.planner_max_steps} tool rounds.\n\n"
             f"AVAILABLE SKILLS:\n{catalog or '(none)'}\n\n"
             f"MEMORY (lessons from earlier runs):\n{mem_text or '(none)'}")
    tools = [t for t in PLANNER_TOOLS if use_skills or t["function"]["name"] not in ("run_skill", "list_skills")]

    # ----------------------------------------------------------------- loop --
    answer, ok = "", False
    try:
        reply = await llm.chat(role="planner", message=first, system=PLANNER_SYSTEM, tools=tools)
        thread = reply.thread_id
        rounds = 0
        last_text = reply.text or ""
        nudges = 0
        while rounds < cfg.planner_max_steps:
            if not (reply.status == "REQUIRES_ACTION" and reply.tool_calls):
                # model answered in prose without finishing: push it to act (live models do this often)
                if nudges >= 3:
                    break
                nudges += 1
                if (reply.text or "").strip():   # an empty turn is a transport hiccup: continue silently
                    emit("nudge", {"message": f"planner thought in prose; asking it to act ({nudges}/3)"})
                reply = await llm.chat(
                    role="planner", thread_id=thread, system=PLANNER_SYSTEM, tools=tools,
                    message="You have not finished. Do not describe plans in prose. Call a tool NOW: "
                            "web_search(queries), read_urls(urls), delegate_browse / parallel_browse to browse, run_skill if a skill fits, "
                            "or final_answer(answer) with the actual requested data once you have it.")
                last_text = reply.text or last_text
                continue
            rounds += 1
            nudges = 0  # only CONSECUTIVE prose replies count as a stall
            outputs, finished = [], False
            soft = int(getattr(cfg, "planner_soft_rounds", 6))
            hard_stop = rounds > soft + 2
            for tc in reply.tool_calls:
                a = tc.args or {}
                try:
                    if hard_stop and tc.name in RESEARCH_TOOLS:
                        out = ("ERROR: research budget exhausted. Do not search or browse again: call final_answer NOW "
                               "with the best answer from what you already have, and state any uncertainty.")
                        emit("budget", {"message": "research budget exhausted; asking for the final answer"})
                    elif tc.name == "final_answer":
                        answer = str(a.get("answer", ""))
                        finished = True
                        break
                    elif tc.name == "write_todos":
                        out = t_todos(a)
                    elif tc.name == "write_file":
                        out = t_write_file(a)
                    elif tc.name == "read_file":
                        out = t_read_file(a)
                    elif tc.name == "list_skills":
                        out = t_list_skills(a)
                    elif tc.name == "run_skill":
                        out = await t_run_skill(a)
                    elif tc.name == "web_search":
                        out = await t_web_search(a)
                    elif tc.name == "delegate_browse":
                        out = await t_delegate(a)
                    elif tc.name == "parallel_browse":
                        out = await t_parallel(a)
                    elif tc.name == "read_urls":
                        out = await t_read_urls(a)
                    else:
                        out = f"ERROR: unknown tool '{tc.name}'"
                except Exception as e:  # noqa: BLE001
                    out = f"ERROR: tool crashed: {e!r}"
                    emit("warn", {"message": out})
                outputs.append({"tool_call_id": tc.id, "output": _clip(out, MAX_OUT + 600)})
            if finished:
                ok = True
                break
            if rounds >= soft and outputs and not hard_stop:
                outputs[-1]["output"] += (f"\n\n[budget] Tool round {rounds}; the research budget is {soft} rounds. "
                                          "You have enough to answer: call final_answer now, noting any conflict "
                                          "between sources instead of searching further.")
            reply = await llm.submit(role="planner", thread_id=thread, outputs=outputs)
            last_text = reply.text or last_text
        if not ok:
            answer = last_text or "(budget exhausted)"
            if reply.status == "REQUIRES_ACTION":
                answer = last_text or "(budget exhausted)"
    except Exception as e:  # noqa: BLE001
        answer = answer or f"(run crashed: {e!r})"
        emit("warn", {"message": f"planner loop crashed: {e!r}"})

    emit("final", {"answer": answer, "ok": ok})
    if learn:
        try:
            await reflect(task, answer, ok, "\n".join(digest), llm, memory, on_event)
        except Exception as e:  # noqa: BLE001
            emit("warn", {"message": f"reflect failed: {e!r}"})
    u1 = llm.usage()
    return RunResult(task=task, answer=answer, ok=ok, steps=stats["steps"],
                     llm_calls=u1["calls"] - u0["calls"], tokens=u1["tokens"] - u0["tokens"],
                     cost_usd=u1["cost_usd"] - u0["cost_usd"], seconds=time.monotonic() - t0,
                     skills_used=skills_used, skills_created=skills_created, notes=dict(workspace))
