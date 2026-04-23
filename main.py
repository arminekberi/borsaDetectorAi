"""
BIST Telegram sinyal botu — giriş noktası.

Akış:
1) Logging ve ayarlar
2) Telegram test mesajı
3) İzlenecek hisseler için döngü: veri çek -> sinyal üret -> cooldown ile bildir

TODO: Gerçek veri entegrasyonu — Yahoo / Matriks / broker API; sembol eşlemesi ve oturum.
TODO: Backtest modülü — tarihsel OHLCV ile strateji performans ölçümü.
TODO: Çoklu timeframe analizi — üst TF trend + alt TF giriş birleştirme.
TODO: TradingView webhook entegrasyonu — alarm -> sunucu endpoint -> Telegram.
TODO: WhatsApp entegrasyonu — Meta Cloud API veya üçüncü parti gateway.
TODO: Web panel — izlenen semboller, son sinyaller, parametre ayarları.
"""

from __future__ import annotations

import logging
import sys

from config.settings import get_settings, validate_telegram_credentials
from market.data_fetcher import build_fetcher
from alerts.state_manager import StateManager
from bot.telegram_sender import send_test_message
from scheduler.runner import ScanRunner
from utils.logger import setup_logging

logger = logging.getLogger("bist_bot")


def main() -> None:
    settings = get_settings()
    setup_logging(settings.log_level, "bist_bot")

    try:
        validate_telegram_credentials(settings)
    except ValueError as e:
        logger.error("%s", e)
        sys.exit(1)

    logger.info("Telegram test mesajı gönderiliyor...")
    try:
        send_test_message(settings.bot_token, settings.chat_id)
        logger.info("Test mesajı gönderildi.")
    except Exception:
        logger.exception("Test mesajı başarısız; token/chat_id kontrol edin.")
        sys.exit(1)

    fetcher = build_fetcher(settings.data_source or "yahoo")

    logger.info(
        "Takip listesi (%d hisse): %s",
        len(settings.watchlist),
        ", ".join(settings.watchlist),
    )

    state = StateManager(
        path=settings.signal_state_path,
        watch_expiry_hours=settings.watch_expiry_hours,
        breakeven_on_tp1=settings.breakeven_on_tp1,
    )

    logger.info(
        "Tarama başladı (mod: 15m boundary sleep, fetcher: %s)",
        settings.data_source or settings.fetcher_type,
    )
    runner = ScanRunner(settings=settings, fetcher=fetcher, state_manager=state)
    runner.run_forever()


if __name__ == "__main__":
    main()
