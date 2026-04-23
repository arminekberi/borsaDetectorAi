"""State-based sinyal takip sistemi."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

from market.models import Signal
from utils.helpers import round_price

logger = logging.getLogger(__name__)


def _parse_iso(ts: str) -> datetime:
    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


class TradeState(str, Enum):
    NONE = "NONE"
    WATCH = "WATCH"
    BUY = "BUY"
    TP1 = "TP1"
    TP2 = "TP2"
    STOPPED = "STOPPED"
    CLOSED = "CLOSED"
    EXPIRED = "EXPIRED"


@dataclass
class TransitionResult:
    symbol: str
    changed: bool
    previous_state: str
    current_state: str
    reason: str
    notify: bool
    payload: Dict[str, Any]


@dataclass
class StateManager:
    path: Path
    watch_expiry_hours: float = 48.0
    breakeven_on_tp1: bool = True

    def _load(self) -> Dict[str, Any]:
        if not self.path.exists():
            return {}
        try:
            raw = self.path.read_text(encoding="utf-8")
            return json.loads(raw) if raw.strip() else {}
        except (json.JSONDecodeError, OSError) as exc:
            logger.error("State dosyasi okunamadi: %s", exc)
            return {}

    def _save(self, data: Dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def _default_state(self, symbol: str) -> Dict[str, Any]:
        now = datetime.now(timezone.utc).isoformat()
        return {
            "symbol": symbol,
            "state": TradeState.NONE.value,
            "entry": 0.0,
            "stop": 0.0,
            "tp1": 0.0,
            "tp2": 0.0,
            "signal_type": "WATCH",
            "timeframe": "4H",
            "reason": "",
            "created_at": now,
            "updated_at": now,
            "expires_at": now,
            "last_price": 0.0,
        }

    def _transition(
        self,
        symbol: str,
        previous: str,
        current: str,
        reason: str,
        payload: Dict[str, Any],
        notify: bool,
    ) -> TransitionResult:
        if previous != current:
            logger.info("%s transitioned %s -> %s", symbol, previous, current)
        return TransitionResult(symbol, previous != current, previous, current, reason, notify, payload)

    def get_state(self, symbol: str) -> Dict[str, Any]:
        data = self._load()
        return dict(data.get(symbol.upper(), self._default_state(symbol.upper())))

    def _write_state(self, symbol: str, rec: Dict[str, Any]) -> None:
        data = self._load()
        data[symbol.upper()] = rec
        self._save(data)

    def register_setup(self, signal: Signal, now: Optional[datetime] = None) -> TransitionResult:
        now = now or datetime.now(timezone.utc)
        symbol = signal.symbol.upper()
        rec = self.get_state(symbol)
        prev = str(rec.get("state", TradeState.NONE.value))

        if prev in (TradeState.WATCH.value, TradeState.BUY.value, TradeState.TP1.value):
            return self._transition(symbol, prev, prev, "active state already exists", rec, notify=False)

        expires = now + timedelta(hours=self.watch_expiry_hours)
        rec.update(
            {
                "symbol": symbol,
                "state": TradeState.WATCH.value,
                "entry": round_price(signal.entry),
                "stop": round_price(signal.stop_loss),
                "tp1": round_price(signal.target_1),
                "tp2": round_price(signal.target_2),
                "signal_type": signal.signal_type.value,
                "timeframe": signal.timeframe,
                "reason": signal.reason,
                "created_at": now.isoformat(),
                "updated_at": now.isoformat(),
                "expires_at": expires.isoformat(),
                "last_price": round_price(signal.last_close if signal.last_close is not None else signal.entry),
            }
        )
        self._write_state(symbol, rec)
        return self._transition(symbol, prev, TradeState.WATCH.value, "new setup", rec, notify=True)

    def update_price(self, symbol: str, last_price: float, now: Optional[datetime] = None) -> TransitionResult:
        now = now or datetime.now(timezone.utc)
        sym = symbol.upper()
        rec = self.get_state(sym)
        prev = str(rec.get("state", TradeState.NONE.value))
        rec["last_price"] = round_price(last_price)
        rec["updated_at"] = now.isoformat()

        entry = float(rec.get("entry", 0.0))
        stop = float(rec.get("stop", 0.0))
        tp1 = float(rec.get("tp1", 0.0))
        tp2 = float(rec.get("tp2", 0.0))

        if prev == TradeState.NONE.value:
            self._write_state(sym, rec)
            return self._transition(sym, prev, prev, "no active setup", rec, notify=False)

        if prev == TradeState.WATCH.value:
            if now >= _parse_iso(str(rec.get("expires_at", now.isoformat()))):
                rec["state"] = TradeState.EXPIRED.value
                self._write_state(sym, rec)
                return self._transition(sym, prev, TradeState.EXPIRED.value, "watch expiry reached", rec, notify=True)
            if last_price >= entry > 0:
                rec["state"] = TradeState.BUY.value
                self._write_state(sym, rec)
                return self._transition(sym, prev, TradeState.BUY.value, "entry triggered", rec, notify=True)
            self._write_state(sym, rec)
            return self._transition(sym, prev, prev, "watching", rec, notify=False)

        if prev == TradeState.BUY.value:
            if last_price <= stop:
                rec["state"] = TradeState.STOPPED.value
                self._write_state(sym, rec)
                return self._transition(sym, prev, TradeState.STOPPED.value, "stop hit", rec, notify=True)
            if last_price >= tp1 > 0:
                rec["state"] = TradeState.TP1.value
                if self.breakeven_on_tp1:
                    rec["stop"] = round_price(entry)
                self._write_state(sym, rec)
                return self._transition(sym, prev, TradeState.TP1.value, "tp1 hit", rec, notify=True)
            self._write_state(sym, rec)
            return self._transition(sym, prev, prev, "buy active", rec, notify=False)

        if prev == TradeState.TP1.value:
            if last_price <= float(rec.get("stop", stop)):
                rec["state"] = TradeState.STOPPED.value
                self._write_state(sym, rec)
                return self._transition(sym, prev, TradeState.STOPPED.value, "stop after tp1", rec, notify=True)
            if last_price >= tp2 > 0:
                rec["state"] = TradeState.TP2.value
                self._write_state(sym, rec)
                return self._transition(sym, prev, TradeState.TP2.value, "tp2 hit", rec, notify=True)
            self._write_state(sym, rec)
            return self._transition(sym, prev, prev, "tp1 active", rec, notify=False)

        if prev == TradeState.TP2.value:
            rec["state"] = TradeState.CLOSED.value
            self._write_state(sym, rec)
            return self._transition(sym, prev, TradeState.CLOSED.value, "closed after tp2", rec, notify=False)

        if prev == TradeState.STOPPED.value:
            rec["state"] = TradeState.CLOSED.value
            self._write_state(sym, rec)
            return self._transition(sym, prev, TradeState.CLOSED.value, "closed after stop", rec, notify=False)

        self._write_state(sym, rec)
        return self._transition(sym, prev, prev, "state unchanged", rec, notify=False)

    def active_symbols(self) -> List[str]:
        data = self._load()
        out: List[str] = []
        for sym, rec in data.items():
            st = str(rec.get("state", TradeState.NONE.value))
            if st in (
                TradeState.WATCH.value,
                TradeState.BUY.value,
                TradeState.TP1.value,
                TradeState.TP2.value,
                TradeState.STOPPED.value,
                TradeState.EXPIRED.value,
            ):
                out.append(sym)
        return out
