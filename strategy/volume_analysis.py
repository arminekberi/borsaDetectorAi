"""
Volume analysis module: breakout volume, VWAP, OBV divergence.

Returns {"bull_score": int, "bear_score": int, "details": dict}.
"""

from __future__ import annotations

import pandas as pd


def _obv(close: pd.Series, volume: pd.Series) -> pd.Series:
    direction = close.diff().apply(lambda x: 1 if x > 0 else (-1 if x < 0 else 0))
    return (direction * volume).cumsum()


def _vwap(high: pd.Series, low: pd.Series, close: pd.Series, volume: pd.Series) -> pd.Series:
    typical = (high + low + close) / 3.0
    return (typical * volume).cumsum() / volume.cumsum()


def analyze_volume(df: pd.DataFrame) -> dict:
    bull_score = 0
    bear_score = 0
    details: dict = {}

    if df is None or len(df) < 21:
        return {"bull_score": bull_score, "bear_score": bear_score, "details": details}

    close = df["close"]
    high = df["high"]
    low = df["low"]
    volume = df["volume"]

    # --- Breakout volume confirmation ---
    last_vol = float(volume.iloc[-1])
    avg_vol = float(volume.iloc[-21:-1].mean())
    vol_ratio = last_vol / avg_vol if avg_vol > 0 else 1.0
    details["vol_ratio"] = round(vol_ratio, 3)

    if vol_ratio > 1.5:
        bull_score += 1
        details["volume_signal"] = "high_volume_bull"
    elif vol_ratio < 0.8:
        bear_score += 1
        details["volume_signal"] = "low_volume_bear"
    else:
        details["volume_signal"] = "neutral"

    # --- VWAP ---
    vwap = _vwap(high, low, close, volume)
    last_vwap = float(vwap.iloc[-1])
    last_close = float(close.iloc[-1])
    details["vwap"] = round(last_vwap, 4)

    if last_close > last_vwap:
        bull_score += 1
        details["vwap_signal"] = "above"
    else:
        bear_score += 1
        details["vwap_signal"] = "below"

    # --- OBV divergence ---
    obv = _obv(close, volume)
    window = 20
    prior_high = float(high.iloc[-window - 1 : -1].max())
    prior_low = float(low.iloc[-window - 1 : -1].min())
    prior_obv_high = float(obv.iloc[-window - 1 : -1].max())
    prior_obv_low = float(obv.iloc[-window - 1 : -1].min())
    last_high = float(high.iloc[-1])
    last_low = float(low.iloc[-1])
    last_obv = float(obv.iloc[-1])

    price_new_hh = last_high >= prior_high
    obv_confirms_hh = last_obv >= prior_obv_high
    price_new_ll = last_low <= prior_low
    obv_confirms_ll = last_obv <= prior_obv_low

    if price_new_hh and not obv_confirms_hh:
        bear_score -= 1  # bearish divergence: price extends but volume doesn't confirm
        details["obv_signal"] = "bearish_divergence"
    elif price_new_ll and not obv_confirms_ll:
        bull_score += 1  # bullish divergence: price dips but selling pressure fades
        details["obv_signal"] = "bullish_divergence"
    else:
        details["obv_signal"] = "neutral"

    return {"bull_score": bull_score, "bear_score": bear_score, "details": details}
