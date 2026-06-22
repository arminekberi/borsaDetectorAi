"""SQLite tabanlı trade outcome logger."""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Dict, List

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol      TEXT    NOT NULL,
    direction   TEXT    NOT NULL,
    timeframe   TEXT    NOT NULL,
    patterns    TEXT    NOT NULL DEFAULT '[]',
    trend_state TEXT    NOT NULL DEFAULT 'neutral',
    entry       REAL    NOT NULL,
    stop        REAL    NOT NULL,
    tp1         REAL    NOT NULL,
    tp2         REAL    NOT NULL,
    opened_at   TEXT    NOT NULL,
    closed_at   TEXT,
    outcome     TEXT,
    pnl_pct     REAL
);
"""


class TradeLogger:
    """Her sinyal açılışını ve kapanışını SQLite'a kaydeder."""

    def __init__(self, db_path: Path) -> None:
        self._db = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.execute(_SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(str(self._db))

    def log_open(
        self,
        symbol: str,
        direction: str,
        timeframe: str,
        patterns: List[str],
        trend_state: str,
        entry: float,
        stop: float,
        tp1: float,
        tp2: float,
        opened_at: datetime,
    ) -> None:
        sql = """
            INSERT INTO trades
                (symbol, direction, timeframe, patterns, trend_state,
                 entry, stop, tp1, tp2, opened_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        with self._conn() as c:
            c.execute(sql, (
                symbol.upper(), direction, timeframe,
                json.dumps(patterns, ensure_ascii=False),
                trend_state, entry, stop, tp1, tp2,
                opened_at.isoformat(),
            ))
        logger.info("TradeLog OPEN  %s %s patterns=%s", symbol, direction, patterns)

    def log_close(
        self,
        symbol: str,
        outcome: str,
        closed_at: datetime,
        pnl_pct: float,
    ) -> None:
        """En son açık trade'i kapatır."""
        sql = """
            UPDATE trades SET closed_at=?, outcome=?, pnl_pct=?
            WHERE id = (
                SELECT id FROM trades
                WHERE symbol=? AND closed_at IS NULL
                ORDER BY id DESC LIMIT 1
            )
        """
        with self._conn() as c:
            n = c.execute(
                sql, (closed_at.isoformat(), outcome, round(pnl_pct, 4), symbol.upper())
            ).rowcount
        if n:
            logger.info("TradeLog CLOSE %s outcome=%s pnl=%.2f%%", symbol, outcome, pnl_pct)
        else:
            logger.warning("TradeLog: açık trade bulunamadı — %s", symbol)

    def get_closed_trades(self, since_days: int = 90) -> List[Dict]:
        sql = """
            SELECT * FROM trades
            WHERE closed_at IS NOT NULL
              AND opened_at >= datetime('now', ?)
            ORDER BY opened_at ASC
        """
        with self._conn() as c:
            c.row_factory = sqlite3.Row
            rows = c.execute(sql, (f"-{since_days} days",)).fetchall()
        return [dict(r) for r in rows]

    def get_open_trades(self) -> List[Dict]:
        sql = "SELECT * FROM trades WHERE closed_at IS NULL ORDER BY opened_at ASC"
        with self._conn() as c:
            c.row_factory = sqlite3.Row
            rows = c.execute(sql).fetchall()
        return [dict(r) for r in rows]

    def summary(self, since_days: int = 90) -> str:
        closed = self.get_closed_trades(since_days)
        opened = self.get_open_trades()
        if not closed:
            return f"Son {since_days} günde kapanmış trade yok. Açık: {len(opened)}"
        counts: Dict[str, int] = {}
        for t in closed:
            o = t.get("outcome") or "?"
            counts[o] = counts.get(o, 0) + 1
        lines = [f"Son {since_days} gün — {len(closed)} kapanmış trade:"]
        for o, n in sorted(counts.items()):
            lines.append(f"  {o}: {n}")
        lines.append(f"Açık pozisyon: {len(opened)}")
        return "\n".join(lines)
