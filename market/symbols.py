"""
İzlenecek BIST sembolleri.

Gerçek veri kaynağı bağlandığında sembol formatı (ör. GARAN.IS) kaynağa göre güncellenmelidir.
"""

from __future__ import annotations

from typing import List, Tuple

# (sembol_kodu, temel_fiyat_anker) — MockFetcher deterministik seri üretmek için kullanır
WATCHLIST: List[Tuple[str, float]] = [
      ("ASELS",    95.0),
      ("PAHOL",    30.0),
      ("VAKFN",    45.0),
      ("BORLEASE", 15.0),
      ("EFOR",     20.0),
      ("SMRVA",    25.0),
      ("NTGAZ",    40.0),
      ("INDES",    60.0),
      ("FROTO",   800.0),
]


def symbol_codes() -> List[str]:
    """Sadece sembol string listesi."""
    return [s for s, _ in WATCHLIST]
