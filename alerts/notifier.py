"""
Sinyal -> Telegram metin formatı ve gönderim orkestrasyonu.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from bot import telegram_sender
from market.models import Signal
from alerts.state_manager import StateManager, TradeState, TransitionResult
from analytics.trade_logger import TradeLogger

logger = logging.getLogger(__name__)


def _pnl(direction: str, entry: float, exit_p: float) -> float:
    if entry <= 0:
        return 0.0
    if direction == "BUY":
        return (exit_p - entry) / entry * 100.0
    return (entry - exit_p) / entry * 100.0


def _log_event(
    trade_logger: Optional[TradeLogger],
    signal: Signal,
    transition: TransitionResult,
) -> None:
    """Trade açılış ve kapanış olaylarını SQLite'a kaydeder."""
    if trade_logger is None:
        return

    state = transition.current_state
    payload = transition.payload
    now = datetime.now(timezone.utc)

    direction = str(payload.get("signal_type", "BUY"))
    entry = float(payload.get("entry", 0.0))
    stop = float(payload.get("stop", 0.0))
    tp1 = float(payload.get("tp1", 0.0))
    tp2 = float(payload.get("tp2", 0.0))
    patterns = list(payload.get("detected_patterns", []))
    trend_state = str(payload.get("trend_state", "neutral"))
    timeframe = str(payload.get("timeframe", signal.timeframe))

    if state == TradeState.WATCH.value:
        trade_logger.log_open(
            symbol=signal.symbol,
            direction=direction,
            timeframe=timeframe,
            patterns=patterns,
            trend_state=trend_state,
            entry=entry,
            stop=stop,
            tp1=tp1,
            tp2=tp2,
            opened_at=now,
        )

    elif state == TradeState.TP2.value:
        trade_logger.log_close(
            symbol=signal.symbol,
            outcome="TP2",
            closed_at=now,
            pnl_pct=_pnl(direction, entry, tp2),
        )

    elif state == TradeState.STOPPED.value:
        if transition.reason == "stop after tp1":
            outcome = "SL_BE"
            exit_p = entry  # breakeven stop = entry
        else:
            outcome = "SL"
            exit_p = stop
        trade_logger.log_close(
            symbol=signal.symbol,
            outcome=outcome,
            closed_at=now,
            pnl_pct=_pnl(direction, entry, exit_p),
        )

    elif state == TradeState.EXPIRED.value:
        last_close = signal.last_close if signal.last_close is not None else entry
        trade_logger.log_close(
            symbol=signal.symbol,
            outcome="EXPIRED",
            closed_at=now,
            pnl_pct=_pnl(direction, entry, last_close),
        )


def _state_title(state: str) -> str:
    mapping = {
        TradeState.WATCH.value: "hazırlık",
        TradeState.BUY.value: "giriş geldi",
        TradeState.TP1.value: "kar al",
        TradeState.TP2.value: "trade başarıyla tamamlandı",
        TradeState.STOPPED.value: "trade kapandı",
        TradeState.EXPIRED.value: "setup süresi doldu",
        TradeState.CLOSED.value: "trade kapandı",
    }
    return mapping.get(state, state.lower())


def format_signal_message(signal: Signal, transition: TransitionResult) -> str:
    """State odaklı Telegram metni."""
    state_name = transition.current_state
    reason = transition.reason
    icon = {
        TradeState.WATCH.value: "📡 WATCH",
        TradeState.BUY.value: "🚀 ENTRY TRIGGERED",
        TradeState.TP1.value: "✅ TP1 HIT",
        TradeState.TP2.value: "🎯 TP2 HIT",
        TradeState.STOPPED.value: "❌ STOP LOSS",
        TradeState.EXPIRED.value: "⌛ WATCH EXPIRED",
        TradeState.CLOSED.value: "🔒 CLOSED",
    }.get(state_name, state_name)

    lines = [
        signal.symbol.upper(),
        icon,
        f"Zaman Dilimi: {signal.timeframe}",
        f"Entry: {signal.entry:.2f}",
        f"Stop: {signal.stop_loss:.2f}",
        f"TP1: {signal.target_1:.2f}",
        f"TP2: {signal.target_2:.2f}",
        f"Gerekçe: {signal.reason_summary()}",
        f"Durum Notu: {_state_title(state_name)} ({reason})",
    ]
    return "\n".join(lines)


def notify_signal(
    bot_token: str,
    chat_id: str,
    signal: Signal,
    transition: TransitionResult,
    trade_logger: Optional[TradeLogger] = None,
) -> bool:
    """Sadece anlamlı state değişimlerinde Telegram bildirimi gönderir ve trade'i kaydeder."""
    if not transition.changed or not transition.notify:
        return False

    _log_event(trade_logger, signal, transition)

    text = format_signal_message(signal, transition)
    try:
        debug_last_close = signal.last_close if signal.last_close is not None else signal.entry
        logger.info(
            "sending signal for %s with last_close=%.4f and entry=%.4f",
            signal.symbol,
            debug_last_close,
            signal.entry,
        )
        telegram_sender.send_message(bot_token, chat_id, text)
        logger.info(
            "State bildirimi gönderildi: %s %s -> %s",
            signal.symbol,
            transition.previous_state,
            transition.current_state,
        )
        return True
    except Exception:
        logger.exception("Sinyal gönderilemedi: %s", signal.symbol)
        return False


def notify_plain_text(bot_token: str, chat_id: str, text: str) -> Optional[dict]:
    """Serbest metin (test mesajı vb.)."""
    return telegram_sender.send_message(bot_token, chat_id, text)
