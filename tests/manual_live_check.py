"""
Canli veri manuel kontrol scripti.

Bu script Telegram'a mesaj gondermez.
Sadece Yahoo'dan veri cekip terminale:
- son close
- son 5 mum
- hesaplanan entry/stop/hedef
yazdirir.
"""

from __future__ import annotations

from market.data_fetcher import YahooFinanceFetcher
from strategy.signal_engine import evaluate_symbol


def run() -> None:
    symbols = ["THYAO", "ASELS", "KCHOL"]
    timeframe = "4h"
    lookback = 120
    fetcher = YahooFinanceFetcher()

    for symbol in symbols:
        print(f"\n=== {symbol} ({timeframe}) ===")
        df = fetcher.get_ohlc(symbol=symbol, interval=timeframe, lookback=lookback)
        if df is None or df.empty:
            print("Veri yok veya cekilemedi.")
            continue

        last_close = float(df.iloc[-1]["close"])
        print(f"last_close: {last_close:.4f}")
        print("last_10_ohlc:")
        print(df.tail(10).to_string())

        signal = evaluate_symbol(symbol=symbol, df=df, timeframe=timeframe.upper())
        if signal is None:
            print("Sinyal uretilmedi.")
            continue

        print(
            "signal:",
            {
                "type": signal.signal_type.value,
                "entry": round(signal.entry, 4),
                "stop_loss": round(signal.stop_loss, 4),
                "target_1": round(signal.target_1, 4),
                "target_2": round(signal.target_2, 4),
                "reason": signal.reason,
                "trend_state": signal.trend_state,
                "detected_patterns": signal.detected_patterns,
            },
        )


if __name__ == "__main__":
    run()
