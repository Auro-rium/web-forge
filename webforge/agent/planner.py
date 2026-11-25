"""Planner: the deep-agent orchestrator.

The planner never touches the browser. It plans (todos), keeps a file workspace, delegates browsing to
worker sub-agents (one, or many in parallel, each with its own LLM thread and tab), searches and reads
the web, hands synthesis to an analyst sub-agent, asks the user when genuinely blocked, and has its
final answer checked by a verifier sub-agent. Runs are bounded by a time and $ budget and checkpointed
after every round so they can be resumed.
"""
from __future__ import annotations

import asyncio
import json
import time
from datetime import date
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from webforge.agent.prompts import ANALYST_SYSTEM, PLANNER_SYSTEM, PLANNER_TOOLS
from webforge.agent.verifier import verify
from webforge.agent.worker import WorkerResult, run_worker
from webforge.contracts import RunResult
from webforge.safety import domain_allowed

MAX_OUT = 3000
OFFLOAD_PREVIEW = 1800
ANALYZE_MAX_CHARS = 60000
RESEARCH_TOOLS = {"web_search", "read_urls", "delegate_browse", "parallel_browse"}
NO_ANSWER = "No answer from the user. Proceed with your best judgment and state the assumption you made."

AskFn = Callable[..., Awaitable[Optional[str]]]   # ask(question, options, agent='planner')


def _clip(s: Any, n: int = MAX_OUT) -> str:
    s = s if isinstance(s, str) else json.dumps(s, default=str)
    return s if len(s) <= n else s[:n] + f"...[truncated {len(s) - n} chars]"


async def ask_with_timeout(on_ask: Optional[AskFn], question: str, options: list, timeout: float, agent: str = "planner") -> str:
    """Ask the human; no callback, timeout, error or empty reply all become NO_ANSWER."""
    if on_ask is None:
        return NO_ANSWER
    try:
        ans = await asyncio.wait_for(on_ask(question, options, agent), timeout=timeout)
    except Exception:
        return NO_ANSWER
    return (ans or "").strip() or NO_ANSWER


def load_checkpoint(run_dir: Path) -> Optional[dict]:
    try:
        return json.loads((Path(run_dir) / "checkpoint.json").read_text())
    except Exception:
        return None


async def run_task(task: str, *, llm, browser, cfg, on_event: Optional[Callable] = None,
                   on_ask: Optional[AskFn] = None, run_dir: Optional[Path] = None,
                   resume: Optional[dict] = None) -> RunResult:
    t0 = time.monotonic()
    u0 = llm.usage()

    def emit(kind: str, data: dict) -> None:
        if on_event:
            try:
                on_event(kind, data)
            except Exception:
                pass

    st = resume or {}
    workspace: dict[str, str] = dict(st.get("workspace") or {})
    todos: list = list(st.get("todos") or [])
    digest: list[str] = list(st.get("digest") or [])
    stats = {"steps": int(st.get("steps", 0)), "verify_rounds": 0,
             "spent0": float(st.get("spent", 0.0)), "elapsed0": float(st.get("elapsed", 0.0))}
    verification: dict = {}
    counters: dict[str, int] = {}
    if run_dir:
        try:
            Path(run_dir).mkdir(parents=True, exist_ok=True)
        except Exception:
            run_dir = None

    budget_usd = float(cfg.run_budget_usd)
    deadline_s = float(cfg.run_deadline_s)

    def spent() -> float:
        return stats["spent0"] + llm.usage()["cost_usd"] - u0["cost_usd"]

    def elapsed() -> float:
        return stats["elapsed0"] + time.monotonic() - t0

    def budget_used() -> float:
        return max(spent() / budget_usd if budget_usd > 0 else 0.0,
                   elapsed() / deadline_s if deadline_s > 0 else 0.0)

    # ------------------------------------------------------------ workspace --
    def save(name: str, content: str) -> None:
        workspace[name] = content
        if run_dir:
            try:
                root = Path(run_dir).resolve()
                f = (root / name).resolve()
                if root in f.parents:
                    f.parent.mkdir(parents=True, exist_ok=True)
                    f.write_text(content)
            except Exception:
                pass

    def offload(kind: str, text: str, ext: str = "md") -> str:
        """Keep big outputs out of the planner's context: full text goes to a workspace file."""
        if len(text) <= MAX_OUT:
            return text
        counters[kind] = counters.get(kind, 0) + 1
        name = f"results/{kind}-{counters[kind]}.{ext}"
        save(name, text)
        return (text[:OFFLOAD_PREVIEW] + f"\n...[{len(text) - OFFLOAD_PREVIEW} more chars saved to file '{name}'; "
                f"use read_file(name='{name}', offset={OFFLOAD_PREVIEW}) or analyze(files=['{name}'], ...)]")

    def outputs() -> list[str]:
        return sorted(n for n in workspace if n.startswith("outputs/"))

    def checkpoint(status: str, rounds: int) -> None:
        if not run_dir:
            return
        try:
            (Path(run_dir) / "checkpoint.json").write_text(json.dumps({
                "task": task, "status": status, "rounds": rounds, "todos": todos, "workspace": workspace,
                "digest": digest[-80:], "steps": stats["steps"], "spent": spent(), "elapsed": elapsed(),
            }, ensure_ascii=False, default=str))
        except Exception:
            pass

    sem = asyncio.Semaphore(max(1, int(cfg.max_parallel)))
    agent_n = {"n": 0}

    def tagged(agent: str) -> Callable:
        def _ev(kind: str, data: dict) -> None:
            emit(kind, {**(data or {}), "agent": agent})
        return _ev

    async def ask_human(question: str, options: list, agent: str = "planner") -> str:
        emit("ask", {"question": question, "options": options, "agent": agent})
        ans = await ask_with_timeout(on_ask, question, options, float(cfg.ask_timeout_s), agent)
        emit("answer", {"answer": ans, "agent": agent})
        digest.append(f"asked user: {question[:160]} -> {ans[:120]}")
        return ans

    # ----------------------------------------------------------- browsing --
    async def _browse(subtask: str, start_url, tab, agent: str) -> WorkerResult:
        ident = getattr(tab, "identify", None)
        if ident:
            try:
                ident(agent)
            except Exception:
                pass
        ev = tagged(agent)
        ev("agent_state", {"state": "running", "subtask": subtask[:200]})
        try:
            wr = await run_worker(subtask, start_url, llm=llm, browser=tab, cfg=cfg, on_event=ev,
                                  ask=ask_human, agent=agent)
        except Exception:
            ev("agent_state", {"state": "failed", "steps": 0, "last": "worker crashed"})
            raise
        ev("agent_state", {"state": "done" if wr.success else "failed", "steps": wr.steps,
                           "last": wr.summary[:100]})
        stats["steps"] += wr.steps
        digest.append(f"[{agent}] '{subtask[:120]}' -> {'ok' if wr.success else 'FAILED'} in {wr.steps} steps: "
                      f"{wr.summary[:200]}")
        return wr

    def _report(agent: str, wr: WorkerResult) -> str:
        data = "" if wr.data is None else json.dumps(wr.data, default=str, ensure_ascii=False)
        head = (f"[{agent}] {'SUCCESS' if wr.success else 'FAILED'} ({wr.steps} steps, {wr.domain})\n"
                f"summary: {wr.summary}\n")
        if wr.denied:
            head += (f"USER DID NOT APPROVE: {'; '.join(wr.denied)}. Do not retry it and do not start another "
                     "sub-agent for it: tell the user in final_answer that it was not done and why.\n")
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
        return offload("browse", _report(agent, await _browse(subtask, start_url, browser, agent)))

    async def t_parallel(a: dict) -> str:
        jobs = []
        for it in (a.get("subtasks") or [])[:8]:
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
                    return agent, await _browse(subtask, start_url, tab, agent)
                except Exception as e:  # noqa: BLE001
                    return agent, WorkerResult(False, f"worker crashed: {e!r}")
                finally:
                    try:
                        await tab.close()
                    except Exception:
                        pass

        done = await asyncio.gather(*(one(j) for j in jobs))
        ok = sum(1 for _a, wr in done if wr.success)
        return offload("parallel", f"{ok}/{len(done)} subtasks succeeded\n\n" +
                       "\n\n".join(_report(agent, wr) for agent, wr in done))

    async def t_read_urls(a: dict) -> str:
        urls = [str(u).strip() for u in (a.get("urls") or ([a["url"]] if a.get("url") else []))
                if str(u).strip()][:8]
        if not urls:
            return "ERROR: urls must be a non-empty list"
        emit("read_urls", {"urls": urls})

        async def one(i: int, url: str) -> str:
            if not domain_allowed(url, cfg.allowed_domains):
                return f"## {url}\nBLOCKED: domain not in allowed list"
            async with sem:
                tab = await browser.new_tab()
                try:
                    if hasattr(tab, "identify"):
                        tab.identify("read")
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

    # --------------------------------------------------------- sub-agents --
    async def t_analyze(a: dict) -> str:
        names = [str(n) for n in (a.get("files") or [])]
        instruction = str(a.get("instruction", "")).strip()
        if not instruction:
            return "ERROR: instruction is required"
        missing = [n for n in names if n not in workspace]
        if missing:
            return f"ERROR: no file(s) {missing}. Files: {sorted(workspace)}"
        corpus = "\n\n".join(f"===== FILE {n} =====\n{workspace[n]}" for n in names)[:ANALYZE_MAX_CHARS]
        emit("analyze", {"files": names, "instruction": instruction[:200]})
        r = await llm.chat(role="analyst", system=ANALYST_SYSTEM, tools=[],
                           message=f"TASK CONTEXT: {task}\n\nINSTRUCTION: {instruction}\n\n{corpus or '(no files)'}")
        text = r.text or "(analyst returned nothing)"
        digest.append(f"analyze {names}: {instruction[:100]}")
        target = str(a.get("save_as") or "").strip().strip("/").replace("..", "_")
        if target:
            name = target if target.startswith("outputs/") else f"outputs/{target}"
            save(name, text)
            emit("output", {"name": name, "chars": len(text)})
            return offload("analysis", f"saved to '{name}'.\n\n{text}")
        return offload("analysis", text)

    async def t_ask(a: dict) -> str:
        q = str(a.get("question", "")).strip()
        if not q:
            return "ERROR: question is required"
        opts = [str(o) for o in (a.get("options") or [])][:6]
        return "USER ANSWER: " + await ask_human(q, opts)

    # ------------------------------------------------------ workspace tools --
    def t_todos(a: dict) -> str:
        todos[:] = a.get("todos") or []
        emit("todos", {"todos": todos})
        return f"todos saved ({len(todos)} items)"

    def t_write_file(a: dict) -> str:
        name = str(a.get("name", "notes.md")).replace("..", "_").strip("/") or "notes.md"
        save(name, str(a.get("content", "")))
        if name.startswith("outputs/"):
            emit("output", {"name": name, "chars": len(workspace[name])})
        return f"saved {name} ({len(workspace[name])} chars)"

    def t_read_file(a: dict) -> str:
        name = str(a.get("name", ""))
        if name not in workspace:
            return f"ERROR: no file '{name}'. Files: {sorted(workspace)}"
        text = workspace[name]
        off = max(0, int(a.get("offset", 0) or 0))
        lim = max(200, min(int(a.get("limit", MAX_OUT) or MAX_OUT), 8000))
        more = (f"\n...[{len(text) - off - lim} more chars; read_file(name='{name}', offset={off + lim})]"
                if off + lim < len(text) else "")
        return text[off:off + lim] + more

    tools_impl = {"write_todos": t_todos, "write_file": t_write_file, "read_file": t_read_file,
                  "web_search": t_web_search, "read_urls": t_read_urls, "delegate_browse": t_delegate,
                  "parallel_browse": t_parallel, "analyze": t_analyze, "ask_user": t_ask}

    async def finish_or_verify(candidate: str, hard_stop: bool) -> Optional[str]:
        """None = accept the answer; otherwise the verifier's feedback for the planner."""
        nonlocal verification
        if hard_stop or stats["verify_rounds"] >= int(cfg.verify_rounds):
            return None
        stats["verify_rounds"] += 1
        emit("verify", {"message": f"verifier checking the answer ({stats['verify_rounds']}/{cfg.verify_rounds})"})
        verification = await verify(task, candidate, digest, sorted(workspace), llm)
        emit("verdict", verification)
        if verification.get("complete", True):
            return None
        return ("VERIFIER: the answer is not complete yet.\n"
                f"missing: {verification.get('missing')}\nunsupported: {verification.get('unsupported_claims')}\n"
                "Fix these, then call final_answer again.")

    # ------------------------------------------------------------ first msg --
    secret_names = getattr(getattr(browser, "secrets", None), "names", lambda: [])()
    if resume:
        files = "\n".join(f"- {n} ({len(c)} chars)" for n, c in sorted(workspace.items())) or "(none)"
        first = (f"RESUMING AN INTERRUPTED RUN. TASK: {task}\n\nToday: {date.today().isoformat()}.\n"
                 f"Todos so far: {json.dumps(todos, default=str)[:1500]}\n\nWorkspace files:\n{files}\n\n"
                 "What was done (latest last):\n" + "\n".join(digest[-30:]) +
                 "\n\nContinue from here; do not redo finished work. Use read_file to recall details.")
        emit("resume", {"files": len(workspace), "todos": len(todos)})
    else:
        first = (f"TASK: {task}\n\nToday: {date.today().isoformat()}. "
                 f"Allowed domains: {', '.join(cfg.allowed_domains) or 'any public website'}. "
                 f"Budget: about ${budget_usd:.2f} and {int(deadline_s)} s.")
    if secret_names:
        first += f"\nSECRETS available to sub-agents as {{{{secret:NAME}}}}: {', '.join(secret_names)}"
    emit("task_start", {"task": task})

    # ----------------------------------------------------------------- loop --
    answer, ok = "", False
    rounds = int(st.get("rounds", 0))
    try:
        reply = await llm.chat(role="planner", message=first, system=PLANNER_SYSTEM, tools=PLANNER_TOOLS)
        thread = reply.thread_id
        last_text = reply.text or ""
        nudges = 0
        max_rounds = rounds + int(cfg.planner_max_steps)
        while rounds < max_rounds:
            if not (reply.status == "REQUIRES_ACTION" and reply.tool_calls):
                if nudges >= 3:
                    break
                nudges += 1
                if (reply.text or "").strip():   # an empty turn is a transport hiccup: continue silently
                    emit("nudge", {"message": f"planner replied in prose; asking it to act ({nudges}/3)"})
                reply = await llm.chat(
                    role="planner", thread_id=thread, system=PLANNER_SYSTEM, tools=PLANNER_TOOLS,
                    message="You have not finished. Do not describe plans in prose. Call a tool NOW, "
                            "or final_answer(answer) once the task is done.")
                last_text = reply.text or last_text
                continue
            rounds += 1
            nudges = 0
            used = budget_used()
            hard_stop = used >= 1.0
            outs, finished = [], False
            for tc in reply.tool_calls:
                a = tc.args or {}
                try:
                    if tc.name == "final_answer":
                        feedback = await finish_or_verify(str(a.get("answer", "")), hard_stop)
                        if feedback is None:
                            answer, finished = str(a.get("answer", "")), True
                            break
                        out = feedback
                    elif hard_stop and tc.name in RESEARCH_TOOLS:
                        out = ("ERROR: time/cost budget exhausted. Do not search or browse again: call final_answer "
                               "NOW with the best result you have, and state what is unfinished.")
                        emit("budget", {"message": "budget exhausted; asking for the final answer"})
                    elif tc.name in tools_impl:
                        r = tools_impl[tc.name](a)
                        out = await r if asyncio.iscoroutine(r) else r
                    else:
                        out = f"ERROR: unknown tool '{tc.name}'"
                except Exception as e:  # noqa: BLE001
                    out = f"ERROR: tool crashed: {e!r}"
                    emit("warn", {"message": out})
                outs.append({"tool_call_id": tc.id, "output": _clip(out, MAX_OUT + 600)})
            if finished:
                ok = True
                break
            checkpoint("running", rounds)
            if used >= 0.7 and outs and not hard_stop:
                outs[-1]["output"] += (f"\n\n[budget] {int(used * 100)}% of the time/cost budget used "
                                       f"(${spent():.3f}, {int(elapsed())} s). Wrap up: finish the essential "
                                       "parts and call final_answer, noting anything unfinished.")
            reply = await llm.submit(role="planner", thread_id=thread, outputs=outs)
            last_text = reply.text or last_text
        if not ok:
            answer = last_text or "(stopped before finishing)"
    except asyncio.CancelledError:
        checkpoint("cancelled", rounds)
        raise
    except Exception as e:  # noqa: BLE001
        answer = answer or f"(run crashed: {e!r})"
        emit("warn", {"message": f"planner loop crashed: {e!r}"})
        checkpoint("error", rounds)
    else:
        checkpoint("done" if ok else "stopped", rounds)

    emit("final", {"answer": answer, "ok": ok, "outputs": outputs(), "verification": verification})
    u1 = llm.usage()
    return RunResult(task=task, answer=answer, ok=ok, steps=stats["steps"],
                     llm_calls=u1["calls"] - u0["calls"], tokens=u1["tokens"] - u0["tokens"],
                     cost_usd=u1["cost_usd"] - u0["cost_usd"], seconds=time.monotonic() - t0,
                     outputs=outputs(), verification=verification, notes=dict(workspace))
