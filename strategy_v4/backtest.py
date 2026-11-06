"""
Günlük olay tabanlı portföy backtest'i.

Sıra (her gün):
1. Önceki kapanışta işaretlenen çıkışlar açılıştan yapılır.
2. Bekleyen alım emirleri açılıştan veya limit fiyatından dolar.
3. Stoplar kontrol edilir; açılış stopun altındaysa açılıştan çıkılır.
4. Gün sonunda stoplar, çıkış kuralları ve yeni sinyaller güncellenir.

Kararlar yalnızca gün sonu verisiyle verilir ve en erken ertesi gün uygulanır.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from strategy_v4.config import StrategyConfig
from strategy_v4.features import compute_features, compute_score, market_regime
from strategy_v4.sizing import initial_stop, max_entry_price, position_quantity


@dataclass
class Panel:
    dates: pd.DatetimeIndex
    symbols: List[str]
    open: np.ndarray
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray
    volume: np.ndarray
    atr: np.ndarray
    ema50: np.ndarray
    rel: np.ndarray
    adv: np.ndarray
    features: Dict[str, pd.DataFrame]
    sectors: List[str]
    regime: np.ndarray


def build_panel(frames: Dict[str, pd.DataFrame], xu100: pd.Series, sectors: Dict[str, str], cfg: StrategyConfig) -> Panel:
    features = {k: compute_features(v, xu100, cfg) for k, v in sorted(frames.items())}
    dates = pd.DatetimeIndex(sorted(set().union(*[set(f.index) for f in features.values()])))
    symbols = list(features)

    def matrix(col: str) -> np.ndarray:
        return np.column_stack([features[s][col].reindex(dates).to_numpy(dtype=float) for s in symbols])

    regime = market_regime(xu100, replace(cfg, use_regime=True)).reindex(dates, method="ffill").fillna(False).to_numpy(dtype=bool)
    return Panel(
        dates=dates, symbols=symbols,
        open=matrix("open"), high=matrix("high"), low=matrix("low"), close=matrix("close"), volume=matrix("volume"),
        atr=matrix("atr"), ema50=matrix("ema50"), rel=matrix("rel_12_1"), adv=matrix("adv_try"),
        features=features, sectors=[sectors.get(s, "Unknown") for s in symbols], regime=regime,
    )


def score_matrices(panel: Panel, cfg: StrategyConfig):
    scores, eligible = [], []
    for s in panel.symbols:
        sc = compute_score(panel.features[s], cfg).reindex(panel.dates)
        scores.append(sc["score"].to_numpy(dtype=float))
        eligible.append(sc["eligible"].fillna(False).to_numpy(dtype=bool))
    return np.column_stack(scores), np.column_stack(eligible)


@dataclass
class Position:
    idx: int
    qty: int
    entry: float
    entry_i: int
    stop: float
    risk_per_share: float
    top_close: float


@dataclass
class Result:
    equity: pd.Series
    invested: pd.Series
    trades: pd.DataFrame
    costs: float


def _locked_down(o, h, l, c, prev) -> bool:
    return prev > 0 and h <= l * 1.0005 and c / prev - 1.0 <= -0.09


def _locked_up(o, h, l, c, prev) -> bool:
    return prev > 0 and h <= l * 1.0005 and c / prev - 1.0 >= 0.09


def _is_scan_day(dates: pd.DatetimeIndex, i: int, weekday: Optional[int]) -> bool:
    """`weekday=4` haftanın son işlem günü, `None` her gün demektir."""
    if weekday is None:
        return True
    if weekday == 4:
        return i + 1 >= len(dates) or dates[i + 1].isocalendar()[:2] != dates[i].isocalendar()[:2]
    return dates[i].weekday() == weekday


def run_backtest(
    panel: Panel,
    cfg: StrategyConfig,
    start: str,
    end: str,
    cash_index: Optional[pd.Series] = None,
    allowed: Optional[List[str]] = None,
) -> Result:
    score, eligible = score_matrices(panel, cfg)
    dates = panel.dates
    i0 = int(dates.searchsorted(pd.Timestamp(start)))
    i1 = int(dates.searchsorted(pd.Timestamp(end), side="right"))
    o, h, l, c = panel.open, panel.high, panel.low, panel.close
    atr, ema50 = panel.atr, panel.ema50
    regime = panel.regime if cfg.use_regime else np.ones(len(dates), dtype=bool)
    universe = np.ones(len(panel.symbols), dtype=bool)
    if allowed is not None:
        universe = np.array([s in allowed for s in panel.symbols])

    cash_growth = None
    if cash_index is not None:
        ci = cash_index.reindex(dates, method="ffill")
        cash_growth = (ci / ci.shift(1)).fillna(1.0).to_numpy()

    cash = cfg.initial_capital
    positions: Dict[int, Position] = {}
    pending: Dict[int, dict] = {}
    exit_flags: Dict[int, str] = {}
    trades: List[dict] = []
    curve, invested_curve = [], []
    total_costs = 0.0
    month_key, month_start_equity = None, cfg.initial_capital
    last_close = np.full(len(panel.symbols), np.nan)
    if i0 > 0:
        for s in range(len(panel.symbols)):
            valid = c[: i0, s][~np.isnan(c[: i0, s])]
            if len(valid):
                last_close[s] = valid[-1]

    def close_position(s: int, price: float, i: int, reason: str, stop_fill: bool) -> None:
        nonlocal cash, total_costs
        p = positions.pop(s)
        slip = cfg.stop_slippage if stop_fill else 0.0
        fill = price * (1.0 - slip)
        cost = p.qty * fill * cfg.cost_per_side
        cash += p.qty * fill - cost
        total_costs += cost
        gross = (fill - p.entry) * p.qty
        trades.append({
            "symbol": panel.symbols[s], "entry_date": dates[p.entry_i], "exit_date": dates[i], "entry": p.entry,
            "exit": fill, "qty": p.qty, "reason": reason, "days": i - p.entry_i, "r": (fill - p.entry) / p.risk_per_share,
            "ret": fill / p.entry - 1.0, "pnl": gross - cost - p.qty * p.entry * cfg.cost_per_side,
            "stop_pct": p.risk_per_share / p.entry,
        })

    for i in range(i0, i1):
        day = dates[i]
        if cash_growth is not None:
            cash *= float(cash_growth[i])
        key = (day.year, day.month)
        equity_open = cash + sum(pos.qty * (last_close[s] if not np.isnan(last_close[s]) else pos.entry) for s, pos in positions.items())
        if key != month_key:
            month_key, month_start_equity = key, equity_open
        brake_on = equity_open / month_start_equity - 1.0 <= cfg.monthly_brake

        for s, reason in list(exit_flags.items()):
            if s not in positions:
                exit_flags.pop(s)
                continue
            if np.isnan(o[i, s]) or _locked_down(o[i, s], h[i, s], l[i, s], c[i, s], last_close[s]):
                continue
            close_position(s, float(o[i, s]), i, reason, False)
            exit_flags.pop(s)

        filled_today = 0
        order_list = sorted(pending.items(), key=lambda kv: -kv[1]["score"])
        for s, order in order_list:
            order["days_left"] -= 1
            valid_bar = not np.isnan(o[i, s])
            can_fill = (
                valid_bar and s not in positions and len(positions) < cfg.max_positions
                and filled_today < cfg.max_new_entries_per_day and not brake_on
                and not _locked_up(o[i, s], h[i, s], l[i, s], c[i, s], last_close[s])
            )
            if can_fill:
                sector = panel.sectors[s]
                same = sum(1 for q in positions if panel.sectors[q] == sector)
                can_fill = sector == "Unknown" or same < cfg.max_per_sector
            if can_fill:
                price = None
                if o[i, s] <= order["limit"]:
                    price = float(o[i, s])
                elif l[i, s] <= order["limit"]:
                    price = float(order["limit"])
                if price is not None:
                    stop = initial_stop(price, order["atr"], cfg)
                    risk_ps = price - stop
                    equity_now = cash + sum(
                        pos.qty * (o[i, q] if not np.isnan(o[i, q]) else last_close[q]) for q, pos in positions.items()
                    )
                    qty = position_quantity(equity_now, price, stop, order["adv"], cash, cfg)
                    if qty > 0:
                        cost = qty * price * cfg.cost_per_side
                        cash -= qty * price + cost
                        total_costs += cost
                        positions[s] = Position(s, qty, price, i, stop, risk_ps, price)
                        filled_today += 1
                        pending.pop(s, None)
                        continue
            if order["days_left"] <= 0:
                pending.pop(s, None)

        for s, pos in list(positions.items()):
            if np.isnan(o[i, s]):
                continue
            if _locked_down(o[i, s], h[i, s], l[i, s], c[i, s], last_close[s]):
                continue
            if o[i, s] <= pos.stop:
                close_position(s, float(o[i, s]), i, "stop_gap", True)
            elif l[i, s] <= pos.stop:
                close_position(s, float(pos.stop), i, "stop", True)

        for s in range(len(panel.symbols)):
            if not np.isnan(c[i, s]):
                last_close[s] = c[i, s]

        scan_today = _is_scan_day(dates, i, cfg.scan_weekday)
        exit_today = _is_scan_day(dates, i, cfg.exit_weekday)
        for s, pos in positions.items():
            if np.isnan(c[i, s]):
                continue
            pos.top_close = max(pos.top_close, float(c[i, s]))
            if cfg.trail_atr is not None:
                pos.stop = max(pos.stop, pos.top_close - cfg.trail_atr * float(atr[i, s]))
            if s in exit_flags or (cfg.exit_checks_on_scan_day_only and not exit_today):
                continue
            if cfg.exit_below_ema50 and c[i, s] < ema50[i, s]:
                exit_flags[s] = "ema50"
            elif cfg.exit_score is not None and score[i, s] < cfg.exit_score:
                exit_flags[s] = "score"
            elif (
                cfg.time_stop_days is not None and i - pos.entry_i >= cfg.time_stop_days
                and pos.top_close < pos.entry + cfg.time_stop_r * pos.risk_per_share
            ):
                exit_flags[s] = "time"

        if regime[i] and scan_today:
            for s in np.flatnonzero(universe):
                if s in positions or s in pending or np.isnan(c[i, s]):
                    continue
                if eligible[i, s] and score[i, s] >= cfg.buy_score:
                    pending[s] = {
                        "limit": max_entry_price(float(c[i, s]), float(atr[i, s]), cfg), "atr": float(atr[i, s]),
                        "adv": float(panel.adv[i, s]), "score": float(score[i, s]),
                        "days_left": cfg.entry_valid_sessions,
                    }

        value = sum(pos.qty * last_close[s] for s, pos in positions.items())
        curve.append(cash + value)
        invested_curve.append(value / (cash + value))

    for s in list(positions):
        close_position(s, float(last_close[s]), i1 - 1, "end", False)

    index = dates[i0:i1]
    return Result(
        equity=pd.Series(curve, index=index), invested=pd.Series(invested_curve, index=index),
        trades=pd.DataFrame(trades), costs=total_costs,
    )


def metrics(res: Result) -> dict:
    eq = res.equity
    t = res.trades
    years = (eq.index[-1] - eq.index[0]).days / 365.25
    total = eq.iloc[-1] / eq.iloc[0] - 1.0
    dd = (eq / eq.cummax() - 1.0).min()
    daily = eq.pct_change().dropna()
    out = {
        "getiri_%": round(total * 100, 1),
        "yillik_%": round(((1 + total) ** (1 / years) - 1) * 100, 1),
        "maks_dusus_%": round(dd * 100, 1),
        "sharpe0": round(float(daily.mean() / daily.std() * np.sqrt(250)), 2) if daily.std() > 0 else 0.0,
        "portfoyde_%": round(res.invested.mean() * 100, 1),
        "islem": len(t),
    }
    if len(t):
        wins = t.loc[t["r"] > 0, "pnl"].sum()
        losses = -t.loc[t["r"] <= 0, "pnl"].sum()
        out.update({
            "kazanma_%": round((t["r"] > 0).mean() * 100, 1), "ort_R": round(t["r"].mean(), 3), "medyan_R": round(t["r"].median(), 3),
            "kar_faktoru": round(wins / losses, 2) if losses > 0 else float("inf"), "ort_gun": round(t["days"].mean(), 1),
            "ort_stop_%": round(t["stop_pct"].mean() * 100, 1),
        })
    return out


def with_config(cfg: StrategyConfig, **changes) -> StrategyConfig:
    return replace(cfg, **changes)
