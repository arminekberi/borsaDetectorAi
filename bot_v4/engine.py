"""
Tarama, sinyal, kademeli çıkış takibi ve kağıt portföy mantığı.

Akış (spesifikasyon Bölüm 5-9, v4 tabanı üzerinde):
- Gün içi tarama ve gün sonu kapanış aynı `_process` yolunu kullanır.
- Kural AL adayı → limit kontrolü → AI incelemesi → kod eminlik skorunu hesaplar
  → dört koşulun hepsi sağlanırsa kullanıcıya 🟢 AL gider.
- Eşik altı ve ALMA adayları gölge portföyde izlenir, kullanıcıya gitmez.
- Takip (15 dk): `exits.step` ile stop/TP; kağıt-gölgede kısmi satış uygulanır,
  gerçek pozisyonda sadece bildirilir.
- Haftalık çıkış: zaman stopu AI'sız; skor/EMA50 yumuşak çıkışında AI'a SAT/TUT
  sorulur.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import date, datetime
from typing import Any, Callable, Dict, List, Optional, Tuple

import pandas as pd

from bot_v4 import messages as msg
from bot_v4 import session
from bot_v4.analyst import AI_AL, AI_ALMA, AI_SAT, AI_TUT, AI_YOK, Analyst, Review
from bot_v4.market import StaleData
from bot_v4.settings import BotConfig
from bot_v4.store import Store
from strategy_v4.config import StrategyConfig
from strategy_v4.exits import PositionState, levels_for, step as exit_step, time_exit_due
from strategy_v4.live import Evaluation, evaluate
from strategy_v4.sizing import entry_limit, position_plan, stop_price

logger = logging.getLogger("bot_v4.engine")
Send = Callable[[str], None]
MANAGED_MODES = ("paper", "shadow")


class Engine:
    def __init__(self, cfg: BotConfig, strategy: StrategyConfig, store: Store, market: Any,
                 analyst: Analyst, send: Send, sectors: Dict[str, str]):
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

    # ---------------------------------------------------------------- değerlendirme
    def _index(self, day: date, include_today: bool, last: Optional[float] = None) -> pd.Series:
        series = self.market.xu100(day, include_today=include_today)
        if last is not None:
            series = series.copy()
            series.loc[pd.Timestamp(day)] = last
        return series

    def evaluate_all(self, now: datetime, symbols: List[str], provisional: bool
                     ) -> Tuple[Dict[str, Evaluation], List[str], Dict[str, float]]:
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

    # ---------------------------------------------------------------- portföy
    def _remaining(self, position: Dict[str, Any]) -> int:
        value = position.get("remaining_qty")
        return int(value if value is not None else position["qty"])

    def equity(self, mode: str, prices: Dict[str, float]) -> float:
        value = self.cfg.portfolio_try + self.store.realized_pnl(mode)
        for p in self.store.open_positions(mode):
            qty = self._remaining(p)
            last = prices.get(p["symbol"], p["entry"])
            value += (last - p["entry"]) * qty - p["entry"] * qty * self.strategy.cost_per_side
        return value

    def _cash(self, mode: str) -> float:
        cost = self.strategy.cost_per_side
        cash = self.cfg.portfolio_try + self.store.realized_pnl(mode)
        for p in self.store.open_positions(mode):
            cash -= p["entry"] * self._remaining(p) * (1.0 + cost)
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

    def _pnl(self, position: Dict[str, Any], price: float, qty: Optional[int] = None) -> float:
        cost = self.strategy.cost_per_side
        qty = self._remaining(position) if qty is None else qty
        entry = position["entry"]
        return (price - entry) * qty - cost * (entry + price) * qty

    def _state_of(self, position: Dict[str, Any]) -> PositionState:
        """Veritabanı satırından çıkış durumu kurar."""
        entry = float(position["entry"])
        inf = float("inf")
        return PositionState(
            entry=entry, qty=int(position.get("initial_qty") or position["qty"]),
            remaining=self._remaining(position), stop=float(position["stop"]),
            tp1=float(position["tp1"]) if position.get("tp1") is not None else inf,
            tp2=float(position["tp2"]) if position.get("tp2") is not None else inf,
            tp3=float(position["tp3"]) if position.get("tp3") is not None else inf,
            reached=position.get("reached_level") or "",
            top_price=entry, risk=entry - float(position["stop"]),
        )

    def _open(self, candidate_id: Optional[int], symbol: str, mode: str, price: float,
              atr: float, qty: int, decision: str, now: datetime) -> Optional[int]:
        if qty <= 0 or self.store.open_position_for(symbol, mode):
            return None
        lv = levels_for(price, atr, self.strategy)
        return self.store.open_position(
            symbol=symbol, mode=mode, entry=price, qty=qty, initial_qty=qty, remaining_qty=qty,
            stop=lv["stop"], tp1=lv["tp1"], tp2=lv["tp2"], tp3=lv["tp3"],
            exit_model=self.strategy.exit_model, atr=atr, candidate_id=candidate_id,
            decision=decision, opened_at=now.isoformat(),
        )

    # ---------------------------------------------------------------- sinyal işleme
    def _process(self, evaluations: Dict[str, Evaluation], prices: Dict[str, float],
                 now: datetime, slot: str = "") -> Tuple[List[str], List[str], Dict[str, int]]:
        """
        Tek sinyal yolu. Dönen üçlü: (bildirilenler, bloke olanlar, sayaçlar).

        Kullanıcıya AL yalnızca dört koşulun hepsi sağlanırsa gider
        (spesifikasyon Bölüm 5). Eşik altı ve ALMA gölgede izlenir.
        """
        day = now.date()
        fresh: List[str] = []
        blocked: List[str] = []
        tally = {"esik_alti": 0, "alma": 0, "ai_yok": 0, "al": 0}
        shadow_notes: List[str] = []
        ranked = sorted(
            (e for e in evaluations.values() if e.eligible and e.score >= self.strategy.buy_score),
            key=lambda e: (-e.score, -e.rel_12_1),
        )
        equity = self.equity(self.mode, prices)
        for ev in ranked:
            symbol = ev.symbol
            if (self.store.open_position_for(symbol, self.mode) or self.store.active_for(symbol)
                    or self.store.candidate_today(symbol, day)):
                continue
            al_today = self.store.count_al_today(day)
            reason = self._block_reason(symbol, now, al_today, equity)
            reference = prices.get(symbol, ev.close)
            limit = entry_limit(reference, ev.atr, self.strategy)
            stop = stop_price(reference, ev.atr, self.strategy)
            plan = position_plan(equity, reference, stop, ev.adv_try, self._cash(self.mode), self.strategy)
            valid_until = (day if ev.provisional
                           else session.add_sessions(day, self.strategy.entry_valid_sessions))
            base = dict(
                symbol=symbol, signal_date=day.isoformat(),
                stage="provisional" if ev.provisional else "confirmed", score=ev.score,
                scan_slot=slot or (ev.scan_slot or ""),
                breakdown={"trend": ev.trend_pts, "guc": ev.momentum_pts, "hacim": ev.volume_pts,
                           "mum": ev.candle_pts, "mum_adlari": ev.patterns},
                reference=reference, atr=ev.atr, adv=ev.adv_try, max_entry=limit, stop=stop,
                qty=plan["qty"], valid_until=valid_until.isoformat(), created_at=now.isoformat(),
            )
            if reason:
                self.store.add_candidate(status="blocked", decision="BLOKE", note=reason, **base)
                blocked.append(f"{symbol} ({ev.score:.0f}): {reason}")
                continue

            review = self._review(symbol, now, reference, include_today=not ev.provisional)
            olasilik = float(review.olasilik) if review.olasilik is not None else 0.0
            eminlik = self.cfg.eminlik(ev.score, olasilik)
            notify = self.cfg.passes_four_conditions(ev.score, review.decision, olasilik,
                                                     self.strategy.buy_score)
            cid = self.store.add_candidate(
                status="active", decision=review.decision, note=review.summary[:300],
                ai_olasilik=review.olasilik, eminlik=eminlik, **base,
            )
            view = {"max_entry": limit, "stop": stop, "plan": plan, "eminlik": eminlik,
                    "slot": slot or (ev.scan_slot or ""),
                    "when": now.strftime("%H:%M") if ev.provisional else "",
                    "levels": levels_for(limit, ev.atr, self.strategy),
                    "valid_text": ("bugün seans sonuna kadar" if ev.provisional
                                   else f"{self.strategy.entry_valid_sessions} seans geçerli")}

            if notify:
                tally["al"] += 1
                self.send(msg.buy_message(ev, review, view, self.cfg, self.strategy))
                fresh.append(f"{symbol} {ev.score:.0f}: AL (eminlik {eminlik:.0f})")
            elif review.decision == AI_YOK:
                tally["ai_yok"] += 1
                self.send(msg.ai_missing_message(ev, review, self.cfg.paper_mode))
                fresh.append(f"{symbol} {ev.score:.0f}: AI YOK")
            elif review.decision == AI_ALMA:
                tally["alma"] += 1
                shadow_notes.append(f"{symbol}")
                if self.cfg.notify_alma:
                    self.send(msg.alma_message(ev, review, eminlik, self.cfg.paper_mode))
            else:
                tally["esik_alti"] += 1
                shadow_notes.append(f"{symbol} {eminlik:.0f}")

            # AI_YOK golge portfoye de girmez; golge yalnizca esik alti ve ALMA icindir
            if ev.provisional and review.decision != AI_YOK:
                self._fill_candidate(cid, reference, now, notify)
        tally["golge"] = len(shadow_notes)
        tally["golge_liste"] = shadow_notes  # type: ignore[assignment]
        return fresh, blocked, tally

    def _review(self, symbol: str, now: datetime, last: float, include_today: bool,
                force: bool = False) -> Review:
        day = now.date()
        try:
            daily = self.market.daily(symbol, day, include_today=include_today)
            hourly = self.market.hourly(symbol, day)
            xu = self.market.xu100(day, include_today=include_today)
            return self.analyst.review(symbol, now, last, daily, hourly, xu, force=force)
        except Exception as exc:
            logger.exception("%s AI incelemesi başarısız", symbol)
            return Review(decision=AI_YOK, error=str(exc)[:120])

    def _fill_candidate(self, candidate_id: int, price: float, now: datetime, notify: bool) -> None:
        """Dört koşulu geçen aday kağıt portföye, geçmeyen gölge portföye girer."""
        cand = self.store.candidate(candidate_id)
        if cand is None or cand["status"] != "active" or price > cand["max_entry"]:
            return
        if notify:
            if self.cfg.paper_mode:
                pid = self._open(candidate_id, cand["symbol"], "paper", price, cand["atr"],
                                 cand["qty"], AI_AL, now)
                if pid:
                    self.store.update_candidate(candidate_id, status="filled")
            return
        self._open(candidate_id, cand["symbol"], "shadow", price, cand["atr"], cand["qty"],
                   cand["decision"], now)
        self.store.update_candidate(candidate_id, status="filled")

    # ---------------------------------------------------------------- zamanlanmış işler
    def scan_intraday(self, now: datetime, slot: str = "") -> None:
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
            fresh, blocked, tally = self._process(evaluations, prices, now, slot)
            if not self.cfg.scan_summary:
                return
            near = sorted(
                (e for e in evaluations.values()
                 if e.eligible and 55 <= e.score < self.strategy.buy_score),
                key=lambda e: -e.score,
            )[:3]
            self.send(msg.scan_report(
                now.strftime("%H:%M"), slot, self._index_change(day),
                msg.position_lines(self.store.open_positions(self.mode), prices),
                [f"{e.symbol}: {e.score:.0f}" for e in near],
                fresh + blocked, errors, len(evaluations), tally,
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
        """15 dakikada bir: kademeli stop/TP takibi ve aday takibi."""
        with self.lock:
            day = now.date()
            positions = self.store.open_positions()
            candidates = self.store.active_candidates()
            symbols = sorted({p["symbol"] for p in positions} | {c["symbol"] for c in candidates})
            bars: Dict[str, Any] = {}
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
        """
        Son takip penceresindeki 15m barlardan kurulan aralıkla `exits.step`.

        Gerçek modda pozisyon değiştirilmez, yalnızca bildirilir; kullanıcı
        `/sattim` ile kaydeder. Kağıt ve gölgede kısmi satış uygulanır.
        """
        managed = p["mode"] in MANAGED_MODES
        if p["exit_flag"] and managed and p["opened_at"][:10] != now.date().isoformat():
            price = bar.open if bar.date == now.date() else bar.close
            self._close(p, price, now, p["exit_flag"])
            return

        state = self._state_of(p)
        window_open = getattr(bar, "recent_open", bar.close)
        window_high = getattr(bar, "recent_high", bar.close)
        window_low = getattr(bar, "recent_low", bar.close)
        new_state, events = exit_step(state, window_open, window_high, window_low, self.strategy)
        if not events:
            return

        alerted = {"tp1": "tp1_alerted", "tp2": "tp2_alerted", "tp3": "tp3_alerted"}
        for ev in events:
            flag = alerted.get(ev.kind)
            if flag and p.get(flag):
                continue  # her seviye bir kez bildirilir, yeniden başlatmada tekrar yok
            if ev.kind in ("stop", "stop_gap"):
                if managed:
                    fill = ev.price * (1.0 - self.strategy.stop_slippage)
                    pnl = self._pnl(p, fill, ev.qty)
                    self.store.sell_part(p["id"], "stop", fill, ev.qty, pnl, now)
                    if p["mode"] == "paper":
                        self.send(msg.stop_message(p, fill, pnl, ev.qty, self.cfg.paper_mode))
                elif not p["stop_alerted"]:
                    self.store.update_position(p["id"], stop_alerted=1)
                    self.send(msg.stop_message(p, ev.price, None, ev.qty, False))
                return
            # TP seviyesi
            if managed:
                pnl = self._pnl(p, ev.price, ev.qty)
                self.store.sell_part(p["id"], ev.kind, ev.price, ev.qty, pnl, now,
                                     stop=ev.stop_after, reached=ev.kind)
            else:
                self.store.update_position(p["id"], stop=ev.stop_after, reached_level=ev.kind)
            if flag:
                self.store.update_position(p["id"], **{flag: 1})
            if p["mode"] != "shadow":
                self.send(msg.tp_message(p, ev, self.cfg.paper_mode, managed))

    def _close(self, p: Dict[str, Any], price: float, now: datetime, reason: str) -> None:
        qty = self._remaining(p)
        pnl = self._pnl(p, price, qty)
        self.store.sell_part(p["id"], reason, price, qty, pnl, now)
        if p["mode"] == "paper":
            self.send(msg.close_message(p, price, pnl, reason, True))

    def _check_candidate(self, cand: Dict[str, Any], last: float, now: datetime) -> None:
        day_text = now.date().isoformat()
        if cand["valid_until"] < day_text:
            self.store.update_candidate(cand["id"], status="expired")
            return
        if cand["stage"] != "confirmed":
            return
        passes = self.cfg.passes_four_conditions(
            cand["score"], cand["decision"], cand["ai_olasilik"] or 0.0, self.strategy.buy_score
        )
        if last <= cand["max_entry"]:
            self._fill_candidate(cand["id"], last, now, passes)
        elif passes and not cand["limit_alerted"]:
            self.store.update_candidate(cand["id"], limit_alerted=1)
            self.send(
                f"⏫ {cand['symbol']}: fiyat {msg.tl(last)} TL, alım sınırı "
                f"{msg.tl(cand['max_entry'])} TL aşıldı. Kovalamayın."
            )

    def close_job(self, now: datetime) -> None:
        """Günlük barlar kesinleşince çalışır. Veri gecikirse StaleData fırlatır."""
        with self.lock:
            day = now.date()
            self.market.clear_cache()
            symbols = sorted(set(self.store.watchlist()) | {p["symbol"] for p in self.store.open_positions()})
            evaluations, errors, prices = self.evaluate_all(now, symbols, provisional=False)
            self.last_evaluations = evaluations

            for cand in self.store.settle_list(day):
                self._settle(cand, evaluations.get(cand["symbol"]), now)
            watch = set(self.store.watchlist())
            fresh, blocked, tally = self._process(
                {s: e for s, e in evaluations.items() if s in watch}, prices, now, "S5"
            )

            exits = self._daily_exit_checks(evaluations, now)
            index = self.market.xu100(day, include_today=True)
            for mode in ("paper", "real", "shadow"):
                if self.store.open_positions(mode) or self.store.closed_positions(mode):
                    self.store.record_equity(day, mode, self.equity(mode, prices), float(index.iloc[-1]))
            self.send(msg.daily_report(
                now, self.cfg, evaluations, prices, fresh, blocked, exits, errors, index,
                self.store.open_positions(self.mode), self.equity(self.mode, prices), tally,
            ))

    def _settle(self, cand: Dict[str, Any], ev: Optional[Evaluation], now: datetime) -> None:
        """Gün içi sinyalin kapanışta teyidi veya iptali."""
        day_text = now.date().isoformat()
        if cand["stage"] == "confirmed":
            if cand["valid_until"] <= day_text:
                self.store.update_candidate(cand["id"], status="expired")
            return
        valid = ev is not None and ev.eligible and ev.score >= self.strategy.buy_score
        tag = "[KAĞIT] " if self.cfg.paper_mode else ""
        passes = self.cfg.passes_four_conditions(
            cand["score"], cand["decision"], cand["ai_olasilik"] or 0.0, self.strategy.buy_score
        )
        if not valid:
            filled = cand["status"] == "filled"
            self.store.update_candidate(cand["id"], stage="confirmed",
                                        status="filled" if filled else "cancelled")
            if passes:
                score = f"{ev.score:.0f}" if ev else "yok"
                self.send(f"{tag}❎ SİNYAL İPTAL\n{cand['symbol']}: kapanış skoru {score}. "
                          f"Almadıysan alma. Aldıysan seviyeler geçerli.")
            return
        limit = entry_limit(ev.close, ev.atr, self.strategy)
        until = session.add_sessions(now.date(), self.strategy.entry_valid_sessions)
        if cand["status"] == "filled":
            self.store.update_candidate(cand["id"], stage="confirmed")
            return
        self.store.update_candidate(
            cand["id"], stage="confirmed", score=ev.score, reference=ev.close, atr=ev.atr,
            max_entry=limit, stop=stop_price(ev.close, ev.atr, self.strategy),
            valid_until=until.isoformat(),
        )
        if passes:
            self.send(f"{tag}✅ SİNYAL TEYİT\n{cand['symbol']}: kapanış skoru {ev.score:.0f}. "
                      f"Alım sınırı {msg.tl(limit)} TL, {self.strategy.entry_valid_sessions} seans geçerli.")

    def _daily_exit_checks(self, evaluations: Dict[str, Evaluation], now: datetime) -> List[str]:
        """
        Zaman stopu her gün (AI'sız); skor/EMA50 yumuşak çıkışı haftalık kontrol
        gününde ve AI'a SAT/TUT sorarak.
        """
        out: List[str] = []
        day = now.date()
        weekly = session.is_last_trading_day_of_week(day)
        for p in self.store.open_positions():
            if p["exit_flag"]:
                continue
            ev = evaluations.get(p["symbol"])

            # 1. Zaman stopu — her gün, AI'a sorulmaz
            state = self._state_of(p)
            sessions_held = self._sessions_held(p, day)
            due = time_exit_due(state, sessions_held, self.strategy)
            if due:
                self.store.update_position(p["id"], exit_flag=due)
                if p["mode"] != "shadow":
                    prefix = "[KAĞIT] " if p["mode"] == "paper" else ""
                    label = "zaman stopu" if due == "time" else "azami tutma süresi"
                    self.send(f"{prefix}🔴 SAT | {label}\n{p['symbol']}: {sessions_held} seans doldu, "
                              f"sonraki seans açılışında çık.")
                    out.append(p["symbol"])
                continue

            # 2. AI'ın verdiği TUT süresi — her gün, süre dolunca kural satar
            hold_until = p.get("soft_exit_hold_until")
            if hold_until:
                if day.isoformat() > hold_until:
                    self.store.update_position(p["id"], exit_flag="score")
                    if p["mode"] != "shadow" and ev is not None:
                        self.send(msg.soft_exit_message(p, ev, None, self.cfg.paper_mode,
                                                        expired=True))
                        out.append(p["symbol"])
                continue  # süre sürüyorsa yeni inceleme yapılmaz

            # 3. Yeni yumuşak çıkış — haftalık kontrol günü. Ölçümde günlük
            #    kontrol Evren B'yi +%12,8'den +%3,3'e düşürdü.
            if self.strategy.exit_checks_on_scan_day_only and not weekly:
                continue
            if ev is None:
                continue
            soft = self._soft_exit_reason(ev, p, day)
            if soft is None:
                continue
            if self._handle_soft_exit(p, ev, soft, now):
                out.append(p["symbol"])
        return out

    def _sessions_held(self, p: Dict[str, Any], day: date) -> int:
        opened = date.fromisoformat(p["opened_at"][:10])
        count, cursor = 0, opened
        while cursor < day:
            cursor = session.next_trading_day(cursor)
            count += 1
        return count

    def _soft_exit_reason(self, ev: Evaluation, p: Dict[str, Any], day: date) -> Optional[str]:
        if self.strategy.exit_score is not None and ev.score < self.strategy.exit_score:
            return "score"
        if self.strategy.soft_exit_ema50 and self.strategy.exit_model != "atr":
            try:
                daily = self.market.daily(p["symbol"], day, include_today=True)
                from strategy_v4.features import ema

                if float(daily["close"].iloc[-1]) < float(ema(daily["close"], 50).iloc[-1]):
                    return "ema50"
            except Exception:
                logger.exception("%s EMA50 kontrolü başarısız", p["symbol"])
        return None

    def _handle_soft_exit(self, p: Dict[str, Any], ev: Evaluation, reason: str,
                          now: datetime) -> bool:
        """
        Yumuşak çıkış: AI'a SAT/TUT sorulur. AI_YOK, hata veya bütçe → SAT.

        TUT aynı pozisyon için bir kez verilebilir (`soft_exit_asked`); süre
        takibi `_daily_exit_checks` içindedir.
        """
        day = now.date()
        review = Review(decision=AI_SAT, error="AI kapalı")
        if self.cfg.ai_enabled and not p.get("soft_exit_asked"):
            try:
                daily = self.market.daily(p["symbol"], day, include_today=True)
                xu = self.market.xu100(day, include_today=True)
                review = self.analyst.review_exit(
                    p["symbol"], now, float(daily["close"].iloc[-1]), daily, xu, p
                )
            except Exception as exc:
                logger.exception("%s satış incelemesi başarısız", p["symbol"])
                review = Review(decision=AI_SAT, error=str(exc)[:120])
            self.store.update_position(p["id"], soft_exit_asked=1)

        if review.decision == AI_TUT:
            until = session.add_sessions(day, self.strategy.soft_exit_max_hold)
            self.store.update_position(p["id"], soft_exit_hold_until=until.isoformat())
            if p["mode"] != "shadow":
                self.send(msg.soft_exit_message(p, ev, review, self.cfg.paper_mode, until=until))
            return False
        self.store.update_position(p["id"], exit_flag=reason)
        if p["mode"] != "shadow":
            self.send(msg.soft_exit_message(p, ev, review, self.cfg.paper_mode))
        return True

    def sunday_job(self, now: datetime) -> None:
        """Hafta sonu incelemesi. `SUNDAY_REVIEW=false` iken çalışmaz."""
        if not self.cfg.sunday_review:
            return
        with self.lock:
            for cand in self.store.active_candidates():
                passes = self.cfg.passes_four_conditions(
                    cand["score"], cand["decision"], cand["ai_olasilik"] or 0.0,
                    self.strategy.buy_score,
                )
                if not passes:
                    continue
                review = self._review(cand["symbol"], now, cand["reference"],
                                      include_today=False, force=True)
                if review.decision == AI_AL:
                    continue
                if review.decision == AI_YOK:
                    self.send(f"⚠️ {cand['symbol']}: Pazar AI incelemesi yapılamadı "
                              f"({review.error}). Haberleri elle kontrol et.")
                    continue
                olasilik = float(review.olasilik or 0.0)
                self.store.update_candidate(cand["id"], decision=review.decision,
                                            note=review.summary[:300], ai_olasilik=review.olasilik,
                                            eminlik=self.cfg.eminlik(cand["score"], olasilik))
                self.send(f"⛔ {cand['symbol']}: hafta sonu incelemesi ALMA.\n{review.summary}")
            self.send(self.performance_text(now))

    # ---------------------------------------------------------------- sorgular
    def performance_text(self, now: datetime) -> str:
        return msg.performance_report(now, self.cfg, self.store, self._remaining)
