-- ============================================================
-- Supabase schema untuk Smart Money Screening Bot
-- Jalankan di Supabase SQL editor sebelum menjalankan bot.
-- ============================================================

create table if not exists signals (
    id             bigint generated always as identity primary key,
    symbol         text not null,
    direction      text not null,               -- LONG | SHORT
    generated_at   timestamptz not null default now(),
    score          int,
    grade          text,                        -- A+ | A | B | REJECTED
    entry          numeric,
    sl             numeric,
    tp1            numeric,
    tp2            numeric,
    tp3            numeric,
    layer_results  jsonb,                        -- snapshot semua layer (untuk trace/debug)
    smart_money_zones jsonb,                     -- order block / FVG / liquidity sweep zones (Layer 4)
    indicators_snapshot jsonb,
    sent           boolean default false,
    fail_layer     text,                         -- diisi jika signal gagal / tidak dikirim (hard-stop)
    soft_fail_layers jsonb,                      -- layer 4-6 yang FAIL tapi tidak menghentikan pipeline
    -- kolom untuk backtesting / outcome tracking
    -- outcome ditentukan oleh EVENT PERTAMA yang tersentuh (lihat trade_outcome.py):
    -- TP1_FIRST | TP2_FIRST | TP3_FIRST | SL_FIRST | OPEN_EXPIRED. NULL = masih open,
    -- belum tersentuh apa pun (outcome_tracker.py masih akan mengecek lagi tiap interval).
    outcome        text,
    pnl_pct        numeric,
    closed_at      timestamptz,
    created_at     timestamptz not null default now()
);

-- ---------- Migrasi V2: kolom tambahan untuk outcome tracker terpusat (trade_outcome.py) ----------
-- Aman dijalankan berulang (IF NOT EXISTS) baik di database baru maupun yang sudah berjalan
-- dengan skema V1 di atas.
alter table signals add column if not exists mfe_pct numeric;              -- max favorable excursion sejak entry (%)
alter table signals add column if not exists mae_pct numeric;              -- max adverse excursion sejak entry (%)
alter table signals add column if not exists time_to_outcome_hours numeric; -- durasi entry -> outcome (jam)
alter table signals add column if not exists ambiguous_same_bar boolean default false; -- SL & TP tersentuh di candle outcome yang sama, urutan diasumsikan (lihat trade_outcome.py)

-- ---------- Migrasi V2 Phase 5: setup_id (lihat setup_identity.py & ringkasan_perbaikan.md P2) ----------
-- Identitas unik satu setup (symbol+direction+structure_event+zone) - dipakai anti-duplikasi
-- yang lebih presisi (get_open_signal_setup_id()) dan analitik ("signal dgn liquidity sweep +
-- FVG performanya bagaimana?" tanpa perlu join manual ke layer_logs).
alter table signals add column if not exists setup_id text;
create index if not exists idx_signals_setup_id on signals (setup_id);

-- ---------- Migrasi V2 Phase 7: score breakdown (flat) + correlation awareness ----------
-- Score breakdown DIRATAKAN jadi kolom sendiri (bukan cuma di dalam layer_results/JSON) supaya
-- bisa langsung di-query untuk analisis per-kategori confluence score (lihat
-- layers/layer9_scoring.py & ringkasan_perbaikan.md P1.11/P2). Nama kategori mengikuti
-- config.py::scoring_weights V2 (rebalanced) - BUKAN skema lama (trend_aligned/bos/dst).
alter table signals add column if not exists score_regime_4h numeric;
alter table signals add column if not exists score_structure_1h numeric;
alter table signals add column if not exists score_liquidity_event numeric;
alter table signals add column if not exists score_smc_location numeric;
alter table signals add column if not exists score_entry_trigger_15m numeric;
alter table signals add column if not exists score_volume numeric;
alter table signals add column if not exists score_momentum numeric;
alter table signals add column if not exists score_oi numeric;
alter table signals add column if not exists score_risk_quality numeric;

-- Correlation awareness (lihat core/correlation_tracker.py) - METADATA kesadaran risiko,
-- bukan bagian dari skor. Berapa banyak sinyal SEARAH lain sudah lolos di SIKLUS SCAN yang
-- sama sebelum sinyal ini (kemungkinan BTC-beta/market-wide move, bukan edge independen).
alter table signals add column if not exists correlation_same_direction_count int;
alter table signals add column if not exists correlation_high_risk boolean default false;

create index if not exists idx_signals_symbol on signals (symbol);
create index if not exists idx_signals_generated_at on signals (generated_at desc);
create index if not exists idx_signals_outcome on signals (outcome);

-- Log setiap layer (baik lolos maupun gagal) untuk keperluan debugging & refinement sistem.
create table if not exists layer_logs (
    id            bigint generated always as identity primary key,
    symbol        text not null,
    layer_number  int not null,
    layer_name    text not null,
    status        text not null,   -- PASS | FAIL | SKIPPED
    reason        text,
    data          jsonb,
    created_at    timestamptz not null default now()
);

create index if not exists idx_layer_logs_symbol on layer_logs (symbol);
create index if not exists idx_layer_logs_created_at on layer_logs (created_at desc);

-- ---------- Migrasi Phase 8: scan metrics (Watchlist size, lihat ringkasan_perbaikan.md P2) ----------
-- Tabel BARU (bukan ALTER tabel signals) karena metrics ini per SIKLUS SCAN, bukan per
-- symbol/sinyal - satu baris di sini mewakili satu kali scan_watchlist() selesai (lihat
-- core/metrics.py & main.py::job()), dipakai untuk membandingkan beban API/computation
-- saat watchlist diperbesar bertahap (100 -> 150 -> 200), sesuai saran ringkasan_perbaikan.md:
-- "Dengan ini kita tahu apakah bottleneck berasal dari API atau computation."
create table if not exists scan_metrics (
    id                bigint generated always as identity primary key,
    scanned_at        timestamptz not null default now(),
    watchlist_size    int,
    watchlist_mode    text,
    scan_duration_ms  numeric,
    request_count     int,
    failed_requests   int,
    retry_count       int,
    symbols_processed int,
    signals_generated int
);

create index if not exists idx_scan_metrics_scanned_at on scan_metrics (scanned_at desc);
