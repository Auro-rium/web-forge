"""WebForge CLI: python -m webforge <command>."""
from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import os
import sys
import time
from pathlib import Path


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="webforge", description="WebForge: an autonomous computer-use agent")
    sub = p.add_subparsers(dest="cmd")

    def run_opts(r):
        r.add_argument("--mock", action="store_true", help="offline MockLLM (CLI smoke test)")
        r.add_argument("--headed", action="store_true", help="show the browser window")
        r.add_argument("--plain", action="store_true", help="plain line output instead of the live view")
        r.add_argument("--approval", choices=["ask", "auto", "deny"], default=None,
                       help="consequential actions (submit/send/login/delete): ask (default), auto, deny")
        r.add_argument("--cdp", metavar="URL", default=None,
                       help="drive your own Chrome via remote debugging (sets WEBFORGE_CDP_URL)")
        r.add_argument("--secret", action="append", default=[], metavar="NAME=env:VAR",
                       help="make a secret available as {{secret:NAME}}, read from environment variable VAR")

    r = sub.add_parser("run", help="run a task")
    r.add_argument("task")
    run_opts(r)

    rs = sub.add_parser("resume", help="resume an interrupted run from its checkpoint")
    rs.add_argument("run_id", help="folder name under runs/")
    run_opts(rs)

    w = sub.add_parser("serve", help="web UI + API server")
    w.add_argument("--port", type=int, default=None, help="default: $PORT or 8000")
    w.add_argument("--host", default="127.0.0.1")
    w.add_argument("--cdp", metavar="URL", default=None, help="drive your own Chrome (sets WEBFORGE_CDP_URL)")
    w.add_argument("--headed", action="store_true", help="show the real browser window while the agent runs")

    c = sub.add_parser("chrome", help="launch a Chrome with remote debugging for WebForge to drive")
    c.add_argument("--port", type=int, default=None, help="default: 9222")
    c.add_argument("--profile", default=None, help="dedicated profile directory")
    c.add_argument("--stop-on-exit", action="store_true", help="close Chrome when this command exits")

    f = sub.add_parser("serve-fixtures", help="serve the offline test site (forms, data pages)")
    f.add_argument("--port", type=int, default=8765)
    return p


def _secrets(specs: list[str]):
    from webforge.safety import Secrets
    s = Secrets()
    for spec in specs:
        name, _, src = spec.partition("=")
        if src.startswith("env:"):
            s.set(name, os.getenv(src[4:], ""))
        else:
            raise SystemExit(f"--secret {spec}: use NAME=env:VARIABLE (values never go on the command line)")
    return s


async def _cmd_run(args, task: str, run_dir: Path, resume: dict | None = None) -> int:
    from rich.console import Console
    from webforge.agent.planner import run_task
    from webforge.browser.session import open_browser
    from webforge.config import Config
    from webforge.llm import make_llm
    from webforge.ui import LiveUI, PlainUI, render_result

    cfg = Config()
    if args.approval:
        cfg.approval = args.approval
    console = Console()
    llm = make_llm(cfg, mock=args.mock)
    run_dir.mkdir(parents=True, exist_ok=True)
    result = None
    browser = None
    try:
        try:
            browser = await open_browser(cfg, _secrets(args.secret), headless=not args.headed)
        except RuntimeError as e:
            print(str(e), file=sys.stderr)
            return 2
        ui = PlainUI(task, console) if args.plain else LiveUI(task, console)
        with ui:
            result = await run_task(task, llm=llm, browser=browser, cfg=cfg, on_event=ui.on_event,
                                    on_ask=ui.ask, run_dir=run_dir, resume=resume)
        render_result(result, console, run_dir)
        (run_dir / "result.json").write_text(
            json.dumps(dataclasses.asdict(result), indent=2, ensure_ascii=False, default=str), encoding="utf-8")
        console.print(f"Run saved to {run_dir}  (resume with: python -m webforge resume {run_dir.name})")
    finally:
        for closer in (getattr(browser, "close", None), getattr(llm, "aclose", None)):
            if closer is None:
                continue
            try:
                await closer()
            except Exception:
                pass
    return 0 if result is not None and result.ok else 1


def _cmd_chrome(args) -> int:
    from webforge import chrome as ch
    port = args.port or ch.DEFAULT_PORT
    profile = Path(args.profile) if args.profile else ch.DEFAULT_PROFILE
    try:
        proc, cmd = ch.launch_chrome(port=port, profile=profile, headless=False)
    except RuntimeError as e:
        print(str(e), file=sys.stderr)
        return 2
    url = f"http://127.0.0.1:{port}"
    print(f"Chrome: {cmd[0]}")
    print("Command: " + " ".join(cmd))
    print("Point WebForge at it (same terminal, before run/serve):")
    print(f"  export WEBFORGE_CDP_URL={url}")
    print(f"  (Windows cmd:  set WEBFORGE_CDP_URL={url})")
    print("Chrome is running. Press Ctrl-C to exit" + (" (Chrome will be closed)." if args.stop_on_exit
                                                        else " (Chrome stays open)."))
    try:
        while proc.poll() is None:
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        if args.stop_on_exit:
            ch.stop_chrome(proc)
    return 0


def _cmd_fixtures(args) -> int:
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
    if getattr(args, "cdp", None):
        os.environ["WEBFORGE_CDP_URL"] = args.cdp
    if args.cmd == "chrome":
        return _cmd_chrome(args)
    if args.cmd == "run":
        from webforge.config import Config
        return asyncio.run(_cmd_run(args, args.task, Config().runs_dir / time.strftime("%Y%m%d_%H%M%S")))
    if args.cmd == "resume":
        from webforge.agent.planner import load_checkpoint
        from webforge.config import Config
        run_dir = Config().runs_dir / args.run_id
        cp = load_checkpoint(run_dir)
        if not cp:
            print(f"No checkpoint in {run_dir}")
            return 1
        return asyncio.run(_cmd_run(args, cp["task"], run_dir, resume=cp))
    if args.cmd == "serve":
        if args.headed:
            os.environ["WEBFORGE_HEADED"] = "1"
        from webforge.server import serve
        serve(args.host, args.port or int(os.getenv("PORT", "8000") or 8000))
        return 0
    if args.cmd == "serve-fixtures":
        return _cmd_fixtures(args)
    return 1


if __name__ == "__main__":
    sys.exit(main())
