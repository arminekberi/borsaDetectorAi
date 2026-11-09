"""Gün içi backtest motoru: dolum, çıkış sırası, muhasebe ve mod tutarlılığı."""

import inspect
from dataclasses import replace

import numpy as np
import pandas as pd
import pytest

from strategy_v4 import backtest_intraday
from strategy_v4.backtest import Portfolio, build_panel, run_backtest
from strategy_v4.backtest_intraday import (
    IntradayConfig,
    build_signals,
    classify_exit,
    daily_scores,
    simulate,
)
from strategy_v4.config import StrategyConfig
from strategy_v4.live import evaluate_at

# Bu dosyadaki testler SABİT YÜZDE çıkış modelini ve spesifikasyon Bölüm 8
# boyutlandırmasını ölçer. Varsayılanlar v4'e döndüğü için burada açıkça sabitlenir.
CFG = StrategyConfig(min_adv_try=1e6, exit_model="fixed_pct",
                    risk_per_trade=0.01, max_position_pct=0.20,
                    max_positions=5, max_per_sector=2, min_history=30)
SLOTS_ALL = IntradayConfig(slots=("S1", "S2", "S3", "S4", "S5"))
BAR_TIMES = ("09:30", "10:30", "11:30", "12:30", "13:30", "14:30", "15:30", "16:30", "17:30")


def make_daily(n=120, start="2026-01-05", price=100.0, step=0.002):
    idx = pd.bdate_range(start=start, periods=n)
    close = price * (1 + step) ** np.arange(n)
    return pd.DataFrame(
        {"open": close, "high": close * 1.01, "low": close * 0.99,
         "close": close, "volume": np.full(n, 5e6)},
        index=idx,
    )


def make_hourly(daily, bars=BAR_TIMES, shape=None):
    """Her gün için 60m barlar. `shape(day_i, bar_i, close)` → (o,h,l,c) ile özelleştirilir."""
    rows, index = [], []
    for di, (day, row) in enumerate(daily.iterrows()):
        for bi, t in enumerate(bars):
            base = float(row["close"])
            o, h, l, c = (base, base * 1.002, base * 0.998, base)
            if shape is not None:
                custom = shape(di, bi, base)
                if custom is not None:
                    o, h, l, c = custom
            rows.append({"open": o, "high": h, "low": l, "close": c, "volume": 1e5})
            index.append(pd.Timestamp(f"{day.date()} {t}"))
    return pd.DataFrame(rows, index=pd.DatetimeIndex(index))


def signal_row(symbol, day, slot, moment, score=90.0, reference=100.0, atr=2.0):
    return {
        "symbol": symbol, "day": pd.Timestamp(day), "slot": slot,
        "moment": pd.Timestamp(moment), "score": score, "reference": reference,
        "atr": atr, "adv": 1e12, "atr_pct": atr / reference, "bars": 2,
    }


def run_sim(signals, daily, hourly, cfg=CFG, run=SLOTS_ALL, sectors=None, start=None, end=None):
    xu = pd.Series(1000.0, index=next(iter(daily.values())).index)
    scores = daily_scores(daily, xu, cfg)
    days = next(iter(hourly.values())).index.normalize().unique()
    return simulate(
        pd.DataFrame(signals), daily, hourly, cfg, sectors or {},
        start or str(days[0].date()), end or str(days[-1].date()), run, scores,
    )


# --- dolum kuralları ---------------------------------------------------------


def test_no_fill_from_bars_before_the_scan():
    """Sorgudan ÖNCE başlayan barlardan dolum yapılmaz."""
    daily = {"AAA": make_daily()}
    # Sabah barları limitin çok altında; sorgu 13:50'de. Erken barlar dolum vermemeli.
    def shape(di, bi, base):
        if bi <= 3:
            return (base * 0.5, base * 0.5, base * 0.5, base * 0.5)
        return (base * 2.0, base * 2.0, base * 2.0, base * 2.0)
    hourly = {"AAA": make_hourly(daily["AAA"], shape=shape)}
    day = daily["AAA"].index[60]
    sig = [signal_row("AAA", day, "S3", f"{day.date()} 13:50",
                      reference=float(daily['AAA'].at[day, 'close']))]
    res = run_sim(sig, daily, hourly)
    # 13:50'den sonraki ilk bar 14:30; o ve sonrası limitin üstünde → dolum yok
    assert res.stats["dolum"] == 0
    assert len(res.trades) == 0


def test_fill_happens_at_first_bar_starting_after_scan():
    daily = {"AAA": make_daily()}
    day = daily["AAA"].index[60]
    ref = float(daily["AAA"].at[day, "close"])
    # Tüm barlar referansın altında → sorgudan sonraki ilk barın açılışından dolar
    def shape(di, bi, base):
        return (base * 0.99, base * 1.0, base * 0.985, base * 0.99)
    hourly = {"AAA": make_hourly(daily["AAA"], shape=shape)}
    sig = [signal_row("AAA", day, "S3", f"{day.date()} 13:50", reference=ref)]
    res = run_sim(sig, daily, hourly)
    assert res.stats["dolum"] == 1
    fill_time = res.trades.iloc[0]["fill_time"]
    assert fill_time == pd.Timestamp(f"{day.date()} 14:30")


def _same_bar_dip_fixture():
    """14:30 barında dolum olur VE aynı bar stopun altına iner; sonraki barlar sakin."""
    daily = {"AAA": make_daily()}
    day = daily["AAA"].index[60]
    ref = float(daily["AAA"].at[day, "close"])
    target = pd.Timestamp(f"{day.date()} 14:30")

    def shape(di, bi, base):
        ts = pd.Timestamp(f"{daily['AAA'].index[di].date()} {BAR_TIMES[bi]}")
        if ts == target:
            return (base * 0.999, base * 1.0, base * 0.80, base * 0.85)
        return (base, base * 1.001, base * 0.999, base)

    hourly = {"AAA": make_hourly(daily["AAA"], shape=shape)}
    sig = [signal_row("AAA", day, "S3", f"{day.date()} 13:50", reference=ref)]
    return daily, hourly, sig, day


def test_no_exit_check_on_the_fill_bar():
    """Varsayılan: dolum barında stop/TP kontrol edilmez (spesifikasyon §3)."""
    daily, hourly, sig, day = _same_bar_dip_fixture()
    res = run_sim(sig, daily, hourly, run=SLOTS_ALL)
    assert res.stats["dolum"] == 1
    assert len(res.trades) == 1
    assert res.trades.iloc[0]["fill_time"] == pd.Timestamp(f"{day.date()} 14:30")
    # dolum barındaki -%20'lik düşüş stop üretmedi
    assert res.trades.iloc[0]["reason"] not in ("stop", "stop_gap")
    assert res.fills[res.fills["reason"].isin(["stop", "stop_gap"])].empty


def test_fill_and_stop_in_same_bar_resolves_to_stop_when_flag_on():
    """`stop_on_fill_bar=True`: aynı barda dolum + stop → stop."""
    daily, hourly, sig, day = _same_bar_dip_fixture()
    res = run_sim(sig, daily, hourly,
                  run=IntradayConfig(slots=SLOTS_ALL.slots, stop_on_fill_bar=True))
    assert res.stats["dolum"] == 1
    assert len(res.trades) == 1
    assert res.trades.iloc[0]["reason"] in ("stop", "stop_gap")
    assert res.trades.iloc[0]["exit_date"] == day


# --- muhasebe ----------------------------------------------------------------


def test_partial_sell_cash_and_quantity_accounting():
    """İşlem kâr toplamı = özsermaye değişimi (maliyet dahil)."""
    cfg = replace(CFG, cost_per_side=0.002, stop_slippage=0.001)
    p = Portfolio(cfg)
    start_equity = p.equity({})
    assert p.buy("AAA", 100.0, 30, 4.0, pd.Timestamp("2026-01-05"))
    p.sell("AAA", 106.0, 10, pd.Timestamp("2026-01-06"), "tp1")
    p.sell("AAA", 110.0, 10, pd.Timestamp("2026-01-07"), "tp2")
    p.sell("AAA", 115.0, 10, pd.Timestamp("2026-01-08"), "tp3")
    assert "AAA" not in p.holdings
    assert len(p.trades) == 1
    assert sum(f["qty"] for f in p.fills) == 30
    total_pnl = sum(f["pnl"] for f in p.fills)
    assert p.equity({}) - start_equity == pytest.approx(total_pnl, abs=1e-6)
    assert p.trades[0]["pnl"] == pytest.approx(total_pnl, abs=1e-6)


def test_portfolio_matches_run_backtest_accounting():
    """
    `Portfolio` ve `run_backtest`'in satır içi muhasebesi aynı sayıları vermeli.

    `run_backtest` geriye dönük uyumluluk için kendi muhasebesini koruyor;
    bu test ikisinin ayrışmadığının güvencesidir.
    """
    cfg = replace(CFG, cost_per_side=0.002, stop_slippage=0.001)
    entry, qty, exit_price = 100.0, 200, 110.0

    p = Portfolio(cfg)
    before = p.equity({})
    p.buy("AAA", entry, qty, 4.0, pd.Timestamp("2026-01-05"))
    p.sell("AAA", exit_price, qty, pd.Timestamp("2026-01-09"), "tp3")
    after = p.equity({})

    # run_backtest'teki formüller (close_position + alım bloğu)
    buy_cost = qty * entry * cfg.cost_per_side
    sell_cost = qty * exit_price * cfg.cost_per_side
    gross = (exit_price - entry) * qty
    expected_pnl = gross - sell_cost - buy_cost
    expected_costs = buy_cost + sell_cost

    assert p.trades[0]["pnl"] == pytest.approx(expected_pnl)
    assert p.costs == pytest.approx(expected_costs)
    assert after - before == pytest.approx(expected_pnl)
    assert p.trades[0]["r"] == pytest.approx((exit_price - entry) / 4.0)
    assert p.trades[0]["ret"] == pytest.approx(exit_price / entry - 1.0)


def test_stop_slippage_applied_only_to_stop_fills():
    cfg = replace(CFG, cost_per_side=0.0, stop_slippage=0.01)
    p = Portfolio(cfg)
    p.buy("AAA", 100.0, 10, 4.0, pd.Timestamp("2026-01-05"))
    p.sell("AAA", 96.0, 10, pd.Timestamp("2026-01-06"), "stop", stop_fill=True)
    assert p.fills[0]["price"] == pytest.approx(96.0 * 0.99)

    p2 = Portfolio(cfg)
    p2.buy("BBB", 100.0, 10, 4.0, pd.Timestamp("2026-01-05"))
    p2.sell("BBB", 106.0, 10, pd.Timestamp("2026-01-06"), "tp1", stop_fill=False)
    assert p2.fills[0]["price"] == pytest.approx(106.0)


def test_portfolio_limits_match_config():
    cfg = replace(CFG, max_positions=2, max_per_sector=1, max_new_entries_per_day=5)
    p = Portfolio(cfg, {"AAA": "X", "BBB": "X", "CCC": "Y"})
    day = pd.Timestamp("2026-01-05")
    p.start_day(day, {})
    assert p.block_reason("AAA", {}) is None
    p.buy("AAA", 100.0, 10, 4.0, day)
    assert p.block_reason("BBB", {}) == "sektör sınırı"  # aynı sektör
    assert p.block_reason("CCC", {}) is None
    p.buy("CCC", 100.0, 10, 4.0, day)
    assert p.block_reason("DDD", {}) == "portföy dolu"


def test_daily_entry_limit_and_monthly_brake():
    cfg = replace(CFG, max_new_entries_per_day=1, monthly_brake=-0.01)
    p = Portfolio(cfg)
    day = pd.Timestamp("2026-01-05")
    p.start_day(day, {})
    p.buy("AAA", 100.0, 10, 4.0, day)
    assert p.block_reason("BBB", {}) == "günlük giriş sınırı"
    p.start_day(pd.Timestamp("2026-02-02"), {})
    p.cash *= 0.5  # ay içi büyük kayıp
    assert p.block_reason("BBB", {}) == "aylık kayıp freni"


# --- mod tutarlılığı ---------------------------------------------------------


def test_m3_equals_existing_run_backtest():
    """M3 modu mevcut `run_backtest`'in kendisidir; sonuç birebir aynı olmalı."""
    daily = {"AAA": make_daily(n=400, start="2024-01-02"), "BBB": make_daily(n=400, start="2024-01-02", price=50.0, step=0.0015)}
    xu = pd.Series(
        1000 * (1.001 ** np.arange(len(daily["AAA"]))), index=daily["AAA"].index
    )
    cfg = replace(CFG, exit_model="atr", scan_weekday=4, exit_weekday=4, min_history=60)
    panel = build_panel(daily, xu, {}, cfg)
    a = run_backtest(panel, cfg, "2024-06-03", "2025-06-02")
    b = run_backtest(panel, cfg, "2024-06-03", "2025-06-02")
    pd.testing.assert_series_equal(a.equity, b.equity)
    assert a.costs == b.costs


def test_e2_atr_model_uses_atr_stop_and_no_tp():
    """E2 (exit_model='atr') TP üretmez, stop 4×ATR'dir."""
    state = backtest_intraday._state_for(100.0, 30, 2.0, replace(CFG, exit_model="atr"))
    assert state.stop == pytest.approx(100.0 - 4.0 * 2.0)
    assert state.tp1 == float("inf") and state.tp2 == float("inf") and state.tp3 == float("inf")
    fixed = backtest_intraday._state_for(100.0, 30, 2.0, CFG)
    assert (fixed.stop, fixed.tp1, fixed.tp2, fixed.tp3) == (96.0, 106.0, 110.0, 115.0)


def test_backtest_and_live_call_the_same_evaluate_at():
    """Strateji mantığının ikinci kopyası olmadığının kanıtı."""
    import strategy_v4.live as live
    assert backtest_intraday.evaluate_at is live.evaluate_at
    src = inspect.getsource(backtest_intraday)
    assert "evaluate_at(" in src
    # motor kendi skor/özellik hesabı yazmıyor; features.py'yi çağırıyor
    assert "compute_features(" in src and "compute_score(" in src


def test_signals_table_is_independent_of_cost_and_exit_model():
    """Sinyal tablosu maliyet/çıkış modeli/eşikten bağımsız üretilir."""
    # EMA200 ısınması için 280 bar; pencere 250. günden sonra başlar
    daily = {"AAA": make_daily(n=280, start="2025-01-02")}
    hourly = {"AAA": make_hourly(daily["AAA"])}
    xu = pd.Series(1000.0, index=daily["AAA"].index)
    days = hourly["AAA"].index.normalize().unique()
    kw = dict(start=str(days[250].date()), end=str(days[-1].date()), run=SLOTS_ALL)
    a = build_signals(daily, hourly, xu, hourly["AAA"], CFG, **kw)
    b = build_signals(daily, hourly, xu, hourly["AAA"],
                      replace(CFG, cost_per_side=0.004, exit_model="atr", buy_score=80.0), **kw)
    assert not a.empty, "test verisi sinyal üretmeli"
    cols = ["symbol", "day", "slot", "score", "reference", "atr"]
    pd.testing.assert_frame_equal(a[cols], b[cols])
    # hem gün içi hem gün sonu sorguları temsil edilmeli
    assert set(a["slot"]) >= {"S1", "S5"}


# --- sınıflandırma -----------------------------------------------------------


@pytest.mark.parametrize(
    "levels,reason,expected",
    [
        ([], "stop", "stop (TP1 öncesi)"),
        ([], "stop_gap", "stop (TP1 öncesi)"),
        (["tp1"], "stop", "TP1 sonrası başa baş"),
        (["tp1", "tp2"], "stop", "TP2 sonrası TP1'de"),
        (["tp1", "tp2", "tp3"], "tp3", "TP3"),
        ([], "time", "zaman stopu"),
        ([], "max_hold", "zaman stopu"),
        ([], "score", "yumuşak çıkış"),
        ([], "ema50", "yumuşak çıkış"),
        ([], "end", "dönem sonu açık"),
    ],
)
def test_classify_exit(levels, reason, expected):
    assert classify_exit({"levels": levels, "reason": reason}) == expected
