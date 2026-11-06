"""Strateji v4 botu. Varsayılan olarak kağıt modunda çalışır."""

from __future__ import annotations

import json
import logging
import sys
import threading
from datetime import datetime
from zoneinfo import ZoneInfo

from bot_v4.analyst import Analyst
from bot_v4.engine import Engine
from bot_v4.market import YahooMarket
from bot_v4.scheduler import Scheduler
from bot_v4.settings import ROOT, load_config, load_watchlist_seed
from bot_v4.store import Store
from bot_v4.telegram_app import build_application, make_sender
from strategy_v4.config import StrategyConfig
from utils.logger import setup_logging

logger = logging.getLogger("bist_bot")


def main() -> None:
    cfg = load_config()
    setup_logging("INFO", "bist_bot")
    if not cfg.bot_token or not cfg.chat_id:
        logger.error("BOT_TOKEN ve CHAT_ID .env içinde tanımlı olmalı")
        sys.exit(1)

    now = datetime.now(ZoneInfo("Europe/Istanbul"))
    store = Store(cfg.db_path)
    store.seed_watchlist(load_watchlist_seed(), now)
    sectors = json.loads((ROOT / "config" / "sectors_v4.json").read_text(encoding="utf-8"))
    send = make_sender(cfg)
    engine = Engine(cfg, StrategyConfig(), store, YahooMarket(), Analyst(cfg, store), send, sectors)

    mode = "KAĞIT" if cfg.paper_mode else "GERÇEK BİLDİRİM"
    ai = "açık" if cfg.ai_enabled else "kapalı"
    send(f"🤖 Bot v4 başladı | Mod: {mode} | AI: {ai} | İzleme listesi: {len(store.watchlist())} hisse\n/yardim yazarak komutları görebilirsin.")

    threading.Thread(target=Scheduler(cfg, engine).run_forever, daemon=True, name="scheduler").start()
    build_application(cfg, engine).run_polling()


if __name__ == "__main__":
    main()
