"""
İzlenecek BIST sembolleri.

Gerçek veri kaynağı bağlandığında sembol formatı (ör. GARAN.IS) kaynağa göre güncellenmelidir.
"""

from __future__ import annotations

from typing import List, Tuple

# (sembol_kodu, temel_fiyat_anker) — MockFetcher deterministik seri üretmek için kullanır
WATCHLIST: List[Tuple[str, float]] = [
    ("THYAO", 285.0),
    ("ASELS", 95.0),
    ("KCHOL", 145.0),
    ("GARAN", 145.0),
    ("AKBNK", 52.0),
    ("EREGL", 48.0),
    ("TUPRS", 170.0),
    ("SAHOL", 82.0),
    ("BIMAS", 490.0),
    ("ISCTR", 16.0),
]


def symbol_codes() -> List[str]:
    """Sadece sembol string listesi."""
    return [s for s, _ in WATCHLIST]
