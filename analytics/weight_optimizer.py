"""
Manuel ağırlık kalibrasyon aracı.

Kullanım:
    cd bist-telegram-bot
    python analytics/weight_optimizer.py          # son 90 gün
    python analytics/weight_optimizer.py 60       # son 60 gün
    python analytics/weight_optimizer.py 30 --dry # sadece rapor, dosyayı kaydetme

Sonuç:
    config/pattern_weights.json güncellenir.
    Bir sonraki bot çalıştırmasında yeni ağırlıklar devreye girer.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Dict, List

BASE_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(BASE_DIR))

from analytics.trade_logger import TradeLogger  # noqa: E402

WEIGHTS_PATH = BASE_DIR / "config" / "pattern_weights.json"
DB_PATH = BASE_DIR / "data" / "trade_log.db"

DEFAULT_WEIGHTS: Dict[str, float] = {
    "bullish engulfing":   2.0,
    "bearish engulfing":   2.0,
    "bullish pin bar":     2.0,
    "bearish pin bar":     2.0,
    "inside bar":          0.0,
    "support reaction":    1.0,
    "resistance breakout": 1.0,
    "breakout retest":     1.0,
    "HH/HL structure":     1.0,
    "LH/LL structure":     1.0,
}

# Outcome → sayısal skor (-1 kayıp, +1 tam kazanç)
OUTCOME_SCORE: Dict[str, float] = {
    "TP2":      1.0,
    "SL_BE":    0.3,   # TP1 geldi sonra breakeven stop
    "EXPIRED":  0.0,
    "SL":      -1.0,
}

WEIGHT_MIN = 0.2
WEIGHT_MAX = 4.0
LEARNING_RATE = 0.20   # ağırlık değişim hızı
MIN_TRADES = 3         # güncelleme için minimum trade sayısı


def _score(outcome: str) -> float:
    return OUTCOME_SCORE.get(outcome, 0.0)


def analyze(trades: List[dict]) -> Dict[str, dict]:
    """Her pattern için kazanç istatistiği hesaplar."""
    stats: Dict[str, dict] = {}
    for t in trades:
        try:
            patterns = json.loads(t.get("patterns") or "[]")
        except Exception:
            continue
        s = _score(t.get("outcome", ""))
        for p in patterns:
            if p not in stats:
                stats[p] = {"count": 0, "score_sum": 0.0, "wins": 0}
            stats[p]["count"] += 1
            stats[p]["score_sum"] += s
            if s > 0.1:
                stats[p]["wins"] += 1
    return {
        p: {
            "count":     v["count"],
            "avg_score": round(v["score_sum"] / v["count"], 3),
            "win_rate":  round(v["wins"] / v["count"] * 100, 1),
        }
        for p, v in stats.items()
    }


def calibrate(since_days: int = 90, dry_run: bool = False) -> None:
    current = (
        json.loads(WEIGHTS_PATH.read_text(encoding="utf-8"))
        if WEIGHTS_PATH.exists()
        else dict(DEFAULT_WEIGHTS)
    )

    if not DB_PATH.exists():
        print(f"Trade log bulunamadı: {DB_PATH}")
        print("Bot henüz sinyal kaydetmemiş olabilir.")
        return

    tl = TradeLogger(DB_PATH)
    trades = tl.get_closed_trades(since_days=since_days)

    print(f"\n{'=' * 72}")
    print(f"  BIST Bot — Ağırlık Kalibrasyon Raporu")
    print(f"  Son {since_days} gün  |  {len(trades)} kapanmış trade")
    if dry_run:
        print("  [DRY RUN — dosya kaydedilmeyecek]")
    print(f"{'=' * 72}\n")

    print(tl.summary(since_days))
    print()

    if not trades:
        print("Yeterli veri yok, ağırlıklar değişmedi.")
        return

    perf = analyze(trades)
    new_weights = dict(current)

    col = f"{'Pattern':<25} {'İşlem':>6} {'Win%':>7} {'Ort Skor':>9} {'Eski':>6} {'Yeni':>6}  ∆"
    print(col)
    print("─" * len(col))

    all_patterns = sorted(set(list(DEFAULT_WEIGHTS.keys()) + list(perf.keys())))
    changed = False
    for pattern in all_patterns:
        old_w = current.get(pattern, DEFAULT_WEIGHTS.get(pattern, 1.0))
        if pattern not in perf or perf[pattern]["count"] < MIN_TRADES:
            cnt = perf[pattern]["count"] if pattern in perf else 0
            print(f"  {pattern:<23} {cnt:>6}  — yetersiz veri, atlandı")
            continue

        p = perf[pattern]
        avg = p["avg_score"]
        # Orantılı düzeltme: mevcut ağırlıkla çarpılır, küçük ağırlıklar çok azalmaz
        adj = LEARNING_RATE * avg * old_w
        new_w = round(max(WEIGHT_MIN, min(WEIGHT_MAX, old_w + adj)), 3)
        new_weights[pattern] = new_w
        changed = changed or abs(new_w - old_w) > 0.001

        arrow = "↑" if new_w > old_w + 0.001 else ("↓" if new_w < old_w - 0.001 else "→")
        print(
            f"  {pattern:<23} {p['count']:>6} {p['win_rate']:>6.1f}% {avg:>9.3f} "
            f"{old_w:>6.2f} {new_w:>6.3f}  {arrow}"
        )

    print()
    if not changed:
        print("Değişecek ağırlık yok (veri yetersiz veya değişim sıfır).")
        return

    if dry_run:
        print("[DRY RUN] Dosya yazılmadı.")
        return

    WEIGHTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    WEIGHTS_PATH.write_text(json.dumps(new_weights, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Ağırlıklar kaydedildi → {WEIGHTS_PATH}")
    print("Bir sonraki bot başlatmasında yeni ağırlıklar aktif olur.")


if __name__ == "__main__":
    args = sys.argv[1:]
    dry = "--dry" in args
    days_args = [a for a in args if a.isdigit()]
    days = int(days_args[0]) if days_args else 90
    calibrate(since_days=days, dry_run=dry)
