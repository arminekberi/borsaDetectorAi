"""Bot ayarları. Strateji parametreleri `strategy_v4/config.py` içindedir."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Tuple

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

DEFAULT_AI_DOMAINS = (
    "kap.org.tr", "borsaistanbul.com", "bigpara.hurriyet.com.tr", "bloomberght.com",
    "aa.com.tr", "dunya.com", "fintables.com",
)


def _flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    return default if raw is None else raw.strip().lower() in ("1", "true", "yes", "on")


def _times(raw: str) -> Tuple[str, ...]:
    return tuple(t.strip() for t in raw.split(",") if t.strip())


@dataclass(frozen=True)
class BotConfig:
    bot_token: str = ""
    chat_id: str = ""
    db_path: str = "data/bot_v4.db"
    paper_mode: bool = True
    portfolio_try: float = 100_000.0
    scan_times: Tuple[str, ...] = ("10:30", "12:00", "14:00", "16:00", "17:30")
    close_time: str = "18:45"
    half_day_close_time: str = "13:30"
    sunday_time: str = "20:00"
    ai_enabled: bool = True
    ai_model: str = "claude-sonnet-5-5"
    ai_max_searches: int = 10
    ai_max_tokens: int = 4000
    ai_monthly_budget_usd: float = 20.0
    ai_input_usd_per_mtok: float = 3.0
    ai_output_usd_per_mtok: float = 15.0
    ai_search_usd: float = 0.01
    ai_allowed_domains: Tuple[str, ...] = DEFAULT_AI_DOMAINS

    @property
    def active_mode(self) -> str:
        return "paper" if self.paper_mode else "real"


def load_config() -> BotConfig:
    db = os.getenv("BOT_DB_PATH", "data/bot_v4.db").strip()
    db_path = db if Path(db).is_absolute() else str(ROOT / db)
    domains = os.getenv("AI_ALLOWED_DOMAINS", "")
    return BotConfig(
        bot_token=os.getenv("BOT_TOKEN", "").strip(),
        chat_id=os.getenv("CHAT_ID", "").strip(),
        db_path=db_path,
        paper_mode=_flag("PAPER_MODE", True),
        portfolio_try=float(os.getenv("PORTFOLIO_TRY", "100000")),
        scan_times=_times(os.getenv("SCAN_TIMES", "10:30,12:00,14:00,16:00,17:30")),
        close_time=os.getenv("CLOSE_TIME", "18:45").strip(),
        sunday_time=os.getenv("SUNDAY_TIME", "20:00").strip(),
        ai_enabled=_flag("AI_ENABLED", True) and bool(os.getenv("ANTHROPIC_API_KEY", "").strip()),
        ai_model=os.getenv("AI_MODEL", "claude-sonnet-5-5").strip(),
        ai_max_searches=int(os.getenv("AI_MAX_SEARCHES", "10")),
        ai_monthly_budget_usd=float(os.getenv("AI_MONTHLY_BUDGET_USD", "20")),
        ai_input_usd_per_mtok=float(os.getenv("AI_INPUT_USD_PER_MTOK", "3")),
        ai_output_usd_per_mtok=float(os.getenv("AI_OUTPUT_USD_PER_MTOK", "15")),
        ai_allowed_domains=tuple(d.strip() for d in domains.split(",") if d.strip()) or DEFAULT_AI_DOMAINS,
    )


def load_watchlist_seed() -> Tuple[str, ...]:
    raw = os.getenv("WATCHLIST", "")
    return tuple(s.strip().upper().replace(".IS", "") for s in raw.split(",") if s.strip())
