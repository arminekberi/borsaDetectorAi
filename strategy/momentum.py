"""
Momentum analysis module: RSI, MACD histogram, EMA slope, Stochastic RSI.

Returns {"bull_score": int, "bear_score": int, "details": dict}.
"""

from __future__ import annotations

import pandas as pd


def _ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()


def _rsi(close: pd.Series, length: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1.0 / length, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / length, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, float("nan"))
    return 100 - (100 / (1 + rs))


def _stochrsi_k(close: pd.Series, length: int = 14, rsi_length: int = 14, k: int = 3) -> pd.Series:
    rsi = _rsi(close, rsi_length)
    rsi_min = rsi.rolling(length).min()
    rsi_max = rsi.rolling(length).max()
    raw = (rsi - rsi_min) / (rsi_max - rsi_min + 1e-9)
    return (raw * 100).rolling(k).mean()


def analyze_momentum(df: pd.DataFrame) -> dict:
    bull_score = 0
    bear_score = 0
    details: dict = {}

    if df is None or len(df) < 30:
        return {"bull_score": bull_score, "bear_score": bear_score, "details": details}

    close = df["close"]

    # --- RSI(14) ---
    rsi = _rsi(close, 14)
    last_rsi = float(rsi.iloc[-1])
    if not pd.isna(last_rsi):
        details["rsi"] = round(last_rsi, 2)
        if last_rsi > 72:
            bull_score -= 2
            details["rsi_signal"] = "overbought"
        elif last_rsi < 28:
            bear_score -= 2
            details["rsi_signal"] = "oversold"
        elif 60 <= last_rsi <= 70:
            bull_score += 1
            details["rsi_signal"] = "bullish_zone"
        elif 30 <= last_rsi <= 40:
            bear_score += 1
            details["rsi_signal"] = "bearish_zone"
        else:
            details["rsi_signal"] = "neutral"

    # --- MACD histogram (EMA8 - EMA21, signal EMA9) ---
    fast = _ema(close, 8)
    slow = _ema(close, 21)
    macd_line = fast - slow
    signal_line = _ema(macd_line, 9)
    histogram = macd_line - signal_line

    if len(histogram.dropna()) >= 2:
        last_hist = float(histogram.iloc[-1])
        prev_hist = float(histogram.iloc[-2])
        details["macd_hist"] = round(last_hist, 6)
        if last_hist > 0 and last_hist > prev_hist:
            bull_score += 1
            details["macd_signal"] = "positive_rising"
        elif last_hist < 0 and last_hist < prev_hist:
            bear_score += 1
            details["macd_signal"] = "negative_falling"
        else:
            details["macd_signal"] = "neutral"

    # --- EMA8 slope over 5 bars (% change) ---
    ema8 = _ema(close, 8)
    if len(ema8.dropna()) >= 6:
        current_ema = float(ema8.iloc[-1])
        prev_ema = float(ema8.iloc[-6])
        if prev_ema != 0 and not pd.isna(prev_ema):
            slope_pct = (current_ema - prev_ema) / abs(prev_ema) * 100
            details["ema8_slope_pct"] = round(slope_pct, 4)
            if slope_pct > 0.1:
                bull_score += 1
                details["ema8_signal"] = "rising"
            elif slope_pct < -0.1:
                bear_score += 1
                details["ema8_signal"] = "falling"
            else:
                details["ema8_signal"] = "flat"

    # --- Stochastic RSI(14, 3, 3) ---
    stochrsi_k = _stochrsi_k(close, length=14, rsi_length=14, k=3)
    last_k = float(stochrsi_k.iloc[-1]) if not stochrsi_k.empty else float("nan")
    if not pd.isna(last_k):
        details["stoch_rsi_k"] = round(last_k, 2)
        if last_k > 80:
            bull_score -= 1
            details["stoch_rsi_signal"] = "overbought"
        elif last_k < 20:
            bear_score -= 1
            details["stoch_rsi_signal"] = "oversold"
        else:
            details["stoch_rsi_signal"] = "neutral"

    return {"bull_score": bull_score, "bear_score": bear_score, "details": details}
