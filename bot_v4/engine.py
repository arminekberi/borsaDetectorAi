"""Tarama, sinyal, stop takibi ve kağıt portföy mantığı."""

from __future__ import annotations

import json
import logging
import threading
from datetime import date, datetime
from typing import Any, Callable, Dict, List, Optional, Tuple

import pandas as pd

from bot_v4 import messages as msg
from bot_v4 import session
from bot_v4.analyst import AI_AL, AI_ALMA, AI_BEKLE, AI_YOK, Analyst, Review
from bot_v4.market import StaleData
from bot_v4.settings import BotConfig
from bot_v4.store import Store
from strategy_v4.config import StrategyConfig
from strategy_v4.live import Evaluation, evaluate
from strategy_v4.sizing import initial_stop, max_entry_price, position_quantity

logger = logging.getLogger("bot_v4.engine")
Send = Callable[[str], None]


class Engine:
    def __init__(self, cfg: BotConfig, strategy: StrategyConfig, store: Store, market: Any, analyst: Analyst, send: Send, sectors: Dict[str, str]):
        self.cfg = cfg
        self.strategy = strategy
        self.store = store
        self.market = market
        self.analyst = analyst
        self.send = send
        self.sectors = sectors
        self.lock = threading.RLock()
        self.last_evaluations: Dict[str, Evaluation] = {}

    @property
    def mode(self) -> str:
        return self.cfg.active_mode

    # Değerlendirme
    def _index(self, day: date, include_today: bool, last: Optional[float] = None) -> pd.Series:
        series = self.market.xu100(day, include_today=include_today)
        if last is not None:
            series = series.copy()
            series.loc[pd.Timestamp(day)] = last
        return series

    def evaluate_all(self, now: datetime, symbols: List[str], provisional: bool) -> Tuple[Dict[str, Evaluation], List[str], Dict[str, float]]:
        day = now.date()
        index_last = self._safe_index_last(day) if provisional else None
        xu = self._index(day, not provisional, index_last)
        out: Dict[str, Evaluation] = {}
        errors: List[str] = []
        stale: List[str] = []
        prices: Dict[str, float] = {}
        for symbol in symbols:
            try:
                history = self.market.daily(symbol, day, include_today=not provisional)
                bar = None
                if provisional:
                    bar = self.market.intraday(symbol, day)
                    if bar is None:
                        errors.append(symbol)
                        continue
                    prices[symbol] = bar.close
                ev = evaluate(symbol, history, xu, self.strategy, bar.as_dict() if bar else None)
                if ev is None:
                    errors.append(symbol)
                    continue
                out[symbol] = ev
                prices.setdefault(symbol, ev.close)
            except StaleData:
                stale.append(symbol)
            except Exception:
                logger.exception("%s değerlendirilemedi", symbol)
                errors.append(symbol)
        if stale and len(stale) * 2 >= len(symbols):
            raise StaleData(f"{len(stale)} hissenin kapanış verisi henüz yok")
        return out, errors + stale, prices

    def _safe_index_last(self, day: date) -> Optional[float]:
        try:
            return self.market.index_last(day)
        except Exception:
            logger.exception("Endeks fiyatı alınamadı")
            return None

    # Portföy
    def equity(self, mode: str, prices: Dict[str, float]) -> float:
        value = self.cfg.portfolio_try + self.store.realized_pnl(mode)
        for p in self.store.open_positions(mode):
            last = prices.get(p["symbol"], p["entry"])
            value += (last - p["entry"]) * p["qty"] - p["entry"] * p["qty"] * self.strategy.cost_per_side
        return value

    def _cash(self, mode: str) -> float:
        cost = self.strategy.cost_per_side
        cash = self.cfg.portfolio_try + self.store.realized_pnl(mode)
        for p in self.store.open_positions(mode):
            cash -= p["entry"] * p["qty"] * (1.0 + cost)
        return max(cash, 0.0)

    def _brake_on(self, now: datetime, equity: float) -> bool:
        key = f"month_equity:{self.mode}:{now:%Y-%m}"
        start = self.store.get(key)
        if start is None:
            self.store.set(key, str(equity))
            return False
        return equity / float(start) - 1.0 <= self.strategy.monthly_brake

    def _block_reason(self, symbol: str, now: datetime, al_today: int, equity: float) -> Optional[str]:
        positions = self.store.open_positions(self.mode)
        if self._brake_on(now, equity):
            return "aylık kayıp freni açık"
        if len(positions) >= self.strategy.max_positions:
            return "portföy dolu"
        if al_today >= self.strategy.max_new_entries_per_day:
            return "günlük yeni giriş sınırı doldu"
        sector = self.sectors.get(symbol, "Unknown")
        if sector != "Unknown":
            same = sum(1 for p in positions if self.sectors.get(p["symbol"]) == sector)
            if same >= self.strategy.max_per_sector:
                return "sektör sınırı doldu"
        return None

    def _pnl(self, position: Dict[str, Any], price: float) -> float:
        cost = self.strategy.cost_per_side
        qty, entry = position["qty"], position["entry"]
        return (price - entry) * qty - cost * (entry + price) * qty

    def _open(self, candidate_id: int, symbol: str, mode: str, price: float, atr: float, qty: int, decision: str, now: datetime) -> Optional[int]:
        if qty <= 0 or self.store.open_position_for(symbol, mode):
            return None
        return self.store.open_position(
            symbol=symbol, mode=mode, entry=price, qty=qty, stop=initial_stop(price, atr, self.strategy), atr=atr,
            candidate_id=candidate_id, decision=decision, opened_at=now.isoformat(),
        )

    # Sinyal işleme
    def _process(self, evaluations: Dict[str, Evaluation], prices: Dict[str, float], now: datetime) -> Tuple[List[str], List[str]]:
        day = now.date()
        fresh: List[str] = []
        blocked: List[str] = []
        ranked = sorted(
            (e for e in evaluations.values() if e.eligible and e.score >= self.strategy.buy_score),
            key=lambda e: (-e.score, -e.rel_12_1),
        )
        equity = self.equity(self.mode, prices)
        for ev in ranked:
            symbol = ev.symbol
            if self.store.open_position_for(symbol, self.mode) or self.store.active_for(symbol) or self.store.candidate_today(symbol, day):
                continue
            al_today = self.store.count_al_today(day)
            reason = self._block_reason(symbol, now, al_today, equity)
            reference = prices.get(symbol, ev.close)
            limit = max_entry_price(reference, ev.atr, self.strategy)
            stop = initial_stop(reference, ev.atr, self.strategy)
            cash = self._cash(self.mode)
            qty = position_quantity(equity, reference, stop, ev.adv_try, cash, self.strategy)
            valid_until = day if ev.provisional else session.add_sessions(day, self.strategy.entry_valid_sessions)
            base = dict(
                symbol=symbol, signal_date=day.isoformat(), stage="provisional" if ev.provisional else "confirmed", score=ev.score,
                breakdown={"trend": ev.trend_pts, "guc": ev.momentum_pts, "hacim": ev.volume_pts, "mum": ev.candle_pts, "mum_adlari": ev.patterns},
                reference=reference, atr=ev.atr, adv=ev.adv_try, max_entry=limit, stop=stop, qty=qty, valid_until=valid_until.isoformat(),
                created_at=now.isoformat(),
            )
            if reason:
                self.store.add_candidate(status="blocked", decision="BLOKE", note=reason, **base)
                blocked.append(f"{symbol} ({ev.score:.0f}): {reason}")
                continue

            review = self._review(symbol, now, reference, include_today=not ev.provisional)
            cid = self.store.add_candidate(status="active", decision=review.decision, note=review.summary[:300], **base)
            valid_text = "bugün kapanışa kadar" if ev.provisional else f"{self.strategy.entry_valid_sessions} seans geçerli"
            view = {"max_entry": limit, "stop": stop, "qty": qty, "valid_text": valid_text}
            self.send(msg.signal_message(ev, review, view, self.cfg.paper_mode))
            fresh.append(f"{symbol} {ev.score:.0f}: {review.decision}")
            if ev.provisional:
                self._fill_candidate(cid, reference, now)
        return fresh, blocked

    def _review(self, symbol: str, now: datetime, last: float, include_today: bool, force: bool = False) -> Review:
        day = now.date()
        try:
            daily = self.market.daily(symbol, day, include_today=include_today)
            hourly = self.market.hourly(symbol, day)
            xu = self.market.xu100(day, include_today=include_today)
            return self.analyst.review(symbol, now, last, daily, hourly, xu, force=force)
        except Exception as exc:
            logger.exception("%s AI incelemesi başarısız", symbol)
            return Review(decision=AI_YOK, error=str(exc)[:120])

    def _fill_candidate(self, cid: int, price: float, now: datetime) -> None:
        """AL adayını kağıt portföye, diğerlerini karşılaştırma için gölge portföye alır."""
        cand = self.store.candidate(cid)
        if cand is None or cand["status"] != "active" or price > cand["max_entry"]:
            return
        if cand["decision"] == AI_AL:
            if self.cfg.paper_mode:
                pid = self._open(cid, cand["symbol"], "paper", price, cand["atr"], cand["qty"], AI_AL, now)
                if pid:
                    self.store.update_candidate(cid, status="filled")
                    return
        else:
            self._open(cid, cand["symbol"], "shadow", price, cand["atr"], cand["qty"], cand["decision"], now)
            self.store.update_candidate(cid, status="filled")

    # Zamanlanmış işler
    def scan_intraday(self, now: datetime) -> None:
        with self.lock:
            day = now.date()
            symbols = self.store.watchlist()
            evaluations, errors, prices = self.evaluate_all(now, symbols, provisional=True)
            self.last_evaluations = evaluations
            for p in self.store.open_positions():
                if p["symbol"] not in prices:
                    bar = self.market.intraday(p["symbol"], day)
                    if bar:
                        prices[p["symbol"]] = bar.close
            fresh, blocked = self._process(evaluations, prices, now)
            near = sorted(
                (e for e in evaluations.values() if e.eligible and 55 <= e.score < self.strategy.buy_score),
                key=lambda e: -e.score,
            )[:3]
            index_change = self._index_change(day)
            self.send(msg.scan_report(
                now.strftime("%H:%M"), index_change, msg.position_lines(self.store.open_positions(self.mode), prices),
                [f"{e.symbol}: {e.score:.0f}" for e in near], fresh + blocked, errors,
            ))

    def _index_change(self, day: date) -> Optional[float]:
        try:
            last = self._safe_index_last(day)
            previous = self.market.xu100(day, include_today=False)
            return last / float(previous.iloc[-1]) - 1.0 if last else None
        except Exception:
            logger.exception("Endeks değişimi alınamadı")
            return None

    def monitor(self, now: datetime) -> None:
        with self.lock:
            day = now.date()
            positions = self.store.open_positions()
            candidates = self.store.active_candidates()
            symbols = sorted({p["symbol"] for p in positions} | {c["symbol"] for c in candidates})
            bars = {}
            for symbol in symbols:
                try:
                    bars[symbol] = self.market.intraday(symbol, day)
                except Exception:
                    logger.exception("%s fiyat takibi başarısız", symbol)
            for position in positions:
                bar = bars.get(position["symbol"])
                if bar is not None:
                    self._check_position(position, bar, now)
            for cand in candidates:
                bar = bars.get(cand["symbol"])
                if bar is not None:
                    self._check_candidate(cand, bar.close, now)

    def _check_position(self, p: Dict[str, Any], bar: Any, now: datetime) -> None:
        managed = p["mode"] in ("paper", "shadow")
        if p["exit_flag"] and managed and p["opened_at"][:10] != now.date().isoformat():
            price = bar.open if bar.date == now.date() else bar.close
            self._close(p, price, now, p["exit_flag"])
            return
        if bar.recent_low > p["stop"]:
            return
        if managed:
            price = min(p["stop"], bar.close) * (1.0 - self.strategy.stop_slippage)
            self._close(p, price, now, "stop")
        elif not p["stop_alerted"]:
            self.store.update_position(p["id"], stop_alerted=1)
            self.send(f"🔴 SAT | STOP\n{p['symbol']}: fiyat {msg.tl(bar.close)} TL, stop {msg.tl(p['stop'])} TL.\nSattıysan: /sattim {p['symbol']} FIYAT")

    def _close(self, p: Dict[str, Any], price: float, now: datetime, reason: str) -> None:
        pnl = self._pnl(p, price)
        self.store.close_position(p["id"], price, now, reason, pnl)
        if p["mode"] == "paper":
            names = {"stop": "stop", "score": "haftalık skor düşüşü"}
            self.send(
                f"[KAĞIT] 🔴 SAT | {names.get(reason, reason)}\n{p['symbol']}: {msg.tl(price)} TL "
                f"({msg.pct(price / p['entry'] - 1.0)}) | Kâr/zarar {msg.tl(pnl)} TL"
            )

    def _check_candidate(self, cand: Dict[str, Any], last: float, now: datetime) -> None:
        day_text = now.date().isoformat()
        if cand["valid_until"] < day_text:
            self.store.update_candidate(cand["id"], status="expired")
            return
        if cand["stage"] != "confirmed":
            return
        if last <= cand["max_entry"]:
            self._fill_candidate(cand["id"], last, now)
        elif cand["decision"] == AI_AL and not cand["limit_alerted"]:
            self.store.update_candidate(cand["id"], limit_alerted=1)
            self.send(f"⏫ {cand['symbol']}: fiyat {msg.tl(last)} TL, alım sınırı {msg.tl(cand['max_entry'])} TL aşıldı. Kovalamayın.")

    def close_job(self, now: datetime) -> None:
        """Günlük barlar kesinleşince çalışır. Veri gecikirse StaleData fırlatır ve tekrar denenir."""
        with self.lock:
            day = now.date()
            self.market.clear_cache()
            symbols = sorted(set(self.store.watchlist()) | {p["symbol"] for p in self.store.open_positions()})
            evaluations, errors, prices = self.evaluate_all(now, symbols, provisional=False)
            self.last_evaluations = evaluations

            for cand in self.store.settle_list(day):
                self._settle(cand, evaluations.get(cand["symbol"]), now)
            fresh, blocked = self._process({s: e for s, e in evaluations.items() if s in self.store.watchlist()}, prices, now)

            exits: List[str] = []
            if session.is_last_trading_day_of_week(day):
                exits = self._weekly_exits(evaluations, now)

            index = self.market.xu100(day, include_today=True)
            for mode in ("paper", "real", "shadow"):
                if self.store.open_positions(mode) or self.store.closed_positions(mode):
                    self.store.record_equity(day, mode, self.equity(mode, prices), float(index.iloc[-1]))
            self.send(self._daily_report(now, evaluations, prices, fresh, blocked, exits, errors, index))

    def _settle(self, cand: Dict[str, Any], ev: Optional[Evaluation], now: datetime) -> None:
        day_text = now.date().isoformat()
        if cand["stage"] == "confirmed":
            if cand["valid_until"] <= day_text:
                self.store.update_candidate(cand["id"], status="expired")
            return
        valid = ev is not None and ev.eligible and ev.score >= self.strategy.buy_score
        tag = "[KAĞIT] " if self.cfg.paper_mode else ""
        if not valid:
            filled = cand["status"] == "filled"
            self.store.update_candidate(cand["id"], stage="confirmed", status="filled" if filled else "cancelled")
            if cand["decision"] == AI_AL:
                score = f"{ev.score:.0f}" if ev else "yok"
                self.send(f"{tag}❎ SİNYAL İPTAL\n{cand['symbol']}: kapanış skoru {score}. Almadıysan alma. Aldıysan pozisyon kurala göre devam eder.")
            return
        if cand["status"] == "filled":
            self.store.update_candidate(cand["id"], stage="confirmed")
            if cand["decision"] == AI_AL:
                self.send(f"{tag}✅ SİNYAL TEYİT\n{cand['symbol']}: kapanış skoru {ev.score:.0f}.")
            return
        limit = max_entry_price(ev.close, ev.atr, self.strategy)
        until = session.add_sessions(now.date(), self.strategy.entry_valid_sessions)
        self.store.update_candidate(cand["id"], stage="confirmed", score=ev.score, reference=ev.close, atr=ev.atr, max_entry=limit,
                                    stop=initial_stop(ev.close, ev.atr, self.strategy), valid_until=until.isoformat())
        if cand["decision"] == AI_AL:
            self.send(f"{tag}✅ SİNYAL TEYİT\n{cand['symbol']}: kapanış skoru {ev.score:.0f}. Alım sınırı {msg.tl(limit)} TL, 2 seans geçerli.")

    def _weekly_exits(self, evaluations: Dict[str, Evaluation], now: datetime) -> List[str]:
        out: List[str] = []
        for p in self.store.open_positions():
            ev = evaluations.get(p["symbol"])
            if ev is None or self.strategy.exit_score is None or ev.score >= self.strategy.exit_score or p["exit_flag"]:
                continue
            self.store.update_position(p["id"], exit_flag="score")
            if p["mode"] != "shadow":
                prefix = "[KAĞIT] " if p["mode"] == "paper" else ""
                self.send(f"{prefix}🔴 SAT | Haftalık skor {ev.score:.0f}\n{p['symbol']}: sonraki seans açılışında çık.")
                out.append(p["symbol"])
        return out

    def _daily_report(self, now, evaluations, prices, fresh, blocked, exits, errors, index) -> str:
        equity = self.equity(self.mode, prices)
        start = self.cfg.portfolio_try
        change = float(index.iloc[-1] / index.iloc[-2] - 1.0)
        lines = [
            f"🌙 GÜN SONU {now:%d.%m.%Y}" + (" [KAĞIT]" if self.cfg.paper_mode else ""),
            f"XU100: {msg.pct(change)}",
            f"Portföy: {msg.tl(equity)} TL ({msg.pct(equity / start - 1.0)})",
        ]
        rows = msg.position_lines(self.store.open_positions(self.mode), prices)
        if rows:
            lines += ["", "Pozisyonlar", *rows]
        if fresh or blocked:
            lines += ["", "Sinyaller", *fresh, *blocked]
        if exits:
            lines += ["", "SAT: " + ", ".join(exits)]
        ranked = sorted(evaluations.values(), key=lambda e: -e.score)[:5]
        if ranked:
            lines += ["", "En yüksek skorlar", *[f"{e.symbol}: {e.score:.0f}" + ("" if e.eligible else f" ({e.reasons[0]})") for e in ranked]]
        if errors:
            lines += ["", "Veri alınamayanlar: " + ", ".join(errors)]
        return "\n".join(lines)

    def sunday_job(self, now: datetime) -> None:
        """Hafta sonu haberleriyle geçerli AL adaylarını yeniden inceler ve özet gönderir."""
        with self.lock:
            for cand in self.store.active_candidates():
                if cand["decision"] != AI_AL:
                    continue
                review = self._review(cand["symbol"], now, cand["reference"], include_today=False, force=True)
                if review.decision == AI_AL:
                    continue
                if review.decision == AI_YOK:
                    self.send(f"⚠️ {cand['symbol']}: Pazar AI incelemesi yapılamadı ({review.error}). Haberleri elle kontrol et.")
                    continue
                self.store.update_candidate(cand["id"], decision=review.decision, note=review.summary[:300])
                self.send(f"{'🟡' if review.decision == AI_BEKLE else '⛔'} {cand['symbol']}: hafta sonu incelemesi {review.decision}.\n{review.summary}")
            self.send(self.performance_text(now))

    # Sorgular
    def performance_text(self, now: datetime) -> str:
        lines = [f"📊 PERFORMANS {now:%d.%m.%Y}"]
        names = {"paper": "Kağıt (kural + AI)", "real": "Gerçek", "shadow": "Gölge (AI'ın almadıkları)"}
        for mode, name in names.items():
            closed = self.store.closed_positions(mode)
            opened = self.store.open_positions(mode)
            if not closed and not opened:
                continue
            pnl = sum(p["pnl"] for p in closed)
            wins = sum(1 for p in closed if p["pnl"] > 0)
            ratio = f"%{wins / len(closed) * 100:.0f}" if closed else "yok"
            lines.append(f"{name}: {len(closed)} kapalı, {len(opened)} açık | Kazanma {ratio} | Kâr/zarar {msg.tl(pnl)} TL")
        rows = self.store.equity_rows(self.mode)
        if len(rows) >= 2 and rows[0]["xu100"] and rows[-1]["xu100"]:
            mine = rows[-1]["equity"] / self.cfg.portfolio_try - 1.0
            bench = rows[-1]["xu100"] / rows[0]["xu100"] - 1.0
            lines.append(f"Portföy {msg.pct(mine)} | Aynı dönem XU100 {msg.pct(bench)}")
        ai = self.store.ai_summary()
        lines.append(f"AI kararları: {json.dumps(ai['decisions'], ensure_ascii=False)} | Tahmini maliyet ${ai['cost_usd']:.2f}")
        return "\n".join(lines)
