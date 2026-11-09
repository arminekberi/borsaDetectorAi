"""Giriş sınırı, stop ve pozisyon boyutu. Backtest ve canlı bot aynı hesapları kullanır."""

from __future__ import annotations

import math
from typing import Dict

from strategy_v4.config import StrategyConfig


def max_entry_price(reference: float, atr: float, cfg: StrategyConfig) -> float:
    """Eski v4 (ATR tabanlı) alım sınırı. `exit_model="atr"` içindir."""
    return reference + cfg.entry_buffer_atr * atr


def initial_stop(entry: float, atr: float, cfg: StrategyConfig) -> float:
    """Eski v4 (ATR tabanlı) stop. `exit_model="atr"` içindir."""
    return entry - cfg.stop_atr * atr


def entry_limit(reference: float, atr: float, cfg: StrategyConfig) -> float:
    """`exit_model`'e göre alım sınırı: sabit yüzde → referans × (1 + entry_buffer_pct)."""
    if cfg.exit_model == "atr":
        return max_entry_price(reference, atr, cfg)
    return reference * (1.0 + cfg.entry_buffer_pct)


def stop_price(entry: float, atr: float, cfg: StrategyConfig) -> float:
    """
    `exit_model`'e göre stop.

    - "fixed_pct"  → giriş × (1 − stop_pct)
    - "atr_staged" → giriş − stop_atr × ATR (oynaklığa göre ölçekli)
    - "atr"        → giriş − stop_atr × ATR (eski v4)
    """
    if cfg.exit_model in ("atr", "atr_staged"):
        return initial_stop(entry, atr, cfg)
    return entry - entry * cfg.stop_pct


def position_quantity(equity: float, price: float, stop: float, adv_try: float, cash: float, cfg: StrategyConfig) -> int:
    """Risk, pozisyon, likidite ve nakit sınırlarının en küçüğünü tam adet olarak verir."""
    risk_per_share = price - stop
    if risk_per_share <= 0 or price <= 0:
        return 0
    qty = min(
        cfg.risk_per_trade * equity / risk_per_share,
        cfg.max_position_pct * equity / price,
        cfg.max_pct_of_adv * adv_try / price,
        cash / (price * (1.0 + cfg.cost_per_side)),
    )
    return int(max(math.floor(qty), 0))


def position_plan(
    equity: float, price: float, stop: float, adv_try: float, cash: float, cfg: StrategyConfig
) -> Dict[str, float]:
    """
    Adet ve o işlemin GERÇEK riskini döndürür (üst sınır değil, spesifikasyon Bölüm 8).

    `gercek_risk_pct = adet × giriş × stop_yüzdesi / portföy`. Dar bir stopla
    (örn. −%4) bağlayıcı sınır genelde `max_position_pct` olur, `risk_per_trade`
    değil; bu yüzden mesajda üst sınır değil bu değer gösterilir.

    Örnek: portföy 100.000, giriş 100, stop 96 → adet min(250, 200) = 200,
    gerçek risk 800 TL = %0,80.
    """
    qty = position_quantity(equity, price, stop, adv_try, cash, cfg)
    risk_try = qty * (price - stop)
    binding = "risk"
    if qty > 0 and equity > 0:
        limits = {
            "risk": cfg.risk_per_trade * equity / (price - stop),
            "pozisyon": cfg.max_position_pct * equity / price,
            "likidite": cfg.max_pct_of_adv * adv_try / price,
            "nakit": cash / (price * (1.0 + cfg.cost_per_side)),
        }
        binding = min(limits, key=limits.get)
    return {
        "qty": qty,
        "stop": stop,
        "risk_try": risk_try,
        "risk_pct": risk_try / equity if equity > 0 else 0.0,
        "binding": binding,
    }
