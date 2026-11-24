"""WebForge benchmark: cold (no learning) vs learned (skills + memory accumulate across tasks)."""
from __future__ import annotations

import asyncio
import dataclasses
import json
import re
import shutil
import time
from pathlib import Path
from typing import Any, Optional

from rich.console import Console
from rich.table import Table

TASKS_FILE = Path(__file__).with_name("tasks.yaml")
TASK_TIMEOUT = 300


# ------------------------------------------------------------------ tasks ---

def load_tasks(path: Optional[Path] = None) -> list[dict]:
    import yaml
    data = yaml.safe_load(Path(path or TASKS_FILE).read_text(encoding="utf-8"))
    tasks = data["tasks"] if isinstance(data, dict) else data
    counts: dict[str, int] = {}
    for t in tasks:
        counts[t["family"]] = counts.get(t["family"], 0) + 1
        t["variant_index"] = counts[t["family"]] - 1
    return tasks


def evaluate_check(check: dict, answer: str) -> bool:
    """Evaluate a checker spec against the final answer string (case-insensitive)."""
    answer = answer or ""
    low = answer.lower()
    typ = (check or {}).get("type")
    if typ == "contains_all":
        return all(str(v).lower() in low for v in check.get("values", []))
    if typ == "contains_any":
        return any(str(v).lower() in low for v in check.get("values", []))
    if typ == "regex":
        return re.search(check["pattern"], answer, re.I | re.S) is not None
    if typ == "min_matches":
        return len(re.findall(check["pattern"], answer, re.I)) >= int(check.get("count", 1))
    raise ValueError(f"unknown check type: {typ!r}")


# -------------------------------------------------------------------- run ---

async def _run_one(task: dict, cfg, llm, memory, library, mode: str, run_dir: Path) -> dict:
    from webforge.agent.planner import run_task
    from webforge.browser.session import Browser

    learned = mode == "learned"
    row: dict[str, Any] = {"id": task["id"], "family": task["family"],
                           "variant_index": task.get("variant_index", 0), "mode": mode,
                           "ok": False, "correct": False, "steps": 0, "llm_calls": 0, "tokens": 0,
                           "cost_usd": 0.0, "seconds": 0.0, "skills_used": [], "skills_created": []}
    browser = Browser(chromium_path=cfg.chromium_path, allowed_domains=cfg.allowed_domains)
    t0 = time.time()
    try:
        await browser.start(headless=True)
        res = await asyncio.wait_for(
            run_task(task["prompt"], llm=llm, browser=browser, library=library, memory=memory, cfg=cfg,
                     on_event=None, learn=learned, use_memory=learned, use_skills=learned, run_dir=run_dir),
            timeout=TASK_TIMEOUT)
        row.update(ok=bool(res.ok), steps=res.steps, llm_calls=res.llm_calls, tokens=res.tokens,
                   cost_usd=res.cost_usd, seconds=res.seconds or (time.time() - t0),
                   skills_used=list(res.skills_used), skills_created=list(res.skills_created))
        row["correct"] = bool(row["ok"]) and evaluate_check(task["check"], res.answer)
        row["answer"] = (res.answer or "")[:500]
    except asyncio.TimeoutError:
        row.update(error=f"timeout after {TASK_TIMEOUT}s", seconds=time.time() - t0)
    except Exception as e:  # keep the benchmark going
        row.update(error=f"{type(e).__name__}: {e}", seconds=time.time() - t0)
    finally:
        try:
            await browser.close()
        except Exception:
            pass
    return row


async def run_bench(mode: str, task_ids: Optional[list[str]] = None, repeat: int = 1, mock: bool = False,
                    cfg=None, console: Optional[Console] = None, tasks_file: Optional[Path] = None) -> Path:
    """Run the benchmark; returns the path of the JSONL results file."""
    from webforge.config import Config
    from webforge.llm import make_llm
    from webforge.memory import LocalMemory
    from webforge.skills.library import SkillLibrary

    if mode not in ("cold", "learned"):
        raise ValueError("mode must be cold or learned")
    console = console or Console()
    cfg = cfg or Config()
    ts = time.strftime("%Y%m%d_%H%M%S")
    bench_dir = cfg.data_dir / f"bench_{mode}_{ts}"
    # isolated, empty skills + memory for this benchmark run
    bcfg = dataclasses.replace(cfg, data_dir=bench_dir)
    library = SkillLibrary(bcfg.skills_dir)
    memory = LocalMemory(bcfg)
    llm = make_llm(bcfg, mock=mock)

    tasks = load_tasks(tasks_file)
    if task_ids:
        known = {t["id"] for t in tasks}
        missing = [i for i in task_ids if i not in known]
        if missing:
            raise ValueError(f"unknown task ids: {missing}")
        tasks = [t for t in tasks if t["id"] in set(task_ids)]

    cfg.runs_dir.mkdir(parents=True, exist_ok=True)
    out = cfg.runs_dir / f"bench_{mode}_{ts}.jsonl"
    rows: list[dict] = []
    try:
        for rep in range(max(1, repeat)):
            for task in tasks:
                if mode == "cold":  # nothing may leak between tasks
                    shutil.rmtree(bcfg.skills_dir, ignore_errors=True)
                    library = SkillLibrary(bcfg.skills_dir)
                    try:
                        bcfg.memory_mirror.unlink()
                    except FileNotFoundError:
                        pass
                console.print(f"[bold]{mode}[/bold] {task['id']} (rep {rep + 1}/{repeat}) ...")
                row = await _run_one(task, bcfg, llm, memory, library, mode,
                                     cfg.runs_dir / f"bench_{mode}_{ts}_{task['id']}_{rep}")
                row["rep"] = rep
                rows.append(row)
                with out.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(row, ensure_ascii=False) + "\n")
                console.print(f"   correct={row['correct']} steps={row['steps']} tokens={row['tokens']} "
                              f"{row['seconds']:.1f}s" + (f" ERROR {row['error']}" if row.get("error") else ""))
    finally:
        if hasattr(llm, "aclose"):
            try:
                await llm.aclose()
            except Exception:
                pass
    out.touch()
    print_run_tables(rows, console, title=mode)
    console.print(f"Results: {out}")
    return out


# ----------------------------------------------------------------- tables ---

def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def aggregate(rows: list[dict]) -> dict:
    n = len(rows)
    return {"n": n,
            "success_rate": (sum(1 for r in rows if r.get("correct")) / n) if n else 0.0,
            "steps": _mean([r.get("steps", 0) for r in rows]),
            "tokens": _mean([r.get("tokens", 0) for r in rows]),
            "seconds": _mean([r.get("seconds", 0.0) for r in rows]),
            "cost_usd": _mean([r.get("cost_usd", 0.0) for r in rows])}


def print_run_tables(rows: list[dict], console: Console, title: str = "") -> None:
    t = Table(title=f"Bench {title}: per task")
    for c in ("id", "family", "#", "correct", "steps", "llm", "tokens", "cost $", "sec", "skills used", "created"):
        t.add_column(c)
    for r in rows:
        t.add_row(r["id"], r["family"], str(r.get("variant_index", 0)), "yes" if r.get("correct") else "NO",
                  str(r.get("steps", 0)), str(r.get("llm_calls", 0)), str(r.get("tokens", 0)),
                  f"{r.get('cost_usd', 0):.4f}", f"{r.get('seconds', 0):.1f}",
                  ",".join(r.get("skills_used", [])) or "-", ",".join(r.get("skills_created", [])) or "-")
    console.print(t)
    a = aggregate(rows)
    t2 = Table(title=f"Bench {title}: aggregate")
    for c in ("tasks", "success", "mean steps", "mean tokens", "mean sec", "mean cost $"):
        t2.add_column(c)
    t2.add_row(str(a["n"]), f"{a['success_rate']:.0%}", f"{a['steps']:.1f}", f"{a['tokens']:.0f}",
               f"{a['seconds']:.1f}", f"{a['cost_usd']:.4f}")
    console.print(t2)


# ----------------------------------------------------------------- report ---

def load_jsonl(path) -> list[dict]:
    rows = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def _reduction(cold: float, learned: float) -> float:
    return (cold - learned) / cold * 100.0 if cold else 0.0


def compare(cold_rows: list[dict], learned_rows: list[dict]) -> dict:
    """{'overall': {...}, 'families': {fam: {...}}}; each has cold/learned aggregates and % reductions."""
    def block(c, l):
        ca, la = aggregate(c), aggregate(l)
        return {"cold": ca, "learned": la,
                "reduction": {k: _reduction(ca[k], la[k]) for k in ("steps", "tokens", "seconds", "cost_usd")}}
    fams = sorted({r["family"] for r in cold_rows} | {r["family"] for r in learned_rows})
    return {"overall": block(cold_rows, learned_rows),
            "families": {f: block([r for r in cold_rows if r["family"] == f],
                                  [r for r in learned_rows if r["family"] == f]) for f in fams}}


def report(cold_path, learned_path, console: Optional[Console] = None) -> dict:
    console = console or Console()
    cmp = compare(load_jsonl(cold_path), load_jsonl(learned_path))
    t = Table(title="WebForge: cold vs learned")
    for c in ("scope", "success cold", "success learned", "steps cold", "steps learned", "tokens cold",
              "tokens learned", "sec cold", "sec learned", "steps %", "tokens %", "sec %"):
        t.add_column(c)
    for name, b in [("OVERALL", cmp["overall"])] + list(cmp["families"].items()):
        c, l, r = b["cold"], b["learned"], b["reduction"]
        t.add_row(name, f"{c['success_rate']:.0%}", f"{l['success_rate']:.0%}", f"{c['steps']:.1f}",
                  f"{l['steps']:.1f}", f"{c['tokens']:.0f}", f"{l['tokens']:.0f}", f"{c['seconds']:.1f}",
                  f"{l['seconds']:.1f}", f"{r['steps']:+.0f}%", f"{r['tokens']:+.0f}%", f"{r['seconds']:+.0f}%")
    console.print(t)
    png = plot_report(cmp, Path(learned_path).with_suffix(".png"))
    if png:
        console.print(f"Chart saved: {png}")
    return cmp


def plot_report(cmp: dict, path: Path) -> Optional[Path]:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception:
        return None
    names = ["overall"] + list(cmp["families"])
    blocks = [cmp["overall"]] + list(cmp["families"].values())
    fig, axes = plt.subplots(1, 3, figsize=(13, 4))
    for ax, (key, label) in zip(axes, [("steps", "mean steps"), ("tokens", "mean tokens"), ("seconds", "mean seconds")]):
        x = range(len(names))
        ax.bar([i - 0.2 for i in x], [b["cold"][key] for b in blocks], 0.4, label="cold", color="#9aa0a6")
        ax.bar([i + 0.2 for i in x], [b["learned"][key] for b in blocks], 0.4, label="learned", color="#1a73e8")
        ax.set_xticks(list(x))
        ax.set_xticklabels(names, rotation=20, ha="right")
        ax.set_title(label)
    axes[0].legend()
    fig.tight_layout()
    fig.savefig(path, dpi=120)
    plt.close(fig)
    return path
