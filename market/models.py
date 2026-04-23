"""
Piyasa ve sinyal veri modelleri.

Dataclass kullanımı: Pydantic olmadan hafif ve okunabilir yapı.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import List, Optional


class SignalType(str, Enum):
    BUY = "BUY"
    SELL = "SELL"
    WATCH = "WATCH"


@dataclass
class Signal:
    """Sinyal motoru çıktısı; Telegram ve log ile paylaşılır."""

    symbol: str
    signal_type: SignalType
    entry: float
    stop_loss: float
    target_1: float
    target_2: float
    timeframe: str
    reason: str
    timestamp: datetime
    last_close: Optional[float] = None
    trend_state: str = "neutral"
    detected_patterns: List[str] = field(default_factory=list)
    tags: List[str] = field(default_factory=list)

    def reason_summary(self) -> str:
        """Gerekçe metni; etiketlerle zenginleştirilebilir."""
        if self.tags:
            extra = " + ".join(self.tags)
            return f"{self.reason} + {extra}" if self.reason else extra
        return self.reason
