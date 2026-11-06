"""Telegram mesaj metinleri."""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional

from bot_v4.analyst import AI_AL, AI_ALMA, AI_BEKLE, Review
from strategy_v4.live import Evaluation

PATTERN_NAMES = {
    "morning_star": "sabah yıldızı", "three_white_soldiers": "üç beyaz asker", "bullish_engulfing": "yutan boğa",
    "piercing": "delen mum", "hammer": "çekiç", "bullish_marubozu": "boğa marubozu", "bullish_harami": "boğa harami",
    "evening_star": "akşam yıldızı", "three_black_crows": "üç kara karga", "bearish_engulfing": "yutan ayı",
    "dark_cloud": "kara bulut", "shooting_star": "kayan yıldız", "hanging_man": "asılı adam",
}


def tl(value: float) -> str:
    text = f"{value:,.2f}"
    return text.replace(",", "X").replace(".", ",").replace("X", ".")


def pct(value: float) -> str:
    return f"{value * 100:+.1f}%".replace(".", ",")


def _tag(paper: bool) -> str:
    return "[KAĞIT] " if paper else ""


def breakdown_line(ev: Evaluation) -> str:
    line = f"Trend {ev.trend_pts:.0f} | Güç {ev.momentum_pts:.0f} | Hacim {ev.volume_pts:.0f} | Mum {ev.candle_pts:.0f}"
    if ev.patterns:
        line += "\nMum: " + ", ".join(PATTERN_NAMES.get(p, p) for p in ev.patterns)
    return line


def ai_block(review: Review) -> List[str]:
    lines: List[str] = []
    if review.summary:
        lines.append(f"AI özeti: {review.summary}")
    plan = review.plan or {}
    if review.decision == AI_AL and plan:
        lines.append(
            f"AI planı: Giriş {tl(float(plan['giris_alt']))}-{tl(float(plan['giris_ust']))} | "
            f"Hedef {tl(float(plan['hedef_1']))}-{tl(float(plan['hedef_2']))} | AI stopu {tl(float(plan['stop']))} (bilgi)"
        )
    if review.counter_thesis:
        lines.append(f"Karşı tez: {review.counter_thesis[0]}")
    if review.invalidation and review.decision == AI_AL:
        lines.append(f"Geçersizlik: {review.invalidation}")
    for item in review.news[:3]:
        lines.append(f"Kaynak: {item.get('baslik', '')} ({item.get('tarih', '')}) {item.get('url', '')}")
    for item in review.missing[:2]:
        lines.append(f"Eksik: {item}")
    return lines


def signal_message(ev: Evaluation, review: Review, candidate: Dict[str, Any], paper: bool) -> str:
    kind = "ÖN SİNYAL (gün içi, kapanmamış mum)" if ev.provisional else "SİNYAL (kapanış teyitli)"
    head = f"{ev.symbol} — Skor {ev.score:.0f}/100"
    if review.decision == AI_AL:
        title = f"{_tag(paper)}🟢 AL | {kind}"
        ai = f"AI: AL (güven {review.confidence:.0f}/100)"
    elif review.decision == AI_BEKLE:
        title = f"{_tag(paper)}🟡 BEKLE | {kind}"
        ai = f"AI: BEKLE (güven {review.confidence:.0f}/100)\nKoşul: {review.wait_condition}"
    elif review.decision == AI_ALMA:
        title = f"{_tag(paper)}⛔ ALMA | {kind}"
        ai = f"AI: ALMA (güven {review.confidence:.0f}/100)"
    else:
        title = f"{_tag(paper)}⚠️ KURAL AL, AI YOK | {kind}"
        ai = f"AI incelemesi yapılamadı ({review.error}). AL sayılmaz, elle incele."
    lines = [title, head, breakdown_line(ev)]
    if review.decision == AI_AL:
        lines.extend([
            f"Alım sınırı: {tl(candidate['max_entry'])} TL ({candidate['valid_text']})",
            f"Stop: {tl(candidate['stop'])} TL | Adet: {candidate['qty']}",
        ])
    lines.append(ai)
    lines.extend(ai_block(review))
    lines.append("15 dk gecikmeli veri. Yatırım tavsiyesi değildir.")
    return "\n".join(lines)


def position_lines(rows: Iterable[Dict[str, Any]], prices: Dict[str, float]) -> List[str]:
    lines = []
    for p in rows:
        last = prices.get(p["symbol"])
        if last is None:
            lines.append(f"{p['symbol']}: fiyat yok")
            continue
        lines.append(f"{p['symbol']}: {pct(last / p['entry'] - 1.0)} | Stopa uzaklık {pct(last / p['stop'] - 1.0)}")
    return lines


def scan_report(time_text: str, index_change: Optional[float], positions: List[str], near: List[str], new_signals: List[str], errors: List[str]) -> str:
    market = pct(index_change) if index_change is not None else "yok"
    if not new_signals and not errors:
        return f"🕐 {time_text} Yeni sinyal yok | XU100 {market} | Açık pozisyon: {len(positions)}"
    lines = [f"🕐 {time_text} RAPOR (15 dk gecikmeli)", f"XU100: {market}"]
    if new_signals:
        lines += ["", "Yeni sinyaller", *new_signals]
    if positions:
        lines += ["", "Pozisyonlar", *positions]
    if near:
        lines += ["", "Eşiğe yakın", *near]
    if errors:
        lines += ["", "Veri alınamayanlar: " + ", ".join(errors)]
    return "\n".join(lines)
