"""BIST tatil ve yarim gun takvimi (guncellenebilir)."""

from __future__ import annotations

# ISO tarih formati: YYYY-MM-DD
BIST_HOLIDAYS = {
    "2026-01-01",
    "2026-03-20",
    "2026-03-21",
    "2026-03-22",
    "2026-03-23",
    "2026-04-23",
    "2026-05-01",
    "2026-05-19",
    "2026-07-15",
    "2026-08-30",
    "2026-10-29",
}

BIST_HALF_DAYS = {
    "2026-03-19",
    "2026-05-18",
    "2026-09-24",
    "2026-12-31",
}

# Seans saatleri (Europe/Istanbul) — BIST resmi: 09:40–18:10
REGULAR_SESSION_START = (9, 40)
REGULAR_SESSION_END = (18, 10)
HALF_DAY_SESSION_END = (13, 0)

# 4H setup olusum saatleri (Istanbul): 10:00, 14:00, 18:00 (yuvarlak)
FOUR_H_SIGNAL_TIMES = ((10, 0), (14, 0), (18, 0))
