"""Telegram bağlantısı. Yetkisiz sohbetlerden gelen hiçbir mesaj işlenmez."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Callable, List
from zoneinfo import ZoneInfo

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from bot.telegram_sender import send_message
from bot_v4.commands import COMMANDS
from bot_v4.engine import Engine
from bot_v4.session import in_session
from bot_v4.settings import BotConfig
from handlers.voice_chat import handle_text, handle_voice, set_context_provider

logger = logging.getLogger("bot_v4.telegram")
TZ = ZoneInfo("Europe/Istanbul")
LIMIT = 3900


def split_message(text: str) -> List[str]:
    parts, current = [], ""
    for line in text.split("\n"):
        while len(line) > LIMIT:
            parts.append(line[:LIMIT])
            line = line[LIMIT:]
        if len(current) + len(line) + 1 > LIMIT:
            parts.append(current)
            current = line
        else:
            current = f"{current}\n{line}" if current else line
    if current:
        parts.append(current)
    return parts


def make_sender(cfg: BotConfig) -> Callable[[str], None]:
    def send(text: str) -> None:
        for part in split_message(text):
            try:
                send_message(cfg.bot_token, cfg.chat_id, part)
            except Exception:
                logger.exception("Telegram mesajı gönderilemedi")

    return send


def build_application(cfg: BotConfig, engine: Engine) -> Application:
    allowed = filters.Chat(chat_id=int(cfg.chat_id))
    app = Application.builder().token(cfg.bot_token).build()

    def wrap(name: str):
        action = COMMANDS[name]

        async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
            args = context.args or []
            try:
                now = datetime.now(TZ)
                reply = await asyncio.to_thread(action, engine, args, now)
            except Exception:
                logger.exception("%s komutu başarısız", name)
                reply = "Komut çalıştırılamadı, tekrar dener misin?"
            for part in split_message(reply):
                await update.message.reply_text(part)

        return handler

    async def scan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        now = datetime.now(TZ)
        if not in_session(now):
            await update.message.reply_text("Seans kapalı. Son skorlar için /liste yazabilirsin.")
            return
        await update.message.reply_text("Tarama başladı.")
        try:
            await asyncio.to_thread(engine.scan_intraday, now)
        except Exception:
            logger.exception("Elle tarama başarısız")
            await update.message.reply_text("Tarama başarısız oldu.")

    for name in COMMANDS:
        app.add_handler(CommandHandler(name, wrap(name), filters=allowed))
    app.add_handler(CommandHandler("tara", scan, filters=allowed))
    app.add_handler(MessageHandler(allowed & filters.VOICE, handle_voice))
    app.add_handler(MessageHandler(allowed & filters.TEXT & ~filters.COMMAND, handle_text))

    def context_text() -> str:
        portfolio = ", ".join(sorted({p["symbol"] for p in engine.store.open_positions() if p["mode"] != "shadow"})) or "yok"
        candidates = ", ".join(c["symbol"] for c in engine.store.active_candidates()) or "yok"
        return f"Açık pozisyonlar: {portfolio}\nAktif adaylar: {candidates}"

    set_context_provider(engine.store.watchlist, context_text)
    return app
