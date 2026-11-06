"""BIST işlem takvimi yardımcıları."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

from config.bist_calendar import BIST_HALF_DAYS, BIST_HOLIDAYS, KNOWN_CALENDAR_YEARS

SESSION_OPEN = time(10, 0)
SESSION_CLOSE = time(18, 10)
HALF_DAY_CLOSE = time(13, 0)


def is_trading_day(day: date) -> bool:
    return day.weekday() < 5 and day.isoformat() not in BIST_HOLIDAYS


def is_half_day(day: date) -> bool:
    return day.isoformat() in BIST_HALF_DAYS


def calendar_known(day: date) -> bool:
    return day.year in KNOWN_CALENDAR_YEARS


def session_close(day: date) -> time:
    return HALF_DAY_CLOSE if is_half_day(day) else SESSION_CLOSE


def in_session(moment: datetime) -> bool:
    day = moment.date()
    return is_trading_day(day) and SESSION_OPEN <= moment.time() <= session_close(day)


def next_trading_day(day: date) -> date:
    cursor = day + timedelta(days=1)
    while not is_trading_day(cursor):
        cursor += timedelta(days=1)
    return cursor


def add_sessions(day: date, sessions: int) -> date:
    cursor = day
    for _ in range(sessions):
        cursor = next_trading_day(cursor)
    return cursor


def is_last_trading_day_of_week(day: date) -> bool:
    if not is_trading_day(day):
        return False
    return next_trading_day(day).isocalendar()[:2] != day.isocalendar()[:2]
