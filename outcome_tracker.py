"""
outcome_tracker.py
--------------------
Proses tracking outcome OTOMATIS untuk sinyal yang sudah terkirim: mengecek pergerakan
harga sejak `generated_at` untuk menentukan outcome-nya, lalu menulis hasilnya ke
Supabase (kolom outcome/pnl_pct/closed_at/mfe_pct/mae_pct/time_to_outcome_hours).

Tanpa ini, win-rate riil bot TIDAK PERNAH bisa diketahui - kolom outcome akan selamanya
NULL dan tidak ada cara mengevaluasi apakah threshold/parameter bot ini benar-benar
menghasilkan sinyal yang profitable.

Cara pakai:
    python outcome_tracker.py          # jalankan sekali
Atau import track_outcomes() dan panggil berkala dari scheduler (lihat main.py).

Metodologi
----------
Definisi "outcome" (event PERTAMA yang tersentuh, bukan level terjauh yang PERNAH
tersentuh) dipusatkan di trade_outcome.evaluate_trade_path() - fungsi yang SAMA persis
dipakai backtest.py, supaya live dan backtest tidak pernah punya jawaban berbeda untuk
kejadian yang sama (lihat ringkasan_perbaikan.md P0 & P0.2).

- Candle dicek pada timeframe settings.tf_outcome (default 15m - lebih granular dari
  timeframe sinyal tf_structure) sejak generated_at, supaya urutan SL-vs-TP bisa dibedakan.
- Kalau outcome sudah ketemu (TP1/2/3_FIRST atau SL_FIRST) -> tulis ke Supabase.
- Kalau belum ada apa pun yang tersentuh:
  - umur signal < outcome_max_age_hours -> tetap OPEN, tidak diupdate (dicek lagi nanti).
  - umur signal >= outcome_max_age_hours -> OPEN_EXPIRED, pakai pnl_pct/mfe/mae unrealized
    dari candle terakhir yang tersedia, supaya tidak menggantung selamanya.
"""

import sys
from datetime import datetime, timezone

from loguru import logger

from config import settings
from core.exchange_client import exchange_client
from core.supabase_client import supabase_store
from trade_outcome import evaluate_trade_path, OPEN_EXPIRED


def _evaluate_signal(row: dict) -> dict | None:
    """
    Return dict siap dikirim ke supabase_store.update_signal_outcome() kalau outcome
    sudah bisa ditentukan (baik closed maupun OPEN_EXPIRED), None kalau masih harus
    ditunggu (belum tersentuh apa pun dan belum expired).
    """
    symbol = row["symbol"]
    direction = row["direction"]
    entry = row.get("entry")
    sl = row.get("sl")
    tp1, tp2, tp3 = row.get("tp1"), row.get("tp2"), row.get("tp3")
    generated_at = row.get("generated_at")

    if entry is None or sl is None:
        logger.warning(f"[{symbol}] Signal id={row.get('id')} tidak punya entry/SL, skip tracking")
        return None

    try:
        generated_dt = datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
    except Exception as e:
        logger.warning(f"[{symbol}] Gagal parse generated_at '{generated_at}' ({e}), skip tracking")
        return None

    since_ms = int(generated_dt.timestamp() * 1000)
    age_hours = (datetime.now(timezone.utc) - generated_dt).total_seconds() / 3600

    try:
        df = exchange_client.fetch_ohlcv_since_df(symbol, settings.tf_outcome, since_ms, limit=1000)
    except Exception as e:
        logger.error(f"[{symbol}] Gagal fetch candle untuk tracking outcome: {e}")
        return None

    # Candle PERTAMA dari fetch_ohlcv_since_df(since=generated_at) bisa jadi candle yang
    # SEDANG BERJALAN saat signal digenerate (bukan sepenuhnya "setelah" entry) - buang
    # supaya tidak ada risiko look-ahead/self-fulfilling pada candle entry itu sendiri.
    if not df.empty and df.index[0] <= generated_dt:
        df = df.iloc[1:]

    result = evaluate_trade_path(
        df, direction, entry, sl, tp1, tp2, tp3,
        entry_time=generated_dt,
        same_bar_policy=settings.outcome_same_bar_policy,
    )
    if result is None:
        return None

    if result.still_open:
        if age_hours < settings.outcome_max_age_hours:
            return None  # masih open, belum expired - cek lagi nanti
        outcome = OPEN_EXPIRED
    else:
        outcome = result.outcome

    return {
        "outcome": outcome,
        "pnl_pct": result.pnl_pct,
        "closed_at": result.closed_at,
        "mfe_pct": result.mfe_pct,
        "mae_pct": result.mae_pct,
        "time_to_outcome_hours": (
            round(result.time_to_outcome_seconds / 3600, 4)
            if result.time_to_outcome_seconds is not None else None
        ),
        "ambiguous_same_bar": result.ambiguous_same_bar,
    }


def track_outcomes() -> int:
    """Jalankan satu putaran tracking untuk semua open signal. Return jumlah signal yang di-update."""
    if not settings.enable_outcome_tracking:
        logger.info("Outcome tracking dimatikan (ENABLE_OUTCOME_TRACKING=false)")
        return 0

    open_signals = supabase_store.fetch_open_signals()
    if not open_signals:
        logger.info("Tidak ada open signal untuk di-track.")
        return 0

    logger.info(f"Tracking outcome untuk {len(open_signals)} open signal...")
    updated = 0
    for row in open_signals:
        result = _evaluate_signal(row)
        if result is None:
            continue
        supabase_store.update_signal_outcome(row["id"], result)
        flag = " [AMBIGU: SL&TP di candle sama]" if result.get("ambiguous_same_bar") else ""
        logger.info(f"[{row['symbol']}] id={row['id']} -> {result['outcome']} ({result['pnl_pct']:+.2f}%){flag}")
        updated += 1

    logger.info(f"Outcome tracking selesai: {updated}/{len(open_signals)} signal di-update.")
    return updated


if __name__ == "__main__":
    logger.remove()
    logger.add(sys.stdout, level=settings.log_level,
                format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level}</level> | {message}")
    track_outcomes()
