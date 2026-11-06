"""Giriş sınırı, stop ve pozisyon boyutu. Backtest ve canlı bot aynı hesapları kullanır."""

from __future__ import annotations

import math

from strategy_v4.config import StrategyConfig


def max_entry_price(reference: float, atr: float, cfg: StrategyConfig) -> float:
    return reference + cfg.entry_buffer_atr * atr


def initial_stop(entry: float, atr: float, cfg: StrategyConfig) -> float:
    return entry - cfg.stop_atr * atr


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
