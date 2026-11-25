"""Live terminal UI for WebForge runs (Rich), plus a plain line-per-event fallback."""
from __future__ import annotations

import time
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
    "skill_run": ("magenta", "#"),
    "skill_result": ("magenta", "#"),
    "observe": ("dim", "o"),
    "lesson": ("yellow", "!"),
    "skill_built": ("bold green", "+"),
    "memory_hits": ("blue", "~"),
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
    if kind == "skill_run":
        return f"skill {d.get('name')} {_trunc(d.get('params', ''), 60)}"
    if kind == "skill_result":
        return f"skill {d.get('name')} {'ok' if d.get('ok') else 'FAILED'}: {_trunc(d.get('message', ''), 70)}"
    if kind == "observe":
        return f"observe {_trunc(d.get('url', ''), 60)} ({d.get('n_elements', '?')} elements)"
    if kind == "action":
        mark = "ok" if d.get("ok", True) else "FAIL"
        return f"{d.get('action')} {_trunc(d.get('args', ''), 50)} [{mark}] {_trunc(d.get('message', ''), 50)}"
    if kind == "memory_hits":
        return f"memory: {_trunc(d.get('text', ''))}"
    if kind == "skill_built":
        return f"built skill {d.get('name')}"
    if kind == "lesson":
        return f"lesson: {_trunc(d.get('text', ''))}"
    if kind == "final":
        return f"final: {_trunc(d.get('answer', ''))}"
    if kind == "warn":
        return f"warn: {_trunc(d.get('message', ''))}"
    if kind == "nudge":
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

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def on_event(self, kind: str, data: dict) -> None:
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
        self.skills_used = 0
        self.skills_built = 0
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

    # -- events
    def on_event(self, kind: str, data: dict) -> None:
        data = data or {}
        try:
            if kind == "todos":
                self.todos = list(data.get("todos") or [])
            elif kind == "action":
                self.steps += 1
            elif kind == "skill_run":
                self.skills_used += 1
            elif kind == "skill_built":
                self.skills_built += 1
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
        stats = Text(f"steps: {self.steps}   skills used: {self.skills_used}   skills built: {self.skills_built}",
                     style="bold")
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


def render_result(result, console: Optional[Console] = None) -> None:
    console = console or Console()
    console.print(Panel(str(result.answer or "(no answer)"), title="Final answer" + ("" if result.ok else " (incomplete)"),
                        border_style="green" if result.ok else "red"))
    t = Table(title="Metrics", show_header=False)
    t.add_column("metric", style="bold")
    t.add_column("value")
    t.add_row("steps", str(result.steps))
    t.add_row("llm_calls", str(result.llm_calls))
    t.add_row("tokens", f"{result.tokens:,}")
    t.add_row("cost", f"${result.cost_usd:.4f}")
    t.add_row("seconds", f"{result.seconds:.1f}")
    t.add_row("skills used", ", ".join(result.skills_used) or "-")
    t.add_row("skills created", ", ".join(result.skills_created) or "-")
    console.print(t)
