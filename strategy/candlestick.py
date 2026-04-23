"""
Mum formasyonları — son kapanış mumu için basit boolean kontroller.

Gerçek ticaret için ek doğrulama (hacim, trend bağlamı) signal_engine içinde birleştirilir.
"""

from __future__ import annotations

from typing import List, Tuple

import pandas as pd


def _body_high_low(row: pd.Series) -> Tuple[float, float, float]:
    o, h, l, c = float(row["open"]), float(row["high"]), float(row["low"]), float(row["close"])
    body_top = max(o, c)
    body_bot = min(o, c)
    body = abs(c - o)
    return body, body_top, body_bot


def bullish_engulfing(df: pd.DataFrame) -> bool:
    """Önceki mum gövdesini yukarı yönde içine alan boğa mumu."""
    if len(df) < 2:
        return False
    prev, cur = df.iloc[-2], df.iloc[-1]
    b_prev, top_prev, bot_prev = _body_high_low(prev)
    b_cur, top_cur, bot_cur = _body_high_low(cur)
    if b_prev == 0 or b_cur == 0:
        return False
    prev_bear = float(prev["close"]) < float(prev["open"])
    cur_bull = float(cur["close"]) > float(cur["open"])
    engulfs = top_cur >= top_prev and bot_cur <= bot_prev
    return prev_bear and cur_bull and engulfs


def bearish_engulfing(df: pd.DataFrame) -> bool:
    """Önceki mum gövdesini aşağı yönde içine alan ayı mumu."""
    if len(df) < 2:
        return False
    prev, cur = df.iloc[-2], df.iloc[-1]
    b_prev, top_prev, bot_prev = _body_high_low(prev)
    b_cur, top_cur, bot_cur = _body_high_low(cur)
    if b_prev == 0 or b_cur == 0:
        return False
    prev_bull = float(prev["close"]) > float(prev["open"])
    cur_bear = float(cur["close"]) < float(cur["open"])
    engulfs = top_cur >= top_prev and bot_cur <= bot_prev
    return prev_bull and cur_bear and engulfs


def pin_bar_rejection(df: pd.DataFrame, wick_ratio: float = 2.0) -> Tuple[bool, bool]:
    """
    Fitil reddi: alt veya üst fitil gövdeden belirgin uzun.
    Dönüş: (bullish_pin, bearish_pin)
    """
    if len(df) < 1:
        return False, False
    row = df.iloc[-1]
    o, h, l, c = float(row["open"]), float(row["high"]), float(row["low"]), float(row["close"])
    body = max(abs(c - o), 1e-9)
    upper_wick = h - max(o, c)
    lower_wick = min(o, c) - l

    bullish_pin = lower_wick >= wick_ratio * body and upper_wick < body * 1.5
    bearish_pin = upper_wick >= wick_ratio * body and lower_wick < body * 1.5
    return bullish_pin, bearish_pin


def inside_bar(df: pd.DataFrame) -> bool:
    """İç mum: aralık bir önceki mumun içinde."""
    if len(df) < 2:
        return False
    inn = df.iloc[-1]
    out = df.iloc[-2]
    return float(inn["high"]) < float(out["high"]) and float(inn["low"]) > float(out["low"])


def detect_patterns(df: pd.DataFrame) -> List[str]:
    """Son mum için tespit edilen formasyon etiketleri."""
    tags: List[str] = []
    if bullish_engulfing(df):
        tags.append("bullish engulfing")
    if bearish_engulfing(df):
        tags.append("bearish engulfing")
    bull_pin, bear_pin = pin_bar_rejection(df)
    if bull_pin:
        tags.append("bullish pin bar")
    if bear_pin:
        tags.append("bearish pin bar")
    if inside_bar(df):
        tags.append("inside bar")
    return tags
