"""v4 tabanı + ATR'ye bağlı kademeli TP: eminlik akışı, TP bildirimleri, satışta AI."""

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest

from bot_v4 import messages as msg
from bot_v4.analyst import AI_AL, AI_ALMA, AI_SAT, AI_TUT, AI_YOK, Review
from bot_v4.engine import Engine
from bot_v4.market import IntradayBar, StaleData
from bot_v4.settings import BotConfig
from bot_v4.store import Store
from strategy_v4.config import StrategyConfig
from strategy_v4.exits import levels_for

TZ = ZoneInfo("Europe/Istanbul")
MONDAY = date(2026, 11, 2)
FRIDAY = date(2026, 11, 6)


def at(day, hhmm):
    h, m = hhmm.split(":")
    return datetime(day.year, day.month, day.day, int(h), int(m), tzinfo=TZ)


def frame(end=date(2026, 10, 30), n=320, step=0.004, seed=1):
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range(end=pd.Timestamp(end), periods=n)
    c = 100 * np.exp(np.cumsum(rng.normal(step, 0.008, n)))
    o = np.r_[c[0], c[:-1]]
    return pd.DataFrame(
        {"open": o, "high": np.maximum(o, c) * 1.006, "low": np.minimum(o, c) * 0.994,
         "close": c, "volume": 2e6},
        index=idx,
    )


class Market:
    def __init__(self, frames):
        self.frames = frames
        self.xu = pd.Series(1000 * np.exp(np.linspace(0, 0.3, 400)),
                            index=pd.bdate_range(end=pd.Timestamp("2026-11-20"), periods=400))
        self.window: dict = {}

    def clear_cache(self):
        pass

    def xu100(self, as_of, include_today=False):
        stamp = pd.Timestamp(as_of)
        return self.xu[self.xu.index <= stamp] if include_today else self.xu[self.xu.index < stamp]

    def daily(self, symbol, as_of, include_today=False):
        d = self.frames[symbol]
        stamp = pd.Timestamp(as_of)
        if include_today:
            if d.index[-1] < stamp:
                raise StaleData("yok")
            return d[d.index <= stamp]
        return d[d.index < stamp]

    def intraday(self, symbol, as_of):
        if symbol not in self.frames:
            return None
        last = float(self.frames[symbol]["close"].iloc[-1])
        o, h, l = self.window.get(symbol, (last, last * 1.001, last * 0.999))
        return IntradayBar(as_of, o, h, l, l if l < o else h, 1e6,
                           recent_low=l, recent_high=h, recent_open=o)

    def index_last(self, as_of):
        return float(self.xu.iloc[-5])

    def hourly(self, symbol, as_of=None):
        return self.frames[symbol].tail(10)

    def index_hourly(self, as_of=None):
        return pd.DataFrame()

    def validate_symbol(self, symbol, as_of):
        return symbol in self.frames


class Analyst:
    def __init__(self, decision=AI_AL, olasilik=90.0, exit_decision=AI_SAT):
        self.decision, self.olasilik, self.exit_decision = decision, olasilik, exit_decision
        self.calls, self.exit_calls = [], []

    def review(self, symbol, now, last, daily, hourly, xu100, force=False):
        self.calls.append(symbol)
        return Review(decision=self.decision, olasilik=self.olasilik, summary="özet",
                      risks=["risk"], error="hata" if self.decision == AI_YOK else "")

    def review_exit(self, symbol, now, last, daily, xu100, position, force=False):
        self.exit_calls.append(symbol)
        return Review(decision=self.exit_decision, olasilik=70.0, summary="satış özeti",
                      hold_condition="EMA50 üstünde kalırsa")


def build(tmp_path, frames=None, mode="paper", **kw):
    frames = frames or {"AAA": frame()}
    analyst_kw = {k: kw.pop(k) for k in ("decision", "olasilik", "exit_decision") if k in kw}
    cfg = BotConfig(bot_token="t", chat_id="1", db_path=str(tmp_path / "s.db"),
                    paper_mode=(mode == "paper"), ai_enabled=True, **kw)
    store = Store(cfg.db_path)
    sent: list = []
    market = Market(frames)
    strategy = StrategyConfig(min_adv_try=1e6)
    engine = Engine(cfg, strategy, store, market, Analyst(**analyst_kw), sent.append, {})
    for s in frames:
        store.add_symbol(s, at(MONDAY, "09:00"))
    return engine, sent, market


def open_paper(engine, symbol="AAA", entry=100.0, qty=30, atr=2.0, mode="paper", **kw):
    lv = levels_for(entry, atr, engine.strategy)
    return engine.store.open_position(
        symbol=symbol, mode=mode, entry=entry, qty=qty, initial_qty=qty, remaining_qty=qty,
        stop=lv["stop"], tp1=lv["tp1"], tp2=lv["tp2"], tp3=lv["tp3"],
        exit_model=engine.strategy.exit_model, atr=atr,
        opened_at=at(MONDAY, "10:30").isoformat(), **kw
    ), lv


# --- eminlik akışı -----------------------------------------------------------


def test_four_conditions_gate_the_notification(tmp_path):
    cfg = BotConfig()
    # kural 76 + AI 84 → eminlik 81 → bildirim
    assert cfg.eminlik(76, 84) == pytest.approx(80.8)
    assert cfg.passes_four_conditions(76, AI_AL, 84, 70)
    # AI olasılığı eşiğin altı
    assert not cfg.passes_four_conditions(76, AI_AL, 69, 70)
    # eminlik eşiğin altı (kural 70 + AI 75 → 72)
    assert not cfg.passes_four_conditions(70, AI_AL, 75, 70)
    # ALMA hiçbir olasılıkla geçmez
    assert not cfg.passes_four_conditions(90, AI_ALMA, 99, 70)
    # kural skoru eşiğin altı
    assert not cfg.passes_four_conditions(69, AI_AL, 99, 70)


def test_candidate_stores_slot_olasilik_and_eminlik(tmp_path):
    engine, sent, _ = build(tmp_path)
    engine.scan_intraday(at(MONDAY, "10:30"), "S1")
    cand = engine.store.candidates_on(MONDAY)[0]
    assert cand["scan_slot"] == "S1"
    assert cand["ai_olasilik"] == 90.0
    assert cand["eminlik"] == engine.cfg.eminlik(cand["score"], 90.0)


def test_eminlik_appears_in_buy_message(tmp_path):
    engine, sent, _ = build(tmp_path)
    engine.scan_intraday(at(MONDAY, "10:30"), "S1")
    buy = next(m for m in sent if "🟢 AL" in m)
    assert "EMİNLİK" in buy and "AI olasılığı 90" in buy
    assert "Gerçek risk" in buy and "TP1" in buy and "TP3" in buy
    assert "S1" in buy and "gün içi" in buy


def test_ai_yok_does_not_open_position(tmp_path):
    engine, sent, _ = build(tmp_path, decision=AI_YOK)
    engine.scan_intraday(at(MONDAY, "10:30"), "S1")
    assert any("KURAL AL, AI YOK" in m for m in sent)
    assert engine.store.open_positions("paper") == []
    assert engine.store.open_positions("shadow") == []


# --- kademeli TP takibi ------------------------------------------------------


def test_tp1_sells_a_third_and_moves_stop_to_breakeven(tmp_path):
    engine, sent, market = build(tmp_path)
    pid, lv = open_paper(engine)
    market.window["AAA"] = (100.0, lv["tp1"] + 0.5, 99.0)
    engine.monitor(at(MONDAY, "11:00"))

    p = engine.store.open_position_for("AAA", "paper")
    assert engine._remaining(p) == 20
    assert p["reached_level"] == "tp1"
    assert p["stop"] == pytest.approx(100.0)  # başa baş
    assert p["tp1_alerted"] == 1
    fills = engine.store.fills(pid)
    assert len(fills) == 1 and fills[0]["kind"] == "tp1" and fills[0]["qty"] == 10
    note = next(m for m in sent if "TP1" in m)
    assert "10 adet sat" in note and "başa baş" in note and "/sattim AAA" in note


def test_each_level_alerts_once_even_after_restart(tmp_path):
    engine, sent, market = build(tmp_path)
    open_paper(engine)
    lv = levels_for(100.0, 2.0, engine.strategy)
    market.window["AAA"] = (100.0, lv["tp1"] + 0.5, 99.5)
    engine.monitor(at(MONDAY, "11:00"))
    count = len([m for m in sent if "TP1" in m])
    engine.monitor(at(MONDAY, "11:15"))
    engine.monitor(at(MONDAY, "11:30"))
    assert len([m for m in sent if "TP1" in m]) == count == 1


def test_full_ladder_closes_position_with_weighted_average(tmp_path):
    engine, sent, market = build(tmp_path)
    pid, lv = open_paper(engine)
    for level, low in ((lv["tp1"], 99.0), (lv["tp2"], lv["tp1"] + 0.5), (lv["tp3"], lv["tp2"] + 0.5)):
        market.window["AAA"] = (low, level + 0.5, low)
        engine.monitor(at(MONDAY, "11:00"))
    closed = engine.store.closed_positions("paper")
    assert len(closed) == 1
    fills = engine.store.fills(pid)
    assert [f["kind"] for f in fills] == ["tp1", "tp2", "tp3"]
    assert sum(f["qty"] for f in fills) == 30
    expected = sum(f["price"] * f["qty"] for f in fills) / 30
    assert closed[0]["exit_price"] == pytest.approx(expected)
    assert closed[0]["pnl"] == pytest.approx(sum(f["pnl"] for f in fills))


def test_stop_closes_remaining_quantity(tmp_path):
    engine, sent, market = build(tmp_path)
    pid, lv = open_paper(engine)
    market.window["AAA"] = (100.0, 100.5, lv["stop"] - 1.0)
    engine.monitor(at(MONDAY, "11:00"))
    closed = engine.store.closed_positions("paper")
    assert len(closed) == 1 and closed[0]["reason"] == "stop"
    assert engine.store.fills(pid)[0]["qty"] == 30
    assert any("STOP" in m for m in sent)


def test_real_mode_only_alerts_and_keeps_quantity(tmp_path):
    engine, sent, market = build(tmp_path, mode="real")
    pid, lv = open_paper(engine, mode="real")
    market.window["AAA"] = (100.0, lv["tp1"] + 0.5, 99.0)
    engine.monitor(at(MONDAY, "11:00"))
    p = engine.store.open_position_for("AAA", "real")
    assert engine._remaining(p) == 30, "gerçek pozisyonda adet değişmez"
    assert p["stop"] == pytest.approx(100.0), "stop taşıma bilgi olarak işlenir"
    assert engine.store.fills(pid) == []
    assert any("TP1" in m and "[KAĞIT]" not in m for m in sent)


def test_shadow_position_is_managed_but_silent(tmp_path):
    engine, sent, market = build(tmp_path)
    pid, lv = open_paper(engine, mode="shadow")
    market.window["AAA"] = (100.0, lv["tp1"] + 0.5, 99.0)
    engine.monitor(at(MONDAY, "11:00"))
    p = engine.store.open_position_for("AAA", "shadow")
    assert engine._remaining(p) == 20  # gölgede kısmi satış uygulanır
    assert not any("TP1" in m for m in sent)  # ama bildirim gitmez


# --- yumuşak çıkış ve satışta AI --------------------------------------------


def test_soft_exit_asks_ai_and_sells_on_sat(tmp_path):
    frames = {"AAA": frame(end=FRIDAY, step=-0.004)}
    engine, sent, _ = build(tmp_path, frames=frames, exit_decision=AI_SAT)
    open_paper(engine, entry=100.0, qty=10)
    engine.close_job(at(FRIDAY, "18:45"))
    assert engine.analyst.exit_calls == ["AAA"]
    assert any("🔴 SAT" in m and "AI: SAT" in m for m in sent)
    assert engine.store.open_position_for("AAA", "paper")["exit_flag"] in ("score", "ema50")


def test_soft_exit_tut_holds_then_sells_when_expired(tmp_path):
    frames = {"AAA": frame(end=FRIDAY, step=-0.004)}
    engine, sent, _ = build(tmp_path, frames=frames, exit_decision=AI_TUT)
    open_paper(engine, entry=100.0, qty=10)
    engine.close_job(at(FRIDAY, "18:45"))
    p = engine.store.open_position_for("AAA", "paper")
    assert p["soft_exit_hold_until"] and not p["exit_flag"]
    assert any("🔵 TUT" in m for m in sent)

    # TUT süresi dolunca kural satar, AI'a TEKRAR sorulmaz
    later = date.fromisoformat(p["soft_exit_hold_until"]) + timedelta(days=1)
    frames["AAA"] = frame(end=later, step=-0.004)
    engine.market.frames["AAA"] = frames["AAA"]
    engine.close_job(at(later, "18:45"))
    assert len(engine.analyst.exit_calls) == 1, "TUT bir kez verilir"
    assert engine.store.open_position_for("AAA", "paper")["exit_flag"]
    assert any("TUT süresi doldu" in m for m in sent)


def test_time_stop_never_asks_ai(tmp_path):
    """Zaman stopunda AI'a sorulmaz, anında bildirilir."""
    frames = {"AAA": frame(end=FRIDAY, step=0.0)}
    engine, sent, _ = build(tmp_path, frames=frames)
    open_paper(engine, entry=100.0, qty=10)
    # 30 seans öncesine çek → max_hold dolsun
    engine.store.update_position(
        engine.store.open_positions("paper")[0]["id"],
        opened_at=at(date(2026, 8, 3), "10:30").isoformat(),
    )
    engine.close_job(at(FRIDAY, "18:45"))
    assert engine.analyst.exit_calls == []
    assert any("SAT" in m and ("zaman stopu" in m or "azami tutma" in m) for m in sent)


def test_stop_never_asks_ai(tmp_path):
    engine, sent, market = build(tmp_path)
    _, lv = open_paper(engine)
    market.window["AAA"] = (100.0, 100.5, lv["stop"] - 1.0)
    engine.monitor(at(MONDAY, "11:00"))
    assert engine.analyst.exit_calls == []


# --- mesaj biçimleri ---------------------------------------------------------


def test_turkish_number_format():
    assert msg.tl(1234.5) == "1.234,50"
    assert msg.tl(182.4) == "182,40"
    assert msg.pct(0.061) == "+6,1%"
    assert msg.pct(-0.041) == "-4,1%"


def test_position_lines_show_remaining_and_next_level(tmp_path):
    engine, _, _ = build(tmp_path)
    open_paper(engine)
    p = engine.store.open_positions("paper")[0]
    line = msg.position_lines([p], {"AAA": 105.0})[0]
    assert "30 adet" in line and "sıradaki TP1" in line
    engine.store.update_position(p["id"], reached_level="tp1", remaining_qty=20)
    p = engine.store.open_positions("paper")[0]
    assert "sıradaki TP2" in msg.position_lines([p], {"AAA": 110.0})[0]
