"""Live terminal UI for WebForge runs (Rich), plus a plain line-per-event fallback."""
from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any, Optional

from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

STYLES = {
    "task_start": ("bold white", "*"),
    "delegate": ("cyan", "->"),
    "search": ("cyan", "?"),
    "nudge": ("dim", "…"),
    "budget": ("yellow", "⏱"),
    "parallel": ("bold cyan", "⇉"),
    "read_urls": ("cyan", "▤"),
    "action": ("white", ">"),
    "observe": ("dim", "o"),
    "ask": ("bold magenta", "?"),
    "answer": ("magenta", "←"),
    "analyze": ("cyan", "∑"),
    "output": ("bold green", "+"),
    "verify": ("yellow", "✓"),
    "verdict": ("yellow", "✓"),
    "resume": ("bold white", "↻"),
    "final": ("bold green", "="),
    "warn": ("bold red", "!"),
}


def _trunc(s: Any, n: int = 100) -> str:
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[: n - 1] + "…"


def describe_event(kind: str, data: dict) -> str:
    d = data or {}
    line = _describe(kind, d)
    return f"[{d['agent']}] {line}" if d.get("agent") else line


def _describe(kind: str, d: dict) -> str:
    if kind == "task_start":
        return _trunc(d.get("task", ""))
    if kind == "todos":
        return f"{len(d.get('todos') or [])} todos"
    if kind == "parallel":
        return f"parallel: {d.get('n')} sub-agents {', '.join(d.get('agents') or [])}"
    if kind == "read_urls":
        return f"read {len(d.get('urls') or [])} page(s): {_trunc(', '.join(d.get('urls') or []), 80)}"
    if kind == "search":
        return f"web search: {_trunc(d.get('query', ''))}"
    if kind == "delegate":
        return f"delegate: {_trunc(d.get('subtask', ''))}"
    if kind == "observe":
        return f"observe {_trunc(d.get('url', ''), 60)} ({d.get('n_elements', '?')} elements)"
    if kind == "action":
        mark = "ok" if d.get("ok", True) else "FAIL"
        return f"{d.get('action')} {_trunc(d.get('args', ''), 50)} [{mark}] {_trunc(d.get('message', ''), 50)}"
    if kind == "ask":
        return f"QUESTION: {_trunc(d.get('question', ''), 160)}"
    if kind == "answer":
        return f"answer: {_trunc(d.get('answer', ''))}"
    if kind == "analyze":
        return f"analyst: {_trunc(d.get('instruction', ''))} over {len(d.get('files') or [])} file(s)"
    if kind == "output":
        return f"deliverable saved: {d.get('name')} ({d.get('chars')} chars)"
    if kind == "verdict":
        return ("verified complete" if d.get("complete") else
                f"verifier: incomplete, missing {_trunc(d.get('missing'), 80)}")
    if kind == "resume":
        return f"resuming with {d.get('files')} files, {d.get('todos')} todos"
    if kind == "final":
        return f"final: {_trunc(d.get('answer', ''))}"
    if kind == "warn":
        return f"warn: {_trunc(d.get('message', ''))}"
    if kind in ("nudge", "verify", "budget"):
        return _trunc(d.get("message", ""))
    return f"{kind} {_trunc(d)}"


def _todo_parts(t: Any) -> tuple[str, str]:
    """Return (status, text) for a todo given as dict or str."""
    if isinstance(t, dict):
        text = t.get("text") or t.get("title") or t.get("task") or t.get("content") or str(t)
        st = str(t.get("status") or ("done" if t.get("done") else "pending")).lower()
        return st, str(text)
    return "pending", str(t)


class PlainUI:
    """One printed line per event."""

    def __init__(self, task: str = "", console: Optional[Console] = None):
        self.task = task
        self.console = console or Console()
        self.t0 = time.time()
        self._last_metrics = 0.0

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    async def ask(self, question: str, options: list, agent: str = "planner") -> str:
        return await _prompt(self.console, question, options)

    def on_event(self, kind: str, data: dict) -> None:
        if kind == "screenshot":
            return
        if kind == "metrics":
            self._metrics_line(data or {})
            return
        try:
            if kind == "todos":
                for t in (data or {}).get("todos") or []:
                    st, text = _todo_parts(t)
                    self.console.print(f"   [{st}] {_trunc(text)}", markup=False, highlight=False)
                return
            self.console.print(f"[{time.time() - self.t0:6.1f}s] {kind:<12} {describe_event(kind, data)}",
                               markup=False, highlight=False)
        except Exception:
            pass


    def _metrics_line(self, d: dict) -> None:
        now = time.monotonic()
        if self._last_metrics and now - self._last_metrics < 5.0:
            return
        self._last_metrics = now
        try:
            self.console.print(
                f"[{time.time() - self.t0:6.1f}s] metrics      {float(d.get('elapsed', 0)):.0f}s, "
                f"{d.get('steps', 0)} steps, {d.get('llm_calls', 0)} calls, {d.get('tokens', 0):,} tokens, "
                f"${float(d.get('run_cost', 0)):.4f}/${float(d.get('run_budget', 0)):.2f}",
                markup=False, highlight=False)
        except Exception:
            pass


class LiveUI:
    """Rich Live dashboard. Falls back to PlainUI behaviour when stdout is not a TTY."""

    def __init__(self, task: str, console: Optional[Console] = None, max_log: int = 14):
        self.task = task
        self.console = console or Console()
        self.max_log = max_log
        self.t0 = time.time()
        self.todos: list = []
        self.log: list[tuple[str, str]] = []
        self.steps = 0
        self.outputs: list[str] = []
        self.interactive = self.console.is_terminal
        self._live: Optional[Live] = None
        self._plain = PlainUI(task, self.console)

    # -- context manager
    def __enter__(self):
        self.t0 = time.time()
        self._plain.t0 = self.t0
        if self.interactive:
            self._live = Live(self._render(), console=self.console, refresh_per_second=4, transient=False)
            self._live.__enter__()
        return self

    def __exit__(self, *a):
        if self._live is not None:
            try:
                self._live.update(self._render())
                self._live.__exit__(*a)
            finally:
                self._live = None
        return False

    # -- human in the loop: pause the live view while asking
    async def ask(self, question: str, options: list, agent: str = "planner") -> str:
        live = self._live
        if live is not None:
            live.stop()
        try:
            return await _prompt(self.console, question, options)
        finally:
            if live is not None:
                live.start()

    # -- events
    def on_event(self, kind: str, data: dict) -> None:
        data = data or {}
        if kind in ("screenshot", "metrics"):
            return
        try:
            if kind == "todos":
                self.todos = list(data.get("todos") or [])
            elif kind == "action":
                self.steps += 1
            elif kind == "output" and data.get("name") not in self.outputs:
                self.outputs.append(data.get("name"))
            if kind != "todos":
                self.log.append((kind, describe_event(kind, data)))
                self.log = self.log[-200:]
            if self._live is not None:
                self._live.update(self._render())
            elif not self.interactive:
                self._plain.on_event(kind, data)
        except Exception:
            pass

    # -- rendering
    def _render(self):
        elapsed = time.time() - self.t0
        header = Panel(Text.assemble(("Task: ", "bold"), _trunc(self.task, 160),
                                     (f"   [{elapsed:5.1f}s]", "dim")), title="WebForge", border_style="cyan")
        todo_txt = Text()
        if not self.todos:
            todo_txt.append("(planning...)", style="dim")
        for t in self.todos:
            st, text = _todo_parts(t)
            if st in ("done", "completed", "complete", "x"):
                todo_txt.append("✔ " + _trunc(text, 60) + "\n", style="green")
            elif st in ("doing", "in_progress", "active", "running"):
                todo_txt.append("▶ " + _trunc(text, 60) + "\n", style="bold yellow")
            else:
                todo_txt.append("○ " + _trunc(text, 60) + "\n", style="dim")
        log_txt = Text()
        for kind, line in self.log[-self.max_log:]:
            style, icon = STYLES.get(kind, ("white", "·"))
            log_txt.append(f"{icon} ", style=style)
            log_txt.append(_trunc(line, 110) + "\n", style=style)
        stats = Text(f"browser steps: {self.steps}   deliverables: {', '.join(self.outputs) or '-'}", style="bold")
        layout = Layout()
        layout.split_column(Layout(header, size=3),
                            Layout(name="body"),
                            Layout(Panel(stats, border_style="dim"), size=3))
        layout["body"].split_row(Layout(Panel(todo_txt, title="TODO", border_style="yellow"), ratio=2),
                                 Layout(Panel(log_txt, title="Activity", border_style="blue"), ratio=5))
        layout.size = None
        return _Fixed(layout, height=self.max_log + 8)


class _Fixed:
    """Render a Layout at a fixed height so Live does not fill the whole terminal."""

    def __init__(self, layout, height: int):
        self.layout, self.height = layout, height

    def __rich_console__(self, console, options):
        yield from console.render(self.layout, options.update_height(self.height))


async def _prompt(console: Console, question: str, options: list) -> str:
    console.print(Panel(question + (f"\n\noptions: {' / '.join(options)}" if options else ""),
                        title="WebForge needs you", border_style="magenta"))
    try:
        return (await asyncio.to_thread(input, "your answer> ")).strip()
    except (EOFError, KeyboardInterrupt):
        return ""


def render_result(result, console: Optional[Console] = None, run_dir: Optional[Path] = None) -> None:
    console = console or Console()
    console.print(Panel(str(result.answer or "(no answer)"), title="Final answer" + ("" if result.ok else " (incomplete)"),
                        border_style="green" if result.ok else "red"))
    t = Table(title="Metrics", show_header=False)
    t.add_column("metric", style="bold")
    t.add_column("value")
    t.add_row("browser steps", str(result.steps))
    t.add_row("llm_calls", str(result.llm_calls))
    t.add_row("tokens", f"{result.tokens:,}")
    t.add_row("cost", f"${result.cost_usd:.4f}")
    t.add_row("seconds", f"{result.seconds:.1f}")
    v = result.verification or {}
    if v:
        t.add_row("verifier", "complete" if v.get("complete") else f"incomplete: {v.get('missing')}")
    for name in result.outputs:
        t.add_row("deliverable", str(Path(run_dir) / name) if run_dir else name)
    console.print(t)
