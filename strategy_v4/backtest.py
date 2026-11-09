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

from dataclasses import dataclass, field, replace
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from strategy_v4.config import StrategyConfig
from strategy_v4.exits import PositionState, levels_for, step as exit_step
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
    # Kademeli çıkış (`exit_model="atr_staged"`) alanları; "atr" modunda kullanılmaz.
    remaining: int = 0
    reached: str = ""
    tp1: float = float("inf")
    tp2: float = float("inf")
    tp3: float = float("inf")
    sold_qty: int = 0
    sold_value: float = 0.0
    realized: float = 0.0
    levels: List[str] = field(default_factory=list)


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


@dataclass
class Holding:
    """Kısmi satışı destekleyen açık pozisyon (gün içi backtest ve canlı ortak şekli)."""

    symbol: str
    entry: float
    qty: int  # başlangıç adedi
    remaining: int
    risk_per_share: float
    entry_day: pd.Timestamp
    fill_key: int = -1  # dolum barının sıra numarası; o barda çıkış kontrolü yapılmaz
    meta: Dict[str, object] = field(default_factory=dict)


class Portfolio:
    """
    Nakit, maliyet, kısmi satış ve limit muhasebesi.

    Formüller `run_backtest` ile aynıdır (maliyet taraf başına `cost_per_side`,
    stop dolumlarına `stop_slippage`); `test_portfolio_matches_run_backtest`
    ikisinin aynı sonucu verdiğini doğrular. `run_backtest` geriye dönük
    uyumluluk için kendi satır içi muhasebesini korur.
    """

    def __init__(self, cfg: StrategyConfig, sectors: Optional[Dict[str, str]] = None):
        self.cfg = cfg
        self.sectors = sectors or {}
        self.cash = cfg.initial_capital
        self.holdings: Dict[str, Holding] = {}
        self.fills: List[dict] = []
        self.trades: List[dict] = []
        self.costs = 0.0
        self.filled_today = 0
        self._month: Optional[tuple] = None
        self._month_start_equity = cfg.initial_capital

    # --- değerleme ---
    def equity(self, prices: Dict[str, float]) -> float:
        value = self.cash
        for sym, h in self.holdings.items():
            value += h.remaining * prices.get(sym, h.entry)
        return value

    def invested_fraction(self, prices: Dict[str, float]) -> float:
        total = self.equity(prices)
        if total <= 0:
            return 0.0
        held = sum(h.remaining * prices.get(s, h.entry) for s, h in self.holdings.items())
        return held / total

    # --- gün döngüsü ---
    def start_day(self, day: pd.Timestamp, prices: Dict[str, float]) -> None:
        self.filled_today = 0
        key = (day.year, day.month)
        if key != self._month:
            self._month = key
            self._month_start_equity = self.equity(prices)

    def brake_on(self, prices: Dict[str, float]) -> bool:
        """Aylık kayıp freni: ay başından beri düşüş `monthly_brake`'i aştıysa yeni alım yok."""
        if self._month_start_equity <= 0:
            return False
        return self.equity(prices) / self._month_start_equity - 1.0 <= self.cfg.monthly_brake

    def block_reason(self, symbol: str, prices: Dict[str, float]) -> Optional[str]:
        """Yeni giriş engelleri; sırası `run_backtest` ile aynı."""
        if self.brake_on(prices):
            return "aylık kayıp freni"
        if len(self.holdings) >= self.cfg.max_positions:
            return "portföy dolu"
        if self.filled_today >= self.cfg.max_new_entries_per_day:
            return "günlük giriş sınırı"
        sector = self.sectors.get(symbol, "Unknown")
        if sector != "Unknown":
            same = sum(1 for s in self.holdings if self.sectors.get(s) == sector)
            if same >= self.cfg.max_per_sector:
                return "sektör sınırı"
        return None

    # --- işlemler ---
    def buy(
        self, symbol: str, price: float, qty: int, risk_per_share: float,
        day: pd.Timestamp, fill_key: int = -1, meta: Optional[dict] = None,
    ) -> bool:
        if qty <= 0 or symbol in self.holdings:
            return False
        gross = qty * price
        cost = gross * self.cfg.cost_per_side
        if gross + cost > self.cash + 1e-9:
            return False
        self.cash -= gross + cost
        self.costs += cost
        self.holdings[symbol] = Holding(
            symbol=symbol, entry=price, qty=qty, remaining=qty,
            risk_per_share=risk_per_share, entry_day=day, fill_key=fill_key,
            meta=dict(meta or {}),
        )
        self.filled_today += 1
        return True

    def sell(
        self, symbol: str, price: float, qty: int, day: pd.Timestamp,
        reason: str, stop_fill: bool = False,
    ) -> float:
        """Kısmi veya tam satış. Stop dolumlarına `stop_slippage` uygulanır."""
        h = self.holdings[symbol]
        qty = min(qty, h.remaining)
        if qty <= 0:
            return 0.0
        fill = price * (1.0 - self.cfg.stop_slippage) if stop_fill else price
        cost = qty * fill * self.cfg.cost_per_side
        self.cash += qty * fill - cost
        self.costs += cost
        entry_cost = qty * h.entry * self.cfg.cost_per_side
        pnl = (fill - h.entry) * qty - cost - entry_cost
        h.remaining -= qty
        self.fills.append({
            "symbol": symbol, "reason": reason, "price": fill, "qty": qty,
            "pnl": pnl, "day": day, "entry": h.entry,
        })
        if h.remaining <= 0:
            self._record_trade(h, day, reason)
            del self.holdings[symbol]
        return pnl

    def _record_trade(self, h: Holding, day: pd.Timestamp, reason: str) -> None:
        rows = [f for f in self.fills if f["symbol"] == h.symbol and f["entry"] == h.entry]
        sold = sum(r["qty"] for r in rows) or 1
        avg_exit = sum(r["price"] * r["qty"] for r in rows) / sold
        pnl = sum(r["pnl"] for r in rows)
        self.trades.append({
            "symbol": h.symbol, "entry_date": h.entry_day, "exit_date": day,
            "entry": h.entry, "exit": avg_exit, "qty": h.qty, "reason": reason,
            "days": (day - h.entry_day).days,
            "r": (avg_exit - h.entry) / h.risk_per_share if h.risk_per_share else 0.0,
            "ret": avg_exit / h.entry - 1.0, "pnl": pnl,
            "stop_pct": h.risk_per_share / h.entry if h.entry else 0.0,
            "levels": [r["reason"] for r in rows],
            **h.meta,
        })

    def close_all(self, prices: Dict[str, float], day: pd.Timestamp, reason: str = "end") -> None:
        for symbol in list(self.holdings):
            h = self.holdings[symbol]
            self.sell(symbol, prices.get(symbol, h.entry), h.remaining, day, reason)


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

    def sell_part(s: int, price: float, qty: int, i: int, reason: str, stop_fill: bool) -> None:
        """Kademeli satış. Pozisyon tamamen kapanınca tek işlem satırı yazılır."""
        nonlocal cash, total_costs
        p = positions[s]
        qty = min(qty, p.remaining)
        if qty <= 0:
            return
        slip = cfg.stop_slippage if stop_fill else 0.0
        fill = price * (1.0 - slip)
        cost = qty * fill * cfg.cost_per_side
        cash += qty * fill - cost
        total_costs += cost
        p.realized += (fill - p.entry) * qty - cost - qty * p.entry * cfg.cost_per_side
        p.sold_value += fill * qty
        p.sold_qty += qty
        p.remaining -= qty
        p.levels.append(reason)
        if p.remaining <= 0:
            avg = p.sold_value / p.sold_qty
            positions.pop(s)
            trades.append({
                "symbol": panel.symbols[s], "entry_date": dates[p.entry_i], "exit_date": dates[i],
                "entry": p.entry, "exit": avg, "qty": p.qty, "reason": reason, "days": i - p.entry_i,
                "r": (avg - p.entry) / p.risk_per_share, "ret": avg / p.entry - 1.0,
                "pnl": p.realized, "stop_pct": p.risk_per_share / p.entry, "levels": list(p.levels),
            })

    staged = cfg.exit_model == "atr_staged"

    for i in range(i0, i1):
        day = dates[i]
        if cash_growth is not None:
            cash *= float(cash_growth[i])
        key = (day.year, day.month)
        equity_open = cash + sum((pos.remaining if staged else pos.qty) * (last_close[s] if not np.isnan(last_close[s]) else pos.entry) for s, pos in positions.items())
        if key != month_key:
            month_key, month_start_equity = key, equity_open
        brake_on = equity_open / month_start_equity - 1.0 <= cfg.monthly_brake

        for s, reason in list(exit_flags.items()):
            if s not in positions:
                exit_flags.pop(s)
                continue
            if np.isnan(o[i, s]) or _locked_down(o[i, s], h[i, s], l[i, s], c[i, s], last_close[s]):
                continue
            if staged:
                sell_part(s, float(o[i, s]), positions[s].remaining, i, reason, False)
            else:
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
                        (pos.remaining if staged else pos.qty) * (o[i, q] if not np.isnan(o[i, q]) else last_close[q])
                        for q, pos in positions.items()
                    )
                    qty = position_quantity(equity_now, price, stop, order["adv"], cash, cfg)
                    if qty > 0:
                        cost = qty * price * cfg.cost_per_side
                        cash -= qty * price + cost
                        total_costs += cost
                        lv = levels_for(price, order["atr"], cfg)
                        positions[s] = Position(
                            s, qty, price, i, stop, risk_ps, price, remaining=qty,
                            tp1=lv["tp1"], tp2=lv["tp2"], tp3=lv["tp3"],
                        )
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
            if staged:
                state = PositionState(
                    entry=pos.entry, qty=pos.qty, remaining=pos.remaining, stop=pos.stop,
                    tp1=pos.tp1, tp2=pos.tp2, tp3=pos.tp3, reached=pos.reached,
                    top_price=pos.top_close, risk=pos.risk_per_share,
                )
                new_state, events = exit_step(
                    state, float(o[i, s]), float(h[i, s]), float(l[i, s]), cfg
                )
                for ev in events:
                    if ev.qty > 0:
                        sell_part(s, ev.price, ev.qty, i, ev.kind,
                                  ev.kind in ("stop", "stop_gap"))
                if s in positions:
                    positions[s].stop = new_state.stop
                    positions[s].reached = new_state.reached
                    positions[s].remaining = new_state.remaining
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

        value = sum((pos.remaining if staged else pos.qty) * last_close[s] for s, pos in positions.items())
        curve.append(cash + value)
        invested_curve.append(value / (cash + value))

    for s in list(positions):
        if staged:
            sell_part(s, float(last_close[s]), positions[s].remaining, i1 - 1, "end", False)
        else:
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
