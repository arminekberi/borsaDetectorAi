"""
Piyasa verisi çekme katmanı — adapter deseni.

BaseFetcher: ortak arayüz
MockFetcher: geliştirme için sahte veri
YahooFinanceFetcher: gerçek OHLCV (yfinance)
FutureRealFetcher: gelecekte farklı veri sağlayıcıları için iskelet
"""

from __future__ import annotations

import abc
import hashlib
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import numpy as np
import pandas as pd
import yfinance as yf

logger = logging.getLogger(__name__)


class BaseFetcher(abc.ABC):
    """Tüm veri kaynakları bu arayüzü uygular."""

    @abc.abstractmethod
    def fetch_ohlcv(
        self,
        symbol: str,
        timeframe: str,
        bars: int = 120,
        anchor_price: Optional[float] = None,
    ) -> pd.DataFrame:
        """
        OHLCV DataFrame döner: index DatetimeIndex (UTC), kolonlar:
        open, high, low, close, volume
        """
        raise NotImplementedError


class MockFetcher(BaseFetcher):
    """
    Geliştirme ve demo için sahte mum serisi.

    Sembol adına göre tekrarlanabilir küçük fiyat hareketleri üretir;
    strateji testlerinde tutarlı davranış sağlar. Gerçek piyasa ile ilişkisi yoktur.
    """

    def __init__(self, seed_offset: int = 0) -> None:
        self._seed_offset = seed_offset

    def fetch_ohlcv(
        self,
        symbol: str,
        timeframe: str,
        bars: int = 120,
        anchor_price: Optional[float] = None,
    ) -> pd.DataFrame:
        h = int(hashlib.sha256(symbol.encode()).hexdigest()[:8], 16)
        rng = np.random.default_rng((h + self._seed_offset) % (2**32))

        base = float(anchor_price) if anchor_price is not None else 50.0 + (h % 200) / 10.0
        # Hafif trend + gürültü + ara sıra mum formasyonuna uygun hareket
        t = np.arange(bars, dtype=np.float64)
        trend = 0.0008 * np.sin(t / 18.0)
        noise = rng.normal(0, 0.008, size=bars)
        # Bazen güçlü mum (engulfing benzeri) için ani sıçrama
        jumps = np.zeros(bars)
        jump_idx = rng.choice(np.arange(10, bars - 5), size=3, replace=False)
        jumps[jump_idx] = rng.choice([-0.04, 0.04, 0.035, -0.035], size=3)

        log_ret = trend + noise + jumps
        close = base * np.exp(np.cumsum(log_ret))

        open_ = np.empty_like(close)
        open_[0] = close[0] / (1 + rng.normal(0, 0.002))
        open_[1:] = close[:-1]

        high = np.maximum(open_, close) * (1 + np.abs(rng.normal(0, 0.004, size=bars)))
        low = np.minimum(open_, close) * (1 - np.abs(rng.normal(0, 0.004, size=bars)))
        volume = rng.integers(10_000, 500_000, size=bars).astype(np.float64)

        # Son birkaç mumda bilinçli pattern: bullish engulfing benzeri (OHLC tutarlı)
        if bars >= 5:
            i = bars - 2
            prev_i = i - 1
            # Önceki mum: düşüş (open > close)
            o_prev = float(close[prev_i]) * 1.012
            c_prev = float(close[prev_i]) * 0.992
            open_[prev_i] = o_prev
            close[prev_i] = c_prev
            high[prev_i] = max(o_prev, c_prev) * 1.001
            low[prev_i] = min(o_prev, c_prev) * 0.999
            # Güncel mum: önceki gövdeyi yutan boğa
            o_cur = c_prev * 0.994
            c_cur = o_cur * 1.028
            open_[i] = o_cur
            close[i] = c_cur
            high[i] = max(o_cur, c_cur) * 1.008
            low[i] = min(o_cur, c_cur) * 0.995

        idx = pd.date_range(
            end=datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0),
            periods=bars,
            freq=_timeframe_to_pandas_freq(timeframe),
            tz=timezone.utc,
        )

        df = pd.DataFrame(
            {
                "open": open_,
                "high": high,
                "low": low,
                "close": close,
                "volume": volume,
            },
            index=idx,
        )
        return df


class FutureRealFetcher(BaseFetcher):
    """
    Gerçek API entegrasyonu için yer tutucu.

    TODO: Gerçek veri entegrasyonu — örn. Yahoo Finance, Matriks, ideal veri sağlayıcı;
          HTTP oturumu, sembol eşlemesi (GARAN -> GARAN.IS), mum frekansı eşlemesi.
    """

    def __init__(self, api_config: Optional[Dict[str, Any]] = None) -> None:
        self._api_config = api_config or {}

    def fetch_ohlcv(
        self,
        symbol: str,
        timeframe: str,
        bars: int = 120,
        anchor_price: Optional[float] = None,
    ) -> pd.DataFrame:
        raise NotImplementedError(
            "FutureRealFetcher henüz bağlı değil. "
            "Şimdilik FETCHER_TYPE=mock kullanın veya bu sınıfı doldurun."
        )


class YahooFinanceFetcher(BaseFetcher):
    """
    Yahoo Finance üzerinden BIST OHLCV verisi.

    Önemli:
    - Türkiye hisseleri için sembole `.IS` eklenir.
    - 4h verisi Yahoo'da doğrudan olmadığı için 60m çekilip 4H'ye gruplanır.
    """

    _INTERVAL_MAP = {
        "15m": "15m",
        "1h": "60m",
        "4h": "60m",
        "1d": "1d",
    }

    def normalize_symbol(self, symbol: str) -> str:
        symbol = symbol.strip().upper()
        if symbol.endswith(".IS"):
            return symbol
        return f"{symbol}.IS"

    def _normalize_interval(self, interval: str) -> str:
        return interval.strip().lower()

    def _period_for(self, interval: str, lookback: int) -> str:
        iv = self._normalize_interval(interval)
        if iv == "4h":
            days = max(7, int((lookback * 4) / 6) + 5)
            return f"{days}d"
        if iv == "1h":
            days = max(7, int(lookback / 6) + 5)
            return f"{days}d"
        if iv == "15m":
            days = max(5, int(lookback / 20) + 3)
            return f"{days}d"
        # Günlükte daha uzun pencere
        days = max(lookback + 30, 90)
        return f"{days}d"

    def get_ohlc(self, symbol: str, interval: str, lookback: int) -> Optional[pd.DataFrame]:
        """
        Yahoo'dan OHLCV çeker, standart kolonlarla döner.

        Dönüş:
        - DataFrame (open/high/low/close/volume)
        - Hata veya veri yoksa None
        """
        try:
            iv = self._normalize_interval(interval)
            if iv not in self._INTERVAL_MAP:
                logger.error("Desteklenmeyen interval: %s", interval)
                return None

            yf_symbol = self.normalize_symbol(symbol)
            yf_interval = self._INTERVAL_MAP[iv]
            period = self._period_for(iv, lookback)

            ticker = yf.Ticker(yf_symbol)
            raw = ticker.history(period=period, interval=yf_interval, auto_adjust=True)
            if raw is None or raw.empty:
                logger.warning("Yahoo veri dönmedi: %s", yf_symbol)
                return None

            # MultiIndex veya farklı başlıklar için normalize
            if isinstance(raw.columns, pd.MultiIndex):
                raw.columns = [c[0] for c in raw.columns]
            raw.columns = [str(c).strip().lower().replace(" ", "_") for c in raw.columns]
            logger.info("Yahoo ham kolonlar (%s): %s", yf_symbol, raw.columns.tolist())

            # auto_adjust=True ile adj_close kolonu gelmez; yine de varsa temizle
            for extra in ("adj_close", "adjclose"):
                if extra in raw.columns:
                    raw = raw.drop(columns=[extra])

            rename_map = {
                "open": "open",
                "high": "high",
                "low": "low",
                "close": "close",
                "volume": "volume",
            }
            if any(k not in raw.columns for k in rename_map):
                logger.error("Beklenen OHLCV kolonları yok: %s", raw.columns.tolist())
                return None

            df = raw.rename(columns=rename_map)[["open", "high", "low", "close", "volume"]].copy()
            df = df.apply(pd.to_numeric, errors="coerce")
            df = df.dropna()
            if df.empty:
                logger.warning("Kolon normalize sonrası boş veri: %s", yf_symbol)
                return None

            # UTC standardizasyonu + timezone farkı logu
            source_tz = str(getattr(df.index, "tz", None))
            if not isinstance(df.index, pd.DatetimeIndex):
                df.index = pd.to_datetime(df.index, utc=True)
            elif df.index.tz is None:
                df.index = df.index.tz_localize("UTC")
            else:
                df.index = df.index.tz_convert("UTC")
            logger.info("Timezone normalize: %s source_tz=%s -> target_tz=%s", yf_symbol, source_tz, df.index.tz)

            # 4h için 60m veriyi grupla
            if iv == "4h":
                df = (
                    df.resample("4h")
                    .agg(
                        {
                            "open": "first",
                            "high": "max",
                            "low": "min",
                            "close": "last",
                            "volume": "sum",
                        }
                    )
                    .dropna()
                )
                logger.info("4H resample son 10 mum (%s):\n%s", yf_symbol, df.tail(10).to_string())

            return df.tail(lookback)
        except Exception:
            logger.exception("Yahoo veri çekme hatası: %s", symbol)
            return None

    def fetch_ohlcv(
        self,
        symbol: str,
        timeframe: str,
        bars: int = 120,
        anchor_price: Optional[float] = None,
    ) -> pd.DataFrame:
        """
        BaseFetcher uyumluluğu için DataFrame döner; hata durumunda boş DataFrame.
        """
        _ = anchor_price  # Yahoo fetcher için kullanılmaz.
        df = self.get_ohlc(symbol=symbol, interval=timeframe, lookback=bars)
        if df is None:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        return df


def _timeframe_to_pandas_freq(tf: str) -> str:
    """Basit timeframe -> pandas offset."""
    tf = tf.upper().strip()
    mapping = {
        "1M": "1min",
        "5M": "5min",
        "15M": "15min",
        "30M": "30min",
        "1H": "1h",
        "4H": "4h",
        "1D": "1D",
        "D": "1D",
        "1W": "1W",
    }
    return mapping.get(tf, "4h")


def build_fetcher(fetcher_type: str) -> BaseFetcher:
    """Ayar string'ine göre uygun fetcher örneği."""
    ft = fetcher_type.lower().strip()
    if ft in ("yahoo", "yfinance"):
        return YahooFinanceFetcher()
    if ft == "mock":
        logger.warning("MockFetcher sadece fallback/test için önerilir.")
        return MockFetcher()
    if ft in ("real", "future_real", "futurerealfetcher"):
        return FutureRealFetcher()
    logger.warning("Bilinmeyen FETCHER_TYPE=%s, YahooFinanceFetcher kullanılıyor", fetcher_type)
    return YahooFinanceFetcher()
