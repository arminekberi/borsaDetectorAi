"""Yerel CSV veya Yahoo'dan günlük veri yükleme."""

from __future__ import annotations

import glob
import os
import time
from pathlib import Path
from typing import Dict, List, Tuple

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


def _cache_path(cache_dir: str, symbol: str, interval: str, period: str) -> Path:
    safe = symbol.replace(".", "_").replace("/", "_")
    return Path(cache_dir) / f"{safe}__{interval}__{period}.parquet"


def _normalize(raw: pd.DataFrame, interval: str) -> pd.DataFrame:
    """Yahoo çıktısını saat dilimsiz Europe/Istanbul indeksli OHLCV'ye çevirir."""
    idx = pd.DatetimeIndex(raw.index)
    if idx.tz is not None:
        idx = idx.tz_convert("Europe/Istanbul").tz_localize(None)
    if interval.endswith("d"):
        idx = idx.normalize()
    out = raw.copy()
    out.index = idx
    out.columns = [c.lower() for c in out.columns]
    missing = [c for c in ("open", "high", "low", "close", "volume") if c not in out.columns]
    if missing:
        raise ValueError(f"eksik sütun: {', '.join(missing)}")
    out = out[["open", "high", "low", "close", "volume"]].astype(float)
    return out[~out.index.duplicated(keep="last")].dropna(how="all").sort_index()


def load_yahoo_cached(
    symbols,
    interval: str = "1d",
    period: str = "2y",
    cache_dir: str = "data/cache",
    auto_adjust: bool = True,
    ttl_hours: float = 12.0,
    refresh: bool = False,
    suffix: str = ".IS",
) -> Dict[str, object]:
    """
    Yahoo verisini parquet önbellekle yükler.

    Dönen sözlük: `{"frames": {sembol: DataFrame}, "errors": [(sembol, sebep)]}`.
    Eksik veya bozuk veri olan hisse SESSİZCE atlanmaz; `errors` listesine girer.

    Önbellek `cache_dir` altında `SEMBOL__interval__period.parquet` olarak durur
    ve `.gitignore`'dadır. Dosya `ttl_hours`'tan eskiyse yenilenir; `refresh=True`
    önbelleği yok sayar. Ağ hatasında taze veri alınamazsa eldeki önbellek
    kullanılır ve hata `errors`'a "önbellekten okundu" notuyla eklenir.
    """
    import yfinance as yf

    Path(cache_dir).mkdir(parents=True, exist_ok=True)
    frames: Dict[str, pd.DataFrame] = {}
    errors: List[Tuple[str, str]] = []
    now = time.time()

    for symbol in symbols:
        path = _cache_path(cache_dir, symbol, interval, period)
        fresh_enough = (
            not refresh and path.exists() and (now - path.stat().st_mtime) < ttl_hours * 3600
        )
        if fresh_enough:
            try:
                frames[symbol] = pd.read_parquet(path)
                continue
            except Exception as exc:  # bozuk önbellek dosyası
                errors.append((symbol, f"önbellek okunamadı, yeniden indiriliyor: {exc}"))

        ticker = f"{symbol}{suffix}" if suffix and not symbol.endswith(suffix) else symbol
        try:
            raw = yf.Ticker(ticker).history(period=period, interval=interval, auto_adjust=auto_adjust)
            if raw is None or raw.empty:
                raise ValueError("Yahoo boş veri döndü")
            frame = _normalize(raw, interval)
            if frame.empty:
                raise ValueError("normalleştirme sonrası veri kalmadı")
            frame.to_parquet(path)
            frames[symbol] = frame
        except Exception as exc:
            if path.exists():
                try:
                    frames[symbol] = pd.read_parquet(path)
                    errors.append((symbol, f"taze veri alınamadı, önbellekten okundu: {exc}"))
                    continue
                except Exception:
                    pass
            errors.append((symbol, str(exc)))

    return {"frames": frames, "errors": errors}


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
