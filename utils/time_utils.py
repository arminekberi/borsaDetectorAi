"""Zaman dilimi ve zaman damgası yardımcıları."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional


def utc_now() -> datetime:
    """Şu anki UTC zamanı (timezone-aware)."""
    return datetime.now(timezone.utc)


def isoformat_utc(dt: Optional[datetime] = None) -> str:
    """ISO 8601 UTC string."""
    t = dt or utc_now()
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return t.astimezone(timezone.utc).isoformat()
