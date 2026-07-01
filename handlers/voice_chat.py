"""
Sesli ve yazılı mesaj handler'ları — Whisper (STT) + Claude (LLM) entegrasyonu.
"""
from __future__ import annotations

import asyncio
import logging
import os
import tempfile
import time
from collections import defaultdict
from typing import Dict, List, Optional

import anthropic
import openai
import yfinance as yf
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import ContextTypes

from market.symbols import symbol_codes

load_dotenv()

logger = logging.getLogger(__name__)

_WATCHLIST = symbol_codes()
_WATCHLIST_STR = ", ".join(_WATCHLIST)

_SYSTEM_PROMPT_BASE = (
    "Sen BorsaDetector AI'ın asistanısın. Kullanıcı BIST hisseleri, teknik analiz, "
    "trading stratejileri ve genel finans hakkında sorular sorar. "
    "Türkçe konuş, kısa ve net cevaplar ver.\n\n"
    f"Takip ettiğin hisseler: {_WATCHLIST_STR}"
)

_PRICE_CACHE: Dict = {"data": "", "ts": 0.0}
_NEWS_CACHE: Dict = {"data": "", "ts": 0.0}
_PRICE_TTL = 300   # 5 dakika
_NEWS_TTL  = 900   # 15 dakika


def _fetch_prices() -> str:
    now = time.time()
    if _PRICE_CACHE["data"] and now - _PRICE_CACHE["ts"] < _PRICE_TTL:
        return _PRICE_CACHE["data"]

    lines = []
    for sym in _WATCHLIST:
        try:
            info = yf.Ticker(f"{sym}.IS").fast_info
            price = getattr(info, "last_price", None)
            prev  = getattr(info, "previous_close", None)
            if price and prev and prev > 0:
                pct  = (price - prev) / prev * 100
                sign = "+" if pct >= 0 else ""
                lines.append(f"  {sym}: {price:.2f} TL ({sign}{pct:.2f}%)")
            else:
                lines.append(f"  {sym}: veri yok")
        except Exception:
            lines.append(f"  {sym}: çekilemedi")

    result = "Güncel fiyatlar (BIST):\n" + "\n".join(lines)
    _PRICE_CACHE["data"] = result
    _PRICE_CACHE["ts"] = now
    return result


def _fetch_news() -> str:
    now = time.time()
    if _NEWS_CACHE["data"] and now - _NEWS_CACHE["ts"] < _NEWS_TTL:
        return _NEWS_CACHE["data"]

    headlines = []
    for sym in _WATCHLIST:
        try:
            news = yf.Ticker(f"{sym}.IS").news or []
            for item in news[:2]:
                title = (item.get("content") or {}).get("title") or item.get("title", "")
                if title:
                    headlines.append(f"[{sym}] {title}")
        except Exception:
            pass

    result = ""
    if headlines:
        result = "Son haberler:\n" + "\n".join(headlines[:12])
    _NEWS_CACHE["data"] = result
    _NEWS_CACHE["ts"] = now
    return result


def _build_system_prompt() -> str:
    parts = [_SYSTEM_PROMPT_BASE]
    prices = _fetch_prices()
    if prices:
        parts.append(prices)
    news = _fetch_news()
    if news:
        parts.append(news)
    return "\n\n".join(parts)

_MAX_HISTORY = 20
_conversation_history: Dict[str, List[dict]] = defaultdict(list)

_openai_client: Optional[openai.OpenAI] = None
_anthropic_client: Optional[anthropic.Anthropic] = None


def _get_openai() -> openai.OpenAI:
    global _openai_client
    if _openai_client is None:
        _openai_client = openai.OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    return _openai_client


def _get_anthropic() -> anthropic.Anthropic:
    global _anthropic_client
    if _anthropic_client is None:
        _anthropic_client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    return _anthropic_client


def get_claude_response(chat_id: str, user_message: str) -> str:
    history = _conversation_history[chat_id]
    history.append({"role": "user", "content": user_message})

    response = _get_anthropic().messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=_build_system_prompt(),
        messages=history,
    )

    reply = response.content[0].text
    history.append({"role": "assistant", "content": reply})

    if len(history) > _MAX_HISTORY:
        _conversation_history[chat_id] = history[-_MAX_HISTORY:]

    return reply


def _transcribe_sync(ogg_path: str) -> str:
    with open(ogg_path, "rb") as f:
        result = _get_openai().audio.transcriptions.create(model="whisper-1", file=f)
    return result.text.strip()


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = str(update.effective_chat.id)
    ogg_path = None
    try:
        voice = update.message.voice
        tg_file = await context.bot.get_file(voice.file_id)

        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False, dir="/tmp") as tmp:
            ogg_path = tmp.name

        await tg_file.download_to_drive(ogg_path)

        text = await asyncio.to_thread(_transcribe_sync, ogg_path)
        if not text:
            await update.message.reply_text("Ses anlaşılamadı, tekrar dener misin?")
            return

        logger.info("Whisper transkript [%s]: %s", chat_id, text)
        reply = await asyncio.to_thread(get_claude_response, chat_id, text)
        await update.message.reply_text(reply)

    except Exception:
        logger.exception("Sesli mesaj işleme hatası [%s]", chat_id)
        await update.message.reply_text("Üzgünüm, bir hata oluştu. Tekrar dener misin?")
    finally:
        if ogg_path and os.path.exists(ogg_path):
            os.unlink(ogg_path)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = str(update.effective_chat.id)
    try:
        text = (update.message.text or "").strip()
        if not text:
            return
        reply = await asyncio.to_thread(get_claude_response, chat_id, text)
        await update.message.reply_text(reply)
    except Exception:
        logger.exception("Yazılı mesaj işleme hatası [%s]", chat_id)
        await update.message.reply_text("Üzgünüm, bir hata oluştu. Tekrar dener misin?")
