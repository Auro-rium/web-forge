"""Planner: the deep-agent orchestrator (todos, scratch files, skills, delegated browsing)."""
from __future__ import annotations

import json
import time
from datetime import date
from pathlib import Path
from typing import Any, Callable, Optional

from webforge.agent.prompts import PLANNER_SYSTEM, PLANNER_TOOLS
from webforge.agent.reflector import reflect
from webforge.agent.worker import run_worker
from webforge.contracts import RunResult
from webforge.skills.builder import SkillBuilder
from webforge.skills.replay import run_skill

MAX_OUT = 3000


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

    async def maybe_build(subtask, wr) -> None:
        if not (learn and wr.success and wr.trace):
            return
        try:
            skill = await builder.build(subtask, wr.trace, wr.summary, browser, wr.domain or None)
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
    async def t_delegate(a: dict) -> str:
        subtask = str(a.get("subtask", "")).strip()
        if not subtask:
            return "ERROR: subtask is required"
        start_url = a.get("start_url") or None
        emit("delegate", {"subtask": subtask, "start_url": start_url})
        hints = ""
        if use_memory:
            try:
                hints = await memory.lessons_for(subtask + " " + (start_url or ""))
            except Exception:
                hints = ""
        wr = await run_worker(subtask, start_url, llm=llm, browser=browser, cfg=cfg, on_event=on_event, hints=hints)
        stats["steps"] += wr.steps
        digest.append(f"subtask '{subtask}' -> {'ok' if wr.success else 'FAILED'} in {wr.steps} steps: {wr.summary[:200]}")
        for s in wr.trace[:25]:
            digest.append(f"  {s.action} {json.dumps(s.args, default=str)[:80]} {s.locator or ''} ok={s.ok}")
        await maybe_build(subtask, wr)
        data = "" if wr.data is None else json.dumps(wr.data, default=str)
        return _clip(f"{'SUCCESS' if wr.success else 'FAILED'} ({wr.steps} steps, {wr.domain})\n"
                     f"summary: {wr.summary}\ndata: {_clip(data, 2200)}")

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

    def t_todos(a: dict) -> str:
        todos[:] = a.get("todos") or []
        emit("todos", {"todos": todos})
        return f"todos saved ({len(todos)} items)"

    def t_write_file(a: dict) -> str:
        name = str(a.get("name", "notes.txt")).replace("/", "_") or "notes.txt"
        workspace[name] = str(a.get("content", ""))
        if run_dir:
            try:
                (Path(run_dir) / name).write_text(workspace[name])
            except Exception:
                pass
        return f"saved {name} ({len(workspace[name])} chars)"

    def t_read_file(a: dict) -> str:
        name = str(a.get("name", ""))
        return _clip(workspace[name]) if name in workspace else f"ERROR: no file '{name}'. Files: {list(workspace)}"

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
             f"Allowed domains: {', '.join(cfg.allowed_domains)}. Max {cfg.planner_max_steps} tool rounds.\n\n"
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
                emit("warn", {"message": f"planner replied without a tool call; nudge {nudges}/3"})
                reply = await llm.chat(
                    role="planner", thread_id=thread, system=PLANNER_SYSTEM, tools=tools,
                    message="You have not finished. Do not describe plans in prose. Call a tool NOW: "
                            "delegate_browse(subtask, start_url) to do browsing, run_skill if a skill fits, "
                            "or final_answer(answer) with the actual requested data once you have it.")
                last_text = reply.text or last_text
                continue
            rounds += 1
            outputs, finished = [], False
            for tc in reply.tool_calls:
                a = tc.args or {}
                try:
                    if tc.name == "final_answer":
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
                    elif tc.name == "delegate_browse":
                        out = await t_delegate(a)
                    else:
                        out = f"ERROR: unknown tool '{tc.name}'"
                except Exception as e:  # noqa: BLE001
                    out = f"ERROR: tool crashed: {e!r}"
                    emit("warn", {"message": out})
                outputs.append({"tool_call_id": tc.id, "output": _clip(out)})
            if finished:
                ok = True
                break
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
