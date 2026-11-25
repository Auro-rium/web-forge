"""Runtime configuration (env / .env)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:  # pragma: no cover
    pass

ROOT = Path(__file__).resolve().parent.parent


@dataclass
class Config:
    api_key: str = field(default_factory=lambda: os.getenv("BACKBOARD_API_KEY", ""))
    base_url: str = field(default_factory=lambda: os.getenv("BACKBOARD_BASE_URL", "https://app.backboard.io/api"))
    provider: str = field(default_factory=lambda: os.getenv("WEBFORGE_PROVIDER", "anthropic"))
    model: str = field(default_factory=lambda: os.getenv("WEBFORGE_MODEL", "claude-haiku-5-5"))
    data_dir: Path = field(default_factory=lambda: Path(os.getenv("WEBFORGE_DATA", ROOT / "data")))
    runs_dir: Path = field(default_factory=lambda: Path(os.getenv("WEBFORGE_RUNS", ROOT / "runs")))
    # hard cap on cumulative Backboard spend (persisted in data/spend.json); cheap model by default
    budget_usd: float = float(os.getenv("WEBFORGE_BUDGET_USD", "3.0"))
    # global spend ledger: independent of data_dir so every run (and test dir) counts toward the cap
    ledger_file: Path = field(default_factory=lambda: Path(os.getenv("WEBFORGE_LEDGER", ROOT / "data" / "spend.json")))
    worker_max_steps: int = int(os.getenv("WEBFORGE_WORKER_STEPS", "25"))
    planner_max_steps: int = int(os.getenv("WEBFORGE_PLANNER_STEPS", "30"))
    # per-run budget: at 70% the planner is told to wrap up, at 100% research/browse tools are refused
    run_budget_usd: float = float(os.getenv("WEBFORGE_RUN_BUDGET_USD", "0.25"))
    run_deadline_s: float = float(os.getenv("WEBFORGE_RUN_DEADLINE_S", "600"))
    # consequential actions (submit, send, login, delete...): ask | auto | deny. Payments are always blocked.
    approval: str = field(default_factory=lambda: os.getenv("WEBFORGE_APPROVAL", "ask").strip().lower())
    ask_timeout_s: float = float(os.getenv("WEBFORGE_ASK_TIMEOUT", "90"))
    verify_rounds: int = int(os.getenv("WEBFORGE_VERIFY_ROUNDS", "2"))
    screenshots: bool = os.getenv("WEBFORGE_SCREENSHOTS", "1") != "0"
    # parallel sub-agents / tabs per tool call (parallel_browse, read_urls, web_search queries)
    max_parallel: int = int(os.getenv("WEBFORGE_PARALLEL", "4"))
    # characters of page text shown to the worker per observation (read() pages through the rest)
    obs_text_chars: int = int(os.getenv("WEBFORGE_OBS_TEXT", "4000"))
    chromium_path: str = field(default_factory=lambda: os.getenv(
        "WEBFORGE_CHROMIUM", "/opt/pw-browsers/chromium" if Path("/opt/pw-browsers/chromium").exists() else ""))
    # empty list = any public website ("*" or unset). Set a comma list to restrict browsing to those domains.
    allowed_domains: list[str] = field(default_factory=lambda: [d.strip() for d in os.getenv(
        "WEBFORGE_ALLOWED_DOMAINS", "*").split(",") if d.strip() and d.strip() != "*"])

    @property
    def state_file(self) -> Path:
        return self.data_dir / "state.json"
