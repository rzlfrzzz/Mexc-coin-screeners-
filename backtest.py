"""
backtest.py
------------
Alat backtest historis untuk memvalidasi threshold/parameter default bot (yang
sebelumnya hanya "masuk akal secara intuisi TA umum", belum pernah diuji terhadap data
MEXC riil). Skrip ini MEMAKAI ULANG modul layer yang PERSIS SAMA dengan pipeline.py
(bukan duplikasi/reimplementasi logika) - dijalankan bar-by-bar di atas data historis,
sehingga hasil backtest benar-benar merepresentasikan apa yang akan bot hasilkan secara
live dengan parameter yang sama.

PENTING - keterbatasan sandbox tempat kode ini ditulis: environment yang dipakai untuk
menyusun kode ini TIDAK bisa mengakses api.mexc.com (network egress dibatasi ke domain
tertentu), jadi backtest terhadap data MEXC riil TIDAK bisa dijalankan/divalidasi dari
sisi saya. Skrip ini sudah diuji logikanya dengan data OHLCV sintetis (lihat komentar di
bagian bawah file / laporan chat) dan berjalan tanpa error, tapi validasi terhadap
angka win-rate/threshold yang REALISTIS tetap perlu dijalankan sendiri oleh Anda di
environment yang punya akses ke MEXC (`pip install -r requirements.txt` lalu jalankan
skrip ini seperti biasa).

Cara pakai:
    # Backtest sederhana, 1 atau lebih symbol, N hari terakhir
    python backtest.py --symbols BTC/USDT:USDT,ETH/USDT:USDT --days 60

    # Grid search parameter (contoh: cari kombinasi score_min_to_send & volume_spike_multiplier
    # dengan win-rate/expectancy terbaik)
    python backtest.py --symbols BTC/USDT:USDT --days 60 --grid-search

Metodologi:
- Untuk tiap symbol, ambil OHLCV historis di 3 timeframe (P1 - Pisahkan Timeframe):
  TF_HTF (4H, trend besar), TF_STRUCTURE (1H, struktur/SMC), TF_ENTRY (15M, momentum/
  volume/entry trigger) - plus TF_OUTCOME (default 15m, sama dengan TF_ENTRY secara
  default tapi konfigurasi independen) khusus untuk menentukan outcome trade.
- Jalan maju bar-demi-bar di timeframe STRUCTURE (mulai dari titik dengan warmup data
  cukup, default 300 candle ~12.5 hari, supaya EMA200 4H & indikator lain valid).
- Di tiap bar, jalankan Layer 1 -> 9 PERSIS seperti pipeline.py (termasuk Layer 0 BTC
  regime kalau BTC data disediakan, dan soft-fail Layer 4-6) menggunakan HANYA data yang
  "sudah diketahui" pada bar tersebut (tidak ada lookahead bias - df di-slice sampai bar
  ini saja).
- Kalau lolos skor minimum, catat sebagai sinyal, lalu outcome-nya ditentukan lewat
  trade_outcome.evaluate_trade_path() - fungsi TERPUSAT yang SAMA dengan yang dipakai
  outcome_tracker.py (live) - berjalan di atas candle settings.tf_outcome (bukan candle
  sinyal 1H) supaya urutan SL-vs-TP di dalam satu candle sinyal bisa dibedakan, bukan
  ditebak. Outcome = event PERTAMA yang tersentuh (TP1_FIRST/TP2_FIRST/TP3_FIRST/SL_FIRST),
  atau OPEN_EXPIRED kalau belum tersentuh apa pun sampai MAX_HOLDING_HOURS. Satu posisi
  terbuka per symbol pada satu waktu (tidak overlap) - standar praktik backtest.
- funding_rate_pct dan oi_change_pct historis TIDAK disimulasikan (data historis funding/OI
  granular tidak selalu tersedia gratis) - filter funding di Layer 1 otomatis di-skip untuk
  backtest (None = graceful skip, sesuai desain layer aslinya), OI confirmation di scoring
  juga otomatis 0 (tidak dapat bonus poin). Artinya skor hasil backtest sedikit BAWAH
  estimasi skor live yang funding/OI datanya tersedia - perbedaan ini disengaja dan
  transparan, bukan bug.
"""

import argparse
import itertools
import sys
from dataclasses import dataclass, field

import pandas as pd
from loguru import logger

from config import settings
from models import Direction, LayerStatus
from core.exchange_client import exchange_client
from trade_outcome import evaluate_trade_path, OPEN_EXPIRED
from layers import (
    layer1_market_health, layer2_trend, layer3_structure,
    layer4_smart_money, layer5_momentum, layer6_volume,
    layer7_entry_trigger, layer8_risk_management, layer9_scoring,
)

WARMUP_BARS = 300
MAX_HOLDING_HOURS = 24 * 7  # 7 hari - kalau belum SL/TP sampai sini, dianggap OPEN_EXPIRED (timeout)


def _check_btc_regime(direction: Direction, btc_htf_slice: pd.DataFrame):
    """
    Replika logika Layer 0 (layers/layer0_btc_regime.py) tapi tanpa cache berbasis wall-clock
    (tidak relevan untuk backtest - tiap bar historis butuh regime BTC pada waktu ITU, bukan
    "sekarang"). Return (passed: bool, btc_direction: str).
    """
    if not settings.enable_btc_regime_filter or btc_htf_slice is None:
        return True, Direction.NONE.value

    btc_data = layer2_trend.compute_trend_direction(btc_htf_slice)
    btc_direction = btc_data.get("trend_direction", Direction.NONE.value)

    if btc_direction == Direction.NONE.value:
        return True, btc_direction
    return btc_direction == direction.value, btc_direction


def _simulate_outcome(df_outcome_full: pd.DataFrame, signal_ts: pd.Timestamp, direction: Direction,
                       entry: float, sl: float, tp1: float, tp2: float, tp3: float):
    """
    Tentukan outcome memakai trade_outcome.evaluate_trade_path() - fungsi TERPUSAT yang
    juga dipakai outcome_tracker.py (live), supaya backtest dan live tidak pernah punya
    definisi "win" yang berbeda (lihat ringkasan_perbaikan.md P0.2).

    df_outcome_full : OHLCV pada timeframe GRANULAR (settings.tf_outcome, mis. 15m),
    bukan df_structure_full (timeframe sinyal) - supaya urutan SL-vs-TP di dalam satu
    candle sinyal bisa dibedakan, bukan ditebak.

    Return None kalau tidak ada cukup data outcome ke depan untuk signal ini (mis. signal
    ini terlalu dekat dengan ujung dataset) - caller sebaiknya skip signal ini, jangan
    dicatat dengan outcome yang tidak valid.
    """
    window_end = signal_ts + pd.Timedelta(hours=MAX_HOLDING_HOURS)
    window = df_outcome_full[(df_outcome_full.index > signal_ts) & (df_outcome_full.index <= window_end)]

    return evaluate_trade_path(
        window, direction.value, entry, sl, tp1, tp2, tp3,
        entry_time=signal_ts.to_pydatetime(),
        same_bar_policy=settings.outcome_same_bar_policy,
    )


@dataclass
class BacktestRecord:
    symbol: str
    generated_at: str
    direction: str
    score: int
    grade: str
    entry: float
    sl: float
    tp1: float
    tp2: float
    tp3: float
    outcome: str
    pnl_pct: float
    hours_held: float
    mfe_pct: float
    mae_pct: float
    ambiguous_same_bar: bool
    soft_fail_layers: list = field(default_factory=list)


ENTRY_WARMUP_BARS = 30  # cukup untuk SMA20 volume (Layer 6) & lookback breakout 20 (Layer 7)


def simulate_symbol(symbol: str, df_htf_full: pd.DataFrame, df_structure_full: pd.DataFrame,
                     df_entry_full: pd.DataFrame, df_outcome_full: pd.DataFrame,
                     btc_htf_full: pd.DataFrame = None) -> list:
    """
    Jalan maju bar-by-bar di df_structure_full (timeframe structure, default 1H), jalankan
    pipeline layer (memakai ulang modul layer asli) memakai data yang di-slice sampai bar
    tsb saja (no lookahead), catat sinyal & outcome.

    df_entry_full : OHLCV timeframe ENTRY (default 15m, settings.tf_entry) - dipakai Layer
    5/6/7/8 persis seperti raw_data["ohlcv_entry"] di live pipeline (lihat config.py P1 -
    Pisahkan Timeframe), di-slice sampai timestamp CANDLE STRUCTURE saat ini saja (tidak
    boleh mengintip candle entry yang closed-nya setelah candle structure ini closed).
    """
    records = []
    i = WARMUP_BARS
    n = len(df_structure_full)

    while i < n - 1:
        df_structure = df_structure_full.iloc[: i + 1]
        current_ts = df_structure.index[-1]
        df_htf = df_htf_full[df_htf_full.index <= current_ts]
        df_entry = df_entry_full[df_entry_full.index <= current_ts]

        if len(df_htf) < 210 or len(df_structure) < WARMUP_BARS or len(df_entry) < ENTRY_WARMUP_BARS:
            i += 1
            continue

        raw_data = {
            "symbol": symbol,
            "ticker": {
                "quoteVolume": float(df_structure["volume"].tail(24).sum() * df_structure["close"].iloc[-1]),
                "last": float(df_entry["close"].iloc[-1]),
            },
            "spread_pct": 0.03,  # data spread historis tidak tersedia - asumsi tight & konstan
            "ohlcv_htf": df_htf,
            "ohlcv_structure": df_structure,
            "ohlcv_entry": df_entry,
            "funding_rate_pct": None,  # data funding historis tidak disimulasikan (lihat docstring modul)
            "oi_change_pct": None,     # data OI historis tidak disimulasikan (lihat docstring modul)
        }

        lr1 = layer1_market_health.run(raw_data)
        if lr1.status != LayerStatus.PASS:
            i += 1
            continue

        lr2 = layer2_trend.run(raw_data)
        if lr2.status != LayerStatus.PASS:
            i += 1
            continue
        direction = Direction(lr2.data["trend_direction"])

        btc_slice = None
        if btc_htf_full is not None:
            btc_slice = btc_htf_full[btc_htf_full.index <= current_ts]
        btc_passed, btc_direction = _check_btc_regime(direction, btc_slice)
        if not btc_passed:
            i += 1
            continue

        lr3 = layer3_structure.run(raw_data)
        structure_aligned = (
            (direction == Direction.LONG and (lr3.data.get("bos_bullish") or lr3.data.get("structure_bias") == "bullish"))
            or
            (direction == Direction.SHORT and (lr3.data.get("bos_bearish") or lr3.data.get("structure_bias") == "bearish"))
        )
        if lr3.status != LayerStatus.PASS or not structure_aligned:
            i += 1
            continue

        lr4, _zones = layer4_smart_money.run(raw_data, direction)
        lr5 = layer5_momentum.run(raw_data, direction)
        lr6 = layer6_volume.run(raw_data)
        soft_fail = [lr.layer_name for lr in (lr4, lr5, lr6) if lr.status != LayerStatus.PASS]

        lr7 = layer7_entry_trigger.run(raw_data, direction, prior_layers_passed=True)
        if lr7.status != LayerStatus.PASS:
            i += 1
            continue

        lr8, risk_plan = layer8_risk_management.run(raw_data, direction)
        if lr8.status != LayerStatus.PASS or risk_plan is None:
            i += 1
            continue

        atr_pct_1h = lr1.data.get("atr_pct_1h", 0.0)
        snapshot = {
            "atr_high": atr_pct_1h > (settings.min_atr_pct * 1.5),
            "not_near_resistance": True,  # disederhanakan untuk backtest
            "btc_regime_aligned": bool(btc_direction == direction.value and btc_direction != Direction.NONE.value),
            "oi_confirmation": False,  # data OI historis tidak disimulasikan
        }
        layer_by_number = {1: lr1, 2: lr2, 3: lr3, 4: lr4, 5: lr5, 6: lr6}
        score = layer9_scoring.run(layer_by_number, snapshot)

        if score.total < settings.score_min_to_send:
            i += 1
            continue

        outcome_result = _simulate_outcome(
            df_outcome_full, current_ts, direction,
            risk_plan.entry, risk_plan.sl, risk_plan.tp1, risk_plan.tp2, risk_plan.tp3,
        )
        if outcome_result is None:
            # Tidak ada cukup data outcome ke depan (signal terlalu dekat ujung dataset) -
            # jangan dicatat dengan outcome yang tidak valid.
            i += 1
            continue

        outcome_label = OPEN_EXPIRED if outcome_result.still_open else outcome_result.outcome
        hours_held = (
            round(outcome_result.time_to_outcome_seconds / 3600, 4)
            if outcome_result.time_to_outcome_seconds is not None else None
        )

        records.append(BacktestRecord(
            symbol=symbol, generated_at=str(current_ts), direction=direction.value,
            score=score.total, grade=score.grade, entry=risk_plan.entry, sl=risk_plan.sl,
            tp1=risk_plan.tp1, tp2=risk_plan.tp2, tp3=risk_plan.tp3,
            outcome=outcome_label, pnl_pct=outcome_result.pnl_pct, hours_held=hours_held,
            mfe_pct=outcome_result.mfe_pct, mae_pct=outcome_result.mae_pct,
            ambiguous_same_bar=outcome_result.ambiguous_same_bar, soft_fail_layers=soft_fail,
        ))

        # satu posisi per symbol pada satu waktu - loncat ke bar mtf pertama SETELAH
        # trade ini closed (bukan +1 candle tetap), supaya tidak overlap dgn trade berikutnya.
        closed_dt = pd.Timestamp(outcome_result.closed_at)
        next_i = int(df_structure_full.index.searchsorted(closed_dt, side="right"))
        i = max(next_i, i + 1)

    return records


def run_backtest(symbols: list, days: int = 60, include_btc_regime: bool = True) -> pd.DataFrame:
    exchange_client.load_markets()
    limit = min(days * 24 + WARMUP_BARS + 50, 1500)

    btc_htf_full = None
    if include_btc_regime and settings.enable_btc_regime_filter:
        logger.info(f"Mengambil data BTC ({settings.btc_regime_symbol}) untuk Layer 0...")
        btc_htf_full = exchange_client.fetch_ohlcv_df(settings.btc_regime_symbol, settings.tf_htf, limit=limit // 4)

    all_records = []
    for symbol in symbols:
        symbol = exchange_client.normalize_symbol(symbol)
        logger.info(f"Backtest {symbol}: mengambil data historis...")
        df_htf_full = exchange_client.fetch_ohlcv_df(symbol, settings.tf_htf, limit=limit // 4)
        df_structure_full = exchange_client.fetch_ohlcv_df(symbol, settings.tf_structure, limit=limit)

        if len(df_structure_full) < WARMUP_BARS + 10:
            logger.warning(f"[{symbol}] Data historis tidak cukup, skip.")
            continue

        since_ms = int(df_structure_full.index[0].timestamp() * 1000)

        # Data timeframe ENTRY (settings.tf_entry, default 15m) - dipaginasi supaya
        # mencakup seluruh window backtest, dipakai Layer 5/6/7/8 (lihat P1 - Pisahkan
        # Timeframe). Kalau TF_ENTRY == TF_OUTCOME (default sama-sama 15m), pakai ulang
        # hasil fetch yang sama supaya tidak dobel request ke exchange.
        logger.info(f"[{symbol}] Mengambil data entry-timeframe ({settings.tf_entry})...")
        df_entry_full = exchange_client.fetch_ohlcv_range_df(symbol, settings.tf_entry, since_ms)

        if df_entry_full.empty:
            logger.warning(f"[{symbol}] Data entry-timeframe ({settings.tf_entry}) tidak tersedia, skip symbol.")
            continue

        # Data outcome-checking pada timeframe GRANULAR (settings.tf_outcome), dipaginasi
        # supaya mencakup seluruh window backtest + buffer holding period di ujung -
        # lihat trade_outcome.py untuk kenapa ini harus lebih granular dari tf_structure.
        if settings.tf_outcome == settings.tf_entry:
            df_outcome_full = df_entry_full
        else:
            logger.info(f"[{symbol}] Mengambil data outcome-checking ({settings.tf_outcome})...")
            df_outcome_full = exchange_client.fetch_ohlcv_range_df(symbol, settings.tf_outcome, since_ms)

        if df_outcome_full.empty:
            logger.warning(f"[{symbol}] Data outcome-checking ({settings.tf_outcome}) tidak tersedia, skip symbol.")
            continue

        records = simulate_symbol(symbol, df_htf_full, df_structure_full, df_entry_full, df_outcome_full, btc_htf_full)
        logger.info(f"[{symbol}] {len(records)} sinyal historis ditemukan.")
        all_records.extend(records)

    if not all_records:
        return pd.DataFrame()

    return pd.DataFrame([r.__dict__ for r in all_records])


def summarize(df: pd.DataFrame) -> dict:
    if df.empty:
        return {"total_signals": 0}

    closed = df[df["outcome"] != OPEN_EXPIRED]
    wins = df[df["outcome"].isin(["TP1_FIRST", "TP2_FIRST", "TP3_FIRST"])]
    losses = df[df["outcome"] == "SL_FIRST"]

    win_rate = len(wins) / len(closed) * 100 if len(closed) else 0.0
    avg_pnl = df["pnl_pct"].mean()
    avg_win_pnl = wins["pnl_pct"].mean() if len(wins) else 0.0
    avg_loss_pnl = losses["pnl_pct"].mean() if len(losses) else 0.0
    expectancy = (win_rate / 100 * avg_win_pnl) + ((1 - win_rate / 100) * avg_loss_pnl) if len(closed) else 0.0
    ambiguous_count = int(df["ambiguous_same_bar"].sum()) if "ambiguous_same_bar" in df else 0

    return {
        "total_signals": len(df),
        "closed": len(closed),
        "open_expired": len(df) - len(closed),
        "win_rate_pct": round(win_rate, 2),
        "avg_pnl_pct": round(avg_pnl, 3),
        "avg_win_pnl_pct": round(avg_win_pnl, 3),
        "avg_loss_pnl_pct": round(avg_loss_pnl, 3),
        "expectancy_pct_per_trade": round(expectancy, 3),
        "avg_mfe_pct": round(df["mfe_pct"].mean(), 3) if "mfe_pct" in df else None,
        "avg_mae_pct": round(df["mae_pct"].mean(), 3) if "mae_pct" in df else None,
        "ambiguous_same_bar_count": ambiguous_count,
        "tp1_first_count": int((df["outcome"] == "TP1_FIRST").sum()),
        "tp2_first_count": int((df["outcome"] == "TP2_FIRST").sum()),
        "tp3_first_count": int((df["outcome"] == "TP3_FIRST").sum()),
        "grade_A+_count": int((df["grade"] == "A+").sum()),
        "grade_A_count": int((df["grade"] == "A").sum()),
        "grade_B_count": int((df["grade"] == "B").sum()),
    }


def grid_search(symbols: list, days: int, param_grid: dict) -> pd.DataFrame:
    """
    Jalankan run_backtest() untuk tiap kombinasi param_grid (cartesian product), dengan
    settings di-monkeypatch sementara per kombinasi, lalu bandingkan expectancy/win-rate.
    Contoh param_grid: {"score_min_to_send": [60, 70, 80], "volume_spike_multiplier": [1.2, 1.5, 2.0]}
    """
    keys = list(param_grid.keys())
    combos = list(itertools.product(*param_grid.values()))
    rows = []

    original_values = {k: getattr(settings, k) for k in keys}
    try:
        for combo in combos:
            overrides = dict(zip(keys, combo))
            for k, v in overrides.items():
                setattr(settings, k, v)

            logger.info(f"Grid search kombinasi: {overrides}")
            df = run_backtest(symbols, days=days)
            summary = summarize(df)
            rows.append({**overrides, **summary})
    finally:
        for k, v in original_values.items():
            setattr(settings, k, v)

    return pd.DataFrame(rows)


def _cli():
    parser = argparse.ArgumentParser(description="Backtest historis untuk validasi threshold bot MEXC screener")
    parser.add_argument("--symbols", type=str, default=",".join(settings.watchlist),
                         help="Comma-separated symbol, contoh: BTC/USDT:USDT,ETH/USDT:USDT")
    parser.add_argument("--days", type=int, default=60, help="Berapa hari data historis ke belakang")
    parser.add_argument("--grid-search", action="store_true", help="Jalankan grid search parameter contoh")
    args = parser.parse_args()

    logger.remove()
    logger.add(sys.stdout, level="INFO", format="<green>{time:HH:mm:ss}</green> | {message}")

    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]

    if args.grid_search:
        param_grid = {
            "score_min_to_send": [60, 70, 80],
            "volume_spike_multiplier": [1.2, 1.5, 2.0],
        }
        result = grid_search(symbols, args.days, param_grid)
        pd.set_option("display.width", 160)
        print("\n=== HASIL GRID SEARCH ===")
        print(result.sort_values("expectancy_pct_per_trade", ascending=False).to_string(index=False))
        return

    df = run_backtest(symbols, days=args.days)
    if df.empty:
        print("Tidak ada sinyal historis yang dihasilkan untuk parameter/periode ini.")
        return

    summary = summarize(df)
    print("\n=== RINGKASAN BACKTEST ===")
    for k, v in summary.items():
        print(f"{k:>28}: {v}")

    out_path = "backtest_signals.csv"
    df.to_csv(out_path, index=False)
    print(f"\nDetail {len(df)} sinyal disimpan ke {out_path}")


if __name__ == "__main__":
    _cli()
