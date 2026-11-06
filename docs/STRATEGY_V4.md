# Strateji v4

**Durum: kabul testini geçemedi. Gerçek parayla kullanılmamalı.**

## Kurallar

Tarama haftanın son işlem günü kapanışından sonra yapılır. Sadece long, spot.

### Skor (100)

| Bileşen | Puan | Ölçüm |
|---|---|---|
| Trend | 25 | `C>EMA20` 8, `EMA20>EMA50` 8, `C>EMA200` 9 |
| 12-1 göreli güç | 25 | `M = (C[t-21]/C[t-252]) / (XU[t-21]/XU[t-252]) - 1`; `25 × clip((M+0,10)/0,50, 0, 1)` |
| 63 gün göreli güç | 15 | `R = getiri63 - XU getiri63`; `15 × clip((R+0,10)/0,30, 0, 1)` |
| 52 hafta zirvesi | 15 | `Y = C / 252 gün en yüksek`; `15 × clip((Y-0,75)/0,25, 0, 1)` |
| Hacim | 5 | Son 20 günde yükseliş/düşüş günü hacmi: `≥1,5` 5, `≥1,2` 3, `≥1,0` 1 |
| Mum | 15 | Sabah yıldızı 15, üç beyaz asker 15, yutan boğa 12, delen mum 10, çekiç 8 |

Mum puanı için düşüş veya destek bağlamı gerekir. Hacim 20 günlük ortalamanın altındaysa puan 0,6 ile çarpılır. Aynı gün birden fazla formasyon varsa en güçlüsü sayılır.

Ayı formasyonları puan düşürmez. Geliştirme verisinde olumsuz getiri tahmini göstermediler; sadece bilgi olarak raporlanırlar.

### Filtreler

- En az 260 işlem günü geçmiş
- 20 günlük ortalama işlem hacmi: backtest'te en az 25 milyon TL
- Son gün değişimi < +%9
- `C ≤ EMA20 + 3 × ATR14`

### Giriş ve çıkış

- **AL:** skor ≥ 70
- **Alım sınırı:** kapanış + 0,5 × ATR14, 2 seans geçerli
- **Pozisyon:** portföyün en fazla %12,5'i, işlem başına en fazla %2,5 risk
- **Limitler:** en fazla 8 pozisyon, sektörde en fazla 3, günde en fazla 2 yeni giriş, ayda −%6'da yeni alım yok
- **Stop:** giriş − 4 × ATR14
- **SAT:** stop kırılırsa veya hafta sonu skor < 50 olursa

## Sonuçlar

Maliyet taraf başına %0,20, stoplarda ek %0,10 kayma.

| Dönem | Strateji | XU100 | Eşit ağırlık |
|---|---|---|---|
| Geliştirme, 2022-09 → 2025-07 | +%451,0, düşüş −%35,2 | +%241,8, düşüş −%22,9 | +%353,9, düşüş −%28,5 |
| Görülmemiş, 2025-08 → 2026-08 | +%16,1, düşüş −%17,2 | +%28,2, düşüş −%13,0 | +%11,4, düşüş −%14,4 |

Görülmemiş dönemde para piyasası fon endeksi yaklaşık +%46,4 kazandı.

Kabul kriterleri:

| Kriter | Sonuç |
|---|---|
| Net getiri XU100'den yüksek | Geçmedi |
| Maksimum düşüş XU100'den düşük | Geçmedi |
| Ortalama R > 0 | Geçti (0,29; %95 GA −0,20 ile 0,82) |
| En az 50 işlem | Yetersiz (36) |

Yaklaşık 55 yapılandırma geliştirme verisinde denendi. Görülmemiş dönem parametreler dondurulduktan sonra bir kez çalıştırıldı. Sonrasındaki gürültü ölçümleri parametre seçmek için kullanılmadı.

## Sınırlar

- Evren 92 güncel BIST hissesi olduğu için hayatta kalma yanılgısı içerir.
- Mevcut izleme listesinin çoğu bu veride yok; listeye özel test yapılamadı.
- Gün içi ön sinyal, canlı stop takibi ve AI analisti backtest edilmedi; bunlar kağıt modunda ölçülür.

## Canlı akış

- `10:30, 12:00, 14:00, 16:00, 17:30`: izleme listesinde ön sinyal taraması
- Skor ≥ 70 ise AI analisti skoru görmeden inceler; AL, BEKLE veya ALMA bildirimi gönderilir
- 15 dakikada bir stop ve alım sınırı kontrol edilir
- `18:45`: günlük skor kesinleşir, ön sinyal teyit veya iptal edilir
- Haftanın son işlem günü: skor < 50 olan pozisyon için SAT
- Pazar `20:00`: geçerli AL adayları hafta sonu haberleriyle yeniden incelenir

`PAPER_MODE=true` iken AL sinyalleri kağıt portföye girer. AI'ın almadığı adaylar gölge portföyde izlenir.

Komutlar: `/izle`, `/birak`, `/takip`, `/aldim`, `/sattim`, `/portfoy`, `/liste`, `/performans`, `/tara`, `/yardim`.

## Çalıştırma

```bash
pytest tests_v4 -q
python scripts/backtest_v4.py --data-dir VERI/ohlcv --xu100 VERI/xu100.csv --start 2025-08-01 --end 2026-08-07 --random 300
```

Fiyat CSV'leri `date, open, high, low, close, volume` sütunlarını içermelidir.
