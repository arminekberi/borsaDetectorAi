"""Telegram komut mantığı. Bu dosya Telegram'a bağlı değildir, doğrudan test edilir."""

from __future__ import annotations

import re
from datetime import datetime
from typing import List, Optional

from bot_v4 import messages as msg
from bot_v4.analyst import AI_AL
from bot_v4.engine import Engine
from strategy_v4.sizing import initial_stop

HELP = """Komutlar
/izle SEMBOL — izleme listesine ekle
/birak SEMBOL — listeden çıkar
/takip — izleme listesi
/aldim SEMBOL FIYAT ADET — gerçek alımı kaydet
/sattim SEMBOL FIYAT — gerçek satışı kaydet
/portfoy — açık pozisyonlar
/liste — son tarama skorları
/performans — sonuç özeti
/tara — hemen ön sinyal taraması yap
/yardim — bu mesaj"""

SYMBOL = re.compile(r"^[A-Z0-9]{3,8}$")


def _symbol(raw: str) -> Optional[str]:
    text = raw.strip().upper().replace(".IS", "")
    return text if SYMBOL.match(text) else None


def _number(raw: str) -> Optional[float]:
    text = raw.strip().replace(" ", "")
    if "," in text:
        text = text.replace(".", "").replace(",", ".")
    try:
        value = float(text)
    except ValueError:
        return None
    return value if value > 0 else None


def izle(engine: Engine, args: List[str], now: datetime) -> str:
    if len(args) != 1 or (symbol := _symbol(args[0])) is None:
        return "Kullanım: /izle SEMBOL"
    if symbol in engine.store.watchlist():
        return f"{symbol} zaten listede."
    if not engine.market.validate_symbol(symbol, now.date()):
        return f"{symbol} için veri bulunamadı. Hisse kodunu kontrol et."
    engine.store.add_symbol(symbol, now)
    return f"{symbol} eklendi. Listede {len(engine.store.watchlist())} hisse var."


def birak(engine: Engine, args: List[str], now: datetime) -> str:
    if len(args) != 1 or (symbol := _symbol(args[0])) is None:
        return "Kullanım: /birak SEMBOL"
    if not engine.store.remove_symbol(symbol):
        return f"{symbol} listede yok."
    open_now = [p for p in engine.store.open_positions() if p["symbol"] == symbol and p["mode"] != "shadow"]
    note = " Açık pozisyon kapanana kadar takip sürecek." if open_now else ""
    return f"{symbol} çıkarıldı.{note}"


def takip(engine: Engine, args: List[str], now: datetime) -> str:
    symbols = engine.store.watchlist()
    return f"İzleme listesi ({len(symbols)}): " + (", ".join(symbols) if symbols else "boş")


def aldim(engine: Engine, args: List[str], now: datetime) -> str:
    if len(args) != 3:
        return "Kullanım: /aldim SEMBOL FIYAT ADET"
    symbol, price, qty = _symbol(args[0]), _number(args[1]), _number(args[2])
    if symbol is None or price is None or qty is None or qty != int(qty):
        return "Kullanım: /aldim SEMBOL FIYAT ADET"
    if engine.store.open_position_for(symbol, "real"):
        return f"{symbol} için açık gerçek pozisyon zaten var."
    cand = engine.store.active_for(symbol)
    atr = cand["atr"] if cand else None
    if atr is None:
        ev = engine.last_evaluations.get(symbol)
        atr = ev.atr if ev else None
    if atr is None:
        from strategy_v4.features import wilder_atr

        daily = engine.market.daily(symbol, now.date())
        atr = float(wilder_atr(daily["high"], daily["low"], daily["close"], 14).iloc[-1])
    stop = initial_stop(price, atr, engine.strategy)
    engine.store.open_position(symbol=symbol, mode="real", entry=price, qty=int(qty), stop=stop, atr=atr,
                               candidate_id=cand["id"] if cand else None, decision=AI_AL if cand else "ELLE", opened_at=now.isoformat())
    if cand:
        engine.store.update_candidate(cand["id"], status="filled")
    return f"{symbol} kaydedildi: {int(qty)} adet, {msg.tl(price)} TL.\nBağlayıcı stop: {msg.tl(stop)} TL. Aracı kurumda stop emrini gir."


def sattim(engine: Engine, args: List[str], now: datetime) -> str:
    if len(args) != 2:
        return "Kullanım: /sattim SEMBOL FIYAT"
    symbol, price = _symbol(args[0]), _number(args[1])
    if symbol is None or price is None:
        return "Kullanım: /sattim SEMBOL FIYAT"
    position = engine.store.open_position_for(symbol, "real")
    if position is None:
        return f"{symbol} için açık gerçek pozisyon yok."
    pnl = engine._pnl(position, price)
    engine.store.close_position(position["id"], price, now, "elle", pnl)
    return f"{symbol} kapatıldı: {msg.tl(price)} TL ({msg.pct(price / position['entry'] - 1.0)}) | Kâr/zarar {msg.tl(pnl)} TL"


def portfoy(engine: Engine, args: List[str], now: datetime) -> str:
    rows = [p for p in engine.store.open_positions() if p["mode"] != "shadow"]
    if not rows:
        return "Açık pozisyon yok."
    lines = ["Açık pozisyonlar"]
    for p in rows:
        tag = "KAĞIT" if p["mode"] == "paper" else "GERÇEK"
        lines.append(f"{p['symbol']} [{tag}]: {p['qty']} adet | Giriş {msg.tl(p['entry'])} | Stop {msg.tl(p['stop'])}")
    return "\n".join(lines)


def liste(engine: Engine, args: List[str], now: datetime) -> str:
    rows = sorted(engine.last_evaluations.values(), key=lambda e: -e.score)
    if not rows:
        return "Henüz tarama sonucu yok. /tara yazabilirsin."
    lines = ["Son tarama"]
    for e in rows[:20]:
        state = "uygun" if e.eligible else e.reasons[0]
        lines.append(f"{e.symbol}: {e.score:.0f} ({state})")
    return "\n".join(lines)


def performans(engine: Engine, args: List[str], now: datetime) -> str:
    return engine.performance_text(now)


def yardim(engine: Engine, args: List[str], now: datetime) -> str:
    return HELP


COMMANDS = {
    "izle": izle, "birak": birak, "takip": takip, "aldim": aldim, "sattim": sattim,
    "portfoy": portfoy, "liste": liste, "performans": performans, "yardim": yardim, "start": yardim,
}
