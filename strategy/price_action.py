"""
Basit price action: destek/direnç, kırılım, retest benzeri.

Son N mumun high/low'larından seviye çıkarımı; MVP için yuvarlak ve okunaklıdır.
"""

from __future__ import annotations

from typing import List, Optional, Tuple

import pandas as pd


def _recent_swing_levels(df: pd.DataFrame, lookback: int = 20) -> Tuple[float, float]:
    """Son lookback içindeki en düşük dip ve en yüksek tepe."""
    seg = df.iloc[-lookback:]
    swing_low = float(seg["low"].min())
    swing_high = float(seg["high"].max())
    return swing_low, swing_high


def support_bounce(df: pd.DataFrame, proximity_pct: float = 1.2) -> bool:
    """
    Fiyat son swing low'a yakın ve son mum yükseliş kapanışı.
    """
    if len(df) < 5:
        return False
    low, _ = _recent_swing_levels(df, lookback=min(20, len(df)))
    last = df.iloc[-1]
    close = float(last["close"])
    low_last = float(last["low"])
    if low == 0:
        return False
    near = abs(low_last - low) / low * 100.0 <= proximity_pct
    bullish_close = close > float(last["open"])
    return near and bullish_close


def resistance_breakout(df: pd.DataFrame, break_buffer_pct: float = 0.05) -> bool:
    """
    Kapanış son swing high üzerinde (küçük tampon) ve önceki kapanış altında kaldı.
    """
    if len(df) < 3:
        return False
    _, high = _recent_swing_levels(df.iloc[:-1], lookback=min(20, len(df) - 1))
    prev_close = float(df.iloc[-2]["close"])
    last_close = float(df.iloc[-1]["close"])
    if high == 0:
        return False
    threshold = high * (1 + break_buffer_pct / 100.0)
    broke = last_close > threshold
    fresh = prev_close <= high
    return broke and fresh


def retest_after_breakout(df: pd.DataFrame) -> bool:
    """
    Basit retest: önceki mum yeni tepeyi kırdı, bu mum seviyeye geri gelip üstünde kapandı.
    """
    if len(df) < 4:
        return False
    sub = df.iloc[-4:-1]
    prior_high = float(sub["high"].max())
    last = df.iloc[-1]
    prev = df.iloc[-2]
    broke = float(prev["close"]) > prior_high and float(prev["high"]) >= prior_high
    pullback = float(last["low"]) <= prior_high * 1.01
    hold = float(last["close"]) > prior_high
    return bool(broke) and pullback and hold


def higher_highs_lows(df: pd.DataFrame, swings: int = 3) -> Optional[str]:
    """
    Son tepe/dip dizilimine göre 'uptrend' / 'downtrend' / None.
    Basit: son 3 local max artıyor mu, son 3 local min artıyor mu (yaklaşık).
    """
    if len(df) < 15:
        return None
    highs = df["high"].values
    lows = df["low"].values
    # Yaklaşık swing: her 5 mumda bir max/min
    idx = range(5, len(df) - 5, 5)
    if len(idx) < swings:
        return None
    recent_highs = [float(highs[i]) for i in list(idx)[-swings:]]
    recent_lows = [float(lows[i]) for i in list(idx)[-swings:]]
    hh = all(recent_highs[i] < recent_highs[i + 1] for i in range(len(recent_highs) - 1))
    hl = all(recent_lows[i] < recent_lows[i + 1] for i in range(len(recent_lows) - 1))
    lh = all(recent_highs[i] > recent_highs[i + 1] for i in range(len(recent_highs) - 1))
    ll = all(recent_lows[i] > recent_lows[i + 1] for i in range(len(recent_lows) - 1))
    if hh and hl:
        return "uptrend"
    if lh and ll:
        return "downtrend"
    return None


def price_action_tags(df: pd.DataFrame) -> List[str]:
    """Tespit edilen price action etiketleri."""
    tags: List[str] = []
    if support_bounce(df):
        tags.append("support reaction")
    if resistance_breakout(df):
        tags.append("resistance breakout")
    if retest_after_breakout(df):
        tags.append("breakout retest")
    hhll = higher_highs_lows(df)
    if hhll == "uptrend":
        tags.append("HH/HL structure")
    elif hhll == "downtrend":
        tags.append("LH/LL structure")
    return tags
