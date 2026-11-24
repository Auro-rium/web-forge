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
    # global spend ledger: independent of data_dir so isolated bench dirs still count toward the cap
    ledger_file: Path = field(default_factory=lambda: Path(os.getenv("WEBFORGE_LEDGER", ROOT / "data" / "spend.json")))
    worker_max_steps: int = int(os.getenv("WEBFORGE_WORKER_STEPS", "25"))
    planner_max_steps: int = int(os.getenv("WEBFORGE_PLANNER_STEPS", "30"))
    chromium_path: str = field(default_factory=lambda: os.getenv(
        "WEBFORGE_CHROMIUM", "/opt/pw-browsers/chromium" if Path("/opt/pw-browsers/chromium").exists() else ""))
    allowed_domains: list[str] = field(default_factory=lambda: [d.strip() for d in os.getenv(
        "WEBFORGE_ALLOWED_DOMAINS",
        "books.toscrape.com,quotes.toscrape.com,en.wikipedia.org,news.ycombinator.com,github.com,localhost,127.0.0.1",
    ).split(",") if d.strip()])

    @property
    def skills_dir(self) -> Path:
        return self.data_dir / "skills"

    @property
    def state_file(self) -> Path:
        return self.data_dir / "state.json"

    @property
    def memory_mirror(self) -> Path:
        return self.data_dir / "memory.jsonl"
