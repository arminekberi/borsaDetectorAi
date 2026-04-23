"""
Stop ve hedef seviyeleri — basit yüzde / ATR benzeri mesafe.

İlk sürümde sabit yüzde mesafeleri kullanılır; ileride ATR tabanlı genişletilebilir.
"""

from __future__ import annotations

from dataclasses import dataclass

from market.models import SignalType


@dataclass(frozen=True)
class RiskLevels:
    stop_loss: float
    target_1: float
    target_2: float


def compute_risk_levels(
    entry: float,
    signal_type: SignalType,
    stop_pct: float = 2.0,
    tp1_pct: float = 3.0,
    tp2_pct: float = 5.0,
) -> RiskLevels:
    """
    Girişe göre stop ve iki hedef.

    BUY: stop altında, hedefler yukarıda.
    SELL: stop üstünde, hedefler aşağıda.
    WATCH: BUY ile aynı mesafe mantığı (izleme / hazırlık).
    """
    e = float(entry)
    if signal_type == SignalType.SELL:
        sl = e * (1 + stop_pct / 100.0)
        t1 = e * (1 - tp1_pct / 100.0)
        t2 = e * (1 - tp2_pct / 100.0)
    else:
        sl = e * (1 - stop_pct / 100.0)
        t1 = e * (1 + tp1_pct / 100.0)
        t2 = e * (1 + tp2_pct / 100.0)
    return RiskLevels(stop_loss=sl, target_1=t1, target_2=t2)
