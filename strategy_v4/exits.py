"""
Sabit yüzde stop/TP çıkış modeli. Canlı bot ve backtest AYNI saf fonksiyonları çağırır.

Spesifikasyon Bölüm 8 ve 9. Bar kuralları (`step`):
- Açılış stopta veya altındaysa açılıştan çıkılır (gap aşağı).
- Açılış TP'nin üstündeyse TP açılıştan dolar (gap yukarı).
- Aynı barda hem stop hem TP mümkünse KÖTÜ senaryo seçilir: önce stop.
- Stop taşındıktan sonra aynı bar içinde yeni stop kontrol edilmez; yeni stop
  sonraki bardan geçerlidir.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from typing import Dict, List, Optional, Tuple

from strategy_v4.config import StrategyConfig

#: Seviye adları, sırayla.
LEVELS = ("tp1", "tp2", "tp3")
PRICE_DECIMALS = 4


@dataclass
class PositionState:
    """Tek pozisyonun çıkış durumu. `step` bu nesneyi değiştirmez, kopyasını döner."""

    entry: float
    qty: int  # başlangıç adedi; TP satış adetleri buna göre hesaplanır
    remaining: int
    stop: float
    tp1: float
    tp2: float
    tp3: float
    reached: str = ""  # "" | "tp1" | "tp2" | "tp3"
    closed_reason: str = ""
    top_price: float = 0.0  # girişten beri görülen en yüksek fiyat
    risk: float = 0.0  # ilk risk (giriş − ilk stop); stop taşınsa da sabit kalır

    @property
    def is_open(self) -> bool:
        return self.remaining > 0 and not self.closed_reason


@dataclass
class ExitEvent:
    """Bir bar içinde oluşan tek olay."""

    kind: str  # "tp1" | "tp2" | "tp3" | "stop" | "stop_gap"
    price: float
    qty: int
    remaining_after: int
    stop_after: float
    note: str = ""


def levels(entry: float, cfg: Optional[StrategyConfig] = None) -> Dict[str, float]:
    """
    Sabit yüzde seviyeleri. `exit_model="fixed_pct"` içindir.

    `levels(100)` → stop 96, tp1 106, tp2 110, tp3 115.
    Kuruş altı float artıkları temizlemek için 4 haneye yuvarlanır.
    """
    cfg = cfg or StrategyConfig()
    return {
        "stop": round(entry - entry * cfg.stop_pct, PRICE_DECIMALS),
        "tp1": round(entry + entry * cfg.tp1_pct, PRICE_DECIMALS),
        "tp2": round(entry + entry * cfg.tp2_pct, PRICE_DECIMALS),
        "tp3": round(entry + entry * cfg.tp3_pct, PRICE_DECIMALS),
    }


def r_multiples(cfg: Optional[StrategyConfig] = None) -> tuple:
    """
    TP'lerin R katları, sabit yüzde ayarlarından türetilir.

    Varsayılanla (stop %4; TP %6/%10/%15) → (1,5 / 2,5 / 3,75). Yani ödül-risk
    geometrisi tek yerde tanımlı; `exit_model` sadece 1R'nin NE OLDUĞUNU değiştirir.
    """
    cfg = cfg or StrategyConfig()
    return (cfg.tp1_pct / cfg.stop_pct, cfg.tp2_pct / cfg.stop_pct, cfg.tp3_pct / cfg.stop_pct)


def levels_atr(entry: float, atr: float, cfg: Optional[StrategyConfig] = None) -> Dict[str, float]:
    """
    Oynaklığa göre ölçeklenmiş seviyeler: 1R = `stop_atr` × ATR14.

    TP'ler `r_multiples` ile aynı R katlarında durur, yani yüzdeler hisseye göre
    değişir ama R geometrisi sabit kalır. `exit_model="atr_staged"` içindir.
    """
    cfg = cfg or StrategyConfig()
    risk = cfg.stop_atr * atr
    m1, m2, m3 = r_multiples(cfg)
    return {
        "stop": round(entry - risk, PRICE_DECIMALS),
        "tp1": round(entry + m1 * risk, PRICE_DECIMALS),
        "tp2": round(entry + m2 * risk, PRICE_DECIMALS),
        "tp3": round(entry + m3 * risk, PRICE_DECIMALS),
    }


def levels_for(entry: float, atr: float, cfg: Optional[StrategyConfig] = None) -> Dict[str, float]:
    """
    `exit_model`'e göre seviyeler:

    - `"fixed_pct"`  → sabit yüzde (stop −%4, TP +%6/+%10/+%15)
    - `"atr_staged"` → 1R = k×ATR, TP'ler aynı R katlarında
    - `"atr"`        → eski v4: k×ATR stop, TP YOK (çıkışı skor yönetir)
    """
    cfg = cfg or StrategyConfig()
    if cfg.exit_model == "atr":
        inf = float("inf")
        return {"stop": round(entry - cfg.stop_atr * atr, PRICE_DECIMALS),
                "tp1": inf, "tp2": inf, "tp3": inf}
    if cfg.exit_model == "atr_staged":
        return levels_atr(entry, atr, cfg)
    return levels(entry, cfg)


def open_state(
    entry: float, qty: int, cfg: Optional[StrategyConfig] = None, atr: float = 0.0
) -> PositionState:
    """Girişten sonraki başlangıç durumu. ATR tabanlı modlarda `atr` gerekir."""
    lv = levels_for(entry, atr, cfg) if (cfg and cfg.exit_model != "fixed_pct") else levels(entry, cfg)
    return PositionState(
        entry=entry, qty=int(qty), remaining=int(qty),
        stop=lv["stop"], tp1=lv["tp1"], tp2=lv["tp2"], tp3=lv["tp3"],
        top_price=entry, risk=entry - lv["stop"],
    )


def _sell_qty(state: PositionState, name: str, cfg: StrategyConfig) -> int:
    """
    TP satış adedi: `floor(başlangıç_adedi × frac)`, kalan son TP'de.

    Bölünemeyen küçük adetlerde (örn. 1 adet) ara TP'lerde 0 çıkabilir; o
    seviyede satış olmaz ama seviye geçilmiş sayılır ve stop taşınır.
    """
    if name == "tp3":
        return state.remaining
    frac = cfg.tp1_sell_frac if name == "tp1" else cfg.tp2_sell_frac
    return min(int(math.floor(state.qty * frac)), state.remaining)


def _moved_stop(state: PositionState, name: str, cfg: StrategyConfig) -> float:
    """Stop taşıma: TP1 sonrası girişe (başa baş), TP2 sonrası TP1'e. Asla aşağı inmez."""
    target = state.stop
    if name == "tp1":
        target = state.entry if cfg.move_stop_after_tp1 == "entry" else state.stop
    elif name == "tp2":
        target = state.tp1 if cfg.move_stop_after_tp2 == "tp1" else state.stop
    return max(state.stop, round(target, PRICE_DECIMALS))


def step(
    state: PositionState,
    bar_open: float,
    bar_high: float,
    bar_low: float,
    cfg: Optional[StrategyConfig] = None,
) -> Tuple[PositionState, List[ExitEvent]]:
    """
    Bir bar (60m veya 15m) için stop/TP olaylarını sırayla üretir.

    Dolum barında çağrılmamalıdır; kontrol bir sonraki bardan başlar.
    """
    cfg = cfg or StrategyConfig()
    s = replace(state)
    events: List[ExitEvent] = []
    if not s.is_open:
        return s, events
    s.top_price = max(s.top_price or s.entry, bar_high)

    # Gap aşağı: açılış stopta veya altında → açılıştan tamamı
    if bar_open <= s.stop:
        return _close_all(s, bar_open, "stop_gap", events)

    stop_moved = False
    for name in LEVELS:
        if not s.is_open:
            break
        if s.reached and LEVELS.index(name) <= LEVELS.index(s.reached):
            continue  # bu seviye daha önce geçildi
        level = getattr(s, name)
        if bar_open >= level:
            fill = bar_open  # gap yukarı: TP açılıştan dolar
        elif bar_high >= level:
            fill = level
        else:
            break  # bu seviyeye değmedi, üstündekiler de değmez

        # Kötü senaryo: TP'ye yükselmek için yol varsa stop da tetiklenmiş olabilir.
        # Açılış TP'nin üstündeyse TP açılış printinde dolmuştur, stop öne geçmez.
        if not stop_moved and bar_open < level and bar_low <= s.stop:
            return _close_all(s, s.stop, "stop", events)

        qty = _sell_qty(s, name, cfg)
        s.remaining -= qty
        s.reached = name
        s.stop = _moved_stop(s, name, cfg)
        if s.remaining <= 0:
            s.closed_reason = name
        events.append(ExitEvent(
            kind=name, price=round(fill, PRICE_DECIMALS), qty=qty,
            remaining_after=s.remaining, stop_after=s.stop,
            note="bölünemediği için satış yok" if qty == 0 else "",
        ))
        stop_moved = True  # taşınan stop bu barda tekrar kontrol edilmez

    if not stop_moved and s.is_open and bar_low <= s.stop:
        return _close_all(s, s.stop, "stop", events)

    return s, events


def _close_all(
    state: PositionState, price: float, kind: str, events: List[ExitEvent]
) -> Tuple[PositionState, List[ExitEvent]]:
    qty = state.remaining
    state.remaining = 0
    state.closed_reason = kind
    events.append(ExitEvent(
        kind=kind, price=round(price, PRICE_DECIMALS), qty=qty,
        remaining_after=0, stop_after=state.stop,
    ))
    return state, events


def time_exit_due(
    state: PositionState, sessions_held: int, cfg: Optional[StrategyConfig] = None
) -> Optional[str]:
    """
    Zaman stopu gerekçesi veya None.

    - `max_hold_sessions` (30) dolduysa → "max_hold"
    - `tp1_horizon_sessions` (10) geçtiyse:
        `time_stop_uses_r=False` → TP1 hiç gelmediyse "time"
        `time_stop_uses_r=True`  → en yüksek fiyat `time_stop_r` R kadar
                                   ilerlemediyse "time" (oynaklığa göre ölçekli)
    """
    cfg = cfg or StrategyConfig()
    if not state.is_open:
        return None
    if sessions_held >= cfg.max_hold_sessions:
        return "max_hold"
    if sessions_held < cfg.tp1_horizon_sessions:
        return None
    if cfg.time_stop_uses_r:
        risk = state.risk or (state.entry - state.stop)
        advance = (max(state.top_price, state.entry) - state.entry) / risk if risk > 0 else 0.0
        return "time" if advance < cfg.time_stop_r else None
    return "time" if not state.reached else None
