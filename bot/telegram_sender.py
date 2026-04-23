"""
Telegram Bot API — requests ile sendMessage.

Token ve chat_id dışarıdan verilir; secret kod içine yazılmaz.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, Optional

import requests

logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org"
_MAX_RETRIES = 3
_RETRY_BASE_DELAY = 2.0  # saniye


def send_message(
    bot_token: str,
    chat_id: str,
    text: str,
    parse_mode: Optional[str] = None,
    timeout: int = 30,
) -> Dict[str, Any]:
    """
    Telegram'a metin mesajı gönderir.
    429 rate limit ve geçici ağ hataları için exponential backoff ile yeniden dener.
    """
    url = f"{TELEGRAM_API}/bot{bot_token}/sendMessage"
    payload: Dict[str, Any] = {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": True,
    }
    if parse_mode:
        payload["parse_mode"] = parse_mode

    last_exc: Optional[Exception] = None
    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            resp = requests.post(url, json=payload, timeout=timeout)

            if resp.status_code == 429:
                retry_after = float(resp.headers.get("Retry-After", _RETRY_BASE_DELAY * attempt))
                logger.warning(
                    "Telegram rate limit (429), %.0fs bekleniyor (deneme %d/%d).",
                    retry_after,
                    attempt,
                    _MAX_RETRIES,
                )
                time.sleep(retry_after)
                continue

            resp.raise_for_status()
            data = resp.json()
            if not data.get("ok"):
                logger.error("Telegram API ok=false: %s", data)
                raise RuntimeError(str(data))
            return data

        except requests.RequestException as exc:
            last_exc = exc
            delay = _RETRY_BASE_DELAY * (2 ** (attempt - 1))
            logger.warning(
                "Telegram gönderim hatası (deneme %d/%d): %s — %.0fs sonra tekrar.",
                attempt,
                _MAX_RETRIES,
                exc,
                delay,
            )
            if attempt < _MAX_RETRIES:
                time.sleep(delay)

    logger.error("Telegram gönderimi başarısız, tüm denemeler tükendi.")
    raise last_exc  # type: ignore[misc]


def send_test_message(bot_token: str, chat_id: str) -> Dict[str, Any]:
    """Bağlantı testi için kısa mesaj."""
    text = "BIST Telegram bot: bağlantı testi OK. Tarama başlıyor."
    return send_message(bot_token, chat_id, text)
