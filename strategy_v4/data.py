"""Yerel CSV veya Yahoo'dan günlük veri yükleme."""

from __future__ import annotations

import glob
import os
from typing import Dict, Tuple

import pandas as pd

from strategy_v4.features import clean_corporate_actions, market_median_move


def _read_price_csv(path: str) -> pd.DataFrame:
    d = pd.read_csv(path)
    first = d.columns[0]
    d = d.rename(columns={first: "date"})
    d = d[pd.to_datetime(d["date"], errors="coerce").notna()].copy()
    d["date"] = pd.to_datetime(d["date"])
    d = d.set_index("date").sort_index()
    d.columns = [c.lower() for c in d.columns]
    d = d[["open", "high", "low", "close", "volume"]].astype(float)
    return d.dropna()


def load_csv_dir(directory: str) -> Dict[str, pd.DataFrame]:
    frames = {}
    for path in sorted(glob.glob(os.path.join(directory, "*.csv"))):
        frames[os.path.basename(path)[:-4].upper()] = _read_price_csv(path)
    return frames


def load_xu100_csv(path: str) -> pd.Series:
    d = pd.read_csv(path)
    d = d.rename(columns={d.columns[0]: "date", d.columns[1]: "close"})
    d["date"] = pd.to_datetime(d["date"])
    return d.set_index("date")["close"].astype(float).sort_index()


def clean_frames(frames: Dict[str, pd.DataFrame]) -> Dict[str, pd.DataFrame]:
    """Sıfır hacimli satırları atar ve kurumsal işlem bozulmalarını düzeltir."""
    median = market_median_move(frames)
    return {k: clean_corporate_actions(v, median) for k, v in frames.items()}


def load_yahoo(symbols, period: str = "6y") -> Tuple[Dict[str, pd.DataFrame], pd.Series]:
    """Canlı kullanım için Yahoo verisi. Sadece günlük kapanmış barlar kullanılmalıdır."""
    import yfinance as yf

    frames = {}
    for sym in symbols:
        d = yf.Ticker(f"{sym}.IS").history(period=period, interval="1d", auto_adjust=True)
        if d.empty:
            continue
        d.index = d.index.tz_localize(None).normalize()
        d.columns = [c.lower() for c in d.columns]
        frames[sym] = d[["open", "high", "low", "close", "volume"]].dropna()
    x = yf.Ticker("XU100.IS").history(period=period, interval="1d", auto_adjust=False)
    x.index = x.index.tz_localize(None).normalize()
    return frames, x["Close"].rename("close")
