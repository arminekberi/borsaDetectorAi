"""
Strateji v4 raporu.

Kullanım:
    python scripts/backtest_v4.py --data-dir veri/ohlcv --xu100 veri/xu100.csv --start 2025-08-01 --end 2026-08-07

`--random N`: aynı evrende rastgele seçim yapan N portföyle karşılaştırır.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Optional

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from strategy_v4 import backtest as bt  # noqa: E402
from strategy_v4.backtest import build_panel, metrics, run_backtest  # noqa: E402
from strategy_v4.config import StrategyConfig  # noqa: E402
from strategy_v4.data import clean_frames, load_csv_dir, load_xu100_csv  # noqa: E402


def benchmark(series: pd.Series) -> dict:
    years = (series.index[-1] - series.index[0]).days / 365.25
    total = series.iloc[-1] / series.iloc[0] - 1.0
    return {
        "getiri_%": round(total * 100, 1), "yillik_%": round(((1 + total) ** (1 / years) - 1) * 100, 1),
        "maks_dusus_%": round((series / series.cummax() - 1).min() * 100, 1),
    }


def alpha_beta(equity: pd.Series, index: pd.Series) -> dict:
    a = equity.pct_change()
    b = index.reindex(equity.index, method="ffill").pct_change()
    d = pd.concat([a, b], axis=1).dropna()
    d.columns = ["s", "m"]
    beta = np.cov(d.s, d.m)[0, 1] / np.var(d.m, ddof=1)
    alpha = (d.s.mean() - beta * d.m.mean()) * 250
    return {"beta": round(float(beta), 2), "alfa_yillik_%": round(float(alpha) * 100, 1)}


def random_baseline(panel, cfg, start, end, runs: int, seed: int = 7) -> np.ndarray:
    """Gerçek skorla aynı alım ve çıkış sıklığında rastgele seçim yapan portföyler."""
    score, eligible = bt.score_matrices(panel, cfg)
    original = bt.score_matrices
    dates = panel.dates
    mask = (dates >= pd.Timestamp(start)) & (dates <= pd.Timestamp(end))
    buy_rate = float((score[mask] >= cfg.buy_score)[eligible[mask]].mean())
    held = score[mask] >= cfg.exit_score
    exit_rate = 1.0 - float(held.mean()) if held.size else 0.1
    exit_rate = max(exit_rate * 0.35, 0.02)
    out = []
    rng = np.random.default_rng(seed)
    try:
        for _ in range(runs):
            u = rng.random(score.shape)
            fake = np.where(u < buy_rate, cfg.buy_score + 1.0, np.where(u > 1.0 - exit_rate, cfg.exit_score - 1.0, cfg.exit_score + 1.0))
            bt.score_matrices = lambda p, c: (fake, eligible)
            r = run_backtest(panel, cfg, start, end)
            out.append(r.equity.iloc[-1] / r.equity.iloc[0] - 1.0)
    finally:
        bt.score_matrices = original
    return np.array(out)


def report(args) -> None:
    cfg = StrategyConfig()
    frames = clean_frames(load_csv_dir(args.data_dir))
    frames = {k: v[v.index <= args.end] for k, v in frames.items()}
    xu = load_xu100_csv(args.xu100)
    xu = xu[xu.index <= args.end]
    sectors = json.load(open(args.sectors, encoding="utf-8")) if args.sectors else {}
    panel = build_panel(frames, xu, sectors, cfg)

    cash_index: Optional[pd.Series] = None
    if args.cash_index:
        cash_index = load_xu100_csv(args.cash_index)

    res = run_backtest(panel, cfg, args.start, args.end)
    m = metrics(res)
    print(f"Dönem {args.start} → {args.end} | {len(panel.symbols)} hisse | tarama: haftanın son işlem günü")
    print("Strateji:", json.dumps({k: float(v) for k, v in m.items()}, ensure_ascii=False))
    print("XU100:", benchmark(xu[args.start:args.end]))
    closes = pd.DataFrame({k: v["close"] for k, v in frames.items()})
    ew = (1 + closes.pct_change(fill_method=None).mean(axis=1)[args.start:args.end]).cumprod()
    print("Eşit ağırlık evren:", benchmark(ew))
    print("XU100'e göre:", alpha_beta(res.equity, xu))
    t = res.trades
    check = cfg.initial_capital + t["pnl"].sum()
    print(f"Muhasebe kontrolü: işlemlerden {check:,.0f} TL, eğriden {res.equity.iloc[-1]:,.0f} TL (son gün açık pozisyon maliyeti hariç)")
    print(f"Toplam maliyet: {res.costs:,.0f} TL")
    print("\nÇıkış sebepleri")
    print(t.groupby("reason").agg(adet=("r", "size"), ort_R=("r", "mean"), ort_gun=("days", "mean")).round(2).to_string())
    print("\nYıllar (giriş yılına göre)")
    print(t.groupby(t.entry_date.dt.year).agg(adet=("r", "size"), ort_R=("r", "mean"), kar_tl=("pnl", "sum")).round(2).to_string())
    print("\nAylık getiri: pozitif ay oranı %", round((res.equity.resample("ME").last().pct_change() > 0).mean() * 100, 1))
    print("\nKâr yoğunlaşması")
    srt = t.sort_values("pnl", ascending=False)
    print("En iyi 5 işlem kârı:", srt.head(5)[["symbol", "entry_date", "days", "ret", "pnl"]].round(3).to_string(index=False))
    print("Toplam kâr:", round(t.pnl.sum()), "| en iyi 5 hariç:", round(srt.iloc[5:].pnl.sum()))
    rng = np.random.default_rng(3)
    boot = rng.choice(t["r"].to_numpy(), (10000, len(t))).mean(axis=1)
    print(f"Ortalama R %95 GA: [{np.percentile(boot, 2.5):.2f}, {np.percentile(boot, 97.5):.2f}]")

    print("\nMaliyet senaryoları (taraf başına)")
    for c in (0.001, 0.002, 0.004):
        r = run_backtest(panel, StrategyConfig(cost_per_side=c), args.start, args.end)
        print(f"%{c * 100:.2f}: {metrics(r)['yillik_%']}% yıllık")
    if cash_index is not None:
        r = run_backtest(panel, cfg, args.start, args.end, cash_index=cash_index)
        ci = cash_index.reindex(res.equity.index, method="ffill").dropna()
        print("\nBoş nakit para piyasasında:", metrics(r)["yillik_%"], "% yıllık, getiri", metrics(r)["getiri_%"], "%")
        print("Para piyasası fonu:", benchmark(ci))
    if args.random:
        d = random_baseline(panel, cfg, args.start, args.end, args.random)
        s = res.equity.iloc[-1] / res.equity.iloc[0] - 1.0
        print(f"\nRastgele seçim ({args.random} portföy): medyan {np.median(d) * 100:.1f}%, %5-%95: {np.percentile(d, 5) * 100:.1f}…{np.percentile(d, 95) * 100:.1f}%")
        print(f"Strateji {s * 100:.1f}% → rastgelelerin %{(d < s).mean() * 100:.1f}'inden iyi")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", required=True)
    ap.add_argument("--xu100", required=True)
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--sectors", default="config/sectors_v4.json")
    ap.add_argument("--cash-index")
    ap.add_argument("--random", type=int, default=0)
    report(ap.parse_args())
