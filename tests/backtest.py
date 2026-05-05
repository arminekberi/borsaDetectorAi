#!/usr/bin/env python3
"""
Walk-forward backtest for BIST signal strategy.

Fetches 1 year of 60m Yahoo data per symbol, resamples to 4H exactly as
the bot does, then simulates bar-by-bar with no lookahead bias.

Usage:
    cd bist-telegram-bot
    python tests/backtest.py

Outputs:
    data/backtest_results.csv   — full trade log
    data/backtest_summary.txt   — per-symbol and overall stats
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

# ── Path setup ────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(BASE_DIR))

# Suppress verbose logging from strategy modules and yfinance
logging.basicConfig(level=logging.WARNING)
for _logger_name in ("yfinance", "urllib3", "peewee", "strategy", "market", "utils"):
    logging.getLogger(_logger_name).setLevel(logging.ERROR)

import yfinance as yf  # noqa: E402 — import after logging setup

from market.models import SignalType          # noqa: E402
from market.symbols import WATCHLIST          # noqa: E402
from strategy.signal_engine import evaluate_symbol  # noqa: E402

DATA_DIR = BASE_DIR / "data"

# ── Backtest parameters ───────────────────────────────────────────────────────
TIMEFRAME   = "4H"
EXPIRY_BARS = 18    # 72 h at 4 h/bar
WARMUP_BARS = 35    # bars before first signal evaluation (covers RSI-14, StochRSI-31, SMA-15)

SYMBOLS      = {"ASELS", "AKBNK"}   # symbols to backtest
ATR_PERIOD   = 14
ATR_SL_MULT  = 1.5   # stop = entry ± ATR(14) × 1.5
ATR_TP1_MULT = 2.5   # tp1  = entry ± ATR(14) × 2.5  (RR = 1.67)
TP2_VARIANTS = [4.0]          # single TP2 multiplier

INITIAL_PORTFOLIO = 100_000.0  # starting capital in TRY
RISK_PCT          = 0.01       # fraction of portfolio risked per trade


# ═══════════════════════════════════════════════════════════════════════════════
# Data fetching
# ═══════════════════════════════════════════════════════════════════════════════

def fetch_year_4h(symbol: str) -> Optional[pd.DataFrame]:
    """
    Fetch 1 year of 60m Yahoo data and resample to 4H bars.
    Replicates YahooFinanceFetcher.get_ohlc(interval="4h") exactly.
    """
    yf_sym = f"{symbol.upper()}.IS"
    print(f"  Fetching {yf_sym} ...", end=" ", flush=True)
    try:
        ticker = yf.Ticker(yf_sym)
        raw = ticker.history(period="365d", interval="60m", auto_adjust=True)
        if raw is None or raw.empty:
            print("NO DATA")
            return None

        if isinstance(raw.columns, pd.MultiIndex):
            raw.columns = [c[0] for c in raw.columns]
        raw.columns = [str(c).strip().lower().replace(" ", "_") for c in raw.columns]

        for extra in ("adj_close", "adjclose"):
            if extra in raw.columns:
                raw = raw.drop(columns=[extra])

        needed = ["open", "high", "low", "close", "volume"]
        if any(c not in raw.columns for c in needed):
            print(f"MISSING COLS: {raw.columns.tolist()}")
            return None

        df = raw[needed].apply(pd.to_numeric, errors="coerce").dropna()

        # UTC normalise — same logic as YahooFinanceFetcher
        if not isinstance(df.index, pd.DatetimeIndex):
            df.index = pd.to_datetime(df.index, utc=True)
        elif df.index.tz is None:
            df.index = df.index.tz_localize("UTC")
        else:
            df.index = df.index.tz_convert("UTC")

        # Resample to 4H — identical aggregation as YahooFinanceFetcher
        df4 = (
            df.resample("4h")
            .agg({"open": "first", "high": "max", "low": "min",
                  "close": "last", "volume": "sum"})
            .dropna()
        )
        print(f"OK — {len(df4)} 4H bars "
              f"({df4.index[0].date()} → {df4.index[-1].date()})")
        return df4

    except Exception as exc:
        print(f"ERROR: {exc}")
        return None


# ═══════════════════════════════════════════════════════════════════════════════
# Trade helpers
# ═══════════════════════════════════════════════════════════════════════════════

def _atr14(df: pd.DataFrame) -> float:
    """ATR(14) with Wilder smoothing (ewm alpha=1/14) on the given slice."""
    hi      = df["high"]
    lo      = df["low"]
    cl      = df["close"]
    prev_cl = cl.shift(1)
    tr = pd.concat(
        [hi - lo, (hi - prev_cl).abs(), (lo - prev_cl).abs()], axis=1
    ).max(axis=1)
    return float(tr.ewm(alpha=1.0 / ATR_PERIOD, adjust=False).mean().iloc[-1])


def _pnl_pct(direction: str, entry: float, exit_p: float) -> float:
    if direction == "BUY":
        return (exit_p - entry) / entry * 100.0
    return (entry - exit_p) / entry * 100.0


def _rr(direction: str, entry: float, sl: float, exit_p: float) -> float:
    risk = abs(entry - sl)
    if risk < 1e-9:
        return 0.0
    if direction == "BUY":
        return (exit_p - entry) / risk
    return (entry - exit_p) / risk


def _close_trade(
    open_trade: dict,
    exit_p: float,
    exit_reason: str,
    exit_time: pd.Timestamp,
    bars_held: int,
) -> dict:
    direction   = open_trade["direction"]
    entry_price = open_trade["entry_price"]
    return {
        "symbol":         open_trade["symbol"],
        "direction":      direction,
        "entry_time":     open_trade["entry_time"].isoformat(),
        "exit_time":      exit_time.isoformat(),
        "entry_price":    round(entry_price, 4),
        "exit_price":     round(exit_p, 4),
        "sl":             round(open_trade["sl_initial"], 4),
        "tp1":            round(open_trade["tp1"], 4),
        "tp2":            round(open_trade["tp2"], 4),
        "exit_reason":    exit_reason,
        "pnl_pct":        round(_pnl_pct(direction, entry_price, exit_p), 4),
        "rr_achieved":    round(_rr(direction, entry_price, open_trade["sl_initial"], exit_p), 4),
        "duration_bars":  bars_held,
        "duration_hours": bars_held * 4,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# Walk-forward simulation
# ═══════════════════════════════════════════════════════════════════════════════

def run_symbol(symbol: str, df: pd.DataFrame, atr_tp2_mult: float) -> Tuple[List[dict], dict]:
    """
    Walk-forward simulation for one symbol.

    Bar i: advance any open trade using bar i's OHLC, then evaluate a new
    signal on df[:i+1] (no lookahead). Entry is at close[i]; tracking starts
    at bar i+1.

    Returns (trade_list, signal_counts).
    """
    trades:     List[dict]   = []
    open_trade: Optional[dict] = None
    counts = {"signals_fired": 0, "trades_taken": 0, "watch_signals": 0}

    for i in range(WARMUP_BARS, len(df) - 1):
        bar      = df.iloc[i]
        bar_time = df.index[i]
        lo       = float(bar["low"])
        hi       = float(bar["high"])
        cl       = float(bar["close"])

        # ── 1. Advance open trade ─────────────────────────────────────────────
        if open_trade is not None:
            direction  = open_trade["direction"]
            entry_p    = open_trade["entry_price"]
            tp1_hit    = open_trade["tp1_hit"]
            cur_sl     = open_trade["cur_sl"]
            tp1        = open_trade["tp1"]
            tp2        = open_trade["tp2"]
            bars_held  = i - open_trade["entry_bar"]
            exit_p: Optional[float] = None
            exit_rsn:  Optional[str] = None

            if direction == "BUY":
                if not tp1_hit:
                    # SL takes priority on same bar (conservative)
                    if lo <= cur_sl:
                        exit_p, exit_rsn = cur_sl, "SL"
                    elif hi >= tp1:
                        open_trade["tp1_hit"] = True
                        open_trade["cur_sl"]  = entry_p  # move SL to breakeven
                else:
                    if lo <= open_trade["cur_sl"]:       # stopped at breakeven
                        exit_p, exit_rsn = entry_p, "TP1_BE"
                    elif hi >= tp2:
                        exit_p, exit_rsn = tp2, "TP2"

            else:  # SELL
                if not tp1_hit:
                    if hi >= cur_sl:
                        exit_p, exit_rsn = cur_sl, "SL"
                    elif lo <= tp1:
                        open_trade["tp1_hit"] = True
                        open_trade["cur_sl"]  = entry_p
                else:
                    if hi >= open_trade["cur_sl"]:
                        exit_p, exit_rsn = entry_p, "TP1_BE"
                    elif lo <= tp2:
                        exit_p, exit_rsn = tp2, "TP2"

            # Expiry check (48 h)
            if exit_p is None and bars_held >= EXPIRY_BARS:
                exit_p, exit_rsn = cl, "EXPIRED"

            if exit_p is not None:
                trades.append(_close_trade(open_trade, exit_p, exit_rsn,
                                           bar_time, bars_held))
                open_trade = None
                # fall through to signal evaluation on same bar

        # ── 2. Evaluate signal (always — counts fired signals even if trade open)
        slice_df = df.iloc[: i + 1]
        signal   = evaluate_symbol(symbol, slice_df, TIMEFRAME)

        if signal is not None:
            stype = signal.signal_type
            if stype in (SignalType.BUY, SignalType.SELL):
                counts["signals_fired"] += 1
                if open_trade is None:
                    # All levels ATR-based (consistent RR regardless of volatility)
                    atr = _atr14(slice_df)
                    if stype == SignalType.BUY:
                        sl  = cl - atr * ATR_SL_MULT
                        tp1 = cl + atr * ATR_TP1_MULT
                        tp2 = cl + atr * atr_tp2_mult
                    else:
                        sl  = cl + atr * ATR_SL_MULT
                        tp1 = cl - atr * ATR_TP1_MULT
                        tp2 = cl - atr * atr_tp2_mult
                    # Enter trade: entry at close of signal bar
                    counts["trades_taken"] += 1
                    open_trade = {
                        "symbol":      symbol,
                        "direction":   stype.value,
                        "entry_time":  bar_time,
                        "entry_bar":   i,
                        "entry_price": cl,
                        "sl_initial":  sl,
                        "cur_sl":      sl,
                        "tp1":         tp1,
                        "tp2":         tp2,
                        "tp1_hit":     False,
                    }
            elif stype == SignalType.WATCH:
                counts["watch_signals"] += 1

    # ── Close any trade still open at end of data ─────────────────────────────
    if open_trade is not None:
        last_bar   = df.iloc[-1]
        last_time  = df.index[-1]
        bars_held  = len(df) - 1 - open_trade["entry_bar"]
        exit_p     = float(last_bar["close"])
        trades.append(_close_trade(open_trade, exit_p, "EXPIRED",
                                   last_time, bars_held))

    return trades, counts


# ═══════════════════════════════════════════════════════════════════════════════
# Statistics
# ═══════════════════════════════════════════════════════════════════════════════

def compute_stats(trades: List[dict]) -> dict:
    if not trades:
        return {
            "total_trades": 0, "win_rate": 0.0, "avg_rr": 0.0,
            "max_drawdown": 0.0, "avg_duration_hours": 0.0,
            "best_trade": None, "worst_trade": None,
        }

    pnls      = [t["pnl_pct"]        for t in trades]
    rrs       = [t["rr_achieved"]     for t in trades]
    durations = [t["duration_hours"]  for t in trades]
    wins      = sum(1 for p in pnls if p > 0)

    # Max drawdown on cumulative PnL series
    cum         = np.cumsum(pnls)
    running_max = np.maximum.accumulate(cum)
    drawdowns   = running_max - cum
    max_dd      = float(np.max(drawdowns)) if len(drawdowns) else 0.0

    return {
        "total_trades":       len(trades),
        "win_rate":           round(wins / len(trades) * 100, 1),
        "avg_rr":             round(float(np.mean(rrs)), 3),
        "max_drawdown":       round(max_dd, 2),
        "avg_duration_hours": round(float(np.mean(durations)), 1),
        "best_trade":         max(trades, key=lambda t: t["pnl_pct"]),
        "worst_trade":        min(trades, key=lambda t: t["pnl_pct"]),
    }


def _fmt_trade(t: Optional[dict]) -> str:
    if t is None:
        return "N/A"
    return (
        f"{t['symbol']} {t['direction']} "
        f"@ {t['entry_price']} → {t['exit_price']} "
        f"({t['pnl_pct']:+.2f}%, {t['exit_reason']})"
    )


def _exit_breakdown(trades: List[dict]) -> Dict[str, int]:
    bd: Dict[str, int] = {}
    for t in trades:
        bd[t["exit_reason"]] = bd.get(t["exit_reason"], 0) + 1
    return bd


# ═══════════════════════════════════════════════════════════════════════════════
# Reporting helpers
# ═══════════════════════════════════════════════════════════════════════════════

def _print_variant_row(label: str, stats: dict, counts: dict,
                       exit_bd: Dict[str, int]) -> None:
    """One compact line per TP2 variant, followed by best/worst."""
    sl  = exit_bd.get("SL",      0)
    tp2 = exit_bd.get("TP2",     0)
    exp = exit_bd.get("EXPIRED", 0)
    be  = exit_bd.get("TP1_BE",  0)
    print(
        f"  {label:<9} │ "
        f"Trades:{stats['total_trades']:>4}  "
        f"Win:{stats['win_rate']:>5.1f}%  "
        f"RR:{stats['avg_rr']:>6.3f}  "
        f"DD:{stats['max_drawdown']:>6.2f}%  "
        f"Dur:{stats['avg_duration_hours']:>4.0f}h  "
        f"Sigs:{counts['signals_fired']:>4}  "
        f"│ SL:{sl:<3} TP2:{tp2:<3} Exp:{exp:<3} BE:{be}"
    )
    print(f"  {'':<9}   Best:  {_fmt_trade(stats['best_trade'])}")
    print(f"  {'':<9}   Worst: {_fmt_trade(stats['worst_trade'])}")


def _write_summary(
    path: Path,
    symbol_dfs: Dict[str, pd.DataFrame],
    variant_results: Dict[float, Dict[str, Tuple[List[dict], dict]]],
) -> None:
    """Write a single summary file covering all symbols and both TP2 variants."""
    with open(path, "w", encoding="utf-8") as f:
        f.write("BIST Signal Strategy — Walk-Forward Backtest\n")
        f.write(f"Generated : {datetime.now(timezone.utc).isoformat()}\n")
        f.write(f"Symbols   : {', '.join(sorted(SYMBOLS))}\n")
        f.write(f"Period    : 1 year of Yahoo 60m data resampled to 4H\n")
        f.write(f"SL / TP1  : ATR×{ATR_SL_MULT} / ATR×{ATR_TP1_MULT}"
                f"  (ATR period={ATR_PERIOD}, computed at signal bar)\n")
        f.write(f"TP2 tested: {' vs '.join(f'ATR×{m}' for m in TP2_VARIANTS)}\n")
        f.write(f"Expiry    : {EXPIRY_BARS * 4}h ({EXPIRY_BARS} bars)\n")
        f.write("\n")

        def _row(label: str, st: dict, counts: dict, bd: Dict[str, int]) -> str:
            return (
                f"  {label:<10} trades={st['total_trades']:<4} "
                f"win={st['win_rate']:<5.1f}% rr={st['avg_rr']:<6.3f} "
                f"dd={st['max_drawdown']:<6.2f}% dur={st['avg_duration_hours']:.0f}h "
                f"sigs={counts['signals_fired']:<4} "
                f"SL={bd.get('SL',0):<3} TP2={bd.get('TP2',0):<3} "
                f"Exp={bd.get('EXPIRED',0):<3} BE={bd.get('TP1_BE',0)}\n"
            )

        # ── Per symbol ──
        f.write("PER SYMBOL\n")
        f.write("─" * 80 + "\n")
        for symbol in sorted(symbol_dfs):
            f.write(f"\n{symbol}\n")
            for tp2m in TP2_VARIANTS:
                trades, counts = variant_results[tp2m][symbol]
                st = compute_stats(trades)
                bd = _exit_breakdown(trades)
                f.write(_row(f"TP2×{tp2m}", st, counts, bd))
                f.write(f"             best : {_fmt_trade(st['best_trade'])}\n")
                f.write(f"             worst: {_fmt_trade(st['worst_trade'])}\n")

        # ── Overall ──
        f.write("\n")
        f.write("═" * 80 + "\n")
        f.write("OVERALL (all symbols combined)\n")
        f.write("═" * 80 + "\n")
        for tp2m in TP2_VARIANTS:
            all_trades = [t for sym_r in variant_results[tp2m].values()
                          for t in sym_r[0]]
            all_counts = {
                "signals_fired": sum(sym_r[1]["signals_fired"]
                                     for sym_r in variant_results[tp2m].values()),
                "trades_taken":  sum(sym_r[1]["trades_taken"]
                                     for sym_r in variant_results[tp2m].values()),
            }
            st = compute_stats(all_trades)
            bd = _exit_breakdown(all_trades)
            f.write(_row(f"TP2×{tp2m}", st, all_counts, bd))
            f.write(f"             best : {_fmt_trade(st['best_trade'])}\n")
            f.write(f"             worst: {_fmt_trade(st['worst_trade'])}\n")


# ═══════════════════════════════════════════════════════════════════════════════
# Portfolio simulation & ASCII chart
# ═══════════════════════════════════════════════════════════════════════════════

def _apply_sizing(
    trades: List[dict],
    initial: float,
    risk_pct: float,
) -> Tuple[List[dict], List[float], List[str]]:
    """
    Apply fixed-fractional sizing to trades sorted by exit_time.

    Position size  = (portfolio × risk_pct) / |entry − sl|
    PnL in TRY     = position_size × (exit − entry)  for BUY
                   = position_size × (entry − exit)  for SELL

    A SL exit therefore always loses exactly risk_pct of portfolio at that
    moment; TP exits gain proportionally more (RR × risk_pct).

    Returns:
        enriched   – trade dicts with position_size, pnl_try, portfolio_after
        equity     – portfolio value after each trade (index 0 = initial)
        dates      – ISO date string at each equity point ("start" for index 0)
    """
    portfolio = initial
    equity:   List[float] = [portfolio]
    dates:    List[str]   = ["start"]
    enriched: List[dict]  = []

    for t in trades:
        entry     = t["entry_price"]
        exit_p    = t["exit_price"]
        sl        = t["sl"]
        direction = t["direction"]

        sl_dist = abs(entry - sl)
        if sl_dist < 1e-9:
            enriched.append({**t, "position_size": 0.0,
                              "pnl_try": 0.0, "portfolio_after": round(portfolio, 2)})
            equity.append(portfolio)
            dates.append(t["exit_time"][:10])
            continue

        position_size = (portfolio * risk_pct) / sl_dist
        pnl_try = position_size * (
            (exit_p - entry) if direction == "BUY" else (entry - exit_p)
        )
        portfolio += pnl_try
        enriched.append({
            **t,
            "position_size":   round(position_size, 4),
            "pnl_try":         round(pnl_try, 2),
            "portfolio_after": round(portfolio, 2),
        })
        equity.append(portfolio)
        dates.append(t["exit_time"][:10])

    return enriched, equity, dates


def _ascii_chart(
    equity: List[float],
    dates:  List[str],
    width:  int = 62,
    height: int = 14,
) -> List[str]:
    """
    Compact ASCII dot-chart of an equity curve.

    Each data point is one dot; Y-axis shows TRY values at top/mid/bottom.
    Multiple data points that map to the same column take the last value.
    """
    if len(equity) < 2:
        return ["  (not enough data for chart)"]

    lo = min(equity)
    hi = max(equity)
    # Add 5 % vertical padding so top/bottom dots are not clipped
    pad = (hi - lo) * 0.05 or abs(hi) * 0.01 or 1.0
    lo -= pad
    hi += pad

    n      = len(equity)
    Y_W    = 13   # width of "  NNN,NNN ┤" label

    def to_row(v: float) -> int:
        return height - 1 - int((v - lo) / (hi - lo) * (height - 1))

    # Build column → row mapping (last value wins for dense series)
    col_map: Dict[int, int] = {}
    for i, eq in enumerate(equity):
        col = min(int(i * width / n), width - 1)
        col_map[col] = to_row(eq)

    # Build grid and mark dots
    grid = [[" "] * width for _ in range(height)]
    for col, row in col_map.items():
        grid[row][col] = "●"

    # Connect adjacent dots with a light horizontal bridge so the curve reads
    # as a continuous line rather than scattered points
    prev_col, prev_row = None, None
    for col in range(width):
        if col not in col_map:
            continue
        row = col_map[col]
        if prev_col is not None and col - prev_col == 1:
            # same height: already connected; different height: draw tiny step
            r0, r1 = min(prev_row, row), max(prev_row, row)
            for r in range(r0, r1 + 1):
                if grid[r][prev_col] == " ":
                    grid[r][prev_col] = "╎"
        prev_col, prev_row = col, row

    # Y-axis labels (top, middle, bottom only to keep it uncluttered)
    lines: List[str] = []
    for r in range(height):
        val = hi - (hi - lo) * r / (height - 1)
        if r in (0, height // 2, height - 1):
            label = f"{val:>11,.0f} ┤"
        else:
            label = " " * 11 + " │"
        lines.append(label + "".join(grid[r]))

    # Bottom axis
    lines.append(" " * (Y_W - 1) + "└" + "─" * width)

    # Date labels: first, middle, last trade date
    non_start = [d for d in dates if d != "start"]
    if len(non_start) >= 2:
        d0 = non_start[0][:7]
        dm = non_start[len(non_start) // 2][:7]
        d1 = non_start[-1][:7]
        base  = Y_W          # column where "─" starts
        dline = [" "] * (base + width + 4)
        for i, c in enumerate(d0):
            dline[base + i] = c
        mid_x = base + width // 2 - len(dm) // 2
        for i, c in enumerate(dm):
            if 0 <= mid_x + i < len(dline):
                dline[mid_x + i] = c
        last_x = base + width - len(d1)
        for i, c in enumerate(d1):
            if 0 <= last_x + i < len(dline):
                dline[last_x + i] = c
        lines.append("".join(dline))

    return lines


# ═══════════════════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    SEP  = "=" * 70
    SEP2 = "-" * 70
    tp2_label = " vs ".join(f"ATR×{m}" for m in TP2_VARIANTS)
    print(SEP)
    print(f"  BIST Signal Strategy — Walk-Forward Backtest (4H, 72h expiry)")
    print(f"  Symbols : {', '.join(sorted(SYMBOLS))}")
    print(f"  SL=ATR×{ATR_SL_MULT}  TP1=ATR×{ATR_TP1_MULT}  TP2: {tp2_label}")
    print(SEP)
    print()

    # ── Phase 1: fetch data once per symbol ───────────────────────────────────
    print("Fetching data ...")
    symbol_dfs: Dict[str, pd.DataFrame] = {}
    for symbol, _ in WATCHLIST:
        if symbol not in SYMBOLS:
            continue
        print(f"  [{symbol}]", end=" ")
        df = fetch_year_4h(symbol)
        if df is not None and len(df) >= WARMUP_BARS + 5:
            symbol_dfs[symbol] = df
        else:
            print(f"  [{symbol}] Skipped — insufficient data")
    print()

    # ── Phase 2: run both variants for every symbol ───────────────────────────
    # variant_results[tp2_mult][symbol] = (trades, counts)
    variant_results: Dict[float, Dict[str, Tuple[List[dict], dict]]] = {}
    for tp2m in TP2_VARIANTS:
        variant_results[tp2m] = {}
        for symbol, df in symbol_dfs.items():
            trades, counts = run_symbol(symbol, df, tp2m)
            variant_results[tp2m][symbol] = (trades, counts)

    # ── Phase 3: print per-symbol side-by-side ────────────────────────────────
    print("PER SYMBOL")
    print(SEP)
    for symbol in sorted(symbol_dfs):
        df = symbol_dfs[symbol]
        print(f"\n[{symbol}]  ({len(df)} bars, "
              f"{df.index[0].date()} → {df.index[-1].date()})")
        for tp2m in TP2_VARIANTS:
            trades, counts = variant_results[tp2m][symbol]
            stats   = compute_stats(trades)
            exit_bd = _exit_breakdown(trades)
            _print_variant_row(f"TP2×{tp2m}", stats, counts, exit_bd)
    print()

    # ── Phase 4: combined (all symbols) per variant ───────────────────────────
    print(SEP)
    print(f"OVERALL  ({' + '.join(sorted(symbol_dfs))} combined)")
    print(SEP)
    for tp2m in TP2_VARIANTS:
        all_trades = [t for sym_r in variant_results[tp2m].values()
                      for t in sym_r[0]]
        all_counts = {
            "signals_fired": sum(r[1]["signals_fired"]
                                 for r in variant_results[tp2m].values()),
            "trades_taken":  sum(r[1]["trades_taken"]
                                 for r in variant_results[tp2m].values()),
            "watch_signals": sum(r[1]["watch_signals"]
                                 for r in variant_results[tp2m].values()),
        }
        overall = compute_stats(all_trades)
        exit_bd = _exit_breakdown(all_trades)
        _print_variant_row(f"TP2×{tp2m}", overall, all_counts, exit_bd)
    print()

    # ── Phase 5: portfolio equity curve (TP2×4.0, chronological) ────────────
    tp2_eq = TP2_VARIANTS[0]   # 4.0
    raw_trades = sorted(
        [t for sym_r in variant_results[tp2_eq].values() for t in sym_r[0]],
        key=lambda t: t["exit_time"],
    )
    enriched, equity, eq_dates = _apply_sizing(raw_trades, INITIAL_PORTFOLIO, RISK_PCT)

    eq_arr      = np.array(equity)
    run_max     = np.maximum.accumulate(eq_arr)
    dd_try      = run_max - eq_arr
    max_dd_try  = float(np.max(dd_try))
    max_dd_pct  = float(np.max(dd_try / run_max)) * 100
    total_ret   = (equity[-1] / INITIAL_PORTFOLIO - 1) * 100

    print(SEP)
    print(f"PORTFOLIO EQUITY CURVE"
          f"  (start={INITIAL_PORTFOLIO:,.0f} TRY, risk={RISK_PCT*100:.0f}%/trade, TP2×{tp2_eq})")
    print(SEP)
    print(f"  Initial value  : {INITIAL_PORTFOLIO:>12,.2f} TRY")
    print(f"  Final value    : {equity[-1]:>12,.2f} TRY")
    print(f"  Total return   : {equity[-1]-INITIAL_PORTFOLIO:>+12,.2f} TRY  ({total_ret:+.2f}%)")
    print(f"  Max drawdown   : {max_dd_try:>12,.2f} TRY  ({max_dd_pct:.2f}%)")
    print(f"  Trades plotted : {len(enriched)}")
    print()
    for line in _ascii_chart(equity, eq_dates):
        print(line)
    print()

    # ── Save CSV (enriched with position sizing) ──────────────────────────────
    csv_path = DATA_DIR / "backtest_results.csv"
    if enriched:
        pd.DataFrame(enriched).to_csv(csv_path, index=False)
        print(f"Trade log  → {csv_path}  ({len(enriched)} rows, includes pnl_try)")

    # ── Save summary ──────────────────────────────────────────────────────────
    summary_path = DATA_DIR / "backtest_summary.txt"
    _write_summary(summary_path, symbol_dfs, variant_results)
    print(f"Summary    → {summary_path}")


if __name__ == "__main__":
    main()
