"""Bot v4 birim testleri: Telegram, Yahoo ve Claude taklit edilir."""

import json
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest

from bot_v4 import commands, session
from bot_v4.analyst import AI_AL, AI_ALMA, AI_BEKLE, AI_YOK, Analyst, Review, extract_json, validate_payload
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

    def hourly(self, symbol):
        return self.frames[symbol].tail(10)

    def validate_symbol(self, symbol, as_of):
        return symbol in self.valid


class FakeAnalyst:
    def __init__(self, decision=AI_AL):
        self.decision = decision
        self.calls = []

    def review(self, symbol, now, last, daily, hourly, xu100, force=False):
        self.calls.append((symbol, now, force))
        return Review(decision=self.decision, confidence=70.0, summary="özet", wait_condition="koşul", error="hata" if self.decision == AI_YOK else "",
                      plan={"giris_alt": last * .99, "giris_ust": last, "stop": last * .9, "hedef_1": last * 1.1, "hedef_2": last * 1.2},
                      counter_thesis=["risk"])


def make_engine(tmp_path, frames=None, decision=AI_AL, paper=True, watch=None):
    frames = frames or {"AAA": uptrend(seed=1)}
    cfg = BotConfig(bot_token="t", chat_id="1", db_path=str(tmp_path / "b.db"), paper_mode=paper, ai_enabled=True)
    store = Store(cfg.db_path)
    sent = []
    market = FakeMarket(frames)
    engine = Engine(cfg, StrategyConfig(min_adv_try=1e6), store, market, FakeAnalyst(decision), sent.append, {})
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
    assert any("🟢 AL" in m and "[KAĞIT]" in m and "ÖN SİNYAL" in m for m in sent)
    p = engine.store.open_positions("paper")
    assert len(p) == 1 and p[0]["decision"] == AI_AL and p[0]["qty"] > 0


def test_same_day_scan_does_not_duplicate_or_call_ai_twice(tmp_path):
    engine, sent = make_engine(tmp_path)
    engine.scan_intraday(at(MONDAY, "10:30"))
    count = len([m for m in sent if "🟢 AL" in m])
    engine.scan_intraday(at(MONDAY, "12:00"))
    assert len([m for m in sent if "🟢 AL" in m]) == count == 1
    assert len(engine.analyst.calls) == 1


@pytest.mark.parametrize("decision,mark", [(AI_BEKLE, "🟡 BEKLE"), (AI_ALMA, "⛔ ALMA"), (AI_YOK, "KURAL AL, AI YOK")])
def test_non_al_decisions_go_to_shadow_only(tmp_path, decision, mark):
    engine, sent = make_engine(tmp_path, decision=decision)
    engine.scan_intraday(at(MONDAY, "10:30"))
    assert any(mark in m for m in sent)
    assert engine.store.open_positions("paper") == []
    assert len(engine.store.open_positions("shadow")) == 1


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
                                     atr=2.0, adv=1e7, max_entry=101.0, stop=92.0, qty=10, valid_until=MONDAY.isoformat(), decision=AI_AL, created_at="x")
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
                                     atr=2.0, adv=1e7, max_entry=101.0, stop=92.0, qty=10, valid_until=date(2026, 11, 4).isoformat(), decision=AI_AL, created_at="x")
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
    engine.store.open_position(symbol="AAA", mode="paper", entry=100.0, qty=10, stop=1.0, atr=2.0, opened_at=at(MONDAY, "10:30").isoformat())
    engine.close_job(at(friday, "18:45"))
    assert any("SAT | Haftalık skor" in m for m in sent)
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
    assert "Bağlayıcı stop" in text
    assert "zaten" in commands.aldim(engine, ["AAA", "150", "10"], now)
    p = engine.store.open_position_for("AAA", "real")
    assert p["entry"] == 150.5 and p["stop"] < 150.5
    done = commands.sattim(engine, ["AAA", "160"], now)
    assert "kapatıldı" in done and engine.store.closed_positions("real")[0]["pnl"] > 0
    assert "açık gerçek pozisyon yok" in commands.sattim(engine, ["AAA", "160"], now)
    assert "Kullanım" in commands.aldim(engine, ["AAA", "-5", "10"], now)


def test_scheduler_runs_each_job_once_and_survives_restart(tmp_path):
    engine, sent = make_engine(tmp_path)
    calls = []
    engine.scan_intraday = lambda now: calls.append(("scan", now.strftime("%H:%M")))
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
    engine.scan_intraday = lambda now: calls.append(now)
    engine.monitor = lambda now: calls.append(now)
    engine.close_job = lambda now: calls.append(now)
    sch = Scheduler(engine.cfg, engine)
    for hhmm in ("10:30", "12:00", "14:00", "18:45"):
        sch.tick(at(date(2026, 10, 29), hhmm))
    assert calls == []
    half = date(2026, 10, 28)
    for hhmm in ("13:15", "13:25"):
        sch.tick(at(half, hhmm))
    assert calls == []
    sch.tick(at(half, "13:30"))
    assert len(calls) == 1


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


def test_sunday_rereview_can_downgrade_candidate(tmp_path):
    engine, sent = make_engine(tmp_path, decision=AI_ALMA, paper=False)
    cid = engine.store.add_candidate(symbol="AAA", signal_date="2026-11-06", stage="confirmed", status="active", score=75, breakdown={}, reference=100.0,
                                     atr=2.0, adv=1e7, max_entry=101.0, stop=92.0, qty=10, valid_until="2026-11-10", decision=AI_AL, created_at="x")
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
    "karar": "AL", "guven": 72, "ozet": "Güçlü görünüm.", "grafik": {}, "haberler": [{"baslik": "H", "tarih": "2026-10-01", "url": "https://kap.org.tr/1", "etki": "olumlu"},
                                                                                  {"baslik": "Uydurma", "tarih": "2026-10-02", "url": "https://uydurma.com/x", "etki": "olumlu"}],
    "temel_gorunum": "", "plan": {"giris_alt": 98, "giris_ust": 100, "stop": 90, "hedef_1": 110, "hedef_2": 120, "tutma_suresi_gun": 30},
    "karsi_tez": ["Bilanço riski"], "gecersizlik_kosulu": "90 altı", "bekle_kosulu": "", "veri_eksikleri": [],
}


def api_message(text_blocks, stop="end_turn", results=True, searches=1):
    content = []
    if results:
        content.append({"type": "server_tool_use", "id": "srv_1", "name": "web_search", "input": {"query": "x"}})
        content.append({"type": "web_search_tool_result", "tool_use_id": "srv_1", "content": [
            {"type": "web_search_result", "url": "https://kap.org.tr/1", "title": "t", "encrypted_content": "e", "page_age": None}]})
    content += [{"type": "text", "text": t} for t in text_blocks]
    return {"id": "msg_1", "type": "message", "role": "assistant", "model": "m", "content": content, "stop_reason": stop, "stop_sequence": None,
            "usage": {"input_tokens": 1000, "output_tokens": 500, "server_tool_use": {"web_search_requests": searches}}}


def analyst_with(tmp_path, responses):
    import anthropic

    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json=responses.pop(0))

    client = anthropic.Anthropic(api_key="x", max_retries=0, http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    cfg = BotConfig(db_path=str(tmp_path / "a.db"), ai_enabled=True, ai_monthly_budget_usd=1.0)
    return Analyst(cfg, Store(cfg.db_path), client), bodies


def review(analyst, price=100.0, force=False):
    d = uptrend(end=date(2026, 10, 30), n=300)
    d = d * (price / float(d["close"].iloc[-1]))
    d["volume"] = 2e6
    xu = pd.Series(np.linspace(1000, 1500, 300), index=d.index)
    return analyst.review("AAA", at(MONDAY, "10:30"), price, d, d.tail(10), xu, force=force)


def test_ai_request_is_blind_and_uses_web_search(tmp_path):
    payload = json.dumps(GOOD, ensure_ascii=False)
    analyst, bodies = analyst_with(tmp_path, [api_message(["Analiz.\n", payload[:30], payload[30:]])])
    r = review(analyst)
    assert r.decision == AI_AL and r.confidence == 72
    body = bodies[0]
    assert body["tools"][0]["type"] == "web_search_20250305" and "kap.org.tr" in body["tools"][0]["allowed_domains"]
    text = json.dumps(body["messages"], ensure_ascii=False).lower() + body["system"].lower()
    assert "skor" not in text and "score" not in text
    assert sum(1 for c in body["messages"][0]["content"] if c["type"] == "image") == 2


def test_ai_drops_unverified_news_urls(tmp_path):
    analyst, _ = analyst_with(tmp_path, [api_message([json.dumps(GOOD)])])
    r = review(analyst)
    assert [n["url"] for n in r.news] == ["https://kap.org.tr/1"]
    assert any("doğrulanamadı" in m for m in r.missing)


def test_ai_al_without_search_results_becomes_bekle(tmp_path):
    analyst, _ = analyst_with(tmp_path, [api_message([json.dumps(GOOD)], results=False, searches=0)])
    r = review(analyst)
    assert r.decision == AI_BEKLE and "elle" in r.wait_condition


def test_ai_invalid_output_retries_then_gives_ai_yok(tmp_path):
    analyst, bodies = analyst_with(tmp_path, [api_message(["JSON yok"]), api_message(['{"karar": "AL"}'])])
    r = review(analyst)
    assert r.decision == AI_YOK and len(bodies) == 2


def test_ai_retry_success_after_invalid_output(tmp_path):
    analyst, _ = analyst_with(tmp_path, [api_message(["bozuk"]), api_message([json.dumps(GOOD)])])
    assert review(analyst).decision == AI_AL


def test_ai_pause_turn_continues_conversation(tmp_path):
    analyst, bodies = analyst_with(tmp_path, [api_message(["Arıyorum"], stop="pause_turn"), api_message([json.dumps(GOOD)])])
    r = review(analyst)
    assert r.decision == AI_AL and bodies[1]["messages"][1]["role"] == "assistant"


def test_ai_same_day_reuses_review_and_force_refreshes(tmp_path):
    analyst, bodies = analyst_with(tmp_path, [api_message([json.dumps(GOOD)]), api_message([json.dumps(GOOD)])])
    review(analyst)
    second = review(analyst)
    assert second.reused and len(bodies) == 1
    review(analyst, force=True)
    assert len(bodies) == 2


def test_ai_budget_stops_calls(tmp_path):
    analyst, bodies = analyst_with(tmp_path, [])
    analyst.store.add_review("BBB", MONDAY, AI_AL, 50, {}, 0, 0, 0, 5.0, at(MONDAY, "10:00"))
    r = review(analyst)
    assert r.decision == AI_YOK and "bütçe" in r.error and bodies == []


def test_ai_records_cost(tmp_path):
    analyst, _ = analyst_with(tmp_path, [api_message([json.dumps(GOOD)], searches=2)])
    review(analyst)
    assert analyst.store.ai_month_cost(MONDAY) == pytest.approx(1000 / 1e6 * 3 + 500 / 1e6 * 15 + 0.02)


@pytest.mark.parametrize("change", [
    {"karar": "BELKI"}, {"guven": 200}, {"ozet": ""}, {"karsi_tez": []},
    {"plan": {"giris_alt": 98, "giris_ust": 100, "stop": 99, "hedef_1": 110, "hedef_2": 120}},
    {"plan": {"giris_alt": 200, "giris_ust": 210, "stop": 190, "hedef_1": 220, "hedef_2": 230}},
])
def test_validate_rejects_bad_payload(change):
    with pytest.raises(ValueError):
        validate_payload({**GOOD, **change}, 100.0)


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
