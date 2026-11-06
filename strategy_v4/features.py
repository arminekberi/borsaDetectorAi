"""Gösterge, filtre ve skor hesapları. Backtest ve canlı tarama aynı fonksiyonları kullanır."""

from __future__ import annotations

from typing import Dict

import numpy as np
import pandas as pd

from strategy_v4.candles import candle_points, detect_candles
from strategy_v4.config import StrategyConfig


def ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False, min_periods=span).mean()


def wilder_atr(high: pd.Series, low: pd.Series, close: pd.Series, length: int = 14) -> pd.Series:
    prev = close.shift(1)
    tr = pd.concat([high - low, (high - prev).abs(), (low - prev).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1.0 / length, adjust=False, min_periods=length).mean()


def compute_features(df: pd.DataFrame, xu100: pd.Series, cfg: StrategyConfig) -> pd.DataFrame:
    """
    Skordan bağımsız hisse özelliklerini hesaplar.

    `df`: tarih indeksli `open, high, low, close, volume`.
    `xu100`: tarih indeksli endeks kapanışı.
    """
    f = df[["open", "high", "low", "close", "volume"]].copy()
    c, h, v = f["close"], f["high"], f["volume"]
    f["ema20"] = ema(c, 20)
    f["ema50"] = ema(c, 50)
    f["ema200"] = ema(c, 200)
    f["atr"] = wilder_atr(f["high"], f["low"], c, 14)
    f["change"] = c.pct_change()
    f["bars"] = np.arange(1, len(f) + 1)

    x = xu100.reindex(f.index, method="ffill")
    f["rel_12_1"] = (c.shift(21) / c.shift(252)) / (x.shift(21) / x.shift(252)) - 1.0
    f["rs63"] = c.pct_change(63) - x.pct_change(63)
    f["high_ratio"] = c / h.rolling(252, min_periods=120).max()

    prev = c.shift(1)
    up_volume = v.where(c > prev, 0.0).rolling(20).sum()
    down_volume = v.where(c < prev, 0.0).rolling(20).sum()
    f["udv"] = up_volume / down_volume.replace(0.0, np.nan)
    f["avg_volume"] = v.rolling(20).mean()
    f["adv_try"] = (c * v).rolling(20).mean()

    f = f.join(detect_candles(f))
    f["candle_volume_ok"] = v >= f["avg_volume"]
    return f


def compute_score(f: pd.DataFrame, cfg: StrategyConfig) -> pd.DataFrame:
    """Puan bileşenlerini, toplam skoru ve filtreleri döndürür."""
    out = pd.DataFrame(index=f.index)
    c = f["close"]
    w1, w2, w3 = cfg.w_trend
    out["trend_pts"] = (
        w1 * (c > f["ema20"]).astype(float)
        + w2 * (f["ema20"] > f["ema50"]).astype(float)
        + w3 * (c > f["ema200"]).astype(float)
    )

    rel = ((f["rel_12_1"] + 0.10) / 0.50).clip(0.0, 1.0)
    rel63 = ((f["rs63"] + 0.10) / 0.30).clip(0.0, 1.0)
    near = ((f["high_ratio"] - 0.75) / 0.25).clip(0.0, 1.0)
    out["mom_pts"] = (
        cfg.w_relative_12_1 * rel.fillna(0.0) + cfg.w_relative_63 * rel63.fillna(0.0) + cfg.w_high * near.fillna(0.0)
    )

    a, b, d = cfg.w_volume
    udv = f["udv"].fillna(np.inf)
    vol = np.select([udv >= 1.5, udv >= 1.2, udv >= 1.0], [a, b, d], default=0.0)
    out["vol_pts"] = vol if cfg.use_volume else 0.0

    if cfg.use_candles:
        out["candle_pts"] = candle_points(f, f["candle_volume_ok"], cfg.w_candle_cap, cfg.candle_low_volume_factor)
    else:
        out["candle_pts"] = 0.0

    raw = out[["trend_pts", "mom_pts", "vol_pts", "candle_pts"]].sum(axis=1)
    out["score"] = raw.clip(0.0, 100.0)

    ready = (f["bars"] >= cfg.min_history) & f["ema200"].notna() & f["atr"].notna()
    out["f_history"] = ready
    out["f_liquidity"] = f["adv_try"] >= cfg.min_adv_try
    out["f_limit_move"] = f["change"] < cfg.max_daily_change
    out["f_extension"] = c <= f["ema20"] + cfg.max_extension_atr * f["atr"]
    out["eligible"] = out[["f_history", "f_liquidity", "f_limit_move", "f_extension"]].all(axis=1)
    return out


def market_regime(xu100: pd.Series, cfg: StrategyConfig) -> pd.Series:
    """XU100 kapanışı EMA üstünde ise True."""
    if not cfg.use_regime:
        return pd.Series(True, index=xu100.index)
    return xu100 > ema(xu100, cfg.regime_ema)


def clean_corporate_actions(df: pd.DataFrame, market_move: pd.Series, threshold: float = 0.125) -> pd.DataFrame:
    """
    Fiyat limitini aşan, hisseye özgü sıçramaları kurumsal işlem bozulması sayar ve geçmişi ölçekler.

    Piyasa geneli hareketlerde (örn. uzun kapanış sonrası açılış) dokunulmaz.
    """
    d = df[df["volume"] > 0].copy()
    r = d["close"].pct_change()
    med = market_move.reindex(d.index).abs()
    breaks = r[(r.abs() > threshold) & (med < 0.06)]
    for ts in sorted(breaks.index, reverse=True):
        pos = d.index.get_loc(ts)
        factor = float(d["close"].iloc[pos] / d["close"].iloc[pos - 1])
        d.iloc[:pos, d.columns.get_indexer(["open", "high", "low", "close"])] *= factor
        d.iloc[:pos, d.columns.get_loc("volume")] /= factor
    return d


def market_median_move(frames: Dict[str, pd.DataFrame]) -> pd.Series:
    """Her gün hisselerin medyan getirisi."""
    closes = pd.DataFrame({k: v["close"] for k, v in frames.items()})
    return closes.pct_change(fill_method=None).median(axis=1)
