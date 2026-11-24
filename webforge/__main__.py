"""WebForge CLI: python -m webforge <command>."""
from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import sys
import time
from pathlib import Path


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="webforge", description="WebForge: a self-improving web-browsing deep agent")
    sub = p.add_subparsers(dest="cmd")

    r = sub.add_parser("run", help="run a task")
    r.add_argument("task")
    r.add_argument("--mock", action="store_true", help="offline MockLLM (CLI smoke test)")
    r.add_argument("--headed", action="store_true")
    r.add_argument("--no-learn", action="store_true")
    r.add_argument("--no-memory", action="store_true")
    r.add_argument("--no-skills", action="store_true")
    r.add_argument("--plain", action="store_true", help="plain line output instead of live UI")

    s = sub.add_parser("skills", help="manage learned skills")
    ss = s.add_subparsers(dest="skills_cmd", required=True)
    ss.add_parser("list")
    sh = ss.add_parser("show"); sh.add_argument("name")
    rm = ss.add_parser("rm"); rm.add_argument("name")

    m = sub.add_parser("memory", help="manage memory")
    ms = m.add_subparsers(dest="memory_cmd", required=True)
    ms.add_parser("list")
    ms.add_parser("reset")

    b = sub.add_parser("bench", help="benchmark")
    bs = b.add_subparsers(dest="bench_cmd", required=True)
    br = bs.add_parser("run")
    br.add_argument("--mode", choices=["cold", "learned"], required=True)
    br.add_argument("--tasks", default="", help="comma-separated task ids")
    br.add_argument("--repeat", type=int, default=1)
    br.add_argument("--mock", action="store_true")
    rp = bs.add_parser("report")
    rp.add_argument("cold"); rp.add_argument("learned")

    w = sub.add_parser("serve", help="web UI + API server")
    w.add_argument("--port", type=int, default=8000)
    w.add_argument("--host", default="127.0.0.1")

    f = sub.add_parser("serve-fixtures", help="serve the offline demo site")
    f.add_argument("--port", type=int, default=8765)
    return p


async def _cmd_run(args) -> int:
    from rich.console import Console
    from webforge.browser.session import Browser
    from webforge.config import Config
    from webforge.llm import make_llm
    from webforge.memory import make_memory
    from webforge.skills.library import SkillLibrary
    from webforge.ui import LiveUI, PlainUI, render_result
    from webforge.agent.planner import run_task  # lazy: may not exist in partial checkouts

    cfg = Config()
    console = Console()
    llm = make_llm(cfg, mock=args.mock)
    memory = make_memory(cfg, local_only=args.mock)
    library = SkillLibrary(cfg.skills_dir)
    browser = Browser(chromium_path=cfg.chromium_path, allowed_domains=cfg.allowed_domains)
    run_dir = cfg.runs_dir / time.strftime("%Y%m%d_%H%M%S")
    run_dir.mkdir(parents=True, exist_ok=True)
    result = None
    try:
        await browser.start(headless=not args.headed)
        ui = PlainUI(args.task, console) if args.plain else LiveUI(args.task, console)
        with ui:
            result = await run_task(args.task, llm=llm, browser=browser, library=library, memory=memory,
                                    cfg=cfg, on_event=ui.on_event, learn=not args.no_learn,
                                    use_memory=not args.no_memory, use_skills=not args.no_skills,
                                    run_dir=run_dir)
        render_result(result, console)
        (run_dir / "result.json").write_text(
            json.dumps(dataclasses.asdict(result), indent=2, ensure_ascii=False, default=str), encoding="utf-8")
        console.print(f"Run saved to {run_dir}")
    finally:
        for closer in (browser.close, getattr(llm, "aclose", None), getattr(memory, "aclose", None)):
            if closer:
                try:
                    await closer()
                except Exception:
                    pass
    return 0 if result is not None and result.ok else 1


def _cmd_skills(args) -> int:
    from rich.console import Console
    from rich.table import Table
    from webforge.config import Config
    from webforge.skills.library import SkillLibrary, is_demoted
    console = Console()
    lib = SkillLibrary(Config().skills_dir)
    if args.skills_cmd == "list":
        t = Table(title="Skills")
        for c in ("name", "domain", "params", "steps", "uses", "ok", "description"):
            t.add_column(c)
        for s in lib.list(include_demoted=True):
            t.add_row(s.name + (" (demoted)" if is_demoted(s) else ""), s.domain, ",".join(s.params),
                      str(len(s.steps)), str(s.uses), str(s.successes), s.description[:70])
        console.print(t)
    elif args.skills_cmd == "show":
        s = lib.get(args.name)
        if not s:
            console.print(f"No such skill: {args.name}")
            return 1
        console.print_json(json.dumps(s.to_dict(), ensure_ascii=False))
    else:
        ok = lib.delete(args.name)
        console.print("Deleted." if ok else f"No such skill: {args.name}")
        return 0 if ok else 1
    return 0


async def _cmd_memory(args) -> int:
    from rich.console import Console
    from webforge.config import Config
    from webforge.memory import make_memory
    console = Console()
    mem = make_memory(Config())
    try:
        if args.memory_cmd == "list":
            items = await mem.list()
            for i in items:
                console.print(f"- {i.get('content', '')[:200]}  [dim]{i.get('metadata', {})}[/dim]")
            console.print(f"{len(items)} memories")
        else:
            await mem.reset()
            console.print("Memory reset.")
    finally:
        if hasattr(mem, "aclose"):
            await mem.aclose()
    return 0


async def _cmd_bench(args) -> int:
    from bench.run_bench import report, run_bench
    if args.bench_cmd == "run":
        ids = [x.strip() for x in args.tasks.split(",") if x.strip()] or None
        await run_bench(args.mode, ids, repeat=args.repeat, mock=args.mock)
    else:
        report(args.cold, args.learned)
    return 0


def _cmd_serve(args) -> int:
    import functools
    import http.server
    from webforge.config import ROOT
    directory = str(ROOT / "fixtures" / "site")
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=directory)
    with http.server.ThreadingHTTPServer(("127.0.0.1", args.port), handler) as srv:
        print(f"Serving {directory} at http://localhost:{args.port}/  (Ctrl-C to stop)")
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            pass
    return 0


def main(argv=None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if not args.cmd:
        parser.print_help()
        return 0
    if args.cmd == "run":
        return asyncio.run(_cmd_run(args))
    if args.cmd == "skills":
        return _cmd_skills(args)
    if args.cmd == "memory":
        return asyncio.run(_cmd_memory(args))
    if args.cmd == "bench":
        return asyncio.run(_cmd_bench(args))
    if args.cmd == "serve":
        from webforge.server import serve
        serve(args.host, args.port)
        return 0
    if args.cmd == "serve-fixtures":
        return _cmd_serve(args)
    return 1


if __name__ == "__main__":
    sys.exit(main())
