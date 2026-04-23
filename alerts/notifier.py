"""
Sinyal -> Telegram metin formatı ve gönderim orkestrasyonu.
"""

from __future__ import annotations

import logging
from typing import Optional

from bot import telegram_sender
from market.models import Signal
from alerts.state_manager import StateManager, TradeState, TransitionResult

logger = logging.getLogger(__name__)


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
) -> bool:
    """Sadece anlamlı state değişimlerinde Telegram bildirimi gönderir."""
    if not transition.changed or not transition.notify:
        return False
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
