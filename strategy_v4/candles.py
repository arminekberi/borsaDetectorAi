"""Günlük mum formasyonları. Her formasyon yalnızca o güne kadarki veriyi kullanır."""

from __future__ import annotations

import numpy as np
import pandas as pd

from strategy_v4.config import BULLISH_CANDLE_POINTS, INFO_CANDLES


def detect_candles(f: pd.DataFrame) -> pd.DataFrame:
    """`open, high, low, close, ema20, ema50, atr` sütunlarından formasyon sütunları üretir."""
    o, h, l, c = f["open"], f["high"], f["low"], f["close"]
    body = (c - o).abs()
    rng = (h - l).replace(0.0, np.nan)
    upper = h - pd.concat([o, c], axis=1).max(axis=1)
    lower = pd.concat([o, c], axis=1).min(axis=1) - l
    avg_body = body.rolling(14).mean().shift(1)
    green = c > o
    red = c < o
    o1, c1, o2, c2 = o.shift(1), c.shift(1), o.shift(2), c.shift(2)
    body1, body2 = body.shift(1), body.shift(2)
    green1, red1, green2, red2 = green.shift(1, fill_value=False), red.shift(1, fill_value=False), green.shift(2, fill_value=False), red.shift(2, fill_value=False)
    long1 = body1 >= avg_body
    long2 = body2 >= avg_body
    mid1 = (o1 + c1) / 2.0
    mid2 = (o2 + c2) / 2.0
    top1 = pd.concat([o1, c1], axis=1).max(axis=1)
    bot1 = pd.concat([o1, c1], axis=1).min(axis=1)

    prior_decline = c1 < c.shift(4)
    prior_advance = c1 > c.shift(4)
    near_support = (l <= f["ema20"] + 0.5 * f["atr"]) | (l <= f["ema50"] + 0.5 * f["atr"])
    high_20 = h.shift(1).rolling(20).max()
    near_high = h >= 0.97 * high_20
    bull_ctx = prior_decline | near_support
    bear_ctx = prior_advance & near_high

    hammer_shape = (lower >= 2.0 * body) & (upper <= 0.35 * body.clip(lower=1e-9) + 0.1 * rng) & (body <= 0.4 * rng)
    star_shape = (upper >= 2.0 * body) & (lower <= 0.35 * body.clip(lower=1e-9) + 0.1 * rng) & (body <= 0.4 * rng)

    out = pd.DataFrame(index=f.index)
    out["hammer"] = hammer_shape & bull_ctx
    out["hanging_man"] = hammer_shape & bear_ctx
    out["shooting_star"] = star_shape & bear_ctx
    out["bullish_engulfing"] = green & red1 & (o <= c1 * 1.002) & (c >= o1) & (body > body1) & (body1 >= 0.3 * avg_body) & bull_ctx
    out["bearish_engulfing"] = red & green1 & (o >= c1 * 0.998) & (c <= o1) & (body > body1) & (body1 >= 0.3 * avg_body) & bear_ctx
    out["piercing"] = green & red1 & long1 & (o <= c1 * 1.002) & (c > mid1) & (c < o1) & bull_ctx
    out["dark_cloud"] = red & green1 & long1 & (o >= c1 * 0.998) & (c < mid1) & (c > o1) & bear_ctx
    small1 = body1 <= 0.5 * avg_body
    out["morning_star"] = green & red2 & long2 & small1 & (c > mid2) & bull_ctx
    out["evening_star"] = red & green2 & long2 & small1 & (c < mid2) & bear_ctx
    strong = body >= 0.6 * avg_body
    strong1 = body1 >= 0.6 * avg_body
    strong2 = body2 >= 0.6 * avg_body
    out["three_white_soldiers"] = (
        green & green1 & green2 & strong & strong1 & strong2 & (c > c1) & (c1 > c2)
        & (o >= bot1) & (o <= top1) & (upper <= 0.4 * body)
    )
    out["three_black_crows"] = (
        red & red1 & red2 & strong & strong1 & strong2 & (c < c1) & (c1 < c2)
        & (o >= bot1) & (o <= top1) & (lower <= 0.4 * body) & bear_ctx.shift(2, fill_value=False)
    )
    out["bullish_marubozu"] = green & (body >= 1.5 * avg_body) & (upper <= 0.1 * body) & (lower <= 0.1 * body)
    out["bullish_harami"] = green & red1 & long1 & (o > bot1) & (c < top1) & (body <= 0.6 * body1) & bull_ctx
    return out.fillna(False).astype(bool)


def candle_points(candles: pd.DataFrame, volume_ok: pd.Series, cap: float, low_volume_factor: float) -> pd.Series:
    """En güçlü boğa formasyonunun puanını verir; ortalama altı hacimde puan düşürülür."""
    bull = pd.Series(0.0, index=candles.index)
    for name, pts in BULLISH_CANDLE_POINTS.items():
        bull = bull.where(~candles[name], np.maximum(bull, pts))
    bull = bull.where(volume_ok.fillna(False), bull * low_volume_factor)
    return bull.clip(0.0, cap)


def candle_labels(row: pd.Series) -> list:
    """Tek satırdaki aktif formasyon adlarını döndürür."""
    names = list(BULLISH_CANDLE_POINTS) + list(INFO_CANDLES)
    return [n for n in names if bool(row.get(n, False))]
