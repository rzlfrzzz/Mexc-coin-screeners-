# Smart Money Screening Bot — MEXC Futures (USDT-M Perpetual) Only

Bot screening trading berbasis **Smart Money Concepts** & **Market Structure**, dengan 9 layer
filter progresif. Setiap layer independen, dapat di-debug sendiri, dan setiap signal yang lolos
disertai penjelasan lengkap alasannya.

> **Bot ini dikunci khusus untuk MEXC Futures (USDT-M Perpetual)** lewat ccxt (`exchange_id="mexc"`,
> `defaultType="swap"`). Exchange tidak bisa diganti lewat `.env` — semua threshold layer (volume,
> spread, ATR, dsb.) ditala untuk karakteristik market MEXC futures.

### Requirement Python

Butuh **Python 3.10 atau lebih baru** (kode memakai sintaks type hint `X | None` dan `tuple[...]`
yang tidak didukung di Python 3.9 ke bawah).

## Struktur Layer

| # | Layer | Fungsi | Tipe Gate |
|---|-------|--------|-----------|
| 0 | BTC Market Regime | Trend 4H BTC (EMA200) harus align dengan direction altcoin | **Hard** (toggleable) |
| 1 | Market Health | Volume, spread, ATR, pump/dump ekstrem, funding rate ekstrem | **Hard** |
| 2 | Trend Besar (4H) | EMA200 4H -> tentukan mode LONG/SHORT only | **Hard** |
| 3 | Market Structure (1H) | Swing HH/HL/LH/LL, BOS, CHoCH (adaptive fractal lookback) | **Hard** |
| 4 | Smart Money Area | Order Block, Fair Value Gap, Liquidity Sweep | **Soft** (scoring) |
| 5 | Konfirmasi Momentum | RSI(14), MACD histogram | **Soft** (scoring) |
| 6 | Volume | Volume 1H vs SMA20 Volume | **Soft** (scoring) |
| 7 | Entry Trigger | Pattern konfirmasi (engulfing / breakout close) | **Hard** |
| 8 | Risk Management | Hitung Entry/SL/TP1/TP2/TP3 otomatis | **Hard** |
| 9 | Scoring System | Skor 0-100 (termasuk BTC regime & OI confirmation), kirim jika >=70 | **Hard** (threshold) |

**Desain fail-fast vs soft-scoring:** Layer 0/1/2/3/7/8 adalah prasyarat struktural (tanpa
salah satunya, sinyal tidak valid sama sekali atau tidak punya arah/SL) sehingga tetap
hard-stop kalau gagal — failure point dicatat ke tabel `layer_logs` di Supabase. Layer 4/5/6
sebelumnya juga hard-stop, tapi sekarang bersifat **soft**: kalau gagal, hasilnya tetap
direkam dan tetap mempengaruhi skor Layer 9 (poin dikurangi, bukan otomatis gugur), supaya
sinyal dengan kombinasi kekuatan lain yang bagus tidak gagal total hanya karena satu dari
tiga layer "pendukung" ini tidak lolos. Layer mana saja yang soft-fail untuk suatu sinyal
tercatat di field `soft_fail_layers`.

### Layer 0 — BTC Market Regime (baru)

Altcoin sangat berkorelasi dengan BTC. Sebelum sinyal altcoin dievaluasi, bot mengecek trend
4H BTC (logika EMA200 yang sama dengan Layer 2). Kalau regime BTC jelas berlawanan arah
dengan direction altcoin, sinyal di-skip. Kalau regime BTC netral/sideways, filter ini tidak
memblokir. Regime BTC di-cache & refresh berkala (`BTC_REGIME_REFRESH_MINUTES`), bukan fetch
ulang tiap symbol, supaya hemat API call. Bisa dimatikan lewat `ENABLE_BTC_REGIME_FILTER=false`.

### Funding Rate (Layer 1) & Open Interest Confirmation (scoring, baru)

Funding rate ekstrem (`MAX_FUNDING_RATE_ABS_PCT`) dianggap tanda crowded trade satu sisi dan
memblokir sinyal di Layer 1 (kalau data funding tersedia dari MEXC — kalau tidak, cek ini
di-skip secara graceful, tidak memblokir). Open Interest confirmation bersifat soft/scoring
saja (`OI_CONFIRMATION_MIN_CHANGE_PCT`) karena data historis OI via ccxt/MEXC tidak selalu
stabil — kenaikan OI signifikan menambah skor karena mengindikasikan posisi baru benar-benar
dibangun, bukan hanya short-covering/long-unwind.

### Adaptive Swing/Fractal Lookback (Layer 3, baru)

Sebelumnya lookback fractal untuk deteksi swing high/low konstan (N=3) untuk semua pair.
Sekarang dihitung otomatis dari rata-rata ATR% 1H coin itu sendiri: coin ber-volatilitas
rendah pakai lookback lebih kecil (lebih sensitif), coin ber-volatilitas tinggi pakai
lookback lebih besar (mengurangi swing palsu akibat noise). Nilai dihitung sekali di Layer 3
lalu dipakai ulang oleh Layer 4 dan Layer 8 untuk konsistensi definisi swing per symbol.
Rentang & threshold bisa diatur lewat `SWING_LOOKBACK_MIN/MAX/DEFAULT` dan
`SWING_LOOKBACK_LOW_ATR_PCT/HIGH_ATR_PCT`.

## Struktur Folder

```
trading_bot/
├── main.py                     # entry point (scheduler loop)
├── pipeline.py                 # orkestrasi 9 layer untuk 1 symbol
├── config.py                   # load & validasi environment variables
├── models.py                   # dataclass: TradeSignal, LayerResult, RiskPlan, dst
├── core/
│   ├── exchange_client.py      # wrapper ccxt (OHLCV, ticker, orderbook)
│   ├── supabase_client.py      # simpan signal & layer log
│   └── telegram_notifier.py    # format & kirim pesan ke Telegram
├── indicators/
│   └── technical.py            # EMA, SMA, RSI, MACD, ATR (implementasi manual)
├── layers/
│   ├── layer1_market_health.py
│   ├── layer2_trend.py
│   ├── layer3_structure.py
│   ├── layer4_smart_money.py
│   ├── layer5_momentum.py
│   ├── layer6_volume.py
│   ├── layer7_entry_trigger.py
│   ├── layer8_risk_management.py
│   └── layer9_scoring.py
├── supabase_schema.sql         # skema tabel signals & layer_logs
├── .env.example
└── requirements.txt
```

## Setup

### 1. Install dependency

```bash
python -m venv venv
source venv/bin/activate      # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Siapkan Supabase

1. Buat project baru di https://supabase.com
2. Buka **SQL Editor**, jalankan isi file `supabase_schema.sql`
3. Ambil `Project URL` dan `service_role key` (atau `anon key` jika RLS diatur sesuai kebutuhan)
   dari **Project Settings > API**

### 3. Siapkan Bot Telegram

1. Chat ke [@BotFather](https://t.me/BotFather) di Telegram, buat bot baru dengan `/newbot`
2. Simpan **bot token** yang diberikan
3. Tambahkan bot ke channel/group tujuan signal (atau chat langsung ke bot untuk personal)
4. Dapatkan **chat id**:
   - Untuk personal chat: kirim pesan ke bot, lalu buka
     `https://api.telegram.org/bot<TOKEN>/getUpdates` dan cari field `chat.id`
   - Untuk channel: tambahkan bot sebagai admin, lalu chat id biasanya berupa `-100xxxxxxxxxx`

### 4. Konfigurasi `.env`

```bash
cp .env.example .env
```

Isi semua variabel di `.env`:
- Tidak ada `EXCHANGE_API_KEY` / `EXCHANGE_API_SECRET` lagi. Bot ini hanya memanggil endpoint
  publik MEXC (OHLCV, ticker, order book) dan tidak pernah menyentuh akun/eksekusi order, jadi
  API key/secret exchange tidak dibutuhkan sama sekali. Exchange sendiri (MEXC) sudah hardcoded,
  tidak ada `EXCHANGE_ID` lagi di `.env`.
- `WATCHLIST_SYMBOLS` — daftar pair yang mau discan, pisahkan koma. **Wajib format perpetual ccxt**
  `BASE/QUOTE:QUOTE`, contoh `BTC/USDT:USDT`, `ETH/USDT:USDT` (bukan `BTC/USDT` saja — itu format
  spot dan akan salah pasar). Kalau kamu telanjur menulis format spot, bot akan otomatis
  menormalisasinya ke format futures lewat `ExchangeClient.normalize_symbol()`. Dipakai penuh
  kalau `WATCHLIST_MODE=static`, atau jadi watchlist awal/fallback kalau `WATCHLIST_MODE=dynamic`.
- `WATCHLIST_MODE` — `static` (default, pakai `WATCHLIST_SYMBOLS` apa adanya) atau `dynamic`
  (otomatis pakai top-N symbol MEXC Futures berdasarkan volume transaksi 24 jam, refresh berkala).
  Dengan `dynamic`, watchlist tidak perlu diisi manual dan otomatis mengikuti coin yang lagi
  ramai ditransaksikan.
- `WATCHLIST_TOP_N` — jumlah symbol top-volume yang diambil kalau mode `dynamic` (default `20`).
- `WATCHLIST_REFRESH_HOURS` — seberapa sering watchlist dinamis di-refresh, dalam jam (default
  `12`). Refresh terjadi otomatis di awal setiap scan kalau sudah lewat interval ini — bukan job
  terpisah, jadi tidak nambah proses baru. Refresh pertama selalu terjadi saat bot start.
- `WATCHLIST_QUOTE` — quote currency untuk filter pair saat mode dynamic (default `USDT`, sesuai
  MEXC Futures USDT-M).
- `SUPABASE_URL`, `SUPABASE_KEY` — dari langkah 2
- `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` — dari langkah 3
- Threshold lain (volume minimum, RSI, dsb) bisa disesuaikan sesuai kebutuhan
- `EXCHANGE_SANDBOX` — **tidak didukung**, MEXC tidak punya sandbox/testnet di ccxt. Kalau
  di-set `true`, bot tetap jalan ke live market dan cuma menampilkan warning saat start.

### 5. Jalankan bot

```bash
python main.py
```

Bot akan langsung melakukan satu kali scan saat start, lalu berjalan berkala sesuai
`SCAN_INTERVAL_SECONDS`.

## Menyesuaikan Bobot Scoring

Bobot Layer 9 didefinisikan di `config.py` pada `Settings.scoring_weights` (dict). Ubah nilainya
sesuai preferensi — total maksimal tetap disarankan 100 supaya skema bintang (⭐) tetap konsisten:

```python
scoring_weights: dict = field(default_factory=lambda: {
    "trend_aligned": 25,
    "bos": 20,
    "order_block": 15,
    "fvg": 10,
    "volume_spike": 10,
    "rsi": 5,
    "macd": 5,
    "atr_high": 5,
    "not_near_resistance": 5,
})
```

`SCORE_MIN_TO_SEND` di `.env` menentukan skor minimum agar signal dikirim (default 70, sesuai
batas bawah klasifikasi B-Setup).

## Debugging per Layer

Setiap pemanggilan layer mengembalikan objek `LayerResult` (lihat `models.py`) berisi:
`layer_number`, `layer_name`, `status` (PASS/FAIL/SKIPPED), `reason`, dan `data` (nilai mentah
yang dipakai untuk keputusan). Semua `LayerResult` — baik yang lolos maupun gagal — otomatis
disimpan ke tabel `layer_logs` di Supabase, sehingga kamu bisa query:

```sql
select * from layer_logs
where symbol = 'BTC/USDT:USDT'
order by created_at desc
limit 50;
```

untuk melihat persis di layer mana suatu setup gagal, dan kenapa.

## Backtesting / Outcome Tracking

### Outcome tracking (otomatis)

Tabel `signals` punya kolom `outcome`, `pnl_pct`, `closed_at`. Sekarang kolom ini diisi
**otomatis** oleh `outcome_tracker.py`, yang berjalan di scheduler terpisah dari
`scan_watchlist()` (lihat `main.py`, interval diatur lewat `OUTCOME_TRACKING_INTERVAL_SECONDS`,
default tiap 1 jam — tidak perlu secepat scan sinyal baru). Untuk tiap sinyal yang sudah
terkirim tapi `outcome` masih `NULL`, tracker mengambil candle 15m sejak `generated_at` dan
menentukan apakah SL tersentuh duluan (`LOSS_SL`) atau TP1/2/3 tersentuh (`WIN_TP1/2/3`);
kalau belum ada yang tersentuh setelah `OUTCOME_MAX_AGE_HOURS` (default 72 jam), ditandai
`OPEN_EXPIRED` supaya tidak menggantung selamanya.

Jalankan manual sekali:
```bash
python outcome_tracker.py
```
Setelah beberapa waktu berjalan, win-rate riil bisa dihitung langsung dari Supabase:
```sql
select outcome, count(*), avg(pnl_pct)
from signals
where outcome is not null
group by outcome;
```

### Backtest historis (`backtest.py`)

Sebelumnya semua threshold default (RSI, volume spike multiplier, score minimum, dst)
adalah angka "masuk akal secara intuisi TA umum" yang **belum pernah divalidasi** terhadap
data MEXC riil. `backtest.py` menjalankan ulang modul layer yang **persis sama** dengan
`pipeline.py` (bukan reimplementasi terpisah) secara bar-by-bar di atas data historis
(tanpa lookahead bias — tiap bar hanya melihat data sampai saat itu), lalu mensimulasikan
outcome tiap sinyal (SL/TP mana yang tersentuh duluan) untuk menghasilkan win-rate &
expectancy riil.

```bash
# Backtest satu/lebih symbol, 60 hari terakhir
python backtest.py --symbols BTC/USDT:USDT,ETH/USDT:USDT --days 60

# Grid search parameter (contoh bawaan: score_min_to_send x volume_spike_multiplier)
python backtest.py --symbols BTC/USDT:USDT --days 60 --grid-search
```

**Keterbatasan yang perlu diketahui:**
- funding rate & OI historis tidak disimulasikan (data granular historisnya tidak selalu
  tersedia gratis) — filter funding di Layer 1 otomatis di-skip (graceful, sama seperti
  perilaku live saat data funding tidak tersedia), dan OI confirmation di scoring otomatis
  0 poin. Skor hasil backtest karena itu sedikit lebih rendah dari estimasi skor live yang
  funding/OI-nya tersedia — ini disengaja, bukan bug.
- Spread historis diasumsikan konstan (tidak ada data order-book historis gratis).
- Environment tempat kode ini disusun tidak punya akses jaringan ke `api.mexc.com`, jadi
  backtest terhadap data MEXC riil **perlu dijalankan sendiri** di environment Anda —
  skrip ini sudah diuji logikanya (deteksi sinyal, simulasi outcome, agregasi ringkasan)
  dengan data OHLCV sintetis dan terbukti benar, tapi angka win-rate/threshold yang
  realistis baru bisa didapat dari data MEXC riil.

Ubah `param_grid` di `backtest.py` (fungsi `_cli()`) untuk menguji parameter lain, atau
panggil `backtest.grid_search()` langsung dari skrip Python sendiri.

## Perubahan dari Versi Sebelumnya (Bug Fix + MEXC-only)

1. **Fix crash `EXCHANGE_SANDBOX=true`** — MEXC tidak punya sandbox/testnet di ccxt
   (`ex.urls["test"]` kosong), memanggil `set_sandbox_mode(True)` akan raise `TypeError` saat
   bot start. Sekarang opsi ini diabaikan (tidak dipanggil sama sekali) dan hanya menampilkan
   warning kalau `.env` mengaktifkannya.
2. **Fix format symbol futures** — `exchange_client.py` sekarang set
   `options={"defaultType": "swap"}` dan menormalisasi symbol ke format perpetual ccxt
   (`BASE/QUOTE:QUOTE`) lewat `ExchangeClient.normalize_symbol()`, supaya data yang diambil
   benar-benar dari market futures, bukan spot.
3. **Fix data hilang diam-diam saat simpan ke Supabase** — nilai numpy (`numpy.float64`,
   `numpy.bool_`) dan `pandas.Timestamp` hasil perhitungan indikator sekarang disanitasi lewat
   `_json_safe()` di `core/supabase_client.py` sebelum di-insert, supaya tidak gagal serialize
   secara diam-diam.
4. **Fix `smart_money_zones` tidak pernah tersimpan** — field ini dihitung di Layer 4 tapi
   sebelumnya tidak disertakan di `TradeSignal.to_supabase_row()`. Sudah ditambahkan (kolom baru
   `smart_money_zones jsonb` di `supabase_schema.sql`).
5. **Exchange dikunci ke MEXC Futures saja** — `EXCHANGE_ID` dihapus dari `.env`, di-hardcode di
   `config.py`, supaya tidak ada kemungkinan bot tidak sengaja jalan ke exchange lain yang
   threshold-nya belum ditala untuk MEXC.
6. **Retry-with-backoff untuk API call** — semua panggilan ccxt (OHLCV, ticker, order book,
   funding rate, open interest, load markets) sekarang dibungkus retry otomatis untuk error
   transient (network blip, rate limit sesaat), lihat `ExchangeClient._call_with_retry()`.
   Sebelumnya satu kegagalan sesaat langsung membuat symbol tsb di-skip untuk siklus scan itu.
7. **Candle-closed check (anti repaint)** — `fetch_ohlcv_df()` sekarang membuang candle
   terakhir yang masih "live"/belum closed (`DROP_UNCLOSED_CANDLE=true` default), supaya
   sinyal (terutama Layer 7 entry trigger yang deteksi pattern candlestick) tidak berubah-ubah
   antar-scan karena candle yang dievaluasi masih terus terbentuk.
8. **Threshold ATR & volume relatif per-coin** — Layer 1 sekarang juga mengecek percentile
   ATR%/volume coin terhadap histori coin itu sendiri (`ENABLE_RELATIVE_ATR_FILTER`,
   `ENABLE_RELATIVE_VOLUME_FILTER`), bukan hanya angka absolut sama untuk semua symbol.
9. **Outcome tracking & backtest otomatis** — `outcome_tracker.py` (menentukan WIN/LOSS
   otomatis untuk sinyal yang sudah dikirim) dan `backtest.py` (validasi historis + grid
   search parameter) ditambahkan — sebelumnya kolom `outcome`/`pnl_pct` permanen kosong dan
   threshold default tidak pernah divalidasi.

Jika kamu sudah pernah menjalankan tabel Supabase versi lama, jalankan migrasi berikut agar
kolom baru tersedia:

```sql
alter table signals add column if not exists smart_money_zones jsonb;
```

## Catatan Penting

- Bot ini adalah **screening/alerting tool**, bukan auto-executor — tidak ada order yang
  dieksekusi otomatis ke exchange.
- Selalu lakukan paper trading / backtest dulu sebelum menggunakan signal untuk trading real.
- Sesuaikan `MIN_VOLUME_24H_USD`, `SCORE_MIN_TO_SEND`, dan parameter lain sesuai gaya trading dan
  karakteristik pair yang di-screen.
