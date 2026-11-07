"""
60 dakikalık barlardan gün içi kısmi günlük bar kurma. Canlı bot ve backtest ortak.

Yahoo'nun BIST 60m zaman damgası hakkında (Prompt 1'de ASELS.IS ve XU100.IS
gerçek verisiyle doğrulandı, 2026-11-02..2026-11-06):

- Damga bar BAŞLANGICIDIR, bitişi değil.
- Izgara `:30`'dur, `:00` değil. Tam günde 9 bar: 09:30, 10:30, 11:30, 12:30,
  13:30, 14:30, 15:30, 16:30, 17:30.
- Kanıt: ASELS 09:30 barının `open`'ı günlük barın `open`'ına, 60m barların
  `max(high)`/`min(low)` değerleri günlük `high`/`low`'a birebir eşit.
- Bar bitişi = min(damga + 60 dk, seans kapanışı). Son bar (17:30) seans
  kapanışında (18:10) biter, yani 40 dakikalıktır; 18:00 sonrası barı yoktur.
- Uyarı: Yahoo günün ilk barının (09:30) hacmini 0 veriyor. Hacim zaten kapanmış
  günlerden hesaplandığı için (spesifikasyon Bölüm 4) skoru etkilemez.
"""

from __future__ import annotations

from datetime import datetime, time
from typing import Optional

import pandas as pd

BAR_MINUTES = 60
#: Tam gün seans kapanışı (Borsa İstanbul Pay Piyasası: kapanış fiyatından
#: işlemler 18:08-18:10 arasında biter).
SESSION_CLOSE = time(18, 10)
#: Yarım gün seans kapanışı (resmi çizelge: 12:38-12:40).
HALF_DAY_CLOSE = time(12, 40)
DEFAULT_DELAY_MINUTES = 15


def _naive(moment) -> pd.Timestamp:
    """Zaman damgasını saat dilimsiz Europe/Istanbul'a çevirir."""
    ts = pd.Timestamp(moment)
    if ts.tz is not None:
        ts = ts.tz_convert("Europe/Istanbul").tz_localize(None)
    return ts


def _naive_index(frame: pd.DataFrame) -> pd.DataFrame:
    idx = pd.DatetimeIndex(frame.index)
    if idx.tz is not None:
        idx = idx.tz_convert("Europe/Istanbul").tz_localize(None)
    out = frame.copy()
    out.index = idx
    return out


def bar_end(start, session_close: time = SESSION_CLOSE) -> pd.Timestamp:
    """
    60m barın bitiş zamanı: damga + 60 dk, seans kapanışıyla sınırlı.

    Son bar seans kapanışında biter (tam günde 17:30 barı → 18:10).
    """
    begin = _naive(start)
    natural = begin + pd.Timedelta(minutes=BAR_MINUTES)
    close = pd.Timestamp(datetime.combine(begin.date(), session_close))
    return min(natural, close)


def partial_bar_at(
    hourly: pd.DataFrame,
    scan_time,
    delay_minutes: int = DEFAULT_DELAY_MINUTES,
    session_close: time = SESSION_CLOSE,
) -> Optional[dict]:
    """
    Sorgu anında kullanılabilir kısmi günlük barı kurar.

    Sadece `bar_bitişi <= scan_time - delay_minutes` olan BUGÜNÜN 60m barları
    kullanılır; `open` ilk barın açılışı, `high`/`low` uç değerler, `close` son
    barın kapanışı, `volume` toplam. Kullanılabilir bar yoksa (hafta sonu,
    tatil, seans başı) None döner.

    Yarım günlerde `session_close=HALF_DAY_CLOSE` verilmelidir.
    """
    if hourly is None or len(hourly) == 0:
        return None
    scan = _naive(scan_time)
    cutoff = scan - pd.Timedelta(minutes=delay_minutes)
    frame = _naive_index(hourly)
    today = frame[frame.index.normalize() == scan.normalize()]
    if today.empty:
        return None

    ends = [bar_end(ts, session_close) for ts in today.index]
    usable = today[[end <= cutoff for end in ends]]
    if usable.empty:
        return None

    return {
        "date": scan.date(),
        "open": float(usable["open"].iloc[0]),
        "high": float(usable["high"].max()),
        "low": float(usable["low"].min()),
        "close": float(usable["close"].iloc[-1]),
        "volume": float(usable["volume"].sum()),
        "bars": int(len(usable)),
        "last_bar_start": usable.index[-1],
        "last_bar_end": bar_end(usable.index[-1], session_close),
    }


def partial_index_at(
    hourly: pd.DataFrame,
    scan_time,
    delay_minutes: int = DEFAULT_DELAY_MINUTES,
    session_close: time = SESSION_CLOSE,
) -> Optional[float]:
    """Endeks için aynı kural; sadece kısmi kapanış fiyatını döndürür."""
    bar = partial_bar_at(hourly, scan_time, delay_minutes, session_close)
    return None if bar is None else bar["close"]
