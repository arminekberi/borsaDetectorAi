"""Strateji v4 parametreleri. Tüm eşikler ve puanlar burada tek yerde durur."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional, Tuple

# Skora giren boğa formasyonları. Bağlam (düşüş veya destek) koşulu candles.py içindedir.
BULLISH_CANDLE_POINTS: Dict[str, float] = {
    "morning_star": 15.0,
    "three_white_soldiers": 15.0,
    "bullish_engulfing": 12.0,
    "piercing": 10.0,
    "hammer": 8.0,
}

# Puan vermeyen, sadece bilgi amaçlı gösterilen formasyonlar.
INFO_CANDLES = (
    "bullish_marubozu", "bullish_harami", "evening_star", "three_black_crows",
    "bearish_engulfing", "dark_cloud", "shooting_star", "hanging_man",
)


@dataclass(frozen=True)
class StrategyConfig:
    # Skor ağırlıkları (toplam 100)
    w_trend: Tuple[float, float, float] = (8.0, 8.0, 9.0)  # C>EMA20, EMA20>EMA50, C>EMA200
    w_relative_12_1: float = 25.0
    w_relative_63: float = 15.0
    w_high: float = 15.0  # 52 haftalık zirveye yakınlık
    w_volume: Tuple[float, float, float] = (5.0, 3.0, 1.0)  # UDV >=1.5, >=1.2, >=1.0
    w_candle_cap: float = 15.0
    candle_low_volume_factor: float = 0.6

    use_candles: bool = True
    use_volume: bool = True

    # Karar eşikleri
    buy_score: float = 70.0
    exit_score: Optional[float] = 50.0  # Hafta sonu skoru bunun altına düşerse sat

    # Sert filtreler
    min_history: int = 260
    min_adv_try: float = 25_000_000.0
    max_daily_change: float = 0.09
    max_extension_atr: float = 3.0
    regime_ema: int = 50
    use_regime: bool = False  # Geliştirmede getiriyi düşürdü; ölçüm için duruyor

    # Giriş
    entry_buffer_atr: float = 0.5
    entry_valid_sessions: int = 2
    max_new_entries_per_day: int = 2
    scan_weekday: Optional[int] = 4  # None: her gün; 4: haftanın son işlem günü

    # Risk
    risk_per_trade: float = 0.025  # Stoplanırsa portföyden en fazla kayıp
    max_position_pct: float = 0.125
    max_positions: int = 8
    max_per_sector: int = 3
    monthly_brake: float = -0.06
    max_pct_of_adv: float = 0.05

    # Çıkış
    stop_atr: float = 4.0
    trail_atr: Optional[float] = None  # None: iz süren stop yok, çıkışı skor yönetir
    exit_below_ema50: bool = False
    exit_checks_on_scan_day_only: bool = True
    exit_weekday: Optional[int] = 4  # Skor çıkışının kontrol günü: 4 haftanın son işlem günü, None her gün
    time_stop_days: Optional[int] = None  # None: kapalı
    time_stop_r: float = 1.0

    # İşlem maliyeti (taraf başına, komisyon + kayma)
    cost_per_side: float = 0.0020
    stop_slippage: float = 0.0010

    # Portföy
    initial_capital: float = 100_000.0

    # BIST fiyat limiti kilidi
    lock_move: float = 0.09

