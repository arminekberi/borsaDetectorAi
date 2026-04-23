"""BIST seans ve sinyal zamani kontrolleri."""

from __future__ import annotations

from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from config import bist_calendar


def _local_now(now: datetime, tz_name: str = "Europe/Istanbul") -> datetime:
    tz = ZoneInfo(tz_name)
    if now.tzinfo is None:
        return now.replace(tzinfo=tz)
    return now.astimezone(tz)


def is_weekend(now: datetime, tz_name: str = "Europe/Istanbul") -> bool:
    local = _local_now(now, tz_name)
    return local.weekday() >= 5


def is_bist_holiday(day: date) -> bool:
    return day.isoformat() in bist_calendar.BIST_HOLIDAYS


def is_half_day(day: date) -> bool:
    return day.isoformat() in bist_calendar.BIST_HALF_DAYS


def is_market_open(now: datetime, tz_name: str = "Europe/Istanbul") -> bool:
    local = _local_now(now, tz_name)
    if is_weekend(local, tz_name):
        return False
    if is_bist_holiday(local.date()):
        return False

    start = time(*bist_calendar.REGULAR_SESSION_START)
    end = time(*bist_calendar.HALF_DAY_SESSION_END if is_half_day(local.date()) else bist_calendar.REGULAR_SESSION_END)
    current = local.time()
    return start <= current <= end


def is_signal_generation_time(now: datetime, tz_name: str = "Europe/Istanbul") -> bool:
    """15 dakikalik scheduler tetik penceresi (boundary'ye 1 dakika tolerans)."""
    local = _local_now(now, tz_name)
    return local.minute % 15 <= 1


def is_new_4h_candle(now: datetime, tz_name: str = "Europe/Istanbul") -> bool:
    """4H setup uretim zamani: 10:00, 14:00, 18:00 (yarim gunde 14:00/18:00 yok)."""
    local = _local_now(now, tz_name)
    if not is_market_open(local, tz_name):
        return False
    if is_half_day(local.date()) and local.time() > time(*bist_calendar.HALF_DAY_SESSION_END):
        return False
    hm = (local.hour, local.minute)
    return hm in bist_calendar.FOUR_H_SIGNAL_TIMES


def four_hour_candle_key(now: datetime, tz_name: str = "Europe/Istanbul") -> str:
    """Ayni 4H kapanista tekrar setup uretimini engellemek icin key."""
    local = _local_now(now, tz_name)
    candidates = sorted(bist_calendar.FOUR_H_SIGNAL_TIMES)
    chosen = None
    for hh, mm in candidates:
        if (local.hour, local.minute) >= (hh, mm):
            chosen = f"{hh:02d}:{mm:02d}"
    if chosen is None:
        chosen = "PREOPEN"
    return f"{local.date().isoformat()}_{chosen}"
