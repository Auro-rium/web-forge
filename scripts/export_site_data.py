#!/usr/bin/env python
"""Export data for the static web UI into web/data/.

  python scripts/export_site_data.py            # latest bench_cold_*/bench_learned_* from runs/, skills, memory
  python scripts/export_site_data.py --sample   # force the small committed sample bench instead of runs/
  python scripts/export_site_data.py --run runs/web_20261124_101500_ab12   # use a recorded run (events.json) as demo

Writes bench.json, skills.json, memory.json, tasks.json, demo_runs.json. Read-only on runs/ and data/.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from webforge.config import Config  # noqa: E402

OUT = ROOT / "web" / "data"

SAMPLE_COLD = {"id": "books_poetry", "family": "books_category", "variant_index": 0, "mode": "cold", "ok": True,
               "correct": True, "steps": 3, "llm_calls": 8, "tokens": 2924, "cost_usd": 0.0030, "seconds": 25.5,
               "skills_used": [], "skills_created": [], "rep": 0}
SAMPLE_LEARNED = {"id": "books_travel", "family": "books_category", "variant_index": 1, "mode": "learned",
                  "ok": True, "correct": True, "steps": 2, "llm_calls": 4, "tokens": 1310, "cost_usd": 0.0006,
                  "seconds": 12.1, "skills_used": ["extract_category_books_sorted_by_price"],
                  "skills_created": [], "rep": 0}


def _write(name: str, obj) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / name).write_text(json.dumps(obj, indent=1, ensure_ascii=False, default=str) + "\n", encoding="utf-8")
    print(f"wrote web/data/{name}")


def _latest(runs_dir: Path, mode: str):
    files = sorted(p for p in runs_dir.glob(f"bench_{mode}_*.jsonl") if p.is_file() and p.stat().st_size > 0)
    return files[-1] if files else None


def export_bench(cfg: Config, force_sample: bool) -> None:
    cold_p = None if force_sample else _latest(cfg.runs_dir, "cold")
    learned_p = None if force_sample else _latest(cfg.runs_dir, "learned")
    if cold_p and learned_p:
        from bench.run_bench import compare, load_jsonl
        cold, learned = load_jsonl(cold_p), load_jsonl(learned_p)
        for r in cold + learned:
            r.pop("answer", None)
        _write("bench.json", {"sample": False, "cold_file": cold_p.name, "learned_file": learned_p.name,
                              "cold": cold, "learned": learned, "aggregate": compare(cold, learned),
                              "exported_at": time.strftime("%Y-%m-%d %H:%M:%S")})
        return
    _write("bench.json", {"sample": True, "cold_file": None, "learned_file": None,
                          "note": "Sample from a single live pair (Poetry cold vs Travel with the learned skill). "
                                  "Replaced by the full benchmark once scripts/export_site_data.py runs on its output.",
                          "cold": [SAMPLE_COLD], "learned": [SAMPLE_LEARNED], "aggregate": None})


def export_skills(cfg: Config) -> None:
    from webforge.skills.library import SkillLibrary, is_demoted
    out = []
    for s in SkillLibrary(cfg.skills_dir).list(include_demoted=True):
        d = s.to_dict()
        d["demoted"] = is_demoted(s)
        out.append(d)
    _write("skills.json", {"skills": out})


def export_memory(cfg: Config) -> None:
    items = []
    p = cfg.memory_mirror
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            try:
                d = json.loads(line)
                if d.get("content"):
                    items.append({"content": d["content"], "metadata": d.get("metadata", {})})
            except Exception:
                continue
    items.reverse()
    _write("memory.json", {"total": len(items), "memories": items[:200]})


def export_tasks() -> None:
    import yaml
    data = yaml.safe_load((ROOT / "bench" / "tasks.yaml").read_text(encoding="utf-8"))
    items = data["tasks"] if isinstance(data, dict) else data
    _write("tasks.json", {"tasks": [{"id": t["id"], "family": t.get("family", ""), "prompt": t["prompt"]}
                                    for t in items]})


# ------------------------------------------------------------------ demo ---

def _ev(t, kind, **data):
    return {"t": t, "kind": kind, "data": data}


def illustrative_runs() -> list[dict]:
    """Hand-written event lists using the real event vocabulary; metrics and answers are from real runs."""
    note = ("Illustrative replay: the metrics and answer come from a real live run; the event timeline is "
            "reconstructed from the event vocabulary, not a byte-for-byte trace.")
    t1 = "List the titles and prices of the 3 cheapest books in the 'Poetry' category on https://books.toscrape.com"
    a1 = ("The 3 cheapest books in the Poetry category on books.toscrape.com (19 books total, all on one page):\n\n"
          "1. Poems That Make Grown Women Cry - £14.19\n2. Untitled Collection: Sabbath Poems 2014 - £14.27\n"
          "3. The Collected Poems of W.B. Yeats (The Collected Works of W.B. Yeats #1) - £15.42")
    todo1 = lambda s: [{"task": "Open the Poetry category on books.toscrape.com", "status": s[0]},
                       {"task": "Extract titles and prices of all books", "status": s[1]},
                       {"task": "Sort by price and report the 3 cheapest", "status": s[2]}]
    run1 = {
        "id": "demo_cold_poetry", "title": "Cold start: Poetry (the agent learns a skill)", "task": t1,
        "recorded": False, "note": note,
        "events": [
            _ev(0.0, "task_start", task=t1),
            _ev(1.2, "todos", todos=todo1(["doing", "pending", "pending"])),
            _ev(3.1, "delegate", subtask="Open the Poetry category and extract every book's title and price",
                start_url="https://books.toscrape.com"),
            _ev(5.4, "observe", url="https://books.toscrape.com/", n_elements=73),
            _ev(8.0, "action", action="click", args={"idx": 41}, ok=True, message="clicked link 'Poetry'"),
            _ev(10.3, "observe", url="https://books.toscrape.com/catalogue/category/books/poetry_23/index.html",
                n_elements=44),
            _ev(13.0, "action", action="extract", args={"css": "article.product_pod",
                                                       "fields": {"title": "h3 a@title", "price": ".price_color"}},
                ok=True, message="extracted 19 rows"),
            _ev(15.2, "todos", todos=todo1(["done", "done", "doing"])),
            _ev(19.0, "final", answer=a1, ok=True),
            _ev(21.5, "skill_built", name="extract_category_books_sorted_by_price", params=["category"],
                description="Opens a books.toscrape.com category from the side navigation and extracts every book "
                            "listed in it (title and price)."),
            _ev(24.0, "lesson", domain="books.toscrape.com",
                text="Category pages list books on a single page when under ~20 items; check the page count before "
                     "paginating."),
            _ev(25.2, "lesson", domain="books.toscrape.com",
                text="Extract book listings with article.product_pod, reading the full title from h3 a's title "
                     "attribute and price from p.price_color."),
        ],
        "result": {"task": t1, "answer": a1, "ok": True, "steps": 3, "llm_calls": 8, "tokens": 2924,
                   "cost_usd": 0.002979, "seconds": 25.5, "skills_used": [],
                   "skills_created": ["extract_category_books_sorted_by_price"]},
    }
    t2 = "List the titles and prices of the 3 cheapest books in the 'Travel' category on https://books.toscrape.com"
    a2 = ("The 3 cheapest books in the 'Travel' category on books.toscrape.com are:\n\n"
          "1. The Road to Little Dribbling: Adventures of an American in Britain (Notes From a Small Island #2) - £23.21\n"
          "2. 1,000 Places to See Before You Die - £26.08\n3. The Great Railway Bazaar - £30.54")
    todo2 = lambda s: [{"task": "Run the category skill for 'Travel'", "status": s[0]},
                       {"task": "Report the 3 cheapest books", "status": s[1]}]
    run2 = {
        "id": "demo_learned_travel", "title": "Learned: Travel (reuses the skill, no browsing LLM)", "task": t2,
        "recorded": False, "note": note,
        "events": [
            _ev(0.0, "task_start", task=t2),
            _ev(0.8, "memory_hits", text="- Skill extract_category_books_sorted_by_price(category): opens a "
                                        "books.toscrape.com category and extracts every book (title, price).\n"
                                        "- Category pages list books on a single page when under ~20 items."),
            _ev(2.5, "todos", todos=todo2(["doing", "pending"])),
            _ev(4.0, "skill_run", name="extract_category_books_sorted_by_price", params={"category": "Travel"}),
            _ev(8.6, "skill_result", name="extract_category_books_sorted_by_price", ok=True,
                message="replayed 2 steps, extracted 11 rows"),
            _ev(9.4, "todos", todos=todo2(["done", "doing"])),
            _ev(10.6, "final", answer=a2, ok=True),
            _ev(11.8, "lesson", domain="books.toscrape.com",
                text="The category skill transfers to other categories by changing only the 'category' parameter."),
        ],
        "result": {"task": t2, "answer": a2, "ok": True, "steps": 2, "llm_calls": 4, "tokens": 1310,
                   "cost_usd": 0.0006, "seconds": 12.1,
                   "skills_used": ["extract_category_books_sorted_by_price"], "skills_created": []},
    }
    return [run1, run2]


def export_demo(run_dir: str | None) -> None:
    runs = []
    if run_dir:
        d = json.loads((Path(run_dir) / "events.json").read_text(encoding="utf-8"))
        runs.append({"id": d.get("run_id", "recorded"), "title": "Recorded run: " + d["task"][:60],
                     "task": d["task"], "recorded": True, "note": "Recorded from a real run.",
                     "events": d["events"], "result": d.get("result")})
    runs += illustrative_runs()
    _write("demo_runs.json", {"runs": runs})


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sample", action="store_true", help="write the sample bench instead of reading runs/")
    ap.add_argument("--run", help="a runs/web_* directory containing events.json to use as demo replay")
    args = ap.parse_args()
    cfg = Config()
    export_bench(cfg, args.sample)
    export_skills(cfg)
    export_memory(cfg)
    export_tasks()
    export_demo(args.run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
