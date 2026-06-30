"""
İzlenecek BIST sembolleri.

Gerçek veri kaynağı bağlandığında sembol formatı (ör. GARAN.IS) kaynağa göre güncellenmelidir.
"""

from __future__ import annotations

from typing import List, Tuple

# (sembol_kodu, temel_fiyat_anker) — MockFetcher deterministik seri üretmek için kullanır
WATCHLIST: List[Tuple[str, float]] = [
    ("ASELS",  95.0),
    ("KATMR",  20.0),
    ("EKGYO",   7.0),
    ("THYAO", 320.0),
    ("KCHOL", 180.0),
    ("MPARK", 110.0),
    ("TOASO", 230.0),
]


def symbol_codes() -> List[str]:
    """Sadece sembol string listesi."""
    return [s for s, _ in WATCHLIST]
