"""Telegram komut mantığı. Bu dosya Telegram'a bağlı değildir, doğrudan test edilir."""

from __future__ import annotations

import re
from datetime import datetime
from typing import List, Optional

from bot_v4 import messages as msg
from bot_v4.analyst import AI_AL
from bot_v4.engine import Engine
from strategy_v4.exits import levels_for
from strategy_v4.sizing import stop_price

HELP = """Komutlar
/izle SEMBOL — izleme listesine ekle
/birak SEMBOL — listeden çıkar
/takip — izleme listesi
/aldim SEMBOL FIYAT ADET — gerçek alımı kaydet
/sattim SEMBOL FIYAT [ADET] — satışı kaydet (adet verilmezse tamamı)
/portfoy — açık pozisyonlar
/liste — son tarama skorları (sorgu adı ve saatiyle)
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
    lv = levels_for(price, atr, engine.strategy)
    engine.store.open_position(
        symbol=symbol, mode="real", entry=price, qty=int(qty), initial_qty=int(qty),
        remaining_qty=int(qty), stop=lv["stop"], tp1=lv["tp1"], tp2=lv["tp2"], tp3=lv["tp3"],
        exit_model=engine.strategy.exit_model, atr=atr,
        candidate_id=cand["id"] if cand else None,
        decision=AI_AL if cand else "ELLE", opened_at=now.isoformat(),
    )
    if cand:
        engine.store.update_candidate(cand["id"], status="filled")
    f1 = int(int(qty) * engine.strategy.tp1_sell_frac)
    f2 = int(int(qty) * engine.strategy.tp2_sell_frac)
    return "\n".join([
        f"{symbol} kaydedildi: {int(qty)} adet, {msg.tl(price)} TL.",
        f"Stop {msg.tl(lv['stop'])} TL ({msg.pct(lv['stop'] / price - 1)}) — aracı kurumda emri gir.",
        f"TP1 {msg.tl(lv['tp1'])} → {f1} adet | TP2 {msg.tl(lv['tp2'])} → {f2} adet | "
        f"TP3 {msg.tl(lv['tp3'])} → kalan {int(qty) - f1 - f2} adet",
    ])


def sattim(engine: Engine, args: List[str], now: datetime) -> str:
    """
    `/sattim SEMBOL FIYAT [ADET]`.

    Adet verilmezse kalanın tamamı kapanır. Kısmi satışta kalan adet için
    seviyeler korunur; TP1 sonrası kısmi satış girilirse stop başa başa çekilir.
    """
    if len(args) not in (2, 3):
        return "Kullanım: /sattim SEMBOL FIYAT [ADET]"
    symbol, price = _symbol(args[0]), _number(args[1])
    if symbol is None or price is None:
        return "Kullanım: /sattim SEMBOL FIYAT [ADET]"
    position = engine.store.open_position_for(symbol, "real")
    if position is None:
        return f"{symbol} için açık gerçek pozisyon yok."
    remaining = engine._remaining(position)
    qty = remaining
    if len(args) == 3:
        raw = _number(args[2])
        if raw is None or raw != int(raw):
            return "Kullanım: /sattim SEMBOL FIYAT [ADET]"
        qty = int(raw)
        if qty > remaining:
            return f"{symbol} için kalan adet {remaining}. Daha fazlası satılamaz."

    pnl = engine._pnl(position, price, qty)
    stop = None
    reached = position.get("reached_level") or ""
    if qty < remaining and reached in ("", "tp1"):
        # TP1 sonrası (veya ilk) kısmi satışta stop başa başa çekilir
        stop = max(float(position["stop"]), float(position["entry"]))
        reached = reached or "tp1"
    engine.store.sell_part(position["id"], "elle", price, qty, pnl, now,
                           stop=stop, reached=reached if stop is not None else None)
    left = remaining - qty
    head = (f"{symbol}: {qty} adet {msg.tl(price)} TL'den satıldı "
            f"({msg.pct(price / position['entry'] - 1.0)}) | Kâr/zarar {msg.tl(pnl)} TL")
    if left <= 0:
        return head + "\nPozisyon kapandı."
    lines = [head, f"Kalan {left} adet için seviyeler geçerli."]
    if stop is not None:
        lines.append(f"Stop başa başa çekildi: {msg.tl(stop)} TL.")
    return "\n".join(lines)


def portfoy(engine: Engine, args: List[str], now: datetime) -> str:
    rows = [p for p in engine.store.open_positions() if p["mode"] != "shadow"]
    if not rows:
        return "Açık pozisyon yok."
    lines = ["Açık pozisyonlar"]
    for p in rows:
        tag = "KAĞIT" if p["mode"] == "paper" else "GERÇEK"
        qty = engine._remaining(p)
        reached = p.get("reached_level") or ""
        nxt = {"": "TP1", "tp1": "TP2", "tp2": "TP3", "tp3": "—"}.get(reached, "TP1")
        target = p.get(f"tp{nxt[-1]}") if nxt.startswith("TP") else None
        extra = f" | sıradaki {nxt}" + (f" {msg.tl(target)}" if target else "")
        lines.append(f"{p['symbol']} [{tag}]: {qty}/{p.get('initial_qty') or p['qty']} adet | "
                     f"Giriş {msg.tl(p['entry'])} | Stop {msg.tl(p['stop'])}{extra}")
    return "\n".join(lines)


def liste(engine: Engine, args: List[str], now: datetime) -> str:
    rows = sorted(engine.last_evaluations.values(), key=lambda e: -e.score)
    if not rows:
        return "Henüz tarama sonucu yok. /tara yazabilirsin."
    first = rows[0]
    when = first.as_of_time.strftime("%H:%M") if first.as_of_time else "gün sonu"
    slot = first.scan_slot or ("gün içi" if first.provisional else "kapanış")
    lines = [f"Son tarama — {slot} {when}"]
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
