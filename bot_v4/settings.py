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
    #: Yarım günde kapanış işi: resmi kapanış 12:40 + 45 dk veri gecikmesi payı.
    half_day_close_time: str = "13:25"
    sunday_time: str = "20:00"
    #: Pazar incelemesi varsayılan kapalı (her Pazar ek AI maliyeti çıkarıyordu).
    sunday_review: bool = False
    scan_summary: bool = True

    # --- Eminlik skoru (kodla hesaplanır, AI'a gösterilmez) ---
    min_eminlik: float = 75.0
    ai_min_olasilik: float = 70.0
    eminlik_w_kural: float = 0.4
    eminlik_w_ai: float = 0.6
    notify_alma: bool = False

    # --- AI analisti ---
    ai_enabled: bool = True
    #: Doğrulandı (platform.claude.com/docs, 2026-11): ≤100k token promptta
    #: 0,10 USD girdi / 0,50 USD çıktı; 100k üstünde 5 katı (0,50 / 2,50).
    ai_model: str = "claude-haiku-5-5"
    ai_chat_model: str = "claude-haiku-5-5"
    ai_max_searches: int = 2
    ai_max_tokens: int = 800
    ai_max_cost_per_review_usd: float = 0.05
    ai_monthly_budget_usd: float = 5.0
    ai_input_usd_per_mtok: float = 0.10
    ai_output_usd_per_mtok: float = 0.50
    #: Web arama 10 USD / 1.000 arama = arama başına 0,01 USD (doğrulandı).
    ai_search_usd: float = 0.01
    #: Haiku'da 100k token üstü istek 5 kat pahalı; o eşiğe yaklaşmadan kesilir.
    ai_max_input_tokens: int = 60_000
    ai_max_text_tokens: int = 2_000
    ai_max_message_tokens: int = 3_500
    ai_timeout_seconds: float = 120.0
    ai_allowed_domains: Tuple[str, ...] = DEFAULT_AI_DOMAINS

    @property
    def active_mode(self) -> str:
        return "paper" if self.paper_mode else "real"

    def eminlik(self, rule_score: float, ai_olasilik: float) -> float:
        """Eminlik = w_kural × kural_skoru + w_ai × AI olasılığı (0-100)."""
        value = self.eminlik_w_kural * rule_score + self.eminlik_w_ai * ai_olasilik
        return round(min(max(value, 0.0), 100.0), 1)

    def passes_four_conditions(
        self, rule_score: float, decision: str, ai_olasilik: float, buy_score: float
    ) -> bool:
        """
        Kullanıcıya AL bildirimi için dört koşulun HEPSİ gerekir:
        kural ≥ buy_score, AI kararı AL, olasılık ≥ ai_min_olasilik, eminlik ≥ min_eminlik.
        """
        return (
            rule_score >= buy_score
            and decision == "AL"
            and ai_olasilik >= self.ai_min_olasilik
            and self.eminlik(rule_score, ai_olasilik) >= self.min_eminlik
        )


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
        half_day_close_time=os.getenv("HALF_DAY_CLOSE_TIME", "13:25").strip(),
        sunday_time=os.getenv("SUNDAY_TIME", "20:00").strip(),
        sunday_review=_flag("SUNDAY_REVIEW", False),
        scan_summary=_flag("SCAN_SUMMARY", True),
        min_eminlik=float(os.getenv("MIN_EMINLIK", "75")),
        ai_min_olasilik=float(os.getenv("AI_MIN_OLASILIK", "70")),
        eminlik_w_kural=float(os.getenv("EMINLIK_W_KURAL", "0.4")),
        eminlik_w_ai=float(os.getenv("EMINLIK_W_AI", "0.6")),
        notify_alma=_flag("NOTIFY_ALMA", False),
        ai_enabled=_flag("AI_ENABLED", True) and bool(os.getenv("ANTHROPIC_API_KEY", "").strip()),
        ai_model=os.getenv("AI_MODEL", "claude-haiku-5-5").strip(),
        ai_chat_model=os.getenv("AI_CHAT_MODEL", os.getenv("AI_MODEL", "claude-haiku-5-5")).strip(),
        ai_max_searches=int(os.getenv("AI_MAX_SEARCHES", "2")),
        ai_max_tokens=int(os.getenv("AI_MAX_TOKENS", "800")),
        ai_max_cost_per_review_usd=float(os.getenv("AI_MAX_COST_PER_REVIEW_USD", "0.05")),
        ai_monthly_budget_usd=float(os.getenv("AI_MONTHLY_BUDGET_USD", "5")),
        ai_input_usd_per_mtok=float(os.getenv("AI_INPUT_USD_PER_MTOK", "0.10")),
        ai_output_usd_per_mtok=float(os.getenv("AI_OUTPUT_USD_PER_MTOK", "0.50")),
        ai_search_usd=float(os.getenv("AI_SEARCH_USD", "0.01")),
        ai_allowed_domains=tuple(d.strip() for d in domains.split(",") if d.strip()) or DEFAULT_AI_DOMAINS,
    )


def load_watchlist_seed() -> Tuple[str, ...]:
    raw = os.getenv("WATCHLIST", "")
    return tuple(s.strip().upper().replace(".IS", "") for s in raw.split(",") if s.strip())
