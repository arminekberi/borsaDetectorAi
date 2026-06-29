"""
Sesli ve yazılı mesaj handler'ları — Whisper (STT) + Claude (LLM) entegrasyonu.
"""
from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from collections import defaultdict
from typing import Dict, List, Optional

import anthropic
import openai
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import ContextTypes

load_dotenv()

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = (
    "Sen BorsaDetector AI'ın asistanısın. Kullanıcı BIST hisseleri, teknik analiz, "
    "trading stratejileri ve genel finans hakkında sorular sorar. "
    "Türkçe konuş, kısa ve net cevaplar ver."
)

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
        system=_SYSTEM_PROMPT,
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
