"""Tek hisse için canlı değerlendirme. Skor backtest ile aynı fonksiyonlardan gelir."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
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


def evaluate(
    symbol: str,
    history: pd.DataFrame,
    xu100: pd.Series,
    cfg: StrategyConfig,
    today_bar: Optional[dict] = None,
) -> Optional[Evaluation]:
    """
    `history` kapanmış günlük barlardır.

    `today_bar` verilirse sonuç ön sinyaldir: fiyat ve hacim gün içinden alınır,
    mum puanı ise yalnızca son kapanmış günden gelir.
    """
    data = history[["open", "high", "low", "close", "volume"]].copy()
    provisional = today_bar is not None
    if today_bar is not None:
        ts = pd.Timestamp(today_bar["date"])
        data.loc[ts, ["open", "high", "low", "close", "volume"]] = [
            today_bar["open"], today_bar["high"], today_bar["low"], today_bar["close"], today_bar["volume"],
        ]
        data = data.sort_index()
    if len(data) < 30:
        return None

    features = compute_features(data, xu100, cfg)
    scored = compute_score(features, cfg)
    row = scored.iloc[-1]
    feat = features.iloc[-1]
    candle_pts = float(row["candle_pts"])
    score = float(row["score"])
    patterns = candle_labels(feat)
    if provisional:
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
    )
