# BIST Telegram Sinyal Botu

## Projenin amacı

Bu bot Borsa İstanbul Pay Piyasası hisselerini takip eder, 4H strateji kurulumlarını üretir ve 15 dakikalık aralıklarla state bazlı tetikleri izler. Otomatik emir göndermez; yalnızca Telegram bildirimi üretir.

## BIST için çalışma modeli

- Piyasa: Borsa İstanbul spot hisse (VIOP/kaldıraç yok).
- Veri: Yahoo Finance (`.IS` sembol eşlemesi).
- Tarama: her 15 dakika (`SCAN_INTERVAL_SECONDS=900`).
- Strateji: 4H setup üretimi.
- Takip: 15m bazda entry/stop/tp state geçişleri.

## 15 dakikalık tarama + 4H strateji ayrımı

- Bot her 15 dakikada bir döner.
- Her döngüde:
  - aktif state'ler için fiyat takibi yapılır (entry/stop/tp).
  - sadece yeni 4H mum zamanında yeni setup üretilir.
- Böylece bot her 4 saatte rastgele mesaj atmaz; sadece state değişiminde mesaj atar.

## Market açık/kapalı kontrolü

`market/market_calendar.py`:

- `is_weekend`
- `is_bist_holiday`
- `is_half_day`
- `is_market_open`
- `is_signal_generation_time`
- `is_new_4h_candle`

Kurallar:

- Hafta sonu: kapalı
- Resmi tatil: kapalı
- Yarım gün: 13:00 sonrası yeni sinyal yok
- Saat dilimi: `Europe/Istanbul`

## Resmi tatil ve yarım gün mantığı

Tatil/yarım gün listeleri `config/bist_calendar.py` içinde tutulur. Bu dosya manuel güncellenerek takvim sürdürülebilir şekilde yönetilir.

## State machine

Her sembol için durum:

`NONE -> WATCH -> BUY -> TP1 -> TP2 -> CLOSED`

Ara durumlar:

- `STOPPED` (stop tetiklendi)
- `EXPIRED` (WATCH 48 saat tetiklenmedi)

Kurallar:

- Aynı state tekrarında mesaj yok.
- Sadece anlamlı state geçişlerinde mesaj var.
- `BREAKEVEN_ON_TP1=True` ise TP1 sonrası stop entry seviyesine çekilir.

## Kurulum

```bash
cd bist-telegram-bot
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

## .env doldurma

Zorunlu:

- `BOT_TOKEN`
- `CHAT_ID`

Önemli ayarlar:

- `SCAN_INTERVAL_SECONDS=900`
- `PRIMARY_TIMEFRAME=4H`
- `TRACKING_TIMEFRAME=15m`
- `ENTRY_DISTANCE_THRESHOLD_PCT=0.03`
- `MIN_RR=1.5`
- `WATCH_EXPIRY_HOURS=48`
- `BREAKEVEN_ON_TP1=True`
- `MARKET_TIMEZONE=Europe/Istanbul`
- `ENABLE_DEBUG_LOGS=True`

## Çalıştırma

```bash
python main.py
```

## Test modu / canlı doğrulama

Telegram mesajı atmadan canlı veriyi kontrol etmek için:

```bash
python tests/manual_live_check.py
```

Bu test:

- `THYAO`, `ASELS`, `KCHOL` için canlı veri çeker
- son 10 OHLC satırını yazdırır
- `last_close`, `entry`, `stop`, `tp1`, `tp2` yazdırır
- `detected_patterns`, `trend_state` yazdırır

## Bot ne zaman mesaj atar?

- Yeni WATCH setup
- WATCH -> BUY
- BUY -> TP1
- TP1 -> TP2
- BUY/TP1 -> STOPPED
- WATCH -> EXPIRED

## Bot ne zaman susar?

- Aynı state tekrarında
- Market kapalıyken yeni setup üretiminde
- 4H kapanış zamanı değilse setup üretiminde
- Düşük kalite/suspicious sinyal filtrelerinde

## Sorumluluk

Bu yazılım yatırım tavsiyesi değildir.
