"""15 dakikalik tarama runner'i."""

from __future__ import annotations

import logging
import math
import time
import traceback
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from alerts.notifier import notify_signal
from alerts.state_manager import StateManager
from config.settings import Settings
from market.data_fetcher import BaseFetcher
from market.market_calendar import (
    four_hour_candle_key,
    is_market_open,
    is_new_4h_candle,
    is_signal_generation_time,
)
from market.models import Signal, SignalType
from strategy.signal_engine import evaluate_symbol

logger = logging.getLogger(__name__)


@dataclass
class ScanRunner:
    settings: Settings
    fetcher: BaseFetcher
    state_manager: StateManager
    _last_4h_key: Optional[str] = None

    def _tracking_update(self, symbol: str) -> Optional[Signal]:
        df = self.fetcher.fetch_ohlcv(symbol, self.settings.tracking_timeframe, bars=30)
        if df is None or df.empty:
            logger.warning("Tracking veri yok: %s", symbol)
            return None
        last_close = float(df.iloc[-1]["close"])
        transition = self.state_manager.update_price(symbol, last_close)
        state_snapshot = self.state_manager.get_state(symbol)
        signal = Signal(
            symbol=symbol,
            signal_type=SignalType.WATCH,
            timeframe=str(state_snapshot.get("timeframe", self.settings.primary_timeframe)),
            entry=float(state_snapshot.get("entry", 0.0)),
            stop_loss=float(state_snapshot.get("stop", 0.0)),
            target_1=float(state_snapshot.get("tp1", 0.0)),
            target_2=float(state_snapshot.get("tp2", 0.0)),
            reason=str(state_snapshot.get("reason", "")),
            timestamp=datetime.now(timezone.utc),
            last_close=last_close,
            trend_state="tracking",
            detected_patterns=[],
            tags=[],
        )
        if self.settings.enable_debug_logs:
            logger.info(
                "Tracking | %s | last_close=%.4f | entry=%.4f | stop=%.4f | tp1=%.4f | tp2=%.4f | state=%s",
                symbol,
                last_close,
                signal.entry,
                signal.stop_loss,
                signal.target_1,
                signal.target_2,
                transition.current_state,
            )
        notify_signal(self.settings.bot_token, self.settings.chat_id, signal, transition)
        return signal

    def _generate_4h_setup(self, symbol: str) -> None:
        df = self.fetcher.fetch_ohlcv(symbol, self.settings.primary_timeframe.lower(), bars=140)
        if df is None or df.empty:
            logger.warning("4H setup verisi yok: %s", symbol)
            return
        last_close = float(df.iloc[-1]["close"])
        signal = evaluate_symbol(
            symbol=symbol,
            df=df,
            timeframe=self.settings.primary_timeframe,
            entry_distance_threshold_pct=self.settings.entry_distance_threshold_pct,
            min_rr=self.settings.min_rr,
        )
        if signal is None:
            logger.info("4H setup cikmadi: %s", symbol)
            return
        signal.last_close = last_close
        transition = self.state_manager.register_setup(signal)
        if self.settings.enable_debug_logs:
            logger.info(
                "Setup | %s | last_close=%.4f | entry=%.4f | stop=%.4f | tp1=%.4f | tp2=%.4f | state=%s",
                symbol,
                last_close,
                signal.entry,
                signal.stop_loss,
                signal.target_1,
                signal.target_2,
                transition.current_state,
            )
        notify_signal(self.settings.bot_token, self.settings.chat_id, signal, transition)

    def run_once(self) -> None:
        now = datetime.now(timezone.utc)
        if not is_signal_generation_time(now, self.settings.market_timezone):
            logger.info("15m tetik zamani degil, bekleniyor.")
            return

        market_open = is_market_open(now, self.settings.market_timezone)
        if not market_open:
            logger.info("Market closed, skipping signal generation")
            return

        for symbol in self.settings.watchlist:
            try:
                self._tracking_update(symbol)
            except Exception:
                logger.error("Tracking hatasi: %s\n%s", symbol, traceback.format_exc())

        if not is_new_4h_candle(now, self.settings.market_timezone):
            logger.info("Yeni 4H mum yok, setup uretimi atlandi.")
            return

        key = four_hour_candle_key(now, self.settings.market_timezone)
        if key == self._last_4h_key:
            logger.info("Ayni 4H kapanis key tekrarlandi (%s), setup atlandi.", key)
            return

        self._last_4h_key = key
        for symbol in self.settings.watchlist:
            try:
                self._generate_4h_setup(symbol)
            except Exception:
                logger.error("4H setup hatasi: %s\n%s", symbol, traceback.format_exc())

    def _seconds_to_next_quarter(self) -> float:
        """Sonraki 15 dakika sınırına (0, 15, 30, 45) kalan saniyeyi hesaplar."""
        now = datetime.now(timezone.utc)
        elapsed = now.minute * 60 + now.second + now.microsecond / 1_000_000
        interval = 900.0  # 15 dakika
        next_boundary = math.ceil(elapsed / interval) * interval
        secs = next_boundary - elapsed
        # Çok kısa uyku (< 5s) bir sonraki sınıra atlar
        return secs if secs >= 5.0 else secs + interval

    def run_forever(self) -> None:
        while True:
            try:
                self.run_once()
            except Exception:
                logger.error("Runner dongu hatasi:\n%s", traceback.format_exc())
            secs = self._seconds_to_next_quarter()
            logger.info("Sonraki tarama: %.0f saniye sonra.", secs)
            time.sleep(secs)
