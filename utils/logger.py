"""
Merkezi logging kurulumu.

Tüm modüller `logging.getLogger(__name__)` ile alt logger kullanır;
burada kök seviye ve format bir kez ayarlanır.
"""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional

_LOG_DIR = Path(__file__).resolve().parent.parent / "logs"


def setup_logging(level: str = "INFO", name: Optional[str] = None) -> logging.Logger:
    """
    Kök logger'ı yapılandırır; stdout + rotating file handler ekler.
    Tekrar çağrılsa bile handler tekrar eklenmez (idempotent).
    """
    numeric = getattr(logging, level.upper(), logging.INFO)
    root = logging.getLogger()
    root.setLevel(numeric)

    if not root.handlers:
        fmt = logging.Formatter(
            "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )

        console = logging.StreamHandler(sys.stdout)
        console.setLevel(numeric)
        console.setFormatter(fmt)
        root.addHandler(console)

        _LOG_DIR.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            _LOG_DIR / "bist_bot.log",
            maxBytes=5 * 1024 * 1024,  # 5 MB
            backupCount=5,
            encoding="utf-8",
        )
        file_handler.setLevel(numeric)
        file_handler.setFormatter(fmt)
        root.addHandler(file_handler)

    named = logging.getLogger(name) if name else root
    if name:
        named.setLevel(numeric)
    return named
