# 5 sorgu sistemi backtest raporu

Dönem **2025-05-07 → 2026-11-06**. Maliyet taraf başına **%0.20** (aksi belirtilmedikçe), stoplarda ek **%0.10** kayma.

> **AI ve eminlik skoru bu backtest'e dahil DEĞİLDİR.** Geçmişte AI kararını taklit etmek geleceği bilme riski taşır. Sonuçlar **"kural AL'ların hepsi alınsaydı" üst sınırıdır**; canlı sistem AI ve eminlik eşiğiyle bu adayların bir ALT KÜMESİNİ alacak.


## Evren A (izleme listesi)

Semboller (9): `ASELS, BORLS, EFOR, FROTO, INDES, NTGAZ, PAHOL, SMRVA, VAKFN`

60m veri: 2023-12-07 → 2026-11-06, sinyal tablosu 3237 satır


### Modlar

| mod | getiri_% | yillik_% | maks_dusus_% | islem | kazanma_% | ort_R | medyan_R | R_GA95 | kar_faktoru | portfoyde_% | ort_gun | en_iyi5_haric_% |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| M1 — sadece S5 | -9 | -6.1 | -10.5 | 80 | 38.8 | -0.04 | -1.02 | [-0.34; 0.27] | 0.79 | 11.7 | 4.8 | -18.9 |
| M2 — S1-S5 (canlı sistem) | -18.2 | -12.5 | -20.8 | 131 | 35.1 | -0.09 | -1.02 | [-0.30; 0.13] | 0.71 | 19.7 | 4.6 | -28.2 |
| M3 — v4 orijinal (haftalık, ATR stop) | -4.7 | -3.1 | -8.9 | 15 | 20 | -0.28 | -0.64 | [-0.98; 0.63] | 0.55 | 11.9 | 40.1 | -10.3 |
| XU100 | 26.5 | 17 | -28.9 |  |  |  |  |  |  |  |  |  |
| Eşit ağırlık evren | -10.2 | -6.9 | -36.9 |  |  |  |  |  |  |  |  |  |


### Maliyet duyarlılığı (M2)

| maliyet | getiri_% | maks_dusus_% | islem | ort_R | kar_faktoru |
|---|---|---|---|---|---|
| %0.10 | -13.8 | -16.6 | 131 | -0.09 | 0.78 |
| %0.20 | -18.2 | -20.8 | 131 | -0.09 | 0.71 |
| %0.40 | -26.2 | -28.4 | 131 | -0.09 | 0.6 |


### Çıkış modeli (M2 üzerinde)

| çıkış | getiri_% | maks_dusus_% | islem | kazanma_% | ort_R | kar_faktoru | ort_gun |
|---|---|---|---|---|---|---|---|
| E1 sabit yüzde, kademeli (canlı) | -18.2 | -20.8 | 131 | 35.1 | -0.09 | 0.71 | 4.6 |
| E2 v4 ATR stop, skorla çıkış | -1.3 | -6 | 66 | 45.5 | 0.01 | 0.91 | 13 |
| E3 sabit yüzde, TP1'de tamamı | -13.9 | -17.2 | 155 | 39.4 | -0.03 | 0.8 | 3.7 |


### E1 çıkış dağılımı

| çıkış tipi | adet | oran_% | ort_getiri_% | ort_R |
|---|---|---|---|---|
| stop (TP1 öncesi) | 67 | 51.1 | -4.2 | -1.05 |
| yumuşak çıkış | 22 | 16.8 | -0.59 | -0.15 |
| TP3 | 14 | 10.7 | 10.04 | 2.51 |
| TP2 sonrası TP1'de | 13 | 9.9 | 6.82 | 1.7 |
| TP1 sonrası başa baş | 11 | 8.4 | 1.8 | 0.45 |
| zaman stopu | 4 | 3.1 | -0.31 | -0.08 |


### Sorgu saatine göre (S1-S5)

| sorgu | işlem | kazanma_% | ort_R | ort_getiri_% | toplam_pnl |
|---|---|---|---|---|---|
| S1 | 82 | 37.8 | -0.02 | -0.08 | -7622 |
| S2 | 15 | 33.3 | -0.08 | -0.31 | -1972 |
| S3 | 10 | 30 | -0.46 | -1.86 | -3409 |
| S5 | 24 | 29.2 | -0.18 | -0.72 | -5063 |


### Kural skoru dilimine göre

| skor dilimi | işlem | TP1'e ulaşma_% | ort_R | ort_getiri_% |
|---|---|---|---|---|
| 70-74 | 55 | 25.5 | -0.12 | -0.49 |
| 75-79 | 48 | 35.4 | 0.01 | 0.03 |
| 80+ | 28 | 28.6 | -0.19 | -0.76 |


### Gürültü testi — girişten sonraki 10 seansta −%4'e dokunma

| ATR/fiyat | işlem | −%4 dokunma_% | ort_min_% |
|---|---|---|---|
| %2-3 | 2 | 100 | -7.98 |
| %3-4 | 31 | 64.5 | -8.19 |
| >%4 | 98 | 86.7 | -12.1 |


### Başa baş analizi (E1)

| ölçüt | değer |
|---|---|
| TP1'e ulasan ort getiri_% | 6.49 |
| TP1 oncesi stop ort getiri_% | -3.26 |
| gereken TP1 orani_% | 33.4 |
| gerceklesen TP1 orani_% | 29.8 |
| marj_puan | -3.7 |


### Gün içi sinyal iptali (S5 teyidi)

| ölçüt | değer |
|---|---|
| gun ici aday | 114 |
| S5'te iptal | 2 |
| iptal orani_% | 1.8 |
| iptal edilenlerden dolan | 0 |


### Dönemin ilk ve ikinci yarısı (M2)

| donem | getiri_% | maks_dusus_% | islem | ort_R | XU100_% |
|---|---|---|---|---|---|
| ilk yarı (2025-05-07→2026-02-04) | -9.4 | -14.4 | 94 | -0.05 | 53.1 |
| ikinci yarı (2026-02-04→2026-11-06) | -9.7 | -11.2 | 37 | -0.19 | -17.3 |


### Aday akışı ve tahmini AI yükü

- Toplam aday (limite takılmayan): **148**, bloke: 5

- Haftada ortalama **1.9** aday → ayda ~**8** AI incelemesi

- İnceleme başına 0,02-0,03 USD varsayımıyla aylık ~**0.20 USD** (üst sınır 0,05 USD ile ~0.41 USD)

- `AI_MONTHLY_BUDGET_USD=5` bütçesi bu akış için YETERLİ

- Dolum: 131 | S5 iptali: 2


## Evren B (15 likit kontrol)

Semboller (15): `AKBNK, ASELS, BIMAS, EREGL, FROTO, GARAN, ISCTR, KCHOL, KRDMD, PETKM, SAHOL, SASA, THYAO, TUPRS, YKBNK`

60m veri: 2023-12-07 → 2026-11-06, sinyal tablosu 3800 satır


### Modlar

| mod | getiri_% | yillik_% | maks_dusus_% | islem | kazanma_% | ort_R | medyan_R | R_GA95 | kar_faktoru | portfoyde_% | ort_gun | en_iyi5_haric_% |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| M1 — sadece S5 | -13.8 | -9.4 | -16.4 | 77 | 32.5 | -0.14 | -1.02 | [-0.41; 0.15] | 0.66 | 17.5 | 7.1 | -23.6 |
| M2 — S1-S5 (canlı sistem) | -8.8 | -5.9 | -15.3 | 104 | 37.5 | -0.01 | -0.4 | [-0.23; 0.23] | 0.82 | 23.6 | 7.1 | -19.1 |
| M3 — v4 orijinal (haftalık, ATR stop) | 6 | 4 | -6.1 | 13 | 46.2 | 0.5 | -0.16 | [-0.22; 1.36] | 2.4 | 15.1 | 51.7 | -3.9 |
| XU100 | 26.5 | 17 | -28.9 |  |  |  |  |  |  |  |  |  |
| Eşit ağırlık evren | 47.6 | 29.6 | -18.5 |  |  |  |  |  |  |  |  |  |


### Maliyet duyarlılığı (M2)

| maliyet | getiri_% | maks_dusus_% | islem | ort_R | kar_faktoru |
|---|---|---|---|---|---|
| %0.10 | -4.9 | -14.1 | 104 | -0.01 | 0.9 |
| %0.20 | -8.8 | -15.3 | 104 | -0.01 | 0.82 |
| %0.40 | -16 | -17.6 | 104 | -0.01 | 0.68 |


### Çıkış modeli (M2 üzerinde)

| çıkış | getiri_% | maks_dusus_% | islem | kazanma_% | ort_R | kar_faktoru | ort_gun |
|---|---|---|---|---|---|---|---|
| E1 sabit yüzde, kademeli (canlı) | -8.8 | -15.3 | 104 | 37.5 | -0.01 | 0.82 | 7.1 |
| E2 v4 ATR stop, skorla çıkış | 3.6 | -4.1 | 74 | 40.5 | 0.08 | 1.3 | 12.6 |
| E3 sabit yüzde, TP1'de tamamı | -15.1 | -18.3 | 126 | 36.5 | -0.06 | 0.74 | 5.3 |


### E1 çıkış dağılımı

| çıkış tipi | adet | oran_% | ort_getiri_% | ort_R |
|---|---|---|---|---|
| stop (TP1 öncesi) | 46 | 44.2 | -4.14 | -1.04 |
| yumuşak çıkış | 20 | 19.2 | -0.47 | -0.12 |
| TP2 sonrası TP1'de | 13 | 12.5 | 6.91 | 1.73 |
| TP1 sonrası başa baş | 10 | 9.6 | 1.85 | 0.46 |
| TP3 | 9 | 8.7 | 9.72 | 2.43 |
| zaman stopu | 6 | 5.8 | 0.01 | 0 |


### Sorgu saatine göre (S1-S5)

| sorgu | işlem | kazanma_% | ort_R | ort_getiri_% | toplam_pnl |
|---|---|---|---|---|---|
| S1 | 46 | 43.5 | 0.1 | 0.41 | -154 |
| S2 | 11 | 27.3 | -0.01 | -0.06 | -869 |
| S3 | 14 | 57.1 | 0.21 | 0.82 | 1673 |
| S5 | 33 | 24.2 | -0.26 | -1.03 | -9021 |


### Kural skoru dilimine göre

| skor dilimi | işlem | TP1'e ulaşma_% | ort_R | ort_getiri_% |
|---|---|---|---|---|
| 70-74 | 49 | 26.5 | -0 | -0.01 |
| 75-79 | 33 | 27.3 | -0.18 | -0.73 |
| 80+ | 22 | 50 | 0.23 | 0.93 |


### Gürültü testi — girişten sonraki 10 seansta −%4'e dokunma

| ATR/fiyat | işlem | −%4 dokunma_% | ort_min_% |
|---|---|---|---|
| %2-3 | 20 | 75 | -7.32 |
| %3-4 | 49 | 67.3 | -5.96 |
| >%4 | 35 | 85.7 | -8.13 |


### Başa baş analizi (E1)

| ölçüt | değer |
|---|---|
| TP1'e ulasan ort getiri_% | 6.05 |
| TP1 oncesi stop ort getiri_% | -2.87 |
| gereken TP1 orani_% | 32.2 |
| gerceklesen TP1 orani_% | 31.7 |
| marj_puan | -0.4 |


### Gün içi sinyal iptali (S5 teyidi)

| ölçüt | değer |
|---|---|
| gun ici aday | 74 |
| S5'te iptal | 0 |
| iptal orani_% | 0 |
| iptal edilenlerden dolan | 0 |


### Dönemin ilk ve ikinci yarısı (M2)

| donem | getiri_% | maks_dusus_% | islem | ort_R | XU100_% |
|---|---|---|---|---|---|
| ilk yarı (2025-05-07→2026-02-04) | -3.1 | -9.2 | 46 | -0.03 | 53.1 |
| ikinci yarı (2026-02-04→2026-11-06) | -5.9 | -15.3 | 58 | 0 | -17.3 |


### Aday akışı ve tahmini AI yükü

- Toplam aday (limite takılmayan): **116**, bloke: 4

- Haftada ortalama **1.5** aday → ayda ~**6** AI incelemesi

- İnceleme başına 0,02-0,03 USD varsayımıyla aylık ~**0.16 USD** (üst sınır 0,05 USD ile ~0.32 USD)

- `AI_MONTHLY_BUDGET_USD=5` bütçesi bu akış için YETERLİ

- Dolum: 104 | S5 iptali: 0


## Komşuluk testi (Evren B) — kırılganlık için, SEÇİM İÇİN DEĞİL

| varyant | getiri_% | maks_dusus_% | islem | ort_R | kazanma_% |
|---|---|---|---|---|---|
| buy_score=70 | -8.8 | -15.3 | 104 | -0.01 | 37.5 |
| buy_score=75 | -10.1 | -11.7 | 72 | -0.11 | 38.9 |
| buy_score=80 | -4.3 | -7.5 | 39 | -0.04 | 38.5 |
| stop=%3 | -11.1 | -13.2 | 120 | -0.02 | 30 |
| stop=%4 | -8.8 | -15.3 | 104 | -0.01 | 37.5 |
| stop=%5 | -3.9 | -14.8 | 94 | 0.06 | 43.6 |
| TP=5/8/12 | -13.2 | -17.5 | 118 | -0.05 | 38.1 |
| TP=6/10/15 | -8.8 | -15.3 | 104 | -0.01 | 37.5 |
| TP=8/12/18 | -8.9 | -16.1 | 97 | -0.03 | 30.9 |


## Kabul kriterleri (M2, %0,20 maliyet)

| evren | 1. getiri > XU100 | 2. düşüş < XU100 | 3. ort R > 0 | 4. ≥50 işlem |
|---|---|---|---|---|
| Evren A (izleme listesi) | -18.2 vs 26.5 → GEÇMEDİ | -20.8 vs -28.9 → GEÇTİ | -0.089 GA95 [-0.30; 0.13] → GEÇMEDİ | 131 → GEÇTİ |
| Evren B (15 likit kontrol) | -8.8 vs 26.5 → GEÇMEDİ | -15.3 vs -28.9 → GEÇTİ | -0.009 GA95 [-0.23; 0.23] → GEÇMEDİ | 104 → GEÇTİ |


## Yanılgılar ve sınırlar

- **Seçim yanılgısı:** izleme listesi (Evren A) bugünden bakılarak seçildi.

- **Ölçek:** Evren B kullanıcı talebiyle 92 hisseden **15 likit hisseye** indirildi; dönem 730 günden 18 aya kısaldı. Sonuçlar tam evren/tam dönem sonuçları değildir.

- **Hayatta kalma yanılgısı:** `sectors_v4.json` güncel listedir; dönem içinde kotasyondan çıkanlar yok.

- **60m sınırı:** Yahoo 60m verisi ~730 güne kadar; bazı sembollerde `period="730d"` boş dönüp `"2y"`ye düşüldü, başlangıç tarihleri sembole göre farklı.

- **Para piyasası karşılaştırması: karşılaştırılamadı** — güvenilir, indirilebilir bir fon endeksi kaynağı bulunamadı; sayı uydurulmadı.

- **Veri kalitesi:** Yahoo günün ilk 60m barının hacmini 0 veriyor; hacim kapanmış günlerden hesaplandığı için skoru etkilemez.

- **Sektör boşluğu:** Evren A'nın 9 hissesinden 7'si `sectors_v4.json`'da yok, o yüzden `max_per_sector=2` limiti Evren A'da fiilen uygulanmıyor.

- **Belirsizlik:** spesifikasyon §3 "dolum barında stop/TP kontrol edilmez" derken test listesi "aynı barda dolum + stop → stop" diyor. Varsayılan açık metne uyar (`stop_on_fill_bar=False`); karşıt davranış bayrakla açılabilir.
