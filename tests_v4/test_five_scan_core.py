"""5 sorgu sistemi çekirdeği: ortak değerlendirme, kısmi bar ve sabit yüzde çıkışlar."""

from dataclasses import replace
from datetime import date, datetime, time

import numpy as np
import pandas as pd
import pytest

from strategy_v4.config import StrategyConfig
from strategy_v4.exits import LEVELS, levels, open_state, step, time_exit_due
from strategy_v4.features import compute_features, compute_score
from strategy_v4.intraday import (
    HALF_DAY_CLOSE,
    SESSION_CLOSE,
    bar_end,
    partial_bar_at,
    partial_index_at,
)
from strategy_v4.live import evaluate, evaluate_at
from strategy_v4.sizing import entry_limit, position_plan, stop_price

CFG = StrategyConfig(min_adv_try=1e6)


def daily_frame(n=340, seed=5, drift=0.0009, vol=0.015, end="2026-11-06"):
    rng = np.random.default_rng(seed)
    r = rng.normal(drift, vol, n)
    c = 100 * np.exp(np.cumsum(r))
    o = np.r_[c[0], c[:-1]] * np.exp(rng.normal(0, vol * 0.2, n))
    w = np.abs(rng.normal(0, vol * 0.6, (2, n)))
    h = np.maximum(o, c) * (1 + w[0])
    l = np.minimum(o, c) * (1 - w[1])
    v = rng.lognormal(14, 0.35, n)
    idx = pd.bdate_range(end=pd.Timestamp(end), periods=n)
    return pd.DataFrame({"open": o, "high": h, "low": l, "close": c, "volume": v}, index=idx)


def xu_for(df, seed=11):
    rng = np.random.default_rng(seed)
    return pd.Series(1000 * np.exp(np.cumsum(rng.normal(0.0004, 0.010, len(df)))), index=df.index)


def split(df):
    """Son barı 'bugünün kısmi barı', öncesini kapanmış seri yapar."""
    closed, today = df.iloc[:-1], df.iloc[-1]
    bar = {
        "date": df.index[-1].date(), "open": float(today["open"]), "high": float(today["high"]),
        "low": float(today["low"]), "close": float(today["close"]), "volume": float(today["volume"]),
    }
    return closed, bar


def hourly_day(day="2026-11-06", starts=("09:30", "10:30", "11:30", "12:30", "13:30", "14:30", "15:30", "16:30", "17:30")):
    idx = [pd.Timestamp(f"{day} {t}") for t in starts]
    n = len(idx)
    return pd.DataFrame(
        {
            "open": np.arange(100, 100 + n, dtype=float),
            "high": np.arange(101, 101 + n, dtype=float),
            "low": np.arange(99, 99 + n, dtype=float),
            "close": np.arange(100.5, 100.5 + n, dtype=float),
            "volume": np.r_[0.0, np.full(n - 1, 1e6)],  # Yahoo ilk barın hacmini 0 verir
        },
        index=pd.DatetimeIndex(idx),
    )


# --- 1. Ortak değerlendirme: regresyon ve kısmi bar kuralları -----------------


def test_partial_none_matches_previous_evaluate_exactly():
    df = daily_frame()
    xu = xu_for(df)
    new = evaluate_at("AAA", df, None, xu, None, CFG)
    old = evaluate("AAA", df, xu, CFG)
    assert new is not None and old is not None
    assert (new.score, new.trend_pts, new.momentum_pts, new.volume_pts, new.candle_pts) == (
        old.score, old.trend_pts, old.momentum_pts, old.volume_pts, old.candle_pts
    )
    assert (new.adv_try, new.atr, new.close, new.eligible) == (old.adv_try, old.atr, old.close, old.eligible)
    assert new.provisional is False


def test_partial_none_features_identical_to_untruncated():
    """volume_until=None iken compute_features davranışı birebir aynı kalır."""
    df = daily_frame()
    xu = xu_for(df)
    a = compute_features(df, xu, CFG)
    b = compute_features(df, xu, CFG, volume_until=None)
    pd.testing.assert_frame_equal(a, b)


def test_partial_bar_volume_does_not_touch_volume_features():
    df = daily_frame()
    xu = xu_for(df)
    closed, bar = split(df)
    base = evaluate_at("AAA", closed, bar, xu, None, CFG)
    loud = evaluate_at("AAA", closed, {**bar, "volume": bar["volume"] * 10}, xu, None, CFG)
    quiet = evaluate_at("AAA", closed, {**bar, "volume": bar["volume"] * 0.01}, xu, None, CFG)
    for other in (loud, quiet):
        assert other.adv_try == base.adv_try
        assert other.volume_pts == base.volume_pts
        assert other.eligible == base.eligible
        assert other.score == base.score


def test_partial_bar_volume_columns_come_from_closed_days():
    df = daily_frame()
    xu = xu_for(df)
    closed, bar = split(df)
    data = pd.concat([closed, pd.DataFrame([bar]).set_index(pd.DatetimeIndex([bar["date"]]))[closed.columns]])
    hot = data.copy()
    hot.iloc[-1, hot.columns.get_loc("volume")] *= 50
    f = compute_features(hot, xu, CFG, volume_until=closed.index[-1])
    closed_only = compute_features(closed, xu, CFG)
    for col in ("avg_volume", "adv_try", "udv"):
        assert f[col].iloc[-1] == pytest.approx(closed_only[col].iloc[-1])


def test_partial_bar_price_changes_trend_and_ema():
    df = daily_frame()
    xu = xu_for(df)
    closed, bar = split(df)
    low = evaluate_at("AAA", closed, {**bar, "close": bar["close"] * 0.80}, xu, None, CFG)
    high = evaluate_at("AAA", closed, {**bar, "close": bar["close"] * 1.05}, xu, None, CFG)
    assert low.trend_pts != high.trend_pts or low.score != high.score
    assert low.close < high.close


def test_candle_points_never_come_from_partial_bar():
    df = daily_frame()
    xu = xu_for(df)
    closed, bar = split(df)
    closed_eval = evaluate_at("AAA", closed, None, xu, None, CFG)
    last = float(closed["close"].iloc[-1])
    hammer = {"date": bar["date"], "open": last, "high": last * 1.04,
              "low": last * 0.88, "close": last * 1.03, "volume": 5e6}
    ev = evaluate_at("AAA", closed, hammer, xu, None, CFG)
    assert ev.provisional and ev.candle_pts == closed_eval.candle_pts
    assert ev.patterns == closed_eval.patterns


def test_partial_index_only_enters_return_calculation():
    df = daily_frame()
    xu = xu_for(df)
    closed, bar = split(df)
    a = evaluate_at("AAA", closed, bar, xu.iloc[:-1], float(xu.iloc[-1]), CFG)
    b = evaluate_at("AAA", closed, bar, xu.iloc[:-1], float(xu.iloc[-1]) * 1.03, CFG)
    assert a is not None and b is not None
    assert a.rs63 != b.rs63  # endeks getirisi göreli gücü değiştirir
    assert a.adv_try == b.adv_try  # hacme dokunmaz


def test_no_future_data_leak():
    df = daily_frame()
    xu = xu_for(df)
    cut = df.iloc[:-5]
    before = evaluate_at("AAA", cut, None, xu, None, CFG)
    tampered = df.copy()
    tampered.iloc[-5:, :] *= 1.5
    after = evaluate_at("AAA", tampered.iloc[:-5], None, xu, None, CFG)
    assert before.score == after.score


def test_scan_slot_and_as_of_time_are_carried():
    df = daily_frame()
    xu = xu_for(df)
    closed, bar = split(df)
    moment = datetime(2026, 11, 6, 13, 50)
    ev = evaluate_at("AAA", closed, bar, xu, None, CFG, scan_slot="S2", as_of_time=moment)
    assert ev.scan_slot == "S2" and ev.as_of_time == moment
    closed_eval = evaluate_at("AAA", df, None, xu, None, CFG, scan_slot="S5")
    assert closed_eval.scan_slot == "S5" and closed_eval.as_of_time is None


# --- 2. Kısmi bar oluşturucu ---------------------------------------------------


def test_bar_end_is_start_plus_60_clamped_to_session_close():
    assert bar_end("2026-11-06 09:30") == pd.Timestamp("2026-11-06 10:30")
    assert bar_end("2026-11-06 16:30") == pd.Timestamp("2026-11-06 17:30")
    # Son bar seans kapanışında biter, 18:30'da değil
    assert bar_end("2026-11-06 17:30") == pd.Timestamp("2026-11-06 18:10")
    assert bar_end("2026-11-06 12:30", HALF_DAY_CLOSE) == pd.Timestamp("2026-11-06 12:40")


@pytest.mark.parametrize(
    "scan,expected_bars,last_start",
    [
        ("10:44", 0, None),           # 09:30 barı 10:30'da bitti, 10:45'ten önce kullanılamaz
        ("10:45", 1, "09:30"),
        ("11:20", 1, "09:30"),        # spesifikasyondaki saat: tek bar
        ("11:50", 2, "10:30"),
        ("13:20", 3, "11:30"),
        ("13:50", 4, "12:30"),
        ("15:20", 5, "13:30"),
        ("15:50", 6, "14:30"),
        ("17:20", 7, "15:30"),
        ("17:50", 8, "16:30"),
        ("18:25", 9, "17:30"),        # 17:30 barı 18:10'da bitti
    ],
)
def test_partial_bar_at_respects_15_minute_delay(scan, expected_bars, last_start):
    hourly = hourly_day()
    bar = partial_bar_at(hourly, pd.Timestamp(f"2026-11-06 {scan}"))
    if expected_bars == 0:
        assert bar is None
        return
    assert bar["bars"] == expected_bars
    assert bar["last_bar_start"] == pd.Timestamp(f"2026-11-06 {last_start}")
    assert bar["date"] == date(2026, 11, 6)


def test_partial_bar_aggregates_ohlcv_from_usable_bars_only():
    hourly = hourly_day()
    bar = partial_bar_at(hourly, pd.Timestamp("2026-11-06 13:50"))  # 4 bar: 09:30..12:30
    used = hourly.iloc[:4]
    assert bar["open"] == float(used["open"].iloc[0])
    assert bar["high"] == float(used["high"].max())
    assert bar["low"] == float(used["low"].min())
    assert bar["close"] == float(used["close"].iloc[-1])
    assert bar["volume"] == float(used["volume"].sum())


def test_partial_bar_delay_parameter_is_honoured():
    hourly = hourly_day()
    at_1120 = pd.Timestamp("2026-11-06 11:20")
    assert partial_bar_at(hourly, at_1120, delay_minutes=15)["bars"] == 1
    assert partial_bar_at(hourly, at_1120, delay_minutes=0)["bars"] == 1
    assert partial_bar_at(hourly, pd.Timestamp("2026-11-06 11:30"), delay_minutes=0)["bars"] == 2
    assert partial_bar_at(hourly, at_1120, delay_minutes=120) is None


def test_partial_bar_none_on_weekend_holiday_and_before_first_bar():
    hourly = hourly_day()
    assert partial_bar_at(hourly, pd.Timestamp("2026-11-07 13:50")) is None  # cumartesi
    assert partial_bar_at(hourly, pd.Timestamp("2026-10-29 13:50")) is None  # tatil
    assert partial_bar_at(hourly, pd.Timestamp("2026-11-06 10:00")) is None  # ilk bar bitmedi
    assert partial_bar_at(pd.DataFrame(), pd.Timestamp("2026-11-06 13:50")) is None


def test_partial_bar_half_day_skips_bars_after_close():
    hourly = hourly_day(starts=("09:30", "10:30", "11:30", "12:30"))
    bar = partial_bar_at(hourly, pd.Timestamp("2026-11-06 13:25"), session_close=HALF_DAY_CLOSE)
    assert bar["bars"] == 4 and bar["last_bar_end"] == pd.Timestamp("2026-11-06 12:40")


def test_partial_bar_accepts_tz_aware_input():
    hourly = hourly_day()
    aware = hourly.copy()
    aware.index = pd.DatetimeIndex(hourly.index).tz_localize("Europe/Istanbul")
    naive = partial_bar_at(hourly, pd.Timestamp("2026-11-06 13:50"))
    tz = partial_bar_at(aware, pd.Timestamp("2026-11-06 13:50", tz="Europe/Istanbul"))
    assert naive == tz


def test_partial_index_at_returns_close_only():
    hourly = hourly_day()
    moment = pd.Timestamp("2026-11-06 13:50")
    assert partial_index_at(hourly, moment) == partial_bar_at(hourly, moment)["close"]
    assert partial_index_at(hourly, pd.Timestamp("2026-11-07 13:50")) is None


# --- 3. Sabit yüzde çıkış modeli ----------------------------------------------


def test_levels_from_entry_100():
    lv = levels(100.0, CFG)
    assert (lv["stop"], lv["tp1"], lv["tp2"], lv["tp3"]) == (96.0, 106.0, 110.0, 115.0)


def test_tp_ladder_sells_thirds_and_moves_stop():
    s = open_state(100.0, 30, CFG)
    s, ev = step(s, 100.0, 107.0, 99.0, CFG)
    assert [(e.kind, e.qty, e.price) for e in ev] == [("tp1", 10, 106.0)]
    assert s.remaining == 20 and s.stop == 100.0  # stop girişe (başa baş)

    s, ev = step(s, 106.5, 111.0, 106.0, CFG)
    assert [(e.kind, e.qty, e.price) for e in ev] == [("tp2", 10, 110.0)]
    assert s.remaining == 10 and s.stop == 106.0  # stop TP1'e

    s, ev = step(s, 110.5, 116.0, 110.0, CFG)
    assert [(e.kind, e.qty, e.price) for e in ev] == [("tp3", 10, 115.0)]
    assert s.remaining == 0 and s.closed_reason == "tp3"


@pytest.mark.parametrize(
    "qty,expected",
    [(30, [10, 10, 10]), (10, [3, 3, 4]), (11, [3, 3, 5]), (1, [0, 0, 1]), (2, [0, 0, 2])],
)
def test_partial_sell_quantities_sum_to_initial(qty, expected):
    """Her bar o anki (taşınmış) stopun üstünde kalır; sadece TP'ler tetiklenir."""
    s = open_state(100.0, qty, CFG)
    bars = [(100.0, 107.0, 99.0), (106.0, 111.0, 105.0), (110.0, 116.0, 109.0)]
    sold, kinds = [], []
    for bar_open, bar_high, bar_low in bars:
        s, ev = step(s, bar_open, bar_high, bar_low, CFG)
        sold.append(sum(e.qty for e in ev))
        kinds += [e.kind for e in ev]
    assert kinds == ["tp1", "tp2", "tp3"]
    assert sold == expected
    assert sum(sold) == qty and s.remaining == 0


def test_all_three_levels_in_one_bar():
    s = open_state(100.0, 30, CFG)
    s, ev = step(s, 100.0, 120.0, 99.5, CFG)
    assert [e.kind for e in ev] == ["tp1", "tp2", "tp3"]
    assert [e.price for e in ev] == [106.0, 110.0, 115.0]
    assert s.remaining == 0


def test_gap_down_exits_at_open_not_at_stop():
    s = open_state(100.0, 30, CFG)
    s, ev = step(s, 93.0, 95.0, 92.0, CFG)
    assert len(ev) == 1 and ev[0].kind == "stop_gap"
    assert ev[0].price == 93.0 and ev[0].qty == 30
    assert s.remaining == 0


def test_gap_up_fills_tp_at_open():
    s = open_state(100.0, 30, CFG)
    s, ev = step(s, 108.0, 109.0, 107.0, CFG)
    assert ev[0].kind == "tp1" and ev[0].price == 108.0
    assert s.stop == 100.0


def test_stop_wins_when_both_hit_in_same_bar():
    s = open_state(100.0, 30, CFG)
    s, ev = step(s, 100.0, 107.0, 95.0, CFG)  # Low <= 96 ve High >= 106
    assert len(ev) == 1 and ev[0].kind == "stop"
    assert ev[0].price == 96.0 and s.remaining == 0


def test_moved_stop_is_not_checked_again_in_same_bar():
    s = open_state(100.0, 30, CFG)
    # TP1 doluyor, stop 100'e çıkıyor; aynı barın 99'luk düşüşü yeni stopu tetiklemez
    s, ev = step(s, 100.0, 107.0, 99.0, CFG)
    assert [e.kind for e in ev] == ["tp1"] and s.remaining == 20
    # Yeni stop sonraki bardan geçerli
    s, ev = step(s, 101.0, 102.0, 99.0, CFG)
    assert [e.kind for e in ev] == ["stop"] and ev[0].price == 100.0


def test_stop_never_moves_down():
    s = open_state(100.0, 30, CFG)
    s, _ = step(s, 100.0, 107.0, 99.0, CFG)
    assert s.stop == 100.0
    s, _ = step(s, 101.0, 105.0, 100.5, CFG)  # olay yok
    assert s.stop == 100.0
    s, _ = step(s, 106.0, 111.0, 105.0, CFG)  # TP2 → stop TP1'e (106)
    assert s.stop == 106.0


def test_closed_position_is_inert():
    s = open_state(100.0, 10, CFG)
    s, _ = step(s, 93.0, 95.0, 92.0, CFG)
    s2, ev = step(s, 50.0, 200.0, 10.0, CFG)
    assert ev == [] and s2.remaining == 0


def test_time_exit_due_rules():
    s = open_state(100.0, 30, CFG)
    assert time_exit_due(s, 9, CFG) is None
    assert time_exit_due(s, 10, CFG) == "time"  # 10 seansta TP1 yok
    hit, _ = step(s, 100.0, 107.0, 99.0, CFG)
    assert time_exit_due(hit, 10, CFG) is None  # TP1 geldi, zaman stopu yok
    assert time_exit_due(hit, 30, CFG) == "max_hold"


def test_atr_exit_model_keeps_old_v4_prices():
    atr_cfg = replace(CFG, exit_model="atr")
    assert stop_price(100.0, 2.0, atr_cfg) == 100.0 - 4.0 * 2.0
    assert entry_limit(100.0, 2.0, atr_cfg) == 100.0 + 0.5 * 2.0
    assert stop_price(100.0, 2.0, CFG) == 96.0
    assert entry_limit(100.0, 2.0, CFG) == pytest.approx(101.0)


# --- 4. Boyutlandırma ---------------------------------------------------------


def test_position_plan_reports_real_risk():
    plan = position_plan(100_000.0, 100.0, 96.0, adv_try=1e12, cash=1e12, cfg=CFG)
    assert plan["qty"] == 200  # min(1000/4 = 250, 20.000/100 = 200)
    assert plan["risk_try"] == pytest.approx(800.0)
    assert plan["risk_pct"] == pytest.approx(0.008)
    assert plan["binding"] == "pozisyon"


def test_position_plan_risk_binds_with_a_wide_stop():
    plan = position_plan(100_000.0, 100.0, 80.0, adv_try=1e12, cash=1e12, cfg=CFG)
    assert plan["qty"] == 50  # 1.000 TL risk / 20 TL
    assert plan["risk_pct"] == pytest.approx(0.01)
    assert plan["binding"] == "risk"
