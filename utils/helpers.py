"""Genel yardımcı fonksiyonlar (yuvarlama, yüzde vb.)."""

from __future__ import annotations


def round_price(value: float, decimals: int = 2) -> float:
    """Fiyat benzeri sayıları tutarlı yuvarlar."""
    return round(float(value), decimals)


def pct_diff(a: float, b: float) -> float:
    """İki fiyat arasındaki yüzde fark (b'ye göre)."""
    if b == 0:
        return 0.0
    return abs(a - b) / abs(b) * 100.0
