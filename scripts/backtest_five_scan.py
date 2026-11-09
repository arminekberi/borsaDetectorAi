#!/usr/bin/env python3
"""
5 sorgu sisteminin backtest'i. Raporu `data/backtest_five_scan_report.md` yazar.

Amaç canlıda çalışacak sistemin AYNISINI geçmişte ölçmek; parametre optimize
etmek DEĞİL. Komşuluk testi yalnızca kırılganlık içindir, seçim için değil.

Kullanım:
    python scripts/backtest_five_scan.py
    python scripts/backtest_five_scan.py --start 2025-05-07 --end 2026-11-06
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from dataclasses import replace
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from strategy_v4.backtest import build_panel, metrics, run_backtest  # noqa: E402
from strategy_v4.backtest_intraday import (  # noqa: E402
    DEFAULT_SCAN_TIMES,
    IntradayConfig,
    build_signals,
    classify_exit,
    daily_scores,
    simulate,
)
from strategy_v4.config import StrategyConfig  # noqa: E402
from strategy_v4.data import load_yahoo_cached  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("backtest")

# Evren A: izleme listesi (.env WATCHLIST). BORLEASE yerine BORLS.
UNIVERSE_A = ["ASELS", "PAHOL", "VAKFN", "BORLS", "EFOR", "SMRVA", "NTGAZ", "INDES", "FROTO"]
# Evren B: kontrol evreni. sectors_v4.json'dan 15 likit hisse (ölçek kullanıcı
# talebiyle 92'den 15'e indirildi; raporda yanılgı olarak belirtilir).
UNIVERSE_B = [
    "AKBNK", "ASELS", "BIMAS", "EREGL", "FROTO", "GARAN", "ISCTR", "KCHOL",
    "KRDMD", "PETKM", "SAHOL", "SASA", "THYAO", "TUPRS", "YKBNK",
]
COSTS = (0.0010, 0.0020, 0.0040)
BASE_COST = 0.0020


# ---------------------------------------------------------------------------
# Veri
# ---------------------------------------------------------------------------


def load_hourly(symbols: List[str]) -> Dict[str, pd.DataFrame]:
    """60m veri. Bazı sembollerde `period="730d"` boş dönüyor, `"2y"`e düşülür."""
    res = load_yahoo_cached(symbols, interval="60m", period="730d", auto_adjust=False, ttl_hours=999)
    frames = dict(res["frames"])
    errors = list(res["errors"])
    missing = [s for s in symbols if s not in frames]
    if missing:
        alt = load_yahoo_cached(missing, interval="60m", period="2y", auto_adjust=False, ttl_hours=999)
        frames.update(alt["frames"])
        errors = [(s, e) for s, e in errors if s not in alt["frames"]] + list(alt["errors"])
    for s, e in errors:
        log.warning("60m alınamadı %s: %s", s, str(e)[:80])
    return frames


def load_all(symbols: List[str]):
    daily = load_yahoo_cached(symbols, interval="1d", period="6y", auto_adjust=True, ttl_hours=999)
    for s, e in daily["errors"]:
        log.warning("1d alınamadı %s: %s", s, str(e)[:80])
    hourly = load_hourly(symbols)
    xu_d = load_yahoo_cached(["XU100"], interval="1d", period="6y", auto_adjust=False, ttl_hours=999)
    xu_h = load_hourly(["XU100"])
    return daily["frames"], hourly, xu_d["frames"]["XU100"]["close"], xu_h["XU100"]


# ---------------------------------------------------------------------------
# Ölçüm
# ---------------------------------------------------------------------------


def bootstrap_ci(values: np.ndarray, n: int = 2000, seed: int = 7) -> tuple:
    if len(values) < 2:
        return (float("nan"), float("nan"))
    rng = np.random.default_rng(seed)
    means = [rng.choice(values, len(values), replace=True).mean() for _ in range(n)]
    return (float(np.percentile(means, 2.5)), float(np.percentile(means, 97.5)))


def curve_stats(equity: pd.Series, invested: pd.Series, trades: pd.DataFrame) -> dict:
    if equity.empty:
        return {}
    years = max((equity.index[-1] - equity.index[0]).days / 365.25, 1e-9)
    total = equity.iloc[-1] / equity.iloc[0] - 1.0
    dd = float((equity / equity.cummax() - 1.0).min())
    out = {
        "getiri_%": round(total * 100, 1),
        "yillik_%": round(((1 + total) ** (1 / years) - 1) * 100, 1),
        "maks_dusus_%": round(dd * 100, 1),
        "portfoyde_%": round(float(invested.mean()) * 100, 1),
        "islem": int(len(trades)),
    }
    if len(trades):
        r = trades["r"].to_numpy(dtype=float)
        wins = trades.loc[trades["pnl"] > 0, "pnl"].sum()
        losses = -trades.loc[trades["pnl"] <= 0, "pnl"].sum()
        lo, hi = bootstrap_ci(r)
        out.update({
            "kazanma_%": round(float((trades["pnl"] > 0).mean()) * 100, 1),
            "ort_R": round(float(r.mean()), 3),
            "medyan_R": round(float(np.median(r)), 3),
            "R_GA95": f"[{lo:.2f}; {hi:.2f}]",
            "kar_faktoru": round(float(wins / losses), 2) if losses > 0 else float("inf"),
            "ort_gun": round(float(trades["days"].mean()), 1),
        })
        best5 = trades.nlargest(min(5, len(trades)), "pnl")["pnl"].sum()
        start_eq = float(equity.iloc[0])
        out["en_iyi5_haric_%"] = round((equity.iloc[-1] - best5) / start_eq * 100 - 100, 1)
    return out


def benchmark(xu_daily: pd.Series, daily: Dict[str, pd.DataFrame], index: pd.DatetimeIndex) -> dict:
    lo, hi = index[0], index[-1]
    xu = xu_daily[(xu_daily.index >= lo) & (xu_daily.index <= hi)]
    eq_curves = []
    for frame in daily.values():
        s = frame["close"][(frame.index >= lo) & (frame.index <= hi)]
        if len(s) > 1:
            eq_curves.append(s / s.iloc[0])
    ew = pd.concat(eq_curves, axis=1).ffill().mean(axis=1) if eq_curves else pd.Series(dtype=float)
    def stats(curve: pd.Series, name: str) -> dict:
        if len(curve) < 2:
            return {"ad": name}
        total = float(curve.iloc[-1] / curve.iloc[0] - 1.0)
        dd = float((curve / curve.cummax() - 1.0).min())
        years = max((curve.index[-1] - curve.index[0]).days / 365.25, 1e-9)
        return {
            "ad": name, "getiri_%": round(total * 100, 1),
            "yillik_%": round(((1 + total) ** (1 / years) - 1) * 100, 1),
            "maks_dusus_%": round(dd * 100, 1),
        }
    return {"xu100": stats(xu, "XU100"), "esit_agirlik": stats(ew, "Eşit ağırlık evren")}


def halves(res, xu_daily, daily) -> List[dict]:
    eq = res.equity
    if len(eq) < 4:
        return []
    mid = eq.index[len(eq) // 2]
    out = []
    for name, lo, hi in (("ilk yarı", eq.index[0], mid), ("ikinci yarı", mid, eq.index[-1])):
        sub_eq = eq[(eq.index >= lo) & (eq.index <= hi)]
        sub_inv = res.invested[(res.invested.index >= lo) & (res.invested.index <= hi)]
        tr = res.trades
        sub_tr = tr[(tr["exit_date"] >= lo) & (tr["exit_date"] <= hi)] if len(tr) else tr
        row = {"donem": f"{name} ({lo.date()}→{hi.date()})"}
        row.update(curve_stats(sub_eq, sub_inv, sub_tr))
        bm = benchmark(xu_daily, daily, sub_eq.index)
        row["XU100_%"] = bm["xu100"].get("getiri_%")
        out.append(row)
    return out


def md_table(rows: List[dict], cols: Optional[List[str]] = None) -> str:
    if not rows:
        return "_veri yok_\n"
    cols = cols or list({k: None for r in rows for k in r})
    head = "| " + " | ".join(cols) + " |"
    sep = "|" + "|".join("---" for _ in cols) + "|"
    body = []
    for r in rows:
        body.append("| " + " | ".join(_fmt(r.get(c, "")) for c in cols) + " |")
    return "\n".join([head, sep, *body]) + "\n"


def _fmt(v) -> str:
    if isinstance(v, float):
        if np.isnan(v):
            return "—"
        return f"{v:,.2f}".rstrip("0").rstrip(".") if abs(v) < 1000 else f"{v:,.0f}"
    return str(v)


# ---------------------------------------------------------------------------
# Analizler
# ---------------------------------------------------------------------------


def exit_distribution(trades: pd.DataFrame) -> List[dict]:
    if trades.empty:
        return []
    kinds = trades.apply(lambda t: classify_exit(t.to_dict()), axis=1)
    out = []
    for kind, grp in trades.groupby(kinds):
        out.append({
            "çıkış tipi": kind, "adet": len(grp),
            "oran_%": round(len(grp) / len(trades) * 100, 1),
            "ort_getiri_%": round(float(grp["ret"].mean()) * 100, 2),
            "ort_R": round(float(grp["r"].mean()), 2),
        })
    return sorted(out, key=lambda r: -r["adet"])


def slot_breakdown(trades: pd.DataFrame) -> List[dict]:
    if trades.empty or "slot" not in trades:
        return []
    out = []
    for slot, grp in trades.groupby("slot"):
        out.append({
            "sorgu": slot, "işlem": len(grp),
            "kazanma_%": round(float((grp["pnl"] > 0).mean()) * 100, 1),
            "ort_R": round(float(grp["r"].mean()), 2),
            "ort_getiri_%": round(float(grp["ret"].mean()) * 100, 2),
            "toplam_pnl": round(float(grp["pnl"].sum())),
        })
    return sorted(out, key=lambda r: r["sorgu"])


def score_buckets(trades: pd.DataFrame) -> List[dict]:
    if trades.empty or "signal_score" not in trades:
        return []
    def bucket(s):
        if s < 75: return "70-74"
        if s < 80: return "75-79"
        return "80+"
    trades = trades.copy()
    trades["dilim"] = trades["signal_score"].map(bucket)
    trades["tp1"] = trades["levels"].map(lambda l: any(x in ("tp1", "tp2", "tp3") for x in (l or [])))
    out = []
    for name, grp in trades.groupby("dilim"):
        out.append({
            "skor dilimi": name, "işlem": len(grp),
            "TP1'e ulaşma_%": round(float(grp["tp1"].mean()) * 100, 1),
            "ort_R": round(float(grp["r"].mean()), 2),
            "ort_getiri_%": round(float(grp["ret"].mean()) * 100, 2),
        })
    return sorted(out, key=lambda r: r["skor dilimi"])


def noise_test(trades: pd.DataFrame, daily: Dict[str, pd.DataFrame], cfg: StrategyConfig) -> List[dict]:
    """Girişten sonraki 10 seansta gün içi en düşük fiyatın −stop_pct'e değme oranı."""
    if trades.empty:
        return []
    rows = []
    for _, t in trades.iterrows():
        frame = daily.get(t["symbol"])
        if frame is None:
            continue
        after = frame.index[frame.index > t["entry_date"]][: cfg.tp1_horizon_sessions]
        if len(after) == 0:
            continue
        lowest = float(frame.loc[after, "low"].min())
        rows.append({
            "atr_pct": float(t.get("atr_pct", np.nan)),
            "degdi": lowest <= t["entry"] * (1 - cfg.stop_pct),
            "min_%": (lowest / t["entry"] - 1) * 100,
        })
    if not rows:
        return []
    df = pd.DataFrame(rows).dropna(subset=["atr_pct"])
    edges = [0, 0.02, 0.03, 0.04, 1.0]
    labels = ["<%2", "%2-3", "%3-4", ">%4"]
    df["dilim"] = pd.cut(df["atr_pct"], bins=edges, labels=labels, right=False)
    out = []
    for name, grp in df.groupby("dilim", observed=True):
        out.append({
            "ATR/fiyat": str(name), "işlem": len(grp),
            f"−%{cfg.stop_pct*100:.0f} dokunma_%": round(float(grp["degdi"].mean()) * 100, 1),
            "ort_min_%": round(float(grp["min_%"].mean()), 2),
        })
    return out


def breakeven(trades: pd.DataFrame) -> dict:
    """E1 kurgusunda zarar etmemek için gereken TP1 oranı ile gerçekleşen oran."""
    if trades.empty:
        return {}
    t = trades.copy()
    t["tp1"] = t["levels"].map(lambda l: any(x in ("tp1", "tp2", "tp3") for x in (l or [])))
    win, loss = t[t["tp1"]], t[~t["tp1"]]
    if win.empty or loss.empty:
        return {"gerceklesen_tp1_%": round(float(t["tp1"].mean()) * 100, 1)}
    aw, al = float(win["ret"].mean()), float(loss["ret"].mean())
    needed = -al / (aw - al) if aw != al else float("nan")
    return {
        "TP1'e ulasan ort getiri_%": round(aw * 100, 2),
        "TP1 oncesi stop ort getiri_%": round(al * 100, 2),
        "gereken TP1 orani_%": round(needed * 100, 1),
        "gerceklesen TP1 orani_%": round(float(t["tp1"].mean()) * 100, 1),
        "marj_puan": round((float(t["tp1"].mean()) - needed) * 100, 1),
    }


def cancellation_stats(res) -> dict:
    cands = res.candidates
    if cands.empty:
        return {}
    intra = cands[cands["slot"] != "S5"]
    if intra.empty:
        return {}
    cancelled = intra[intra["cancelled_at_s5"]]
    return {
        "gun ici aday": len(intra),
        "S5'te iptal": len(cancelled),
        "iptal orani_%": round(len(cancelled) / len(intra) * 100, 1),
        "iptal edilenlerden dolan": int(cancelled["filled"].sum()) if len(cancelled) else 0,
    }


# ---------------------------------------------------------------------------
# Koşum
# ---------------------------------------------------------------------------


def run_universe(name, symbols, daily_all, hourly_all, xu_d, xu_h, sectors, start, end, base_cfg):
    """Bir evren için tüm mod/maliyet/çıkış/komşuluk varyantlarını koşar."""
    daily = {s: daily_all[s] for s in symbols if s in daily_all}
    hourly = {s: hourly_all[s] for s in symbols if s in hourly_all}
    syms = sorted(set(daily) & set(hourly))
    log.info("[%s] %d sembol: %s", name, len(syms), ", ".join(syms))

    scores = daily_scores({s: daily[s] for s in syms}, xu_d, base_cfg)
    t0 = time.time()
    signals = build_signals(daily, hourly, xu_d, xu_h, base_cfg, start, end,
                            IntradayConfig(), scores, progress=120)
    log.info("[%s] sinyal tablosu: %d satır (%.0fs)", name, len(signals), time.time() - t0)

    def sim(cfg, run):
        return simulate(signals, daily, hourly, cfg, sectors, start, end, run, scores)

    out = {"name": name, "symbols": syms, "signals": signals, "daily": daily,
           "hourly": hourly, "scores": scores, "runs": {}}

    all_slots = IntradayConfig(slots=("S1", "S2", "S3", "S4", "S5"))
    s5_only = IntradayConfig(slots=("S5",))

    # M1 / M2 ana maliyette
    out["runs"]["M1"] = sim(replace(base_cfg, cost_per_side=BASE_COST), s5_only)
    out["runs"]["M2"] = sim(replace(base_cfg, cost_per_side=BASE_COST), all_slots)

    # M3: eski v4 — mevcut run_backtest, gün içi motor DEĞİL
    panel = build_panel(daily, xu_d, sectors, base_cfg)
    m3_cfg = replace(base_cfg, cost_per_side=BASE_COST, exit_model="atr",
                     scan_weekday=4, exit_weekday=4)
    out["runs"]["M3"] = run_backtest(panel, m3_cfg, start, end)
    out["m3_cfg"] = m3_cfg
    out["panel"] = panel

    # maliyet duyarlılığı (M2)
    for cost in COSTS:
        out["runs"][f"M2_c{cost*100:.2f}"] = sim(replace(base_cfg, cost_per_side=cost), all_slots)

    # çıkış modeli karşılaştırması (M2 üzerinde)
    out["runs"]["E2"] = sim(replace(base_cfg, cost_per_side=BASE_COST, exit_model="atr"), all_slots)
    out["runs"]["E3"] = sim(replace(base_cfg, cost_per_side=BASE_COST,
                                    tp1_sell_frac=1.0, tp2_sell_frac=0.0), all_slots)
    return out


def neighbourhood(ctx, base_cfg, start, end, sectors):
    """Kırılganlık testi: seçim için DEĞİL."""
    rows = []
    run = IntradayConfig(slots=("S1", "S2", "S3", "S4", "S5"))
    def one(label, cfg):
        res = simulate(ctx["signals"], ctx["daily"], ctx["hourly"], cfg, sectors,
                       start, end, run, ctx["scores"])
        st = curve_stats(res.equity, res.invested, res.trades)
        rows.append({"varyant": label, "getiri_%": st.get("getiri_%"),
                     "maks_dusus_%": st.get("maks_dusus_%"), "islem": st.get("islem"),
                     "ort_R": st.get("ort_R"), "kazanma_%": st.get("kazanma_%")})
    for bs in (70.0, 75.0, 80.0):
        one(f"buy_score={bs:.0f}", replace(base_cfg, cost_per_side=BASE_COST, buy_score=bs))
    for sp in (0.03, 0.04, 0.05):
        one(f"stop=%{sp*100:.0f}", replace(base_cfg, cost_per_side=BASE_COST, stop_pct=sp))
    for tp in ((0.05, 0.08, 0.12), (0.06, 0.10, 0.15), (0.08, 0.12, 0.18)):
        one(f"TP={tp[0]*100:.0f}/{tp[1]*100:.0f}/{tp[2]*100:.0f}",
            replace(base_cfg, cost_per_side=BASE_COST,
                    tp1_pct=tp[0], tp2_pct=tp[1], tp3_pct=tp[2]))
    return rows


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2025-05-07")
    ap.add_argument("--end", default="2026-11-06")
    ap.add_argument("--out", default="data/backtest_five_scan_report.md")
    args = ap.parse_args()

    sectors = json.loads((ROOT / "config" / "sectors_v4.json").read_text(encoding="utf-8"))
    symbols = sorted(set(UNIVERSE_A) | set(UNIVERSE_B))
    log.info("veri yükleniyor (%d sembol)", len(symbols))
    daily_all, hourly_all, xu_d, xu_h = load_all(symbols)

    base_cfg = StrategyConfig()
    ctxs = {}
    for name, syms in (("Evren A (izleme listesi)", UNIVERSE_A), ("Evren B (15 likit kontrol)", UNIVERSE_B)):
        ctxs[name] = run_universe(name, syms, daily_all, hourly_all, xu_d, xu_h,
                                  sectors, args.start, args.end, base_cfg)

    out: List[str] = []
    w = out.append
    w("# 5 sorgu sistemi backtest raporu\n")
    w(f"Dönem **{args.start} → {args.end}**. Maliyet taraf başına **%{BASE_COST*100:.2f}** "
      f"(aksi belirtilmedikçe), stoplarda ek **%{base_cfg.stop_slippage*100:.2f}** kayma.\n")
    w("> **AI ve eminlik skoru bu backtest'e dahil DEĞİLDİR.** Geçmişte AI kararını "
      "taklit etmek geleceği bilme riski taşır. Sonuçlar **\"kural AL'ların hepsi "
      "alınsaydı\" üst sınırıdır**; canlı sistem AI ve eminlik eşiğiyle bu adayların "
      "bir ALT KÜMESİNİ alacak.\n")

    for name, ctx in ctxs.items():
        w(f"\n## {name}\n")
        w(f"Semboller ({len(ctx['symbols'])}): `{', '.join(ctx['symbols'])}`\n")
        hv = ctx["hourly"]
        w(f"60m veri: {min(f.index[0] for f in hv.values()).date()} → "
          f"{max(f.index[-1] for f in hv.values()).date()}, "
          f"sinyal tablosu {len(ctx['signals'])} satır\n")

        rows = []
        for key, label in (("M1", "M1 — sadece S5"), ("M2", "M2 — S1-S5 (canlı sistem)"),
                           ("M3", "M3 — v4 orijinal (haftalık, ATR stop)")):
            res = ctx["runs"][key]
            inv = getattr(res, "invested", pd.Series([0.0]))
            st = curve_stats(res.equity, inv, res.trades)
            rows.append({"mod": label, **st})
        bm = benchmark(xu_d, ctx["daily"], ctx["runs"]["M2"].equity.index)
        for b in (bm["xu100"], bm["esit_agirlik"]):
            rows.append({"mod": b["ad"], "getiri_%": b.get("getiri_%"),
                         "yillik_%": b.get("yillik_%"), "maks_dusus_%": b.get("maks_dusus_%")})
        w("\n### Modlar\n")
        w(md_table(rows, ["mod", "getiri_%", "yillik_%", "maks_dusus_%", "islem",
                          "kazanma_%", "ort_R", "medyan_R", "R_GA95", "kar_faktoru",
                          "portfoyde_%", "ort_gun", "en_iyi5_haric_%"]))

        w("\n### Maliyet duyarlılığı (M2)\n")
        w(md_table([{"maliyet": f"%{c*100:.2f}",
                     **curve_stats(ctx['runs'][f'M2_c{c*100:.2f}'].equity,
                                   ctx['runs'][f'M2_c{c*100:.2f}'].invested,
                                   ctx['runs'][f'M2_c{c*100:.2f}'].trades)}
                    for c in COSTS],
                   ["maliyet", "getiri_%", "maks_dusus_%", "islem", "ort_R", "kar_faktoru"]))

        w("\n### Çıkış modeli (M2 üzerinde)\n")
        erows = []
        for key, label in (("M2", "E1 sabit yüzde, kademeli (canlı)"),
                           ("E2", "E2 v4 ATR stop, skorla çıkış"),
                           ("E3", "E3 sabit yüzde, TP1'de tamamı")):
            res = ctx["runs"][key]
            erows.append({"çıkış": label, **curve_stats(res.equity, res.invested, res.trades)})
        w(md_table(erows, ["çıkış", "getiri_%", "maks_dusus_%", "islem", "kazanma_%",
                           "ort_R", "kar_faktoru", "ort_gun"]))

        m2 = ctx["runs"]["M2"]
        w("\n### E1 çıkış dağılımı\n")
        w(md_table(exit_distribution(m2.trades)))
        w("\n### Sorgu saatine göre (S1-S5)\n")
        w(md_table(slot_breakdown(m2.trades)))
        w("\n### Kural skoru dilimine göre\n")
        w(md_table(score_buckets(m2.trades)))
        w("\n### Gürültü testi — girişten sonraki 10 seansta −%4'e dokunma\n")
        w(md_table(noise_test(m2.trades, ctx["daily"], base_cfg)))
        w("\n### Başa baş analizi (E1)\n")
        be = breakeven(m2.trades)
        w(md_table([{"ölçüt": k, "değer": v} for k, v in be.items()], ["ölçüt", "değer"]))
        w("\n### Gün içi sinyal iptali (S5 teyidi)\n")
        cs = cancellation_stats(m2)
        w(md_table([{"ölçüt": k, "değer": v} for k, v in cs.items()], ["ölçüt", "değer"]))
        w("\n### Dönemin ilk ve ikinci yarısı (M2)\n")
        w(md_table(halves(m2, xu_d, ctx["daily"]),
                   ["donem", "getiri_%", "maks_dusus_%", "islem", "ort_R", "XU100_%"]))
        w("\n### Aday akışı ve tahmini AI yükü\n")
        cands = m2.candidates
        if not cands.empty:
            weeks = max((m2.equity.index[-1] - m2.equity.index[0]).days / 7.0, 1e-9)
            allowed = cands[cands["blocked"] == ""]
            w(f"- Toplam aday (limite takılmayan): **{len(allowed)}**, bloke: {len(cands) - len(allowed)}\n")
            w(f"- Haftada ortalama **{len(allowed)/weeks:.1f}** aday → ayda ~**{len(allowed)/weeks*4.33:.0f}** AI incelemesi\n")
            w(f"- İnceleme başına 0,02-0,03 USD varsayımıyla aylık ~**{len(allowed)/weeks*4.33*0.025:.2f} USD** "
              f"(üst sınır 0,05 USD ile ~{len(allowed)/weeks*4.33*0.05:.2f} USD)\n")
            w(f"- `AI_MONTHLY_BUDGET_USD=5` bütçesi bu akış için {'YETERLİ' if len(allowed)/weeks*4.33*0.05 < 5 else 'YETERSİZ'}\n")
            w(f"- Dolum: {m2.stats.get('dolum')} | S5 iptali: {m2.stats.get('iptal')}\n")

    # komşuluk testi: Evren B
    bctx = ctxs["Evren B (15 likit kontrol)"]
    w("\n## Komşuluk testi (Evren B) — kırılganlık için, SEÇİM İÇİN DEĞİL\n")
    w(md_table(neighbourhood(bctx, base_cfg, args.start, args.end, sectors),
               ["varyant", "getiri_%", "maks_dusus_%", "islem", "ort_R", "kazanma_%"]))

    # kabul kriterleri
    w("\n## Kabul kriterleri (M2, %0,20 maliyet)\n")
    krows = []
    for name, ctx in ctxs.items():
        res = ctx["runs"]["M2"]
        st = curve_stats(res.equity, res.invested, res.trades)
        bm = benchmark(xu_d, ctx["daily"], res.equity.index)
        r = res.trades["r"].to_numpy(dtype=float) if len(res.trades) else np.array([])
        lo, hi = bootstrap_ci(r) if len(r) > 1 else (float("nan"), float("nan"))
        krows.append({
            "evren": name,
            "1. getiri > XU100": f"{st.get('getiri_%')} vs {bm['xu100'].get('getiri_%')} → "
                                 f"{'GEÇTİ' if (st.get('getiri_%') or -1e9) > (bm['xu100'].get('getiri_%') or 0) else 'GEÇMEDİ'}",
            "2. düşüş < XU100": f"{st.get('maks_dusus_%')} vs {bm['xu100'].get('maks_dusus_%')} → "
                                f"{'GEÇTİ' if (st.get('maks_dusus_%') or -1e9) > (bm['xu100'].get('maks_dusus_%') or -1e9) else 'GEÇMEDİ'}",
            "3. ort R > 0": f"{st.get('ort_R')} GA95 [{lo:.2f}; {hi:.2f}] → "
                            f"{'GEÇTİ' if (st.get('ort_R') or 0) > 0 else 'GEÇMEDİ'}",
            "4. ≥50 işlem": f"{st.get('islem')} → {'GEÇTİ' if (st.get('islem') or 0) >= 50 else 'YETERSİZ KANIT'}",
        })
    w(md_table(krows, ["evren", "1. getiri > XU100", "2. düşüş < XU100", "3. ort R > 0", "4. ≥50 işlem"]))

    w("\n## Yanılgılar ve sınırlar\n")
    w("- **Seçim yanılgısı:** izleme listesi (Evren A) bugünden bakılarak seçildi.\n")
    w("- **Ölçek:** Evren B kullanıcı talebiyle 92 hisseden **15 likit hisseye** indirildi; "
      "dönem 730 günden 18 aya kısaldı. Sonuçlar tam evren/tam dönem sonuçları değildir.\n")
    w("- **Hayatta kalma yanılgısı:** `sectors_v4.json` güncel listedir; dönem içinde "
      "kotasyondan çıkanlar yok.\n")
    w("- **60m sınırı:** Yahoo 60m verisi ~730 güne kadar; bazı sembollerde `period=\"730d\"` "
      "boş dönüp `\"2y\"`ye düşüldü, başlangıç tarihleri sembole göre farklı.\n")
    w("- **Para piyasası karşılaştırması: karşılaştırılamadı** — güvenilir, indirilebilir "
      "bir fon endeksi kaynağı bulunamadı; sayı uydurulmadı.\n")
    w("- **Veri kalitesi:** Yahoo günün ilk 60m barının hacmini 0 veriyor; hacim kapanmış "
      "günlerden hesaplandığı için skoru etkilemez.\n")
    w("- **Sektör boşluğu:** Evren A'nın 9 hissesinden 7'si `sectors_v4.json`'da yok, "
      "o yüzden `max_per_sector=2` limiti Evren A'da fiilen uygulanmıyor.\n")
    w("- **Belirsizlik:** spesifikasyon §3 \"dolum barında stop/TP kontrol edilmez\" derken "
      "test listesi \"aynı barda dolum + stop → stop\" diyor. Varsayılan açık metne uyar "
      "(`stop_on_fill_bar=False`); karşıt davranış bayrakla açılabilir.\n")

    path = ROOT / args.out
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(out), encoding="utf-8")
    log.info("rapor yazıldı: %s", path)
    print("\n".join(out))


if __name__ == "__main__":
    main()
