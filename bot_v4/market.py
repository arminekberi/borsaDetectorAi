"""Yahoo Finance veri erişimi. Üst katman bu sınıfın arayüzüne bağlıdır."""

from __future__ import annotations

import logging
import time as clock
from dataclasses import dataclass
from datetime import date
from typing import Dict, Optional, Tuple

import pandas as pd
import yfinance as yf

from strategy_v4.features import clean_corporate_actions

logger = logging.getLogger("bot_v4.market")


class StaleData(RuntimeError):
    """Beklenen günün verisi henüz gelmedi."""


@dataclass
class IntradayBar:
    date: date
    open: float
    high: float
    low: float
    close: float
    volume: float
    recent_low: float

    def as_dict(self) -> dict:
        return {"date": self.date, "open": self.open, "high": self.high, "low": self.low, "close": self.close, "volume": self.volume}


def _normal_index(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    idx = pd.DatetimeIndex(out.index)
    if idx.tz is not None:
        idx = idx.tz_convert("Europe/Istanbul").tz_localize(None)
    out.index = idx.normalize()
    out.columns = [c.lower() for c in out.columns]
    return out[~out.index.duplicated(keep="last")]


class YahooMarket:
    def __init__(self, retries: int = 2, pause: float = 1.0):
        self.retries = retries
        self.pause = pause
        self._daily: Dict[str, Tuple[float, pd.DataFrame]] = {}
        self._index: Optional[Tuple[float, pd.Series]] = None
        self.ttl = 6 * 3600.0

    def _history(self, symbol: str, **kwargs) -> pd.DataFrame:
        error: Optional[Exception] = None
        for attempt in range(self.retries + 1):
            try:
                data = yf.Ticker(symbol).history(**kwargs)
                if data is not None and not data.empty:
                    return data
            except Exception as exc:  # yfinance ağ hatalarını farklı türlerle iletebiliyor
                error = exc
            if attempt < self.retries:
                clock.sleep(self.pause)
        if error is not None:
            logger.warning("%s veri hatası: %s", symbol, error)
        return pd.DataFrame()

    def xu100(self, as_of: date, include_today: bool = False) -> pd.Series:
        if self._index is None or clock.time() - self._index[0] > self.ttl:
            raw = self._history("XU100.IS", period="2y", interval="1d", auto_adjust=False)
            if raw.empty:
                raise RuntimeError("XU100 verisi alınamadı")
            self._index = (clock.time(), _normal_index(raw)["close"].astype(float))
        return self._cut(self._index[1], as_of, include_today)

    @staticmethod
    def _cut(data, as_of: date, include_today: bool):
        stamp = pd.Timestamp(as_of)
        if include_today:
            if data.index[-1] < stamp:
                raise StaleData(f"{as_of} verisi henüz yok")
            return data[data.index <= stamp]
        return data[data.index < stamp]

    def daily(self, symbol: str, as_of: date, include_today: bool = False) -> pd.DataFrame:
        cached = self._daily.get(symbol)
        if cached is None or clock.time() - cached[0] > self.ttl:
            raw = self._history(f"{symbol}.IS", period="2y", interval="1d", auto_adjust=True)
            if raw.empty:
                raise RuntimeError(f"{symbol} verisi alınamadı")
            frame = _normal_index(raw)[["open", "high", "low", "close", "volume"]].astype(float).dropna()
            index_move = self.xu100(as_of, include_today=False).pct_change().reindex(frame.index).fillna(0.0)
            cached = (clock.time(), clean_corporate_actions(frame, index_move))
            self._daily[symbol] = cached
        return self._cut(cached[1], as_of, include_today)

    def clear_cache(self) -> None:
        self._daily.clear()
        self._index = None

    def _intraday(self, symbol: str, as_of: date) -> Optional[IntradayBar]:
        raw = self._history(symbol, period="5d", interval="15m", auto_adjust=False, prepost=False)
        if raw.empty:
            return None
        idx = pd.DatetimeIndex(raw.index)
        if idx.tz is not None:
            idx = idx.tz_convert("Europe/Istanbul").tz_localize(None)
        data = raw.copy()
        data.index = idx
        data.columns = [c.lower() for c in data.columns]
        today = data[data.index.normalize() == pd.Timestamp(as_of)]
        if today.empty:
            return None
        return IntradayBar(
            date=as_of, open=float(today["open"].iloc[0]), high=float(today["high"].max()), low=float(today["low"].min()),
            close=float(today["close"].iloc[-1]), volume=float(today["volume"].sum()), recent_low=float(today["low"].tail(2).min()),
        )

    def intraday(self, symbol: str, as_of: date) -> Optional[IntradayBar]:
        return self._intraday(f"{symbol}.IS", as_of)

    def index_last(self, as_of: date) -> Optional[float]:
        bar = self._intraday("XU100.IS", as_of)
        return bar.close if bar else None

    def hourly(self, symbol: str) -> pd.DataFrame:
        raw = self._history(f"{symbol}.IS", period="5d", interval="60m", auto_adjust=False)
        if raw.empty:
            return raw
        idx = pd.DatetimeIndex(raw.index)
        if idx.tz is not None:
            idx = idx.tz_convert("Europe/Istanbul").tz_localize(None)
        data = raw.copy()
        data.index = idx
        data.columns = [c.lower() for c in data.columns]
        return data[["open", "high", "low", "close", "volume"]]

    def validate_symbol(self, symbol: str, as_of: date) -> bool:
        raw = self._history(f"{symbol}.IS", period="5d", interval="1d", auto_adjust=True)
        return not raw.empty
