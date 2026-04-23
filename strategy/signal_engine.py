"""
Sinyal motoru: trend filtresi + mum formasyonları + price action birleşimi.

Çıktı: Signal veya None (koşullar sağlanmazsa).
"""

from __future__ import annotations

import logging
from typing import List, Optional

import pandas as pd

from market.models import Signal, SignalType
from market.data_fetcher import YahooFinanceFetcher
from strategy import candlestick, price_action
from strategy.risk_management import compute_risk_levels
from datetime import timezone

from utils.helpers import round_price
from utils.time_utils import utc_now

logger = logging.getLogger(__name__)


def _sma(series: pd.Series, window: int) -> pd.Series:
    return series.rolling(window=window, min_periods=window).mean()


def trend_filter_sma(df: pd.DataFrame, short: int = 5, long: int = 15) -> str:
    """Kısa SMA uzun SMA üstünde ise 'bull', altında 'bear', yoksa 'neutral'."""
    if len(df) < long + 1:
        return "neutral"
    c = df["close"]
    s = _sma(c, short).iloc[-1]
    l = _sma(c, long).iloc[-1]
    if pd.isna(s) or pd.isna(l):
        return "neutral"
    if float(s) > float(l):
        return "bull"
    if float(s) < float(l):
        return "bear"
    return "neutral"


def evaluate_symbol(
    symbol: str,
    df: pd.DataFrame,
    timeframe: str,
    entry_distance_threshold_pct: float = 0.03,
    min_rr: float = 1.5,
) -> Optional[Signal]:
    """
    Verilen OHLCV için sinyal üretir veya None döner.

    Mantık özeti:
    - Trend (SMA) ve HH/LL yapısı uyumlu ise yön güçlenir.
    - Boğa formasyonları + boğa price action -> BUY adayı
    - Ayı formasyonları + ayı price action -> SELL adayı
    - Çelişkili veya zayıf kurulum -> WATCH veya None
    """
    if df is None or len(df) < 20:
        return None

    df = df.copy()
    required_cols = ["open", "high", "low", "close", "volume"]
    if any(col not in df.columns for col in required_cols):
        logger.warning("Eksik kolonlar, sinyal iptal: %s -> %s", symbol, df.columns.tolist())
        return None
    if df[required_cols].tail(10).isna().any().any():
        logger.warning("NaN veri tespit edildi, sinyal iptal: %s", symbol)
        return None
    if not isinstance(df.index, pd.DatetimeIndex):
        df.index = pd.to_datetime(df.index, utc=True)

    trend = trend_filter_sma(df)
    pa_tags = price_action.price_action_tags(df)
    pattern_tags = candlestick.detect_patterns(df)
    debug_window = df[["open", "high", "low", "close"]].tail(10)
    logger.info("Signal debug window %s:\n%s", symbol, debug_window.to_string())

    bullish_score = 0
    bearish_score = 0

    if trend == "bull":
        bullish_score += 2
    elif trend == "bear":
        bearish_score += 2

    for t in pa_tags:
        if t in ("HH/HL structure", "support reaction", "resistance breakout", "breakout retest"):
            bullish_score += 1
        if t in ("LH/LL structure",):
            bearish_score += 1

    for p in pattern_tags:
        pl = p.lower()
        if "bullish" in pl:
            bullish_score += 2
        if "bearish" in pl:
            bearish_score += 2
        if "inside bar" in pl:
            bullish_score += 0
            bearish_score += 0

    # Net yön için eşik
    last_close = float(df.iloc[-1]["close"])
    entry = last_close
    entry = round_price(entry)
    reasons: List[str] = []
    st: Optional[SignalType] = None

    if bullish_score >= 3 and bullish_score > bearish_score:
        st = SignalType.BUY
        reasons.append("trend alignment" if trend == "bull" else "mixed trend")
        reasons.extend(pattern_tags)
        reasons.extend(pa_tags)
    elif bearish_score >= 3 and bearish_score > bullish_score:
        st = SignalType.SELL
        reasons.append("trend alignment" if trend == "bear" else "mixed trend")
        reasons.extend(pattern_tags)
        reasons.extend(pa_tags)
    elif max(bullish_score, bearish_score) >= 2:
        st = SignalType.WATCH
        reasons.append("setup forming")
        reasons.extend(pattern_tags)
        reasons.extend(pa_tags)
    else:
        return None

    # Gerekçe string — tekrarları kaldır, sırayı koru
    seen = set()
    unique_reasons: List[str] = []
    for r in reasons:
        key = r.strip().lower()
        if key and key not in seen:
            seen.add(key)
            unique_reasons.append(r.strip())
    reason_str = " + ".join(unique_reasons[:12])

    risk = compute_risk_levels(entry, st)
    logger.info(
        "Signal debug | symbol=%s | trend=%s | patterns=%s | last_close=%.4f | entry=%.4f | stop=%.4f | t1=%.4f | t2=%.4f",
        symbol,
        trend,
        pattern_tags + pa_tags,
        last_close,
        entry,
        risk.stop_loss,
        risk.target_1,
        risk.target_2,
    )

    distance_ratio = abs(entry - last_close) / max(abs(last_close), 1e-9)
    if st == SignalType.BUY and distance_ratio > entry_distance_threshold_pct:
        logger.warning(
            "suspicious signal | symbol=%s | signal=%s | entry=%.4f | last_close=%.4f | ratio=%.4f",
            symbol,
            st.value,
            entry,
            last_close,
            distance_ratio,
        )
        return None

    if st == SignalType.BUY:
        rr_den = max(entry - risk.stop_loss, 1e-9)
        rr = (risk.target_1 - entry) / rr_den
        if rr < min_rr - 1e-9:  # float hassasiyeti için küçük tolerans
            logger.warning("Dusuk RR nedeniyle sinyal iptal: %s rr=%.3f min_rr=%.3f", symbol, rr, min_rr)
            return None

    ts = df.index[-1]
    if hasattr(ts, "to_pydatetime"):
        ts_dt = ts.to_pydatetime()
    else:
        ts_dt = utc_now()
    if getattr(ts_dt, "tzinfo", None) is None:
        ts_dt = ts_dt.replace(tzinfo=timezone.utc)

    return Signal(
        symbol=symbol,
        signal_type=st,
        entry=entry,
        stop_loss=round_price(risk.stop_loss),
        target_1=round_price(risk.target_1),
        target_2=round_price(risk.target_2),
        timeframe=timeframe,
        reason=reason_str,
        timestamp=ts_dt,
        last_close=round_price(last_close),
        trend_state=trend,
        detected_patterns=pattern_tags + pa_tags,
        tags=[],
    )


def evaluate_symbol_from_yahoo(
    symbol: str,
    timeframe: str = "4h",
    lookback: int = 120,
    fetcher: Optional[YahooFinanceFetcher] = None,
    entry_distance_threshold_pct: float = 0.03,
    min_rr: float = 1.5,
) -> Optional[Signal]:
    """
    Yahoo verisini çekip doğrudan sinyal üretir.
    `signal_engine` üzerinden tek adım test / entegrasyon için yardımcı fonksiyon.
    """
    yf_fetcher = fetcher or YahooFinanceFetcher()
    df = yf_fetcher.get_ohlc(symbol=symbol, interval=timeframe, lookback=lookback)
    if df is None or df.empty:
        return None
    return evaluate_symbol(
        symbol=symbol,
        df=df,
        timeframe=timeframe.upper(),
        entry_distance_threshold_pct=entry_distance_threshold_pct,
        min_rr=min_rr,
    )
