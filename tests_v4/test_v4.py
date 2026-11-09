"""Strateji v4 birim testleri."""

import numpy as np
import pandas as pd
import pytest

from strategy_v4.backtest import Panel, build_panel, metrics, run_backtest
from strategy_v4.candles import candle_points, detect_candles
from strategy_v4.config import BULLISH_CANDLE_POINTS, INFO_CANDLES, StrategyConfig
from strategy_v4.features import clean_corporate_actions, compute_features, compute_score, market_median_move, market_regime, wilder_atr


def make_frame(n=400, seed=1, drift=0.0008, vol=0.02):
    rng = np.random.default_rng(seed)
    r = rng.normal(drift, vol, n)
    c = 100 * np.exp(np.cumsum(r))
    o = np.r_[c[0], c[:-1]] * np.exp(rng.normal(0, vol * 0.2, n))
    w = np.abs(rng.normal(0, vol * 0.6, (2, n)))
    h = np.maximum(o, c) * (1 + w[0])
    l = np.minimum(o, c) * (1 - w[1])
    v = rng.lognormal(14, 0.4, n)
    idx = pd.bdate_range("2022-01-03", periods=n)
    return pd.DataFrame({"open": o, "high": h, "low": l, "close": c, "volume": v}, index=idx)


def xu_for(df, seed=9):
    rng = np.random.default_rng(seed)
    return pd.Series(1000 * np.exp(np.cumsum(rng.normal(0.0005, 0.012, len(df)))), index=df.index)


def single_stock_panel(df, xu, cfg, sector="X"):
    return build_panel({"AAA": df}, xu, {"AAA": sector}, cfg)


def forced_panel(rows, cfg, atr=2.0):
    """Skor ve filtreleri test içinde sabitlemek için elle kurulmuş panel."""
    df = pd.DataFrame(rows, columns=["open", "high", "low", "close"], dtype=float)
    df["volume"] = 1e7
    df.index = pd.bdate_range("2024-01-01", periods=len(df))
    n = len(df)
    arr = lambda col: df[col].to_numpy()[:, None]
    f = df.copy()
    feats = pd.DataFrame(index=df.index)
    return df, Panel(
        dates=df.index, symbols=["AAA"], open=arr("open"), high=arr("high"), low=arr("low"), close=arr("close"), volume=arr("volume"),
        atr=np.full((n, 1), atr), ema50=np.full((n, 1), 1.0), rel=np.zeros((n, 1)), adv=np.full((n, 1), 1e12),
        features={"AAA": feats}, sectors=["X"], regime=np.ones(n, dtype=bool),
    )


@pytest.fixture
def force_signal(monkeypatch):
    """İlk barda AL üret, sonra üretme."""
    def apply(n, signal_days=(0,)):
        s = np.zeros((n, 1))
        for d in signal_days:
            s[d, 0] = 100.0
        monkeypatch.setattr("strategy_v4.backtest.score_matrices", lambda panel, cfg: (s, s >= 70))
    return apply


def test_features_do_not_use_future_bars():
    df = make_frame()
    xu = xu_for(df)
    cfg = StrategyConfig()
    full = compute_score(compute_features(df, xu, cfg), cfg)
    cut = 330
    part = compute_score(compute_features(df.iloc[:cut], xu.iloc[:cut], cfg), cfg)
    a = full.iloc[:cut].drop(columns=["eligible"]).astype(float)
    b = part.drop(columns=["eligible"]).astype(float)
    pd.testing.assert_frame_equal(a, b, check_exact=False, atol=1e-9)


def test_changing_future_prices_does_not_change_past_score():
    df = make_frame()
    xu = xu_for(df)
    cfg = StrategyConfig()
    base = compute_score(compute_features(df, xu, cfg), cfg)["score"]
    other = df.copy()
    other.iloc[-20:, :4] *= 3.0
    changed = compute_score(compute_features(other, xu, cfg), cfg)["score"]
    pd.testing.assert_series_equal(base.iloc[:-20], changed.iloc[:-20])


def test_score_bounds_and_weights():
    cfg = StrategyConfig()
    assert cfg.max_positions * cfg.max_position_pct == 1.0
    assert sum(cfg.w_trend) + cfg.w_relative_12_1 + cfg.w_relative_63 + cfg.w_high + cfg.w_volume[0] + cfg.w_candle_cap == 100.0
    df = make_frame(n=600, drift=0.002)
    s = compute_score(compute_features(df, xu_for(df), cfg), cfg)
    assert s["score"].between(0, 100).all()


def test_atr_is_wilder():
    h = pd.Series([11.0] * 30)
    l = pd.Series([9.0] * 30)
    c = pd.Series([10.0] * 30)
    assert wilder_atr(h, l, c, 14).iloc[-1] == pytest.approx(2.0)


def base_candle_frame():
    n = 40
    c = np.linspace(100, 110, n)
    o = c - 0.2
    f = pd.DataFrame({"open": o, "high": c + 0.1, "low": o - 0.1, "close": c}, index=pd.bdate_range("2024-01-01", periods=n), dtype=float)
    f["ema20"] = f["close"] + 5
    f["ema50"] = f["close"] + 6
    f["atr"] = 1.0
    return f


def test_bullish_engulfing_detected_after_decline():
    f = base_candle_frame()
    f.iloc[-5:-1, f.columns.get_loc("close")] = [108, 107, 106, 105]
    f.iloc[-5:-1, f.columns.get_loc("open")] = [108.5, 108, 107, 106]
    f.iloc[-5:-1, f.columns.get_loc("high")] = [109, 108.2, 107.2, 106.2]
    f.iloc[-5:-1, f.columns.get_loc("low")] = [107.8, 106.8, 105.8, 104.8]
    f.iloc[-1, :4] = [104.9, 108, 104.5, 107.5]
    assert detect_candles(f)["bullish_engulfing"].iloc[-1]


def test_hammer_detected_with_support_context():
    f = base_candle_frame()
    f["ema20"] = f["close"]
    f.iloc[-1, :4] = [109.6, 110.1, 106.8, 110.0]
    out = detect_candles(f)
    assert out["hammer"].iloc[-1]


def test_shooting_star_requires_upper_context():
    f = base_candle_frame()
    f.iloc[-1, :4] = [110.0, 113.5, 109.9, 110.3]
    assert detect_candles(f)["shooting_star"].iloc[-1]


def test_candle_points_use_strongest_pattern_and_volume():
    idx = pd.RangeIndex(3)
    cols = list(BULLISH_CANDLE_POINTS) + list(INFO_CANDLES)
    c = pd.DataFrame(False, index=idx, columns=cols)
    c.loc[0, "morning_star"] = True
    c.loc[1, "bullish_engulfing"] = True
    c.loc[1, "hammer"] = True
    c.loc[2, "evening_star"] = True
    pts = candle_points(c, pd.Series([True, True, True]), 15.0, 0.6)
    assert pts.tolist() == [15.0, 12.0, 0.0]
    low_vol = candle_points(c, pd.Series([False, True, True]), 15.0, 0.6)
    assert low_vol.iloc[0] == pytest.approx(9.0)


def test_entry_is_next_day_never_same_bar(force_signal):
    cfg = StrategyConfig(scan_weekday=None, exit_score=None)
    rows = [[100, 101, 99, 100], [100.5, 102, 100, 101], [101, 103, 100.5, 102], [102, 104, 101, 103]]
    _, p = forced_panel(rows, cfg)
    force_signal(len(rows))
    r = run_backtest(p, cfg, "2024-01-01", "2024-01-04")
    t = r.trades.iloc[0]
    assert t["entry_date"] == p.dates[1]
    assert t["entry"] == 100.5


def test_gap_above_limit_is_not_chased(force_signal):
    cfg = StrategyConfig(scan_weekday=None)
    rows = [[100, 101, 99, 100], [105, 106, 104, 105], [106, 107, 105.5, 106], [107, 108, 106, 107]]
    _, p = forced_panel(rows, cfg)
    force_signal(len(rows))
    r = run_backtest(p, cfg, "2024-01-01", "2024-01-04")
    assert len(r.trades) == 0


def test_limit_fill_when_price_returns(force_signal):
    cfg = StrategyConfig(scan_weekday=None, exit_score=None)
    rows = [[100, 101, 99, 100], [103, 104, 100.5, 103], [103, 104, 102.5, 103]]
    _, p = forced_panel(rows, cfg)
    force_signal(len(rows))
    r = run_backtest(p, cfg, "2024-01-01", "2024-01-03")
    assert r.trades.iloc[0]["entry"] == pytest.approx(101.0)


def test_gap_through_stop_fills_at_open_not_stop(force_signal):
    cfg = StrategyConfig(scan_weekday=None, stop_atr=2.0, exit_score=None, risk_per_trade=0.01, max_position_pct=0.20)
    rows = [[100, 101, 99, 100], [100, 101, 99.5, 100.5], [90, 91, 89, 90], [90, 91, 89, 90]]
    _, p = forced_panel(rows, cfg)
    force_signal(len(rows))
    r = run_backtest(p, cfg, "2024-01-01", "2024-01-04")
    t = r.trades.iloc[0]
    assert t["reason"] == "stop_gap"
    assert t["exit"] == pytest.approx(90 * (1 - cfg.stop_slippage))


def test_stop_never_moves_down(force_signal):
    # iz suren stop eski ATR yolunda; kademeli TP modunda TP'ler devreye girer
    cfg = StrategyConfig(scan_weekday=None, stop_atr=2.0, trail_atr=3.0,
                         exit_score=None, exit_model="atr")
    rows = [[100, 101, 99, 100], [100, 103, 99.5, 102], [102, 112, 101.5, 111], [111, 112, 104, 105.5], [105.5, 106, 104.9, 105], [105, 105.2, 102.5, 103]]
    _, p = forced_panel(rows, cfg, atr=2.0)
    force_signal(len(rows))
    r = run_backtest(p, cfg, "2024-01-01", "2024-01-08")
    t = r.trades.iloc[0]
    assert t["reason"] == "stop"
    assert t["exit"] == pytest.approx((111 - 3 * 2.0) * (1 - cfg.stop_slippage))


def test_locked_limit_down_blocks_exit(force_signal):
    cfg = StrategyConfig(scan_weekday=None, stop_atr=2.0, exit_score=None, risk_per_trade=0.01, max_position_pct=0.20)
    rows = [[100, 101, 99, 100], [100, 101, 99.5, 100], [90, 90, 90, 90], [85, 86, 84, 85], [85, 86, 84, 85]]
    _, p = forced_panel(rows, cfg)
    force_signal(len(rows))
    r = run_backtest(p, cfg, "2024-01-01", "2024-01-05")
    t = r.trades.iloc[0]
    assert t["exit_date"] == p.dates[3]
    assert t["exit"] == pytest.approx(85 * (1 - cfg.stop_slippage))


def test_position_size_respects_risk_and_caps(force_signal):
    cfg = StrategyConfig(scan_weekday=None, stop_atr=2.0, exit_score=None, risk_per_trade=0.01, max_position_pct=0.20)
    rows = [[100, 101, 99, 100], [100, 101, 99.5, 100], [100, 101, 99.5, 100]]
    _, p = forced_panel(rows, cfg, atr=2.0)
    force_signal(len(rows))
    r = run_backtest(p, cfg, "2024-01-01", "2024-01-03")
    assert r.trades.iloc[0]["qty"] == 200  # %20 sınırı, risk 250 adet verirdi
    _, p = forced_panel(rows, cfg, atr=5.0)
    r = run_backtest(p, cfg, "2024-01-01", "2024-01-03")
    assert r.trades.iloc[0]["qty"] == 100  # 1.000 TL risk / 10 TL


def test_costs_reduce_equity_exactly(force_signal):
    cfg = StrategyConfig(cost_per_side=0.002, scan_weekday=None, stop_atr=2.0, exit_score=None, risk_per_trade=0.01, max_position_pct=0.20)
    rows = [[100, 100, 100, 100], [100, 100, 100, 100], [100, 100, 100, 100]]
    _, p = forced_panel(rows, cfg, atr=5.0)
    force_signal(len(rows))
    r = run_backtest(p, cfg, "2024-01-01", "2024-01-03")
    assert r.equity.iloc[-1] == pytest.approx(100000 - 100 * 100 * 0.002)  # kalan açık pozisyon kapanışı eğriye yansımaz
    assert r.costs == pytest.approx(2 * 100 * 100 * 0.002)


def test_regime_blocks_new_entries(monkeypatch):
    cfg = StrategyConfig(scan_weekday=None, use_regime=True)
    rows = [[100, 101, 99, 100], [100, 101, 99.5, 100], [100, 101, 99.5, 100]]
    _, p = forced_panel(rows, cfg)
    s = np.zeros((3, 1))
    s[0, 0] = 100
    monkeypatch.setattr("strategy_v4.backtest.score_matrices", lambda panel, c: (s, s >= 70))
    p.regime[:] = False
    assert len(run_backtest(p, cfg, "2024-01-01", "2024-01-03").trades) == 0


def test_corporate_action_is_neutralized_but_market_gap_kept():
    idx = pd.bdate_range("2024-01-01", periods=6)
    d = pd.DataFrame({"open": [100.0] * 3 + [10.0] * 3, "high": [100.0] * 3 + [10.0] * 3, "low": [100.0] * 3 + [10.0] * 3,
                      "close": [100.0] * 3 + [10.0] * 3, "volume": [1000.0] * 6}, index=idx)
    out = clean_corporate_actions(d, pd.Series(0.0, index=idx))
    assert out["close"].pct_change().abs().max() < 1e-9
    assert out["volume"].iloc[0] == pytest.approx(10000.0)
    kept = clean_corporate_actions(d, pd.Series(0.2, index=idx))
    assert kept["close"].pct_change().abs().max() == pytest.approx(0.9)


def test_end_to_end_is_deterministic():
    frames = {f"S{i}": make_frame(seed=i + 3, drift=0.001) for i in range(6)}
    xu = xu_for(frames["S0"])
    cfg = StrategyConfig(min_adv_try=1e6)
    p = build_panel(frames, xu, {}, cfg)
    a = run_backtest(p, cfg, "2023-02-01", "2023-08-01")
    b = run_backtest(p, cfg, "2023-02-01", "2023-08-01")
    pd.testing.assert_series_equal(a.equity, b.equity)
    m = metrics(a)
    assert m["islem"] >= 0


def test_score_exit_happens_next_open(force_signal, monkeypatch):
    cfg = StrategyConfig(scan_weekday=None, exit_weekday=None)
    rows = [[100, 101, 99, 100], [100, 102, 99, 101], [101, 103, 100, 102], [103, 104, 102, 103], [104, 105, 103, 104]]
    _, p = forced_panel(rows, cfg, atr=1.0)
    s = np.full((5, 1), 60.0)
    s[0, 0] = 90.0
    s[2, 0] = 40.0
    monkeypatch.setattr("strategy_v4.backtest.score_matrices", lambda panel, c: (s, s >= 70))
    t = run_backtest(p, cfg, "2024-01-01", "2024-01-05").trades.iloc[0]
    assert t["reason"] == "score"
    assert t["exit_date"] == p.dates[3]
    assert t["exit"] == 103


def test_max_position_cap_limits_each_holding(force_signal):
    cfg = StrategyConfig(scan_weekday=None, exit_score=None)
    rows = [[100, 101, 99, 100], [100, 101, 99.5, 100], [100, 101, 99.5, 100]]
    _, p = forced_panel(rows, cfg, atr=1.0)
    force_signal(len(rows))
    assert run_backtest(p, cfg, "2024-01-01", "2024-01-03").trades.iloc[0]["qty"] == 125
