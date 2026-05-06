"""
Ortam değişkenleri ve uygulama sabitleri.

Bu modül .env dosyasından BOT_TOKEN ve CHAT_ID okur; çalışma aralığı,
dosya yolları ve strateji parametreleri burada merkezi tutulur.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from market.symbols import symbol_codes as _symbol_codes

# Proje kökü: config/ bir üst dizin
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_SOURCE = "yahoo"


def _load_env() -> None:
    """Önce yerel .env, yoksa örnek dosya yüklenmez; sadece .env aranır."""
    env_path = _PROJECT_ROOT / ".env"
    load_dotenv(dotenv_path=env_path)


_load_env()


@dataclass(frozen=True)
class Settings:
    """Uygulama ayarları (immutable)."""

    bot_token: str
    chat_id: str
    scan_interval_seconds: int
    signal_state_path: Path
    log_level: str
    data_source: str
    fetcher_type: str  # "mock" | "future_real"
    primary_timeframe: str
    tracking_timeframe: str
    entry_distance_threshold_pct: float
    min_rr: float
    watch_expiry_hours: float
    breakeven_on_tp1: bool
    market_timezone: str
    enable_debug_logs: bool
    watchlist: tuple  # ("THYAO", "GARAN", ...)


_DEFAULT_WATCHLIST = tuple(_symbol_codes())


def _parse_watchlist(raw: str) -> tuple:
    """
    'THYAO,GARAN, AKBNK' → ('THYAO', 'GARAN', 'AKBNK')
    Boşluk ve büyük/küçük harf toleransı vardır.
    """
    return tuple(s.strip().upper() for s in raw.split(",") if s.strip())


def get_settings() -> Settings:
    """Ortam değişkenlerinden Settings üretir; eksik kritik değişkenlerde hata verir."""
    bot_token = os.getenv("BOT_TOKEN", "").strip()
    chat_id = os.getenv("CHAT_ID", "").strip()
    scan_interval = int(os.getenv("SCAN_INTERVAL_SECONDS", "900"))
    log_level = os.getenv("LOG_LEVEL", "INFO").upper()
    data_source = os.getenv("DATA_SOURCE", DATA_SOURCE).strip().lower()
    fetcher_type = os.getenv("FETCHER_TYPE", "mock").strip().lower()
    primary_tf = os.getenv("PRIMARY_TIMEFRAME", "4H").strip()
    tracking_tf = os.getenv("TRACKING_TIMEFRAME", "15m").strip()
    entry_dist = float(os.getenv("ENTRY_DISTANCE_THRESHOLD_PCT", "0.03"))
    min_rr = float(os.getenv("MIN_RR", "1.5"))
    watch_expiry = float(os.getenv("WATCH_EXPIRY_HOURS", "48"))
    breakeven = os.getenv("BREAKEVEN_ON_TP1", "true").strip().lower() in ("1", "true", "yes", "on")
    market_tz = os.getenv("MARKET_TIMEZONE", "Europe/Istanbul").strip()
    debug_logs = os.getenv("ENABLE_DEBUG_LOGS", "true").strip().lower() in ("1", "true", "yes", "on")

    state_rel = os.getenv("SIGNAL_STATE_PATH", "data/signal_state.json")
    signal_state_path = (Path(state_rel) if Path(state_rel).is_absolute() else _PROJECT_ROOT / state_rel)

    watchlist_raw = os.getenv("WATCHLIST", "").strip()
    watchlist = _parse_watchlist(watchlist_raw) if watchlist_raw else _DEFAULT_WATCHLIST

    return Settings(
        bot_token=bot_token,
        chat_id=chat_id,
        scan_interval_seconds=max(60, scan_interval),
        signal_state_path=signal_state_path,
        log_level=log_level,
        data_source=data_source,
        fetcher_type=fetcher_type,
        primary_timeframe=primary_tf,
        tracking_timeframe=tracking_tf,
        entry_distance_threshold_pct=max(0.005, entry_dist),
        min_rr=max(0.5, min_rr),
        watch_expiry_hours=max(1.0, watch_expiry),
        breakeven_on_tp1=breakeven,
        market_timezone=market_tz,
        enable_debug_logs=debug_logs,
        watchlist=watchlist,
    )


def validate_telegram_credentials(settings: Settings) -> None:
    """Telegram gönderimi için zorunlu alanları kontrol eder."""
    if not settings.bot_token:
        raise ValueError("BOT_TOKEN .env içinde tanımlı olmalıdır.")
    if not settings.chat_id:
        raise ValueError("CHAT_ID .env içinde tanımlı olmalıdır.")
