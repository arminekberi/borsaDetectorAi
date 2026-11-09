"""BIST tatil ve yarim gun takvimi (guncellenebilir)."""

from __future__ import annotations

# Kaynak: Borsa Istanbul Pay Piyasasi 2026 tatil tablosu.
# Takvimde olmayan yillarda bot uyari verir; yeni tablo yayimlaninca buraya eklenmelidir.
KNOWN_CALENDAR_YEARS = {2026}

# ISO tarih formati: YYYY-MM-DD
BIST_HOLIDAYS = {
    "2026-01-01",
    "2026-03-20",
    "2026-03-21",
    "2026-03-22",
    "2026-04-23",
    "2026-05-01",
    "2026-05-19",
    "2026-05-27",
    "2026-05-28",
    "2026-05-29",
    "2026-05-30",
    "2026-07-15",
    "2026-08-30",
    "2026-10-29",
}

BIST_HALF_DAYS = {
    "2026-03-19",
    "2026-05-26",
    "2026-10-28",
}

# Seans saatleri (Europe/Istanbul). Kaynak: Borsa Istanbul Pay Piyasasi Islem
# Saatleri Cizelgeleri. Tam gun: acilis 09:40-09:55, surekli islem 10:00-18:00,
# kapanis 18:00-18:05, kapanis fiyatindan islemler 18:07-18:10.
# Yarim gun: surekli islem 10:00-12:30, kapanis 12:30-12:35, son islem 12:38-12:40.
REGULAR_SESSION_START = (9, 40)
REGULAR_SESSION_END = (18, 10)
HALF_DAY_SESSION_END = (12, 40)

# 4H setup olusum saatleri (Istanbul): 10:00, 14:00, 18:00 (yuvarlak)
FOUR_H_SIGNAL_TIMES = ((10, 0), (14, 0), (18, 0))
