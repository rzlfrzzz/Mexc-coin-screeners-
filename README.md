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

| # | Layer | Fungsi |
|---|-------|--------|
| 1 | Market Health | Volume, spread, ATR, cek pump/dump ekstrem |
| 2 | Trend Besar (4H) | EMA200 4H -> tentukan mode LONG/SHORT only |
| 3 | Market Structure (1H) | Swing HH/HL/LH/LL, BOS, CHoCH |
| 4 | Smart Money Area | Order Block, Fair Value Gap, Liquidity Sweep |
| 5 | Konfirmasi Momentum | RSI(14), MACD histogram |
| 6 | Volume | Volume 1H vs SMA20 Volume |
| 7 | Entry Trigger | Pattern konfirmasi (engulfing / breakout close) |
| 8 | Risk Management | Hitung Entry/SL/TP1/TP2/TP3 otomatis |
| 9 | Scoring System | Skor 0-100, klasifikasi A+/A/B, kirim jika >=70 |

Jika satu layer gagal, pipeline **berhenti** (fail-fast) dan failure point dicatat ke tabel
`layer_logs` di Supabase — memudahkan proses tracing & refinement.

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
- `EXCHANGE_API_KEY`, `EXCHANGE_API_SECRET` — API key MEXC futures (opsional untuk data publik,
  tapi disarankan diisi untuk rate limit lebih tinggi). Exchange sendiri (MEXC) sudah hardcoded,
  tidak ada `EXCHANGE_ID` lagi di `.env`.
- `WATCHLIST_SYMBOLS` — daftar pair yang mau discan, pisahkan koma. **Wajib format perpetual ccxt**
  `BASE/QUOTE:QUOTE`, contoh `BTC/USDT:USDT`, `ETH/USDT:USDT` (bukan `BTC/USDT` saja — itu format
  spot dan akan salah pasar). Kalau kamu telanjur menulis format spot, bot akan otomatis
  menormalisasinya ke format futures lewat `ExchangeClient.normalize_symbol()`.
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

Tabel `signals` punya kolom `outcome`, `pnl_pct`, `closed_at` yang sengaja dikosongkan saat
insert. Buat proses terpisah (cron job / script tambahan) yang secara berkala mengecek harga
market terhadap `entry/sl/tp1/tp2/tp3` tiap signal yang `outcome` nya masih `NULL`, lalu panggil
`supabase_client.update_signal_outcome()` untuk mencatat hasilnya. Ini dipisah dari pipeline utama
supaya scanning real-time tidak terbebani proses tracking historis.

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
