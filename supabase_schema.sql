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
    outcome        text,                         -- WIN_TP1 | WIN_TP2 | WIN_TP3 | LOSS | BREAKEVEN | OPEN
    pnl_pct        numeric,
    closed_at      timestamptz,
    created_at     timestamptz not null default now()
);

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
