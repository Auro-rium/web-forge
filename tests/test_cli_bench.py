import io
import json
import subprocess
import sys
from pathlib import Path

import pytest
from rich.console import Console

from bench.run_bench import compare, evaluate_check, load_tasks, report
from webforge.contracts import RunResult
from webforge.ui import LiveUI, PlainUI, render_result

ROOT = Path(__file__).resolve().parent.parent

EVENTS = [
    ("task_start", {"task": "t"}),
    ("todos", {"todos": [{"text": "a", "status": "done"}, {"text": "b", "status": "doing"}, "c"]}),
    ("delegate", {"subtask": "x" * 500}),
    ("skill_run", {"name": "s", "params": {"a": 1}}),
    ("skill_result", {"name": "s", "ok": True, "message": "m"}),
    ("observe", {"url": "http://x", "n_elements": 3}),
    ("action", {"action": "click", "args": {"idx": 1}, "ok": False, "message": "boom"}),
    ("memory_hits", {"text": "hit"}),
    ("skill_built", {"name": "s"}),
    ("lesson", {"text": "l"}),
    ("final", {"answer": "done"}),
    ("warn", {"message": "w"}),
    ("unknown_kind", {}),
]


def test_checks():
    assert evaluate_check({"type": "contains_all", "values": ["LOVELACE"]}, "Ada lovelace was")
    assert not evaluate_check({"type": "contains_all", "values": ["a", "zz"]}, "a")
    assert evaluate_check({"type": "contains_any", "values": ["x", "b"]}, "ABC")
    assert evaluate_check({"type": "regex", "pattern": r"£\d+"}, "cost £5")
    assert evaluate_check({"type": "min_matches", "pattern": r"£\d+\.\d\d", "count": 3}, "£1.00 £2.00 £3.50")
    assert not evaluate_check({"type": "min_matches", "pattern": r"£\d+\.\d\d", "count": 3}, "£1.00 £2.00")
    with pytest.raises(ValueError):
        evaluate_check({"type": "nope"}, "")


def test_tasks_yaml():
    tasks = load_tasks()
    assert len(tasks) >= 8
    assert len({t["id"] for t in tasks}) == len(tasks)
    for t in tasks:
        assert t["id"] and t["family"] and t["prompt"] and t["check"]
        evaluate_check(t["check"], "sample")  # spec is well-formed
    assert {t["family"] for t in tasks} >= {"books_category", "quotes_by_tag", "wiki_summary"}
    assert [t["variant_index"] for t in tasks if t["family"] == "wiki_summary"] == [0, 1, 2]


def _write(path, rows):
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")


def _row(i, fam, correct, steps, tokens, sec):
    return {"id": i, "family": fam, "variant_index": 0, "correct": correct, "steps": steps,
            "tokens": tokens, "seconds": sec, "cost_usd": 0.0}


def test_report_math(tmp_path):
    cold, learned = tmp_path / "c.jsonl", tmp_path / "l.jsonl"
    _write(cold, [_row("a", "f1", True, 10, 1000, 20), _row("b", "f1", False, 20, 3000, 40)])
    _write(learned, [_row("a", "f1", True, 10, 1000, 20), _row("b", "f1", True, 2, 500, 10)])
    c = compare(__import__("bench.run_bench", fromlist=["x"]).load_jsonl(cold),
                __import__("bench.run_bench", fromlist=["x"]).load_jsonl(learned))
    o = c["overall"]
    assert o["cold"]["success_rate"] == 0.5 and o["learned"]["success_rate"] == 1.0
    assert o["cold"]["steps"] == 15 and o["learned"]["steps"] == 6
    assert o["reduction"]["steps"] == pytest.approx(60.0)
    assert o["reduction"]["tokens"] == pytest.approx(62.5)
    buf = io.StringIO()
    out = report(cold, learned, Console(file=buf, width=200))
    assert "f1" in buf.getvalue() and out["families"]["f1"]["reduction"]["seconds"] == pytest.approx(50.0)


@pytest.mark.parametrize("make", [lambda c: PlainUI("t", c), lambda c: LiveUI("t", c)])
def test_uis_survive_all_events(make):
    buf = io.StringIO()
    console = Console(file=buf, width=120)  # non-TTY
    with make(console) as ui:
        for k, d in EVENTS:
            ui.on_event(k, d)
    assert buf.getvalue()


def test_liveui_forced_terminal():
    console = Console(file=io.StringIO(), width=120, force_terminal=True)
    with LiveUI("t", console) as ui:
        for k, d in EVENTS:
            ui.on_event(k, d)
    assert ui.steps == 1 and ui.skills_used == 1 and ui.skills_built == 1


def test_render_result():
    buf = io.StringIO()
    render_result(RunResult(task="t", answer="42", ok=True, steps=3, tokens=1234, skills_used=["s"]),
                  Console(file=buf, width=100))
    assert "42" in buf.getvalue() and "1,234" in buf.getvalue()


def test_cli_help():
    p = subprocess.run([sys.executable, "-m", "webforge", "--help"], cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert p.returncode == 0 and "run" in p.stdout and "bench" in p.stdout
    p = subprocess.run([sys.executable, "-m", "webforge", "bench", "run", "--help"], cwd=ROOT,
                       capture_output=True, text=True, timeout=60)
    assert p.returncode == 0 and "--mode" in p.stdout
