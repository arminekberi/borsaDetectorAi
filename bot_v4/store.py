"""SQLite durum deposu. Bot yeniden başlasa da pozisyon, aday ve iş kayıtları korunur."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS watchlist (symbol TEXT PRIMARY KEY, added_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT NOT NULL, signal_date TEXT NOT NULL, stage TEXT NOT NULL,
    status TEXT NOT NULL, score REAL NOT NULL, breakdown TEXT NOT NULL, reference REAL NOT NULL, atr REAL NOT NULL,
    adv REAL NOT NULL, max_entry REAL NOT NULL, stop REAL NOT NULL, qty INTEGER NOT NULL, valid_until TEXT NOT NULL,
    decision TEXT NOT NULL, note TEXT NOT NULL DEFAULT '', limit_alerted INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_candidates_active ON candidates(status, symbol);
CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT NOT NULL, mode TEXT NOT NULL, status TEXT NOT NULL,
    entry REAL NOT NULL, qty INTEGER NOT NULL, stop REAL NOT NULL, atr REAL NOT NULL, candidate_id INTEGER,
    decision TEXT NOT NULL DEFAULT '', opened_at TEXT NOT NULL, exit_price REAL, closed_at TEXT, reason TEXT,
    pnl REAL, exit_flag TEXT, stop_alerted INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_positions_open ON positions(status, mode);
CREATE TABLE IF NOT EXISTS ai_reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT NOT NULL, review_date TEXT NOT NULL, decision TEXT NOT NULL,
    confidence REAL, payload TEXT NOT NULL, input_tokens INTEGER NOT NULL DEFAULT 0, output_tokens INTEGER NOT NULL DEFAULT 0,
    searches INTEGER NOT NULL DEFAULT 0, cost_usd REAL NOT NULL DEFAULT 0, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ai_reviews ON ai_reviews(symbol, review_date);
CREATE TABLE IF NOT EXISTS job_runs (job TEXT PRIMARY KEY, finished_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS equity (day TEXT NOT NULL, mode TEXT NOT NULL, equity REAL NOT NULL, xu100 REAL, PRIMARY KEY (day, mode));
"""


class Store:
    def __init__(self, path: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(path, check_same_thread=False, timeout=30)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(SCHEMA)
        self._db.commit()

    def _run(self, sql: str, args: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._db.execute(sql, tuple(args))
            self._db.commit()
            return cur

    def _all(self, sql: str, args: Iterable[Any] = ()) -> List[Dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self._db.execute(sql, tuple(args)).fetchall()]

    def _one(self, sql: str, args: Iterable[Any] = ()) -> Optional[Dict[str, Any]]:
        rows = self._all(sql, args)
        return rows[0] if rows else None

    # İzleme listesi
    def seed_watchlist(self, symbols: Iterable[str], now: datetime) -> None:
        if self.watchlist() or self.get("watchlist_seeded") == "1":
            return
        for s in symbols:
            self.add_symbol(s, now)
        self.set("watchlist_seeded", "1")

    def watchlist(self) -> List[str]:
        return [r["symbol"] for r in self._all("SELECT symbol FROM watchlist ORDER BY symbol")]

    def add_symbol(self, symbol: str, now: datetime) -> bool:
        return self._run("INSERT OR IGNORE INTO watchlist VALUES (?, ?)", (symbol, now.isoformat())).rowcount > 0

    def remove_symbol(self, symbol: str) -> bool:
        return self._run("DELETE FROM watchlist WHERE symbol = ?", (symbol,)).rowcount > 0

    # Adaylar
    def add_candidate(self, **f: Any) -> int:
        f["breakdown"] = json.dumps(f["breakdown"], ensure_ascii=False)
        cols = ", ".join(f)
        marks = ", ".join("?" for _ in f)
        return int(self._run(f"INSERT INTO candidates ({cols}) VALUES ({marks})", f.values()).lastrowid)

    def update_candidate(self, cid: int, **f: Any) -> None:
        sets = ", ".join(f"{k} = ?" for k in f)
        self._run(f"UPDATE candidates SET {sets} WHERE id = ?", [*f.values(), cid])

    def candidate(self, cid: int) -> Optional[Dict[str, Any]]:
        return self._one("SELECT * FROM candidates WHERE id = ?", (cid,))

    def active_candidates(self) -> List[Dict[str, Any]]:
        return self._all("SELECT * FROM candidates WHERE status = 'active' ORDER BY score DESC, id")

    def candidate_today(self, symbol: str, day: date) -> Optional[Dict[str, Any]]:
        return self._one("SELECT * FROM candidates WHERE symbol = ? AND signal_date = ? ORDER BY id DESC LIMIT 1", (symbol, day.isoformat()))

    def candidates_on(self, day: date) -> List[Dict[str, Any]]:
        return self._all("SELECT * FROM candidates WHERE signal_date = ? ORDER BY score DESC", (day.isoformat(),))

    def count_al_today(self, day: date) -> int:
        row = self._one("SELECT COUNT(*) n FROM candidates WHERE signal_date = ? AND decision = 'AL'", (day.isoformat(),))
        return int(row["n"])

    def settle_list(self, day: date) -> List[Dict[str, Any]]:
        """Kapanışta karara bağlanacak adaylar: aktifler ve bugün dolan ön sinyaller."""
        return self._all(
            "SELECT * FROM candidates WHERE status = 'active' OR (status = 'filled' AND stage = 'provisional' AND signal_date = ?) ORDER BY id",
            (day.isoformat(),),
        )

    def active_for(self, symbol: str) -> Optional[Dict[str, Any]]:
        return self._one("SELECT * FROM candidates WHERE symbol = ? AND status = 'active' ORDER BY id DESC LIMIT 1", (symbol,))

    # Pozisyonlar
    def open_position(self, **f: Any) -> int:
        f.setdefault("status", "open")
        cols = ", ".join(f)
        marks = ", ".join("?" for _ in f)
        return int(self._run(f"INSERT INTO positions ({cols}) VALUES ({marks})", f.values()).lastrowid)

    def update_position(self, pid: int, **f: Any) -> None:
        sets = ", ".join(f"{k} = ?" for k in f)
        self._run(f"UPDATE positions SET {sets} WHERE id = ?", [*f.values(), pid])

    def close_position(self, pid: int, price: float, moment: datetime, reason: str, pnl: float) -> None:
        self.update_position(pid, status="closed", exit_price=price, closed_at=moment.isoformat(), reason=reason, pnl=pnl)

    def open_positions(self, mode: Optional[str] = None) -> List[Dict[str, Any]]:
        if mode:
            return self._all("SELECT * FROM positions WHERE status = 'open' AND mode = ? ORDER BY id", (mode,))
        return self._all("SELECT * FROM positions WHERE status = 'open' ORDER BY id")

    def open_position_for(self, symbol: str, mode: str) -> Optional[Dict[str, Any]]:
        return self._one("SELECT * FROM positions WHERE status = 'open' AND mode = ? AND symbol = ?", (mode, symbol))

    def closed_positions(self, mode: Optional[str] = None) -> List[Dict[str, Any]]:
        if mode:
            return self._all("SELECT * FROM positions WHERE status = 'closed' AND mode = ? ORDER BY closed_at", (mode,))
        return self._all("SELECT * FROM positions WHERE status = 'closed' ORDER BY closed_at")

    def realized_pnl(self, mode: str) -> float:
        return float(self._one("SELECT COALESCE(SUM(pnl), 0) v FROM positions WHERE status = 'closed' AND mode = ?", (mode,))["v"])

    # AI
    def add_review(self, symbol: str, day: date, decision: str, confidence: Optional[float], payload: Dict[str, Any],
                   input_tokens: int, output_tokens: int, searches: int, cost: float, now: datetime) -> None:
        self._run(
            "INSERT INTO ai_reviews (symbol, review_date, decision, confidence, payload, input_tokens, output_tokens, searches, cost_usd, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (symbol, day.isoformat(), decision, confidence, json.dumps(payload, ensure_ascii=False), input_tokens, output_tokens, searches, cost, now.isoformat()),
        )

    def review_on(self, symbol: str, day: date) -> Optional[Dict[str, Any]]:
        row = self._one("SELECT * FROM ai_reviews WHERE symbol = ? AND review_date = ? ORDER BY id DESC LIMIT 1", (symbol, day.isoformat()))
        if row:
            row["payload"] = json.loads(row["payload"])
        return row

    def ai_month_cost(self, day: date) -> float:
        prefix = day.strftime("%Y-%m") + "%"
        return float(self._one("SELECT COALESCE(SUM(cost_usd), 0) v FROM ai_reviews WHERE review_date LIKE ?", (prefix,))["v"])

    def ai_summary(self) -> Dict[str, Any]:
        rows = self._all("SELECT decision, COUNT(*) n FROM ai_reviews GROUP BY decision")
        cost = self._one("SELECT COALESCE(SUM(cost_usd), 0) v FROM ai_reviews")["v"]
        return {"decisions": {r["decision"]: r["n"] for r in rows}, "cost_usd": float(cost)}

    # İş kaydı ve anahtar-değer
    def job_done(self, job: str) -> bool:
        return self._one("SELECT 1 FROM job_runs WHERE job = ?", (job,)) is not None

    def mark_job(self, job: str, now: datetime) -> None:
        self._run("INSERT OR REPLACE INTO job_runs VALUES (?, ?)", (job, now.isoformat()))

    def get(self, key: str, default: Optional[str] = None) -> Optional[str]:
        row = self._one("SELECT value FROM kv WHERE key = ?", (key,))
        return row["value"] if row else default

    def set(self, key: str, value: str) -> None:
        self._run("INSERT OR REPLACE INTO kv VALUES (?, ?)", (key, value))

    def record_equity(self, day: date, mode: str, equity: float, xu100: Optional[float]) -> None:
        self._run("INSERT OR REPLACE INTO equity VALUES (?, ?, ?, ?)", (day.isoformat(), mode, equity, xu100))

    def equity_rows(self, mode: str) -> List[Dict[str, Any]]:
        return self._all("SELECT * FROM equity WHERE mode = ? ORDER BY day", (mode,))
