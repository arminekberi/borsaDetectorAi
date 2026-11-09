"""Bot v4 birim testleri: Telegram, Yahoo ve Claude taklit edilir."""

import json
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest

from bot_v4 import commands, session
from bot_v4.analyst import (
    AI_AL, AI_ALMA, AI_SAT, AI_TUT, AI_YOK, Analyst, Review,
    extract_json, validate_entry, validate_exit,
)
from bot_v4.engine import Engine
from bot_v4.market import IntradayBar, StaleData, YahooMarket
from bot_v4.scheduler import Scheduler
from bot_v4.settings import BotConfig
from bot_v4.store import Store
from bot_v4.telegram_app import build_application, split_message
from strategy_v4.config import StrategyConfig
from strategy_v4.live import evaluate

try:
    import httpx2 as httpx
except ImportError:
    import httpx

TZ = ZoneInfo("Europe/Istanbul")
MONDAY = date(2026, 11, 2)


def at(day, hhmm):
    h, m = hhmm.split(":")
    return datetime(day.year, day.month, day.day, int(h), int(m), tzinfo=TZ)


def uptrend(end=date(2026, 10, 30), n=320, step=0.004, seed=1):
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range(end=pd.Timestamp(end), periods=n)
    c = 100 * np.exp(np.cumsum(rng.normal(step, 0.008, n)))
    o = np.r_[c[0], c[:-1]]
    return pd.DataFrame({"open": o, "high": np.maximum(o, c) * 1.006, "low": np.minimum(o, c) * 0.994, "close": c, "volume": 2e6}, index=idx)


class FakeMarket:
    def __init__(self, frames, price=None):
        self.frames = frames
        self.xu = pd.Series(1000 * np.exp(np.linspace(0, 0.3, 400)), index=pd.bdate_range(end=pd.Timestamp("2026-11-13"), periods=400))
        self.price = price or {}
        self.valid = set(frames)
        self.low = {}

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
        last = self.price.get(symbol, float(self.frames[symbol]["close"].iloc[-1]) * 1.002)
        return IntradayBar(as_of, last, last * 1.003, last * 0.997, last, 1e6, self.low.get(symbol, last * 0.998))

    def index_last(self, as_of):
        return float(self.xu.iloc[-5])

    def hourly(self, symbol, as_of=None):
        return self.frames[symbol].tail(10)

    def index_hourly(self, as_of=None):
        return pd.DataFrame()

    def validate_symbol(self, symbol, as_of):
        return symbol in self.valid


class FakeAnalyst:
    """Yeni tasarim: AL/ALMA + olasilik; satista SAT/TUT."""

    def __init__(self, decision=AI_AL, olasilik=90.0, exit_decision=AI_SAT):
        self.decision = decision
        self.olasilik = olasilik
        self.exit_decision = exit_decision
        self.calls = []
        self.exit_calls = []

    def review(self, symbol, now, last, daily, hourly, xu100, force=False):
        self.calls.append((symbol, now, force))
        return Review(
            decision=self.decision, olasilik=self.olasilik, summary="özet",
            reasons=["gerekçe"], risks=["risk"], resistances=[last * 1.1],
            error="hata" if self.decision == AI_YOK else "",
        )

    def review_exit(self, symbol, now, last, daily, xu100, position, force=False):
        self.exit_calls.append((symbol, now))
        return Review(
            decision=self.exit_decision, olasilik=70.0, summary="satış özeti",
            hold_condition="EMA50 üstünde kalırsa",
        )


def make_engine(tmp_path, frames=None, decision=AI_AL, paper=True, watch=None,
                olasilik=90.0, exit_decision=AI_SAT, **cfg_kw):
    frames = frames or {"AAA": uptrend(seed=1)}
    cfg = BotConfig(bot_token="t", chat_id="1", db_path=str(tmp_path / "b.db"),
                    paper_mode=paper, ai_enabled=True, **cfg_kw)
    store = Store(cfg.db_path)
    sent = []
    market = FakeMarket(frames)
    engine = Engine(cfg, StrategyConfig(min_adv_try=1e6), store, market,
                    FakeAnalyst(decision, olasilik, exit_decision), sent.append, {})
    for s in watch or frames:
        store.add_symbol(s, at(MONDAY, "09:00"))
    return engine, sent


def test_session_calendar_matches_official_2026():
    assert not session.is_trading_day(date(2026, 5, 27))
    assert session.is_trading_day(date(2026, 3, 23))
    assert session.is_half_day(date(2026, 10, 28))
    assert not session.is_half_day(date(2026, 12, 31))
    assert session.next_trading_day(date(2026, 10, 28)) == date(2026, 10, 30)
    assert session.is_last_trading_day_of_week(date(2026, 11, 6))
    assert not session.is_last_trading_day_of_week(date(2026, 11, 5))
    assert not session.calendar_known(date(2027, 1, 4))


def test_provisional_uses_closed_candle_points_only():
    cfg = StrategyConfig(min_adv_try=1e6)
    hist = uptrend()
    xu = pd.Series(np.linspace(1000, 1500, 320), index=hist.index)
    closed = evaluate("AAA", hist, xu, cfg)
    last = float(hist["close"].iloc[-1])
    bar = {"date": MONDAY, "open": last, "high": last * 1.04, "low": last * 0.9, "close": last * 1.03, "volume": 5e6}  # çekiç benzeri gün
    ev = evaluate("AAA", hist, xu, cfg, bar)
    assert ev.provisional and ev.candle_pts == closed.candle_pts
    assert ev.as_of == MONDAY


def test_scan_sends_al_and_opens_paper_position(tmp_path):
    engine, sent = make_engine(tmp_path)
    engine.scan_intraday(at(MONDAY, "10:30"))
    assert any("🟢 AL" in m and "[KAĞIT]" in m and "gün içi" in m and "EMİNLİK" in m for m in sent)
    p = engine.store.open_positions("paper")
    assert len(p) == 1 and p[0]["decision"] == AI_AL and p[0]["qty"] > 0


def test_same_day_scan_does_not_duplicate_or_call_ai_twice(tmp_path):
    engine, sent = make_engine(tmp_path)
    engine.scan_intraday(at(MONDAY, "10:30"))
    count = len([m for m in sent if "🟢 AL" in m])
    engine.scan_intraday(at(MONDAY, "12:00"))
    assert len([m for m in sent if "🟢 AL" in m]) == count == 1
    assert len(engine.analyst.calls) == 1


def test_alma_goes_to_shadow_only(tmp_path):
    engine, sent = make_engine(tmp_path, decision=AI_ALMA, notify_alma=True)
    engine.scan_intraday(at(MONDAY, "10:30"))
    assert any("⛔ ALMA" in m for m in sent)
    assert engine.store.open_positions("paper") == []
    assert len(engine.store.open_positions("shadow")) == 1


def test_ai_yok_warns_and_opens_nothing(tmp_path):
    """AI_YOK 'AL sayılmaz, elle incele' demektir; gölgeye de girmez."""
    engine, sent = make_engine(tmp_path, decision=AI_YOK)
    engine.scan_intraday(at(MONDAY, "10:30"))
    assert any("KURAL AL, AI YOK" in m for m in sent)
    assert engine.store.open_positions("paper") == []
    assert engine.store.open_positions("shadow") == []


def test_alma_is_silent_by_default_but_still_shadowed(tmp_path):
    """NOTIFY_ALMA=false (varsayılan): ALMA kullanıcıya gitmez, gölgede izlenir."""
    engine, sent = make_engine(tmp_path, decision=AI_ALMA)
    engine.scan_intraday(at(MONDAY, "10:30"))
    assert not any("⛔ ALMA" in m for m in sent)
    assert len(engine.store.open_positions("shadow")) == 1
    assert any("gölgede izleniyor" in m for m in sent)  # sorgu özetinde tek satır


def test_below_threshold_eminlik_is_not_notified(tmp_path):
    """AI AL dese bile eminlik eşiğin altındaysa bildirim gitmez."""
    engine, sent = make_engine(tmp_path, decision=AI_AL, olasilik=60.0)
    engine.scan_intraday(at(MONDAY, "10:30"))
    assert not any("🟢 AL" in m for m in sent)
    assert engine.store.open_positions("paper") == []
    assert len(engine.store.open_positions("shadow")) == 1
    cand = engine.store.active_candidates() or engine.store.candidates_on(MONDAY)
    assert cand[0]["eminlik"] < engine.cfg.min_eminlik


def test_real_mode_never_opens_position_by_itself(tmp_path):
    engine, sent = make_engine(tmp_path, paper=False)
    engine.scan_intraday(at(MONDAY, "10:30"))
    assert any("🟢 AL" in m for m in sent)
    assert not any("[KAĞIT]" in m for m in sent)
    assert engine.store.open_positions("real") == [] and engine.store.open_positions("paper") == []
    assert len(engine.store.active_candidates()) == 1


def test_position_limit_blocks_ai_and_al(tmp_path):
    frames = {f"S{i:02d}": uptrend(seed=i + 1) for i in range(10)}
    engine, sent = make_engine(tmp_path, frames=frames)
    for i in range(8):
        engine.store.open_position(symbol=f"X{i}", mode="paper", entry=10.0, qty=1, stop=5.0, atr=1.0, opened_at=at(MONDAY, "09:00").isoformat())
    engine.scan_intraday(at(MONDAY, "10:30"))
    assert engine.analyst.calls == []
    assert not any("🟢 AL" in m for m in sent)
    assert any("portföy dolu" in m for m in sent)


def test_daily_new_entry_limit(tmp_path):
    frames = {f"S{i:02d}": uptrend(seed=i + 1) for i in range(6)}
    engine, sent = make_engine(tmp_path, frames=frames)
    engine.scan_intraday(at(MONDAY, "10:30"))
    assert len([m for m in sent if "🟢 AL" in m]) == 2


def test_stop_closes_paper_and_alerts_real_once(tmp_path):
    engine, sent = make_engine(tmp_path)
    engine.store.open_position(symbol="AAA", mode="paper", entry=100.0, qty=10, stop=90.0, atr=2.5, opened_at=at(MONDAY, "10:30").isoformat())
    engine.store.open_position(symbol="AAA", mode="real", entry=100.0, qty=10, stop=90.0, atr=2.5, opened_at=at(MONDAY, "10:30").isoformat())
    engine.market.price["AAA"] = 89.0
    engine.market.low["AAA"] = 88.5
    engine.monitor(at(MONDAY, "11:00"))
    engine.monitor(at(MONDAY, "11:15"))
    assert engine.store.open_positions("paper") == []
    assert len([m for m in sent if m.startswith("🔴 SAT | STOP")]) == 1
    closed = engine.store.closed_positions("paper")[0]
    assert closed["reason"] == "stop" and closed["pnl"] < 0


def test_close_cancels_weak_provisional_signal(tmp_path):
    frames = {"AAA": uptrend(end=date(2026, 10, 30), step=0.0)}
    engine, sent = make_engine(tmp_path, frames=frames, paper=False)
    day = engine.store.add_candidate(symbol="AAA", signal_date=MONDAY.isoformat(), stage="provisional", status="active", score=72, breakdown={}, reference=100.0,
                                     atr=2.0, adv=1e7, max_entry=101.0, stop=92.0, qty=10, valid_until=MONDAY.isoformat(), decision=AI_AL, ai_olasilik=90.0,
                                     eminlik=85.0, created_at="x")
    hist = frames["AAA"].copy()
    hist.loc[pd.Timestamp(MONDAY)] = [90.0, 91.0, 80.0, 81.0, 2e6]
    engine.market.frames["AAA"] = hist
    engine.close_job(at(MONDAY, "18:45"))
    assert engine.store.candidate(day)["status"] == "cancelled"
    assert any("SİNYAL İPTAL" in m for m in sent)


def test_filled_paper_position_gets_cancel_notice_but_stays_open(tmp_path):
    frames = {"AAA": uptrend(step=0.004)}
    engine, sent = make_engine(tmp_path, frames=frames)
    engine.scan_intraday(at(MONDAY, "10:30"))
    hist = frames["AAA"].copy()
    hist.loc[pd.Timestamp(MONDAY)] = [90.0, 91.0, 40.0, 41.0, 2e6]
    engine.market.frames["AAA"] = hist
    engine.close_job(at(MONDAY, "18:45"))
    assert any("SİNYAL İPTAL" in m for m in sent)
    assert len(engine.store.open_positions("paper")) == 1


def test_confirmed_candidate_expires_after_two_sessions(tmp_path):
    engine, sent = make_engine(tmp_path, paper=False)
    cid = engine.store.add_candidate(symbol="AAA", signal_date=MONDAY.isoformat(), stage="confirmed", status="active", score=75, breakdown={}, reference=100.0,
                                     atr=2.0, adv=1e7, max_entry=101.0, stop=92.0, qty=10, valid_until=date(2026, 11, 4).isoformat(), decision=AI_AL,
                                     ai_olasilik=90.0, eminlik=85.0, created_at="x")
    engine.market.price["AAA"] = 110.0
    engine.monitor(at(date(2026, 11, 3), "10:15"))
    assert any("aşıldı" in m for m in sent)
    engine.monitor(at(date(2026, 11, 3), "10:30"))
    assert len([m for m in sent if "aşıldı" in m]) == 1
    engine.monitor(at(date(2026, 11, 5), "10:15"))
    assert engine.store.candidate(cid)["status"] == "expired"


def test_weekly_exit_flags_on_last_day_and_exits_next_session(tmp_path):
    friday = date(2026, 11, 6)
    frames = {"AAA": uptrend(end=friday, step=-0.004)}
    engine, sent = make_engine(tmp_path, frames=frames)
    engine.store.open_position(symbol="AAA", mode="paper", entry=100.0, qty=10, initial_qty=10,
                               remaining_qty=10, stop=1.0, tp1=1e9, tp2=1e9, tp3=1e9, atr=2.0,
                               opened_at=at(MONDAY, "10:30").isoformat())
    engine.close_job(at(friday, "18:45"))
    assert any("SAT | Kural skoru" in m for m in sent)
    assert len(engine.store.open_positions("paper")) == 1
    engine.monitor(at(date(2026, 11, 9), "10:15"))
    assert engine.store.closed_positions("paper")[0]["reason"] == "score"


def test_midweek_low_score_does_not_sell(tmp_path):
    day = date(2026, 11, 4)
    engine, sent = make_engine(tmp_path, frames={"AAA": uptrend(end=day, step=-0.004)})
    engine.store.open_position(symbol="AAA", mode="paper", entry=100.0, qty=10, stop=1.0, atr=2.0, opened_at=at(MONDAY, "10:30").isoformat())
    engine.close_job(at(day, "18:45"))
    assert not any("SAT" in m for m in sent)


def test_close_job_waits_for_data(tmp_path):
    engine, sent = make_engine(tmp_path, frames={"AAA": uptrend(end=date(2026, 10, 30))})
    with pytest.raises(StaleData):
        engine.close_job(at(MONDAY, "18:45"))


def test_commands_watchlist_and_validation(tmp_path):
    engine, sent = make_engine(tmp_path)
    now = at(MONDAY, "12:00")
    assert "bulunamadı" in commands.izle(engine, ["BORLEASE"], now)
    engine.market.valid.add("BORLS")
    assert "eklendi" in commands.izle(engine, ["borls.is"], now)
    assert "BORLS" in commands.takip(engine, [], now)
    assert "çıkarıldı" in commands.birak(engine, ["BORLS"], now)
    assert "Kullanım" in commands.izle(engine, [], now)


def test_aldim_sattim_track_real_position(tmp_path):
    engine, sent = make_engine(tmp_path)
    now = at(MONDAY, "12:00")
    text = commands.aldim(engine, ["AAA", "150,50", "10"], now)
    assert "Stop" in text and "TP1" in text and "TP3" in text
    assert "zaten" in commands.aldim(engine, ["AAA", "150", "10"], now)
    p = engine.store.open_position_for("AAA", "real")
    assert p["entry"] == 150.5 and p["stop"] < 150.5
    assert p["initial_qty"] == 10 and p["remaining_qty"] == 10 and p["tp1"] > 150.5

    # kısmi satış: kalan adet korunur, stop başa başa çekilir
    part = commands.sattim(engine, ["AAA", "160", "4"], now)
    assert "Kalan 6 adet" in part and "başa baş" in part
    p = engine.store.open_position_for("AAA", "real")
    assert engine._remaining(p) == 6 and p["stop"] == pytest.approx(150.5)
    assert len(engine.store.fills(p["id"])) == 1

    assert "Daha fazlası satılamaz" in commands.sattim(engine, ["AAA", "160", "99"], now)

    done = commands.sattim(engine, ["AAA", "170"], now)
    assert "Pozisyon kapandı" in done
    closed = engine.store.closed_positions("real")[0]
    assert closed["pnl"] > 0 and closed["remaining_qty"] == 0
    # kapanış fiyatı dolumların adet-ağırlıklı ortalaması
    assert closed["exit_price"] == pytest.approx((160 * 4 + 170 * 6) / 10)
    assert "açık gerçek pozisyon yok" in commands.sattim(engine, ["AAA", "160"], now)
    assert "Kullanım" in commands.aldim(engine, ["AAA", "-5", "10"], now)


def test_scheduler_runs_each_job_once_and_survives_restart(tmp_path):
    engine, sent = make_engine(tmp_path)
    calls = []
    engine.scan_intraday = lambda now, slot="": calls.append(("scan", now.strftime("%H:%M")))
    engine.monitor = lambda now: calls.append(("monitor", now.strftime("%H:%M")))
    engine.close_job = lambda now: calls.append(("close", now.strftime("%H:%M")))
    sch = Scheduler(engine.cfg, engine)
    now = at(MONDAY, "09:00")
    while now <= at(MONDAY, "20:00"):
        sch.tick(now)
        if now == at(MONDAY, "13:00"):
            sch = Scheduler(engine.cfg, engine)
        now += timedelta(minutes=5)
    assert [c[1] for c in calls if c[0] == "scan"] == ["10:30", "12:00", "14:00", "16:00", "17:30"]
    assert [c[1] for c in calls if c[0] == "close"] == ["18:45"]
    mon = [c[1] for c in calls if c[0] == "monitor"]
    assert mon[0] == "10:00" and mon[-1] == "18:15" and len(mon) == len(set(mon)) == 34


def test_scheduler_skips_holiday_and_handles_half_day(tmp_path):
    engine, sent = make_engine(tmp_path)
    calls = []
    engine.scan_intraday = lambda now, slot="": calls.append(now)
    engine.monitor = lambda now: calls.append(now)
    engine.close_job = lambda now: calls.append(now)
    sch = Scheduler(engine.cfg, engine)
    for hhmm in ("10:30", "12:00", "14:00", "18:45"):
        sch.tick(at(date(2026, 10, 29), hhmm))
    assert calls == []
    # Yarım gün: resmi kapanış 12:40, kapanış işi 13:25 (12:40 + 45 dk)
    half = date(2026, 10, 28)
    for hhmm in ("13:00", "13:20"):
        sch.tick(at(half, hhmm))
    assert calls == [], "kapanış saatinden önce iş çalışmamalı"
    sch.tick(at(half, "13:25"))
    assert len(calls) == 1
    assert session.session_close(half) == session.HALF_DAY_CLOSE == time(12, 40)


def test_scheduler_skips_intraday_scans_after_half_day_close(tmp_path):
    """Yarım günde seans sonrasına düşen sorgular atlanır (12:40 sonrası)."""
    engine, sent = make_engine(tmp_path)
    slots = []
    engine.scan_intraday = lambda now, slot="": slots.append((now.strftime("%H:%M"), slot))
    engine.monitor = lambda now: None
    engine.close_job = lambda now: None
    sch = Scheduler(engine.cfg, engine)
    half = date(2026, 10, 28)
    for hhmm in ("10:30", "12:00", "14:00", "16:00", "17:30"):
        sch.tick(at(half, hhmm))
    # 10:30 ve 12:00 seans içinde; 14:00 ve sonrası kapanıştan sonra → atlanır
    assert [t for t, _ in slots] == ["10:30", "12:00"]
    assert [s for _, s in slots] == ["S1", "S2"]


def test_scheduler_retries_close_until_data_arrives(tmp_path):
    engine, sent = make_engine(tmp_path)
    tries = []

    def close(now):
        tries.append(now)
        if len(tries) < 2:
            raise StaleData("yok")

    engine.close_job = close
    sch = Scheduler(engine.cfg, engine)
    sch.tick(at(MONDAY, "18:45"))
    sch.tick(at(MONDAY, "18:50"))
    sch.tick(at(MONDAY, "18:56"))
    sch.tick(at(MONDAY, "19:30"))
    assert len(tries) == 2


def test_sunday_job_is_off_by_default(tmp_path):
    """SUNDAY_REVIEW=false (varsayılan): Pazar işi hiç çalışmaz."""
    engine, sent = make_engine(tmp_path)
    engine.store.add_candidate(symbol="AAA", signal_date=MONDAY.isoformat(), stage="confirmed",
                               status="active", score=75, breakdown={}, reference=100.0, atr=2.0,
                               adv=1e7, max_entry=101.0, stop=92.0, qty=10,
                               valid_until=date(2026, 11, 10).isoformat(), decision=AI_AL,
                               ai_olasilik=90.0, eminlik=85.0, created_at="x")
    engine.sunday_job(at(date(2026, 11, 8), "20:00"))
    assert engine.analyst.calls == [] and sent == []
    sch = Scheduler(engine.cfg, engine)
    sch.tick(at(date(2026, 11, 8), "20:00"))
    assert engine.analyst.calls == []


def test_sunday_rereview_can_downgrade_candidate(tmp_path):
    engine, sent = make_engine(tmp_path, decision=AI_ALMA, paper=False, sunday_review=True)
    cid = engine.store.add_candidate(symbol="AAA", signal_date="2026-11-06", stage="confirmed", status="active", score=75, breakdown={}, reference=100.0,
                                     atr=2.0, adv=1e7, max_entry=101.0, stop=92.0, qty=10, valid_until="2026-11-10", decision=AI_AL,
                                     ai_olasilik=90.0, eminlik=85.0, created_at="x")
    engine.sunday_job(at(date(2026, 11, 8), "20:00"))
    assert engine.store.candidate(cid)["decision"] == AI_ALMA
    assert engine.analyst.calls[0][2] is True
    assert any("hafta sonu incelemesi" in m for m in sent)


def test_split_message_respects_telegram_limit():
    parts = split_message("\n".join(["x" * 1000] * 10))
    assert all(len(p) <= 3900 for p in parts) and "".join(parts).count("x") == 10000


def test_unauthorized_chat_cannot_trigger_any_handler(tmp_path):
    engine, _ = make_engine(tmp_path)
    app = build_application(BotConfig(bot_token="123:abc", chat_id="42"), engine)
    handlers = [h for group in app.handlers.values() for h in group]
    assert len(handlers) >= 12
    for handler in handlers:
        text = repr(handler.filters)
        assert "42" in text or "Chat" in text


# Claude API, gerçek SDK ve taklit HTTP katmanıyla test edilir
GOOD = {
    "karar": "AL", "olasilik": 84, "ozet": "Güçlü görünüm.",
    "gerekce": ["trend yukarı"], "riskler": ["bilanço riski"], "direncler": [110.0],
    "haberler": [
        {"baslik": "H", "tarih": "2026-10-01", "url": "https://kap.org.tr/1", "etki": "olumlu"},
        {"baslik": "Uydurma", "tarih": "2026-10-02", "url": "https://uydurma.com/x", "etki": "olumlu"},
    ],
    "veri_eksikleri": [],
}
GOOD_EXIT = {
    "karar": "TUT", "olasilik": 65, "ozet": "Henüz bozulmadı.",
    "tut_kosulu": "EMA50 üstünde kalırsa", "haberler": [], "veri_eksikleri": [],
}


def api_message(text_blocks, stop="end_turn", results=True, searches=1,
                input_tokens=1000, output_tokens=500):
    content = []
    if results:
        content.append({"type": "server_tool_use", "id": "srv_1", "name": "web_search",
                        "input": {"query": "x"}})
        content.append({"type": "web_search_tool_result", "tool_use_id": "srv_1", "content": [
            {"type": "web_search_result", "url": "https://kap.org.tr/1", "title": "t",
             "encrypted_content": "e", "page_age": None}]})
    content += [{"type": "text", "text": t} for t in text_blocks]
    return {"id": "msg_1", "type": "message", "role": "assistant", "model": "m",
            "content": content, "stop_reason": stop, "stop_sequence": None,
            "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens,
                      "server_tool_use": {"web_search_requests": searches}}}


def analyst_with(tmp_path, responses, **cfg_kw):
    import anthropic

    bodies = []

    def handler(request):
        body = json.loads(request.content)
        bodies.append(body)
        if request.url.path.endswith("/count_tokens"):
            return httpx.Response(200, json={"input_tokens": 1500})
        return httpx.Response(200, json=responses.pop(0))

    client = anthropic.Anthropic(
        api_key="x", max_retries=0,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    cfg = BotConfig(db_path=str(tmp_path / "a.db"), ai_enabled=True, **cfg_kw)
    return Analyst(cfg, Store(cfg.db_path), client, StrategyConfig()), bodies


def sent_bodies(bodies):
    """count_tokens çağrılarını ayıklar; sadece gerçek istekler kalır."""
    return [b for b in bodies if "max_tokens" in b]


def frame_for(price=100.0, n=300):
    d = uptrend(end=date(2026, 10, 30), n=n)
    d = d * (price / float(d["close"].iloc[-1]))
    d["volume"] = 2e6
    return d


def review(analyst, price=100.0, force=False):
    d = frame_for(price)
    xu = pd.Series(np.linspace(1000, 1500, len(d)), index=d.index)
    return analyst.review("AAA", at(MONDAY, "10:30"), price, d, d.tail(10), xu, force=force)


def review_exit(analyst, price=100.0, force=False):
    d = frame_for(price)
    xu = pd.Series(np.linspace(1000, 1500, len(d)), index=d.index)
    position = {"entry": price * 1.05, "stop": price * 0.9, "qty": 100, "remaining_qty": 60}
    return analyst.review_exit("AAA", at(MONDAY, "18:45"), price, d, xu, position, force=force)


def test_ai_request_is_blind_and_uses_web_search(tmp_path):
    payload = json.dumps(GOOD, ensure_ascii=False)
    analyst, bodies = analyst_with(tmp_path, [api_message(["Analiz.\n", payload[:30], payload[30:]])])
    r = review(analyst)
    assert r.decision == AI_AL and r.olasilik == 84
    body = sent_bodies(bodies)[0]
    assert body["tools"][0]["type"] == "web_search_20250305"
    assert "kap.org.tr" in body["tools"][0]["allowed_domains"]
    assert body["tools"][0]["max_uses"] == 2
    assert body["max_tokens"] == 800
    # SADECE metin bloklari ve sistem promptu taranir; base64 gorsel verisi
    # tesadufen her dizgiyi icerebilir.
    text = " ".join(
        c["text"] for c in body["messages"][0]["content"] if c["type"] == "text"
    ).lower() + " " + body["system"].lower()
    for forbidden in ("skor", "score", "eminlik", "puan", "70/100"):
        assert forbidden not in text, forbidden
    assert sum(1 for c in body["messages"][0]["content"] if c["type"] == "image") == 2


def test_ai_drops_unverified_news_urls(tmp_path):
    analyst, _ = analyst_with(tmp_path, [api_message([json.dumps(GOOD)])])
    r = review(analyst)
    assert [n["url"] for n in r.news] == ["https://kap.org.tr/1"]
    assert any("doğrulanamadı" in m for m in r.missing)


def test_ai_al_without_search_results_becomes_alma(tmp_path):
    analyst, _ = analyst_with(tmp_path, [api_message([json.dumps(GOOD)], results=False, searches=0)])
    r = review(analyst)
    assert r.decision == AI_ALMA
    assert any("haber kontrolü" in m for m in r.missing)


def test_ai_invalid_output_gets_one_toolless_fix_call(tmp_path):
    """Geçersiz JSON'da ARAÇSIZ tek düzeltme çağrısı; web araması tekrarlanmaz."""
    analyst, bodies = analyst_with(tmp_path, [api_message(["JSON yok"]), api_message(['{"karar": "AL"}'])])
    r = review(analyst)
    assert r.decision == AI_YOK
    calls = sent_bodies(bodies)
    assert len(calls) == 2
    assert "tools" in calls[0] and calls[0]["tools"]
    assert not calls[1].get("tools")  # düzeltme çağrısı araçsız
    assert calls[1]["max_tokens"] == 500


def test_ai_retry_success_after_invalid_output(tmp_path):
    analyst, _ = analyst_with(tmp_path, [api_message(["bozuk"]), api_message([json.dumps(GOOD)])])
    assert review(analyst).decision == AI_AL


def test_ai_pause_turn_continues_conversation(tmp_path):
    analyst, bodies = analyst_with(
        tmp_path, [api_message(["Arıyorum"], stop="pause_turn"), api_message([json.dumps(GOOD)])]
    )
    r = review(analyst)
    assert r.decision == AI_AL
    assert sent_bodies(bodies)[1]["messages"][1]["role"] == "assistant"


def test_ai_cost_cap_stops_pause_turn_continuation(tmp_path):
    """İlk tur bütçenin yarısını geçerse ikinci tur yapılmaz → AI_YOK."""
    analyst, bodies = analyst_with(
        tmp_path,
        [api_message(["Arıyorum"], stop="pause_turn", input_tokens=200_000, output_tokens=50_000)],
        ai_max_cost_per_review_usd=0.05,
    )
    r = review(analyst)
    assert r.decision == AI_YOK and "maliyet" in r.error
    assert len(sent_bodies(bodies)) == 1


def test_ai_same_day_reuses_review_and_force_refreshes(tmp_path):
    analyst, bodies = analyst_with(tmp_path, [api_message([json.dumps(GOOD)]), api_message([json.dumps(GOOD)])])
    review(analyst)
    second = review(analyst)
    assert second.reused and len(sent_bodies(bodies)) == 1
    review(analyst, force=True)
    assert len(sent_bodies(bodies)) == 2


def test_ai_budget_stops_calls(tmp_path):
    analyst, bodies = analyst_with(tmp_path, [], ai_monthly_budget_usd=1.0)
    analyst.store.add_review("BBB", MONDAY, AI_AL, 50, {}, 0, 0, 0, 5.0, at(MONDAY, "10:00"))
    r = review(analyst)
    assert r.decision == AI_YOK and "bütçe" in r.error and sent_bodies(bodies) == []


def test_ai_records_haiku_cost_and_metadata(tmp_path):
    analyst, _ = analyst_with(tmp_path, [api_message([json.dumps(GOOD)], searches=2)])
    review(analyst)
    expected = 1000 / 1e6 * 0.10 + 500 / 1e6 * 0.50 + 2 * 0.01
    assert analyst.store.ai_month_cost(MONDAY) == pytest.approx(expected)
    row = analyst.store.review_on("AAA", MONDAY, kind="entry")
    assert row["model"] == "claude-haiku-5-5" and row["kind"] == "entry"
    assert row["request_count"] == 1


# --- satış incelemesi ---


def test_exit_review_returns_sat_tut_and_one_search(tmp_path):
    analyst, bodies = analyst_with(tmp_path, [api_message([json.dumps(GOOD_EXIT)])])
    r = review_exit(analyst)
    assert r.decision == AI_TUT and r.hold_condition
    body = sent_bodies(bodies)[0]
    assert body["tools"][0]["max_uses"] == 1  # satışta en fazla 1 arama
    assert sum(1 for c in body["messages"][0]["content"] if c["type"] == "image") == 1
    text = " ".join(
        c["text"] for c in body["messages"][0]["content"] if c["type"] == "text"
    ).lower()
    for forbidden in ("skor", "eminlik"):
        assert forbidden not in text


def test_exit_review_failure_defaults_to_sat(tmp_path):
    analyst, _ = analyst_with(tmp_path, [api_message(["bozuk"]), api_message(["yine bozuk"])])
    assert review_exit(analyst).decision == AI_SAT


def test_exit_review_budget_blocked_defaults_to_sat(tmp_path):
    analyst, bodies = analyst_with(tmp_path, [], ai_monthly_budget_usd=1.0)
    analyst.store.add_review("BBB", MONDAY, AI_AL, 50, {}, 0, 0, 0, 5.0, at(MONDAY, "10:00"))
    r = review_exit(analyst)
    assert r.decision == AI_SAT and sent_bodies(bodies) == []


def test_entry_and_exit_reviews_are_tracked_separately(tmp_path):
    analyst, bodies = analyst_with(
        tmp_path, [api_message([json.dumps(GOOD)]), api_message([json.dumps(GOOD_EXIT)])]
    )
    review(analyst)
    review_exit(analyst)
    assert len(sent_bodies(bodies)) == 2  # aynı gün ama farklı tür
    assert analyst.store.review_on("AAA", MONDAY, kind="entry")["decision"] == AI_AL
    assert analyst.store.review_on("AAA", MONDAY, kind="exit")["decision"] == AI_TUT


@pytest.mark.parametrize("change", [
    {"karar": "BELKI"}, {"karar": "BEKLE"}, {"olasilik": 200}, {"olasilik": "yuksek"},
    {"ozet": ""}, {"riskler": []}, {"olasilik": 40},
])
def test_validate_entry_rejects_bad_payload(change):
    with pytest.raises(ValueError):
        validate_entry({**GOOD, **change})


@pytest.mark.parametrize("change", [
    {"karar": "AL"}, {"olasilik": -1}, {"ozet": ""}, {"tut_kosulu": ""},
])
def test_validate_exit_rejects_bad_payload(change):
    with pytest.raises(ValueError):
        validate_exit({**GOOD_EXIT, **change})


def test_extract_json_picks_decision_object():
    assert extract_json('Örnek {"a": 1} sonra ```json\n{"karar": "AL"}\n```')["karar"] == "AL"


def test_yahoo_wrapper_handles_timezone_and_closed_bars(monkeypatch):
    idx = pd.date_range("2026-10-26 00:00", periods=5, freq="B", tz="Europe/Istanbul")
    daily = pd.DataFrame({"Open": 1.0, "High": 1.2, "Low": 0.9, "Close": [1.0, 1.1, 1.0, 1.1, 1.2], "Volume": 100.0}, index=idx)
    i15 = pd.date_range("2026-10-30 10:00", periods=4, freq="15min", tz="Europe/Istanbul").tz_convert("UTC")
    intraday = pd.DataFrame({"Open": [1, 2, 3, 4.0], "High": [2, 3, 4, 5.0], "Low": [0.5, 1, 2, 3.0], "Close": [2, 3, 4, 4.5], "Volume": 10.0}, index=i15)

    class Ticker:
        def __init__(self, symbol):
            self.symbol = symbol

        def history(self, **kw):
            return intraday if kw.get("interval") == "15m" else daily

    monkeypatch.setattr("bot_v4.market.yf.Ticker", Ticker)
    m = YahooMarket(retries=0)
    day = date(2026, 10, 30)
    assert m.daily("AAA", day).index[-1] == pd.Timestamp("2026-10-29")
    assert m.daily("AAA", day, include_today=True).index[-1] == pd.Timestamp(day)
    bar = m.intraday("AAA", day)
    assert (bar.open, bar.high, bar.low, bar.close, bar.volume, bar.recent_low) == (1.0, 5.0, 0.5, 4.5, 40.0, 2.0)
    with pytest.raises(StaleData):
        m.daily("AAA", date(2026, 11, 2), include_today=True)
