"""Telegram mesaj metinleri. Tüm sayılar Türkçe biçimde (182,40)."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Dict, Iterable, List, Optional

from bot_v4.analyst import AI_SAT, AI_TUT, Review
from strategy_v4.live import Evaluation

PATTERN_NAMES = {
    "morning_star": "sabah yıldızı", "three_white_soldiers": "üç beyaz asker",
    "bullish_engulfing": "yutan boğa", "piercing": "delen mum", "hammer": "çekiç",
    "bullish_marubozu": "boğa marubozu", "bullish_harami": "boğa harami",
    "evening_star": "akşam yıldızı", "three_black_crows": "üç kara karga",
    "bearish_engulfing": "yutan ayı", "dark_cloud": "kara bulut",
    "shooting_star": "kayan yıldız", "hanging_man": "asılı adam",
}
DISCLAIMER = "Eminlik bir garanti değildir. 15 dk gecikmeli veri. Yatırım tavsiyesi değildir."
BROKER_NOTE = "Aracı kurumda stop ve TP emirlerini önceden girmen önerilir."


def tl(value: float) -> str:
    text = f"{value:,.2f}"
    return text.replace(",", "X").replace(".", ",").replace("X", ".")


def pct(value: float) -> str:
    return f"{value * 100:+.1f}%".replace(".", ",")


def _tag(paper: bool) -> str:
    return "[KAĞIT] " if paper else ""


def _slot_text(ev: Evaluation, slot: str = "", when: str = "") -> str:
    """Örn. "S1 10:30 (gün içi, mum kapanmadı)" veya "S5 gün sonu (kapanış teyitli)"."""
    name = slot or (ev.scan_slot or "")
    stamp = when or (ev.as_of_time.strftime("%H:%M") if ev.as_of_time else "")
    tail = "(gün içi, mum kapanmadı)" if ev.provisional else "gün sonu (kapanış teyitli)"
    return " ".join(part for part in (name, stamp, tail) if part)


def breakdown_line(ev: Evaluation) -> str:
    line = (f"Trend {ev.trend_pts:.0f} | Güç {ev.momentum_pts:.0f} | "
            f"Hacim {ev.volume_pts:.0f} | Mum {ev.candle_pts:.0f}")
    if ev.patterns:
        line += " (" + ", ".join(PATTERN_NAMES.get(p, p) for p in ev.patterns) + ")"
    return line


def buy_message(ev: Evaluation, review: Review, view: Dict[str, Any], cfg, strategy) -> str:
    """🟢 AL — dört koşulu geçen aday (spesifikasyon Bölüm 4)."""
    lv = view["levels"]
    plan = view["plan"]
    qty = int(plan["qty"])
    entry = view["max_entry"]
    frac1 = int(qty * strategy.tp1_sell_frac)
    frac2 = int(qty * strategy.tp2_sell_frac)
    rest = qty - frac1 - frac2
    lines = [
        f"{_tag(cfg.paper_mode)}🟢 AL | {_slot_text(ev, view.get('slot', ''), view.get('when', ''))}",
        f"{ev.symbol} — EMİNLİK {view['eminlik']:.0f}/100",
        f"Kural skoru {ev.score:.0f} | AI olasılığı {review.olasilik:.0f} "
        f"({strategy.tp1_horizon_sessions} seansta önce TP1)",
        breakdown_line(ev),
        f"Alım sınırı: {tl(entry)} TL ({view['valid_text']})",
        f"Adet: {qty} | Gerçek risk: {pct(plan['risk_pct'])[1:]} ({tl(plan['risk_try'])} TL)",
        f"Stop  {pct(lv['stop'] / entry - 1)} : {tl(lv['stop'])} TL",
        f"TP1  {pct(lv['tp1'] / entry - 1)} : {tl(lv['tp1'])} TL → {frac1} adet sat, stop girişe",
        f"TP2  {pct(lv['tp2'] / entry - 1)} : {tl(lv['tp2'])} TL → {frac2} adet sat, stop TP1'e",
        f"TP3  {pct(lv['tp3'] / entry - 1)} : {tl(lv['tp3'])} TL → kalan {rest} adet",
        f"Seviyeler gerçek alış fiyatına göre yeniden hesaplanır: /aldim {ev.symbol} FIYAT ADET",
    ]
    if review.summary:
        lines.append(f"AI özeti: {review.summary}")
    if review.reasons:
        lines.append("Gerekçe: " + " | ".join(review.reasons))
    if review.risks:
        lines.append("Riskler: " + " | ".join(review.risks))
    if review.resistances:
        lines.append("AI'ın gördüğü dirençler: " + " / ".join(tl(r) for r in review.resistances))
    for item in review.news[:3]:
        lines.append(f"Kaynak: {item.get('baslik', '')} ({item.get('tarih', '')}) {item.get('url', '')}")
    for item in review.missing[:2]:
        lines.append(f"Eksik: {item}")
    lines.append(DISCLAIMER)
    return "\n".join(lines)


def ai_missing_message(ev: Evaluation, review: Review, paper: bool) -> str:
    return "\n".join([
        f"{_tag(paper)}⚠️ KURAL AL, AI YOK | {_slot_text(ev)}",
        f"{ev.symbol} — Kural skoru {ev.score:.0f}/100",
        breakdown_line(ev),
        f"AI incelemesi yapılamadı ({review.error}). AL sayılmaz, elle incele.",
        DISCLAIMER,
    ])


def alma_message(ev: Evaluation, review: Review, eminlik: float, paper: bool) -> str:
    lines = [
        f"{_tag(paper)}⛔ ALMA | {_slot_text(ev)}",
        f"{ev.symbol} — Kural skoru {ev.score:.0f} | AI olasılığı "
        f"{review.olasilik:.0f} | eminlik {eminlik:.0f}",
    ]
    if review.summary:
        lines.append(f"AI özeti: {review.summary}")
    if review.risks:
        lines.append("Riskler: " + " | ".join(review.risks))
    lines.append("Gölge portföyde izleniyor. Yatırım tavsiyesi değildir.")
    return "\n".join(lines)


def tp_message(p: Dict[str, Any], ev: Any, paper: bool, managed: bool) -> str:
    """🟡 TP1 / 🟠 TP2 / 🟢 TP3 bildirimi."""
    icons = {"tp1": "🟡", "tp2": "🟠", "tp3": "🟢"}
    label = ev.kind.upper()
    entry = float(p["entry"])
    head = (f"{_tag(paper and managed)}{icons.get(ev.kind, '🟡')} {label} | "
            f"{p['symbol']} {tl(ev.price)} TL ({pct(ev.price / entry - 1)})")
    lines = [head]
    if ev.qty > 0:
        if ev.kind == "tp3":
            lines.append(f"Kalan {ev.qty} adedin tamamını sat.")
        else:
            target = "başa baş" if ev.kind == "tp1" else "TP1"
            lines.append(f"{ev.qty} adet sat. Kalan {ev.remaining_after} adet için stop "
                         f"{tl(ev.stop_after)} TL'ye ({target}) çekildi.")
    else:
        lines.append(f"Seviye geçildi ama adet bölünemedi. Stop {tl(ev.stop_after)} TL'ye çekildi.")
    if ev.qty > 0:
        lines.append(f"Sattıysan: /sattim {p['symbol']} FIYAT {ev.qty}")
    return "\n".join(lines)


def stop_message(p: Dict[str, Any], price: float, pnl: Optional[float], qty: int,
                 paper: bool) -> str:
    entry = float(p["entry"])
    lines = [
        f"{_tag(paper)}🔴 SAT | STOP — {p['symbol']} {tl(price)} TL ({pct(price / entry - 1)})",
        f"Kalan {qty} adedin tamamını sat.",
    ]
    if pnl is not None:
        lines.append(f"Kâr/zarar {tl(pnl)} TL")
    else:
        lines.append(f"Sattıysan: /sattim {p['symbol']} FIYAT")
    lines.append("15 dk gecikmeli veri, asıl stop emri aracı kurumda girilmeli.")
    return "\n".join(lines)


def close_message(p: Dict[str, Any], price: float, pnl: float, reason: str, paper: bool) -> str:
    names = {"stop": "stop", "score": "skor düşüşü", "ema50": "EMA50 altı",
             "time": "zaman stopu", "max_hold": "azami tutma süresi", "elle": "elle"}
    entry = float(p["entry"])
    return (f"{_tag(paper)}🔴 SAT | {names.get(reason, reason)}\n"
            f"{p['symbol']}: {tl(price)} TL ({pct(price / entry - 1)}) | "
            f"Kâr/zarar {tl(pnl)} TL")


def soft_exit_message(p: Dict[str, Any], ev: Evaluation, review: Optional[Review], paper: bool,
                      until: Optional[date] = None, expired: bool = False) -> str:
    prefix = _tag(paper and p["mode"] == "paper")
    if expired:
        return (f"{prefix}🔴 SAT | TUT süresi doldu\n{p['symbol']}: kural skoru "
                f"{ev.score:.0f}. Sonraki seans açılışında çık.")
    if review is not None and review.decision == AI_TUT:
        when = until.strftime("%d.%m") if until else "—"
        return (f"{prefix}🔵 TUT | Kural skoru {ev.score:.0f}, AI: TUT "
                f"(olasılık {review.olasilik:.0f})\n{p['symbol']}: koşul — "
                f"{review.hold_condition}\nEn geç {when} kapanışında yeniden "
                f"değerlendirilir, koşul olmazsa satılır.")
    ai = ""
    if review is not None and review.decision == AI_SAT and review.olasilik is not None:
        ai = f", AI: SAT (olasılık {review.olasilik:.0f})"
    elif review is not None and review.error:
        ai = f", AI yok ({review.error})"
    reason = review.summary if review and review.summary else "kural tarafı zayıfladı"
    return (f"{prefix}🔴 SAT | Kural skoru {ev.score:.0f}{ai} — yarın açılışta çık.\n"
            f"{p['symbol']}: {reason}")


def position_lines(rows: Iterable[Dict[str, Any]], prices: Dict[str, float]) -> List[str]:
    lines = []
    for p in rows:
        last = prices.get(p["symbol"])
        qty = int(p.get("remaining_qty") or p["qty"])
        reached = p.get("reached_level") or ""
        nxt = {"": "TP1", "tp1": "TP2", "tp2": "TP3", "tp3": "—"}.get(reached, "TP1")
        if last is None:
            lines.append(f"{p['symbol']}: fiyat yok | {qty} adet")
            continue
        lines.append(
            f"{p['symbol']}: {pct(last / p['entry'] - 1.0)} | {qty} adet | "
            f"stop {tl(p['stop'])} | sıradaki {nxt}"
        )
    return lines


def scan_report(time_text: str, slot: str, index_change: Optional[float], positions: List[str],
                near: List[str], signals: List[str], errors: List[str], scanned: int,
                tally: Optional[Dict[str, Any]] = None) -> str:
    """Her sorgudan sonra tek satır özet; sinyal veya bloke varsa kısa liste."""
    market = pct(index_change) if index_change is not None else "yok"
    label = f"{slot} " if slot else ""
    head = f"🕐 {label}{time_text} | {scanned} hisse | "
    shadow = _shadow_line(tally) if tally else ""
    if not signals and not errors:
        short = head + f"sinyal yok | XU100 {market}"
        return short + (f"\n{shadow}" if shadow else "")
    lines = [head + f"XU100 {market}"]
    if signals:
        lines += ["", *signals]
    if positions:
        lines += ["", "Pozisyonlar", *positions]
    if near:
        lines += ["", "Eşiğe yakın: " + ", ".join(near)]
    if shadow:
        lines.append(shadow)
    if errors:
        lines.append("Veri alınamayanlar: " + ", ".join(errors))
    return "\n".join(lines)


def _shadow_line(tally: Dict[str, Any]) -> str:
    bits = []
    if tally.get("esik_alti"):
        names = [n for n in tally.get("golge_liste", []) if " " in n]
        detail = f" ({', '.join(names)})" if names else ""
        bits.append(f"Eşik altı: {tally['esik_alti']}{detail}")
    if tally.get("alma"):
        names = [n for n in tally.get("golge_liste", []) if " " not in n]
        detail = f" ({', '.join(names)})" if names else ""
        bits.append(f"AI ALMA: {tally['alma']}{detail}")
    return " | ".join(bits) + (" — gölgede izleniyor" if bits else "")


def daily_report(now: datetime, cfg, evaluations: Dict[str, Evaluation], prices: Dict[str, float],
                 fresh: List[str], blocked: List[str], exits: List[str], errors: List[str],
                 index, positions: List[Dict[str, Any]], equity: float,
                 tally: Optional[Dict[str, Any]] = None) -> str:
    change = float(index.iloc[-1] / index.iloc[-2] - 1.0) if len(index) > 1 else 0.0
    start = cfg.portfolio_try
    lines = [
        f"🌙 GÜN SONU {now:%d.%m.%Y}" + (" [KAĞIT]" if cfg.paper_mode else ""),
        f"XU100: {pct(change)}",
        f"Portföy: {tl(equity)} TL ({pct(equity / start - 1.0)})",
    ]
    rows = position_lines(positions, prices)
    if rows:
        lines += ["", "Pozisyonlar", *rows]
    if fresh or blocked:
        lines += ["", "Sinyaller", *fresh, *blocked]
    if exits:
        lines += ["", "SAT: " + ", ".join(exits)]
    if tally:
        extra = _shadow_line(tally)
        if extra:
            lines += ["", extra]
    ranked = sorted(evaluations.values(), key=lambda e: -e.score)[:5]
    if ranked:
        lines += ["", "En yüksek skorlar",
                  *[f"{e.symbol}: {e.score:.0f}" + ("" if e.eligible else f" ({e.reasons[0]})")
                    for e in ranked]]
    if errors:
        lines += ["", "Veri alınamayanlar: " + ", ".join(errors)]
    return "\n".join(lines)


EMINLIK_BUCKETS = ((75, 80, "75-79"), (80, 85, "80-84"), (85, 90, "85-89"), (90, 101, "90+"))


def performance_report(now: datetime, cfg, store, remaining_of) -> str:
    """Kağıt, gölge ve gerçek sonuçlar + eminlik kalibrasyon tablosu."""
    lines = [f"📊 PERFORMANS {now:%d.%m.%Y}"]
    names = {"paper": "Kağıt (kural + AI)", "real": "Gerçek",
             "shadow": "Gölge (eşiği geçmeyenler)"}
    for mode, name in names.items():
        closed = store.closed_positions(mode)
        opened = store.open_positions(mode)
        if not closed and not opened:
            continue
        pnl = sum(p["pnl"] or 0.0 for p in closed)
        wins = sum(1 for p in closed if (p["pnl"] or 0.0) > 0)
        ratio = f"%{wins / len(closed) * 100:.0f}" if closed else "yok"
        lines.append(f"{name}: {len(closed)} kapalı, {len(opened)} açık | "
                     f"Kazanma {ratio} | Kâr/zarar {tl(pnl)} TL")

    reasons: Dict[str, int] = {}
    for mode in ("paper", "real"):
        for p in store.closed_positions(mode):
            for fill in store.fills(p["id"]):
                reasons[fill["kind"]] = reasons.get(fill["kind"], 0) + 1
    if reasons:
        total = sum(reasons.values())
        parts = [f"{k.upper()} %{v / total * 100:.0f}" for k, v in sorted(reasons.items())]
        lines.append("Çıkış dağılımı: " + ", ".join(parts))

    rows = store.equity_rows(cfg.active_mode)
    if len(rows) >= 2 and rows[0]["xu100"] and rows[-1]["xu100"]:
        mine = rows[-1]["equity"] / cfg.portfolio_try - 1.0
        bench = rows[-1]["xu100"] / rows[0]["xu100"] - 1.0
        lines.append(f"Portföy {pct(mine)} | Aynı dönem XU100 {pct(bench)}")

    lines += ["", "Kalibrasyon (eminlik dilimi → TP1'e ulaşma)"]
    lines += _calibration(store)
    ai = store.ai_summary()
    lines.append(f"AI kararları: {_decisions(ai['decisions'])} | "
                 f"Gerçek maliyet ${ai['cost_usd']:.2f}")
    return "\n".join(lines)


def _decisions(data: Dict[str, int]) -> str:
    return ", ".join(f"{k} {v}" for k, v in sorted(data.items())) or "yok"


def _calibration(store) -> List[str]:
    """Eminlik dilimlerine göre TP1 oranı. 20 işlemden az dilim 'yetersiz' yazar."""
    trades: List[tuple] = []
    for mode in ("paper", "real", "shadow"):
        for p in store.closed_positions(mode):
            cand = store.candidate(p["candidate_id"]) if p["candidate_id"] else None
            eminlik = (cand or {}).get("eminlik")
            if eminlik is None:
                continue
            kinds = {f["kind"] for f in store.fills(p["id"])}
            trades.append((float(eminlik), bool(kinds & {"tp1", "tp2", "tp3"}),
                           (p["exit_price"] or p["entry"]) / p["entry"] - 1.0))
    out = []
    buckets = list(EMINLIK_BUCKETS) + [(60, 75, "60-74 (gölge)")]
    for lo, hi, label in buckets:
        group = [t for t in trades if lo <= t[0] < hi]
        if not group:
            continue
        if len(group) < 20:
            out.append(f"  {label}: {len(group)} işlem — yetersiz")
            continue
        tp1 = sum(1 for t in group if t[1]) / len(group) * 100
        avg = sum(t[2] for t in group) / len(group) * 100
        out.append(f"  {label}: {len(group)} işlem | TP1 %{tp1:.0f} | ort getiri %{avg:+.1f}")
    return out or ["  henüz kapanmış işlem yok"]
