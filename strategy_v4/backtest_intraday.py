"""
5 sorgu sisteminin gün içi (60m) backtest'i.

Canlıda çalışacak sistemin AYNISI geçmişte çalıştırılır: değerlendirme
`live.evaluate_at`, kısmi bar `intraday.partial_bar_at`, çıkışlar `exits.step`,
boyutlandırma `sizing`, muhasebe `backtest.Portfolio`. Strateji mantığının
ikinci kopyası YOKTUR.

İki aşama:

1. `build_signals(...)` — pahalı aşama. Her işlem günü ve her sorgu saatinde
   `evaluate_at` çağrılır; eşiği geçebilecek tüm değerlendirmeler tabloya yazılır.
   Bu tablo `buy_score`, `stop_pct`, TP seviyeleri, çıkış modeli ve işlem
   maliyetinden BAĞIMSIZDIR — o yüzden bir kez üretilip tüm varyantlarda
   yeniden kullanılır.
2. `simulate(...)` — ucuz aşama. Tabloyu okuyup dolum, çıkış ve portföy
   muhasebesini yürütür.

Günün akışı (60m barlar üzerinde, kronolojik):
- Önceki S5'te işaretlenen çıkışlar günün ilk barının açılışından yapılır.
- Her bar için: (a) açık pozisyonların çıkışları (dolum barı hariç),
  (b) o bardan önce biten sorguların adayları, (c) bekleyen emirlerin dolumu.
- Gün sonu (S5): kapanmış günlük barla aday, gün içi sinyal iptali, zaman
  stopu, yumuşak çıkış işareti.

AI ve eminlik skoru backtest'e DAHİL DEĞİLDİR. Sonuçlar "kural AL'ların hepsi
alınsaydı" üst sınırıdır.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, time
from typing import Dict, List, Optional, Sequence

import numpy as np
import pandas as pd

from strategy_v4.backtest import Portfolio, _locked_up
from strategy_v4.config import StrategyConfig
from strategy_v4.exits import PositionState, levels_for, step, time_exit_due
from strategy_v4.features import compute_features, compute_score
from strategy_v4.intraday import HALF_DAY_CLOSE, SESSION_CLOSE, partial_bar_at, partial_index_at
from strategy_v4.live import evaluate_at
from strategy_v4.sizing import entry_limit, position_quantity, stop_price

logger = logging.getLogger("strategy_v4.backtest_intraday")

#: S1-S4 varsayılan sorgu saatleri. Prompt 1'de Yahoo 60m ızgarasının `:30`
#: olduğu doğrulandı; `:50` her sorguda bir bar daha verir.
DEFAULT_SCAN_TIMES = ("11:50", "13:50", "15:50", "17:50")
INTRADAY_SLOTS = ("S1", "S2", "S3", "S4")
#: Sinyal tablosuna yazılacak en küçük skor (komşuluk testi 70/75/80 için pay).
SIGNAL_MIN_SCORE = 60.0


@dataclass
class IntradayConfig:
    """Koşum ayarları. Strateji parametreleri `StrategyConfig`'tedir."""

    scan_times: Sequence[str] = DEFAULT_SCAN_TIMES
    slots: Sequence[str] = ("S1", "S2", "S3", "S4", "S5")
    delay_minutes: int = 15
    #: Spesifikasyon §3 "dolum barında stop/TP kontrol edilmez" der; test listesi
    #: "aynı barda dolum + stop → stop" der. Varsayılan açık metne uyar. True
    #: yapılırsa dolum barında da stop kontrol edilir (kötü senaryo).
    stop_on_fill_bar: bool = False
    half_days: frozenset = frozenset()

    @property
    def intraday_slots(self) -> List[str]:
        return [s for s in self.slots if s in INTRADAY_SLOTS]

    @property
    def use_close_scan(self) -> bool:
        return "S5" in self.slots


@dataclass
class DailyScores:
    """Kapanmış günlük barlardan skor/özellik tabloları (sembol başına bir kez)."""

    score: Dict[str, pd.Series] = field(default_factory=dict)
    eligible: Dict[str, pd.Series] = field(default_factory=dict)
    ema50: Dict[str, pd.Series] = field(default_factory=dict)
    atr: Dict[str, pd.Series] = field(default_factory=dict)
    adv: Dict[str, pd.Series] = field(default_factory=dict)


def daily_scores(frames: Dict[str, pd.DataFrame], xu_daily: pd.Series, cfg: StrategyConfig) -> DailyScores:
    out = DailyScores()
    for sym, frame in frames.items():
        feats = compute_features(frame, xu_daily, cfg)
        sc = compute_score(feats, cfg)
        out.score[sym] = sc["score"]
        out.eligible[sym] = sc["eligible"].fillna(False)
        out.ema50[sym] = feats["ema50"]
        out.atr[sym] = feats["atr"]
        out.adv[sym] = feats["adv_try"]
    return out


def _parse(text: str) -> time:
    hour, minute = text.split(":")
    return time(int(hour), int(minute))


def _state_for(entry: float, qty: int, atr: float, cfg: StrategyConfig) -> PositionState:
    """
    Çıkış durumu. `exit_model="atr"` iken TP'ler sonsuza alınır: `exits.step`
    yalnızca stop ve gap kurallarını uygular, çıkışı skor yönetir (eski v4).
    """
    lv = levels_for(entry, atr, cfg)
    return PositionState(
        entry=entry, qty=qty, remaining=qty,
        stop=lv["stop"], tp1=lv["tp1"], tp2=lv["tp2"], tp3=lv["tp3"],
        top_price=entry, risk=entry - lv["stop"],
    )


def trading_days(hourly: Dict[str, pd.DataFrame], start: str, end: str) -> List[pd.Timestamp]:
    days = sorted({d for f in hourly.values() for d in f.index.normalize().unique()})
    lo, hi = pd.Timestamp(start), pd.Timestamp(end)
    return [d for d in days if lo <= d <= hi]


def bars_by_day(hourly: Dict[str, pd.DataFrame]) -> Dict[str, Dict[pd.Timestamp, pd.DataFrame]]:
    """Sembol → gün → o günün 60m barları. Döngüde tekrar maskeleme olmasın diye."""
    out: Dict[str, Dict[pd.Timestamp, pd.DataFrame]] = {}
    for sym, frame in hourly.items():
        out[sym] = {day: part for day, part in frame.groupby(frame.index.normalize())}
    return out


# ---------------------------------------------------------------------------
# 1. Sinyal üretimi (pahalı aşama)
# ---------------------------------------------------------------------------


def build_signals(
    daily: Dict[str, pd.DataFrame],
    hourly: Dict[str, pd.DataFrame],
    xu_daily: pd.Series,
    xu_hourly: pd.DataFrame,
    cfg: StrategyConfig,
    start: str,
    end: str,
    run: Optional[IntradayConfig] = None,
    scores: Optional[DailyScores] = None,
    min_score: float = SIGNAL_MIN_SCORE,
    progress: Optional[int] = None,
) -> pd.DataFrame:
    """
    Her (sembol, gün, sorgu) için `evaluate_at` sonucunu tabloya yazar.

    Dönen tablo `buy_score`, çıkış modeli ve maliyetten bağımsızdır; aynı tablo
    tüm mod/maliyet/komşuluk varyantlarında yeniden kullanılır.
    """
    run = run or IntradayConfig()
    symbols = sorted(set(daily) & set(hourly))
    scores = scores or daily_scores({s: daily[s] for s in symbols}, xu_daily, cfg)
    days = trading_days({s: hourly[s] for s in symbols}, start, end)
    by_day = bars_by_day({s: hourly[s] for s in symbols})
    xu_by_day = {day: part for day, part in xu_hourly.groupby(xu_hourly.index.normalize())}
    closed_cache: Dict[str, pd.DataFrame] = {}
    rows: List[dict] = []

    for n, day in enumerate(days):
        if progress and n % progress == 0:
            logger.info("sinyal üretimi %d/%d (%s)", n, len(days), day.date())
        half = day.date().isoformat() in run.half_days
        session_close = HALF_DAY_CLOSE if half else SESSION_CLOSE
        xu_closed = xu_daily[xu_daily.index < day]
        xu_bars = xu_by_day.get(day, pd.DataFrame())

        # --- S1-S4: kısmi barla ---
        for slot, text in zip(INTRADAY_SLOTS, run.scan_times):
            if slot not in run.intraday_slots:
                continue
            at = _parse(text)
            if at >= session_close:
                continue  # yarım günde seans sonrasına düşen sorgu atlanır
            moment = datetime.combine(day.date(), at)
            xu_partial = partial_index_at(xu_bars, moment, run.delay_minutes, session_close)
            for sym in symbols:
                day_bars = by_day[sym].get(day)
                if day_bars is None or day_bars.empty:
                    continue
                bar = partial_bar_at(day_bars, moment, run.delay_minutes, session_close)
                if bar is None:
                    continue
                closed = closed_cache.get(sym)
                if closed is None:
                    # önbellek her günün sonunda temizlenir, o yüzden gün içi geçerli
                    closed = daily[sym][daily[sym].index < day]
                    closed_cache[sym] = closed
                if len(closed) < cfg.min_history:
                    continue
                ev = evaluate_at(
                    sym, closed, bar, xu_closed, xu_partial, cfg,
                    scan_slot=slot, as_of_time=moment,
                )
                if ev is None or not ev.eligible or ev.score < min_score:
                    continue
                rows.append({
                    "symbol": sym, "day": day, "slot": slot, "moment": pd.Timestamp(moment),
                    "score": ev.score, "reference": bar["close"], "atr": ev.atr,
                    "adv": ev.adv_try, "atr_pct": ev.atr / bar["close"] if bar["close"] else 0.0,
                    "bars": bar["bars"],
                })
        closed_cache.clear()

        # --- S5: kapanmış günlük bar ---
        if not run.use_close_scan:
            continue
        for sym in symbols:
            sc = scores.score[sym]
            if day not in sc.index or not bool(scores.eligible[sym].at[day]):
                continue
            score = float(sc.at[day])
            if score < min_score:
                continue
            close = float(daily[sym].at[day, "close"])
            atr = float(scores.atr[sym].at[day])
            rows.append({
                "symbol": sym, "day": day, "slot": "S5",
                "moment": pd.Timestamp(datetime.combine(day.date(), time(18, 45))),
                "score": score, "reference": close, "atr": atr,
                "adv": float(scores.adv[sym].at[day]),
                "atr_pct": atr / close if close else 0.0, "bars": 0,
            })

    table = pd.DataFrame(rows)
    if not table.empty:
        table = table.sort_values(["day", "moment", "score"], ascending=[True, True, False])
    return table.reset_index(drop=True)


# ---------------------------------------------------------------------------
# 2. Simülasyon (ucuz aşama)
# ---------------------------------------------------------------------------


@dataclass
class IntradayResult:
    equity: pd.Series
    invested: pd.Series
    trades: pd.DataFrame
    fills: pd.DataFrame
    candidates: pd.DataFrame
    costs: float
    stats: Dict[str, int] = field(default_factory=dict)


@dataclass
class _Order:
    symbol: str
    slot: str
    signal_day: pd.Timestamp
    score: float
    limit: float
    atr: float
    adv: float
    valid_until: pd.Timestamp
    effective_from: Optional[pd.Timestamp]
    atr_pct: float


def simulate(
    signals: pd.DataFrame,
    daily: Dict[str, pd.DataFrame],
    hourly: Dict[str, pd.DataFrame],
    cfg: StrategyConfig,
    sectors: Dict[str, str],
    start: str,
    end: str,
    run: Optional[IntradayConfig] = None,
    scores: Optional[DailyScores] = None,
    xu_daily: Optional[pd.Series] = None,
) -> IntradayResult:
    """`build_signals` tablosunu alıp dolum, çıkış ve portföy muhasebesini yürütür."""
    run = run or IntradayConfig()
    symbols = sorted(set(daily) & set(hourly))
    if scores is None:
        if xu_daily is None:
            raise ValueError("scores veya xu_daily gerekli")
        scores = daily_scores({s: daily[s] for s in symbols}, xu_daily, cfg)

    days = trading_days({s: hourly[s] for s in symbols}, start, end)
    if not days:
        raise ValueError("aralıkta işlem günü yok")
    by_day = bars_by_day({s: hourly[s] for s in symbols})

    allowed = [s for s in run.slots]
    sig = signals[signals["slot"].isin(allowed) & (signals["score"] >= cfg.buy_score)] if not signals.empty else signals
    sig_by_day: Dict[pd.Timestamp, pd.DataFrame] = (
        {day: part for day, part in sig.groupby("day")} if not sig.empty else {}
    )

    portfolio = Portfolio(cfg, sectors)
    pending: Dict[str, _Order] = {}
    exit_flags: Dict[str, str] = {}
    states: Dict[str, PositionState] = {}
    fill_bar: Dict[str, pd.Timestamp] = {}
    cand_log: List[dict] = []
    curve: List[float] = []
    invested: List[float] = []
    stats = {"aday": 0, "bloke": 0, "dolum": 0, "iptal": 0, "kovalanmadi": 0}

    # Son kapanış fiyatları (değerleme için), gün gün ileri taşınır
    last_close: Dict[str, float] = {}

    for day in days:
        for sym in symbols:
            s = daily[sym]
            if day in s.index:
                last_close[sym] = float(s.at[day, "close"])
        prices = dict(last_close)
        portfolio.start_day(day, prices)
        seen_today: set = set()
        half = day.date().isoformat() in run.half_days
        session_close = HALF_DAY_CLOSE if half else SESSION_CLOSE

        bars = {s: by_day[s][day] for s in symbols if day in by_day[s]}
        if not bars:
            curve.append(portfolio.equity(prices))
            invested.append(portfolio.invested_fraction(prices))
            continue
        grid = sorted({ts for b in bars.values() for ts in b.index})

        # 1. Önceki S5'te işaretlenen çıkışlar: günün ilk barının açılışından
        for sym, reason in list(exit_flags.items()):
            if sym not in portfolio.holdings:
                exit_flags.pop(sym, None)
                continue
            if sym not in bars:
                continue
            portfolio.sell(
                sym, float(bars[sym].iloc[0]["open"]),
                portfolio.holdings[sym].remaining, day, reason,
            )
            states.pop(sym, None)
            fill_bar.pop(sym, None)
            exit_flags.pop(sym, None)

        today_sig = sig_by_day.get(day)

        for ts in grid:
            # (a) açık pozisyonların çıkışları (dolum barı hariç)
            for sym in list(portfolio.holdings):
                if sym not in bars or ts not in bars[sym].index:
                    continue
                if not run.stop_on_fill_bar and fill_bar.get(sym) == ts:
                    continue
                state = states.get(sym)
                if state is None:
                    continue
                row = bars[sym].loc[ts]
                new_state, events = step(
                    state, float(row["open"]), float(row["high"]), float(row["low"]), cfg
                )
                for ev in events:
                    if ev.qty > 0:
                        portfolio.sell(
                            sym, ev.price, ev.qty, day, ev.kind,
                            stop_fill=ev.kind in ("stop", "stop_gap"),
                        )
                states[sym] = new_state
                if sym not in portfolio.holdings:
                    states.pop(sym, None)
                    fill_bar.pop(sym, None)

            # (b) bu bardan ÖNCE biten sorguların adayları
            if today_sig is not None:
                after = [t for t in grid if t > ts]
                prev_bars = [t for t in grid if t < ts]
                window_lo = prev_bars[-1] if prev_bars else pd.Timestamp(day)
                due = today_sig[
                    today_sig["slot"].isin(run.intraday_slots)
                    & (today_sig["moment"] > window_lo)
                    & (today_sig["moment"] <= ts)
                ]
                for _, r in due.iterrows():
                    sym = r["symbol"]
                    if sym in portfolio.holdings or sym in pending or sym in seen_today:
                        continue
                    seen_today.add(sym)
                    reason = portfolio.block_reason(sym, prices)
                    limit = entry_limit(float(r["reference"]), float(r["atr"]), cfg)
                    cand_log.append({
                        "symbol": sym, "slot": r["slot"], "day": day, "score": float(r["score"]),
                        "reference": float(r["reference"]), "limit": limit,
                        "atr_pct": float(r["atr_pct"]), "blocked": reason or "",
                        "cancelled_at_s5": False, "filled": False,
                    })
                    if reason:
                        stats["bloke"] += 1
                        continue
                    stats["aday"] += 1
                    pending[sym] = _Order(
                        symbol=sym, slot=r["slot"], signal_day=day, score=float(r["score"]),
                        limit=limit, atr=float(r["atr"]), adv=float(r["adv"]),
                        valid_until=day, effective_from=ts, atr_pct=float(r["atr_pct"]),
                    )

            # (c) bekleyen emirlerin dolumu
            for sym in sorted(pending, key=lambda s: (-pending[s].score, s)):
                order = pending[sym]
                if order.effective_from is not None and ts < order.effective_from:
                    continue
                if sym in portfolio.holdings:
                    pending.pop(sym, None)
                    continue
                if sym not in bars or ts not in bars[sym].index:
                    continue
                row = bars[sym].loc[ts]
                if portfolio.block_reason(sym, prices):
                    continue
                if _locked_up(row["open"], row["high"], row["low"], row["close"], last_close.get(sym, 0.0)):
                    continue
                fill = None
                if float(row["open"]) <= order.limit:
                    fill = float(row["open"])
                elif float(row["low"]) <= order.limit:
                    fill = order.limit
                if fill is None:
                    continue
                stop = _state_for(fill, 1, order.atr, cfg).stop
                qty = position_quantity(
                    portfolio.equity(prices), fill, stop, order.adv, portfolio.cash, cfg
                )
                if qty <= 0:
                    continue
                ok = portfolio.buy(
                    sym, fill, qty, fill - stop, day,
                    meta={
                        "slot": order.slot, "signal_day": order.signal_day,
                        "signal_score": order.score, "atr_pct": order.atr_pct,
                        "fill_time": ts,
                    },
                )
                if ok:
                    states[sym] = _state_for(fill, qty, order.atr, cfg)
                    fill_bar[sym] = ts
                    stats["dolum"] += 1
                    for rowlog in cand_log:
                        if rowlog["symbol"] == sym and rowlog["day"] == order.signal_day:
                            rowlog["filled"] = True
                    pending.pop(sym, None)
                    if run.stop_on_fill_bar:
                        # Kötü senaryo: dolumdan SONRA aynı barın kalan aralığı
                        # kontrol edilir. Bar açılışı yerine dolum fiyatı verilir,
                        # yoksa gap kuralı yanlışlıkla tetiklenir.
                        new_state, events = step(
                            states[sym], fill, float(row["high"]), float(row["low"]), cfg
                        )
                        for ev in events:
                            if ev.qty > 0:
                                portfolio.sell(
                                    sym, ev.price, ev.qty, day, ev.kind,
                                    stop_fill=ev.kind in ("stop", "stop_gap"),
                                )
                        states[sym] = new_state
                        if sym not in portfolio.holdings:
                            states.pop(sym, None)
                            fill_bar.pop(sym, None)

        # 2. Gün sonu (S5)
        _close_job(
            day, symbols, daily, scores, cfg, run, portfolio, pending, states,
            exit_flags, prices, seen_today, cand_log, stats, today_sig,
        )

        for sym in list(pending):
            if pending[sym].valid_until < day:
                pending.pop(sym, None)

        curve.append(portfolio.equity(prices))
        invested.append(portfolio.invested_fraction(prices))

    portfolio.close_all(dict(last_close), days[-1], "end")

    return IntradayResult(
        equity=pd.Series(curve, index=pd.DatetimeIndex(days)),
        invested=pd.Series(invested, index=pd.DatetimeIndex(days)),
        trades=pd.DataFrame(portfolio.trades),
        fills=pd.DataFrame(portfolio.fills),
        candidates=pd.DataFrame(cand_log),
        costs=portfolio.costs,
        stats=stats,
    )


def _close_job(
    day, symbols, daily, scores, cfg, run, portfolio, pending, states,
    exit_flags, prices, seen_today, cand_log, stats, today_sig,
) -> None:
    """S5: gün içi sinyal iptali, zaman stopu, yumuşak çıkış, yeni S5 adayı."""
    # gün içi sinyal iptali: kapanışta skor eşiğin altındaysa dolmamış emir iptal
    for sym in list(pending):
        order = pending[sym]
        if order.slot == "S5" or order.signal_day != day:
            continue
        sc = scores.score[sym]
        if day not in sc.index:
            continue
        if float(sc.at[day]) < cfg.buy_score or not bool(scores.eligible[sym].at[day]):
            pending.pop(sym, None)
            stats["iptal"] += 1
            for rowlog in cand_log:
                if rowlog["symbol"] == sym and rowlog["day"] == day:
                    rowlog["cancelled_at_s5"] = True

    # zaman stopu ve yumuşak çıkış (backtest'te AI yok → karar SAT)
    for sym in list(portfolio.holdings):
        if sym in exit_flags:
            continue
        holding = portfolio.holdings[sym]
        state = states.get(sym)
        index = daily[sym].index
        sessions = int(((index > holding.entry_day) & (index <= day)).sum())
        if state is not None:
            due = time_exit_due(state, sessions, cfg)
            if due:
                exit_flags[sym] = due
                continue
        sc = scores.score[sym]
        if day not in sc.index:
            continue
        if cfg.exit_score is not None and float(sc.at[day]) < cfg.exit_score:
            exit_flags[sym] = "score"
            continue
        if cfg.soft_exit_ema50 and cfg.exit_model != "atr":
            ema50 = scores.ema50[sym]
            if day in ema50.index and not np.isnan(ema50.at[day]):
                if float(daily[sym].at[day, "close"]) < float(ema50.at[day]):
                    exit_flags[sym] = "ema50"

    # yeni S5 adayı: 2 seans geçerli, ertesi günden itibaren 60m barlarla dolum
    if not run.use_close_scan or today_sig is None:
        return
    due = today_sig[today_sig["slot"] == "S5"].sort_values("score", ascending=False)
    for _, r in due.iterrows():
        sym = r["symbol"]
        if sym in portfolio.holdings or sym in pending or sym in seen_today:
            continue
        seen_today.add(sym)
        reason = portfolio.block_reason(sym, prices)
        limit = entry_limit(float(r["reference"]), float(r["atr"]), cfg)
        cand_log.append({
            "symbol": sym, "slot": "S5", "day": day, "score": float(r["score"]),
            "reference": float(r["reference"]), "limit": limit,
            "atr_pct": float(r["atr_pct"]), "blocked": reason or "",
            "cancelled_at_s5": False, "filled": False,
        })
        if reason:
            stats["bloke"] += 1
            continue
        stats["aday"] += 1
        future = daily[sym].index[daily[sym].index > day]
        valid_until = (
            future[cfg.entry_valid_sessions - 1]
            if len(future) >= cfg.entry_valid_sessions else day
        )
        pending[sym] = _Order(
            symbol=sym, slot="S5", signal_day=day, score=float(r["score"]),
            limit=limit, atr=float(r["atr"]), adv=float(r["adv"]),
            valid_until=valid_until, effective_from=None, atr_pct=float(r["atr_pct"]),
        )


def run_intraday_backtest(
    daily, hourly, xu_daily, xu_hourly, cfg, sectors, start, end,
    run: Optional[IntradayConfig] = None, signals: Optional[pd.DataFrame] = None,
    scores: Optional[DailyScores] = None,
) -> IntradayResult:
    """`build_signals` + `simulate` kısayolu (tek koşum için)."""
    run = run or IntradayConfig()
    symbols = sorted(set(daily) & set(hourly))
    scores = scores or daily_scores({s: daily[s] for s in symbols}, xu_daily, cfg)
    if signals is None:
        signals = build_signals(
            daily, hourly, xu_daily, xu_hourly, cfg, start, end, run, scores
        )
    return simulate(
        signals, daily, hourly, cfg, sectors, start, end, run, scores
    )


def classify_exit(trade: dict) -> str:
    """
    İşlemi spesifikasyon Bölüm 9'daki çıkış tiplerine ayırır.

    `reason` son dolumun sebebidir; kademeli satışta hangi TP'lerin geçildiğini
    `levels` listesi söyler.
    """
    reached = [x for x in trade.get("levels", []) if x in ("tp1", "tp2", "tp3")]
    reason = trade.get("reason", "")
    if "tp3" in reached:
        return "TP3"
    if reason in ("time", "max_hold"):
        return "zaman stopu"
    if reason in ("score", "ema50"):
        return "yumuşak çıkış"
    if reason in ("stop", "stop_gap"):
        if "tp2" in reached:
            return "TP2 sonrası TP1'de"
        if "tp1" in reached:
            return "TP1 sonrası başa baş"
        return "stop (TP1 öncesi)"
    if reason == "end":
        return "dönem sonu açık"
    return reason or "bilinmeyen"
