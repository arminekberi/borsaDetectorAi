"""
Tek hisse için değerlendirme. Canlı bot ve backtest AYNI fonksiyonu çağırır.

`evaluate_at` tek giriş noktasıdır (spesifikasyon Bölüm 4):
- `partial_bar=None` → S5 ve backtest gün sonu: sadece kapanmış barlar.
- `partial_bar` verilince → S1-S4: fiyat kısmi bardan, hacim ve mum puanı son
  kapanmış günden.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Dict, List, Optional

import pandas as pd

from strategy_v4.candles import candle_labels
from strategy_v4.config import StrategyConfig
from strategy_v4.features import compute_features, compute_score

FILTER_REASONS: Dict[str, str] = {
    "f_history": "geçmiş verisi yetersiz",
    "f_liquidity": "işlem hacmi düşük",
    "f_limit_move": "günlük yükseliş tavana yakın",
    "f_extension": "fiyat EMA20'den fazla uzak",
}

SCAN_SLOTS = ("S1", "S2", "S3", "S4", "S5")
MIN_BARS = 30


@dataclass
class Evaluation:
    symbol: str
    as_of: date
    provisional: bool
    score: float
    trend_pts: float
    momentum_pts: float
    volume_pts: float
    candle_pts: float
    eligible: bool
    reasons: List[str]
    close: float
    atr: float
    adv_try: float
    rel_12_1: float
    rs63: float
    high_ratio: float
    patterns: List[str] = field(default_factory=list)
    scan_slot: Optional[str] = None
    as_of_time: Optional[datetime] = None


def evaluate_at(
    symbol: str,
    closed_daily: pd.DataFrame,
    partial_bar: Optional[dict],
    xu100_closed: pd.Series,
    xu100_partial: Optional[float] = None,
    cfg: Optional[StrategyConfig] = None,
    scan_slot: Optional[str] = None,
    as_of_time: Optional[datetime] = None,
) -> Optional[Evaluation]:
    """
    `closed_daily`: kapanmış günlük barlar (`open, high, low, close, volume`).
    `partial_bar`: `{"date", "open", "high", "low", "close", "volume"}` veya None.
    `xu100_closed`: kapanmış endeks kapanışları.
    `xu100_partial`: sorgu anındaki endeks fiyatı; sadece getiri hesabına girer.

    Kısmi bardan SADECE fiyat kullanılır: EMA'lar, trend puanı, göreli güç,
    52 hafta zirve oranı, ATR'ye dayalı aşırı uzama filtresi ve günlük değişim
    filtresi. Hacim bileşenleri (`avg_volume`, `adv_try`, `udv`) ve mum puanı
    son KAPANMIŞ güne kadarki barlardan gelir.
    """
    cfg = cfg or StrategyConfig()
    data = closed_daily[["open", "high", "low", "close", "volume"]].copy()
    provisional = partial_bar is not None
    volume_until: Optional[pd.Timestamp] = None

    if provisional:
        ts = pd.Timestamp(partial_bar["date"])
        # Kısmi bar kapanmış seriyle çakışıyorsa kapanmış kısmı o günün öncesi say.
        data = data[data.index < ts]
        if data.empty:
            return None
        volume_until = data.index[-1]
        data.loc[ts, ["open", "high", "low", "close", "volume"]] = [
            partial_bar["open"], partial_bar["high"], partial_bar["low"],
            partial_bar["close"], partial_bar["volume"],
        ]
        data = data.sort_index()

    if len(data) < MIN_BARS:
        return None

    xu = xu100_closed
    if provisional and xu100_partial is not None:
        xu = xu.copy()
        xu.loc[pd.Timestamp(partial_bar["date"])] = float(xu100_partial)
        xu = xu.sort_index()

    features = compute_features(data, xu, cfg, volume_until=volume_until)
    scored = compute_score(features, cfg)
    row = scored.iloc[-1]
    feat = features.iloc[-1]
    candle_pts = float(row["candle_pts"])
    score = float(row["score"])
    patterns = candle_labels(feat)
    if provisional:
        # Mum puanı yalnızca son kapanmış günden gelir.
        closed_pts = float(scored["candle_pts"].iloc[-2])
        score = float(min(max(score - candle_pts + closed_pts, 0.0), 100.0))
        candle_pts = closed_pts
        patterns = candle_labels(features.iloc[-2])

    reasons = [text for col, text in FILTER_REASONS.items() if not bool(row[col])]
    return Evaluation(
        symbol=symbol,
        as_of=data.index[-1].date(),
        provisional=provisional,
        score=round(score, 1),
        trend_pts=float(row["trend_pts"]),
        momentum_pts=round(float(row["mom_pts"]), 1),
        volume_pts=float(row["vol_pts"]),
        candle_pts=round(candle_pts, 1),
        eligible=not reasons,
        reasons=reasons,
        close=float(feat["close"]),
        atr=float(feat["atr"]),
        adv_try=float(feat["adv_try"]) if pd.notna(feat["adv_try"]) else 0.0,
        rel_12_1=float(feat["rel_12_1"]) if pd.notna(feat["rel_12_1"]) else 0.0,
        rs63=float(feat["rs63"]) if pd.notna(feat["rs63"]) else 0.0,
        high_ratio=float(feat["high_ratio"]) if pd.notna(feat["high_ratio"]) else 0.0,
        patterns=patterns,
        scan_slot=scan_slot,
        as_of_time=as_of_time,
    )


def evaluate(
    symbol: str,
    history: pd.DataFrame,
    xu100: pd.Series,
    cfg: StrategyConfig,
    today_bar: Optional[dict] = None,
) -> Optional[Evaluation]:
    """
    `evaluate_at` için geriye dönük uyumlu sarmalayıcı.

    `xu100` burada kısmi endeks fiyatını zaten içerdiği varsayılır (eski çağrı
    biçimi). Yeni kod doğrudan `evaluate_at` kullanmalıdır.
    """
    return evaluate_at(symbol, history, today_bar, xu100, None, cfg)
