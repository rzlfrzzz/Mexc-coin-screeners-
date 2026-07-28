"""
outcome_tracker.py
--------------------
Proses tracking outcome OTOMATIS untuk sinyal yang sudah terkirim: mengecek pergerakan
harga sejak `generated_at` untuk menentukan apakah SL tersentuh duluan (LOSS) atau
TP1/TP2/TP3 tersentuh (WIN), lalu menulis hasilnya ke Supabase (kolom outcome/pnl_pct/
closed_at yang di skema sudah ada tapi sebelumnya tidak pernah diisi otomatis).

Tanpa ini, win-rate riil bot TIDAK PERNAH bisa diketahui - kolom outcome akan selamanya
NULL dan tidak ada cara mengevaluasi apakah threshold/parameter bot ini benar-benar
menghasilkan sinyal yang profitable.

Cara pakai:
    python outcome_tracker.py          # jalankan sekali
Atau import track_outcomes() dan panggil berkala dari scheduler (lihat main.py).

Metodologi (disederhanakan, cocok untuk tracking otomatis tanpa data tick-by-tick):
- Ambil candle 15m sejak generated_at sampai sekarang.
- Jalan maju candle demi candle secara kronologis:
  - Kalau low candle (LONG) / high candle (SHORT) menyentuh SL -> LOSS, berhenti di situ.
    (Asumsi konservatif: kalau SL dan TP tersentuh di candle yang sama, SL dianggap
    tersentuh lebih dulu - menghindari melebih-lebihkan win rate.)
  - Kalau tidak, cek apakah TP1/TP2/TP3 tersentuh (progresif, TP tertinggi yang valid
    tercapai dalam urutan candle yang sama dicatat).
- Kalau sampai sekarang belum ada yang tersentuh:
  - Umur signal < outcome_max_age_hours -> tetap OPEN, tidak diupdate (dicek lagi nanti).
  - Umur signal >= outcome_max_age_hours -> ditandai OPEN_EXPIRED dengan pnl_pct unrealized
    (harga sekarang vs entry), supaya tidak menggantung selamanya di query "open signals".
"""

import sys
from datetime import datetime, timezone

from loguru import logger

from config import settings
from core.exchange_client import exchange_client
from core.supabase_client import supabase_store

TRACKING_TIMEFRAME = "15m"


def _pnl_pct(direction: str, entry: float, exit_price: float) -> float:
    if direction == "LONG":
        return (exit_price - entry) / entry * 100
    return (entry - exit_price) / entry * 100


def _evaluate_signal(row: dict) -> dict | None:
    """
    Return dict {outcome, pnl_pct, closed_at} kalau outcome sudah bisa ditentukan,
    None kalau masih OPEN dan belum expired (tidak perlu update apa pun).
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
        df = exchange_client.fetch_ohlcv_since_df(symbol, TRACKING_TIMEFRAME, since_ms, limit=1000)
    except Exception as e:
        logger.error(f"[{symbol}] Gagal fetch candle untuk tracking outcome: {e}")
        return None

    if df.empty:
        return None

    tp_targets = [("WIN_TP1", tp1), ("WIN_TP2", tp2), ("WIN_TP3", tp3)]
    tp_targets = [(label, tp) for label, tp in tp_targets if tp is not None]
    best_tp_reached = None  # index terjauh di tp_targets yang tercapai

    for ts, candle in df.iterrows():
        low, high = float(candle["low"]), float(candle["high"])

        sl_hit = (low <= sl) if direction == "LONG" else (high >= sl)
        if sl_hit:
            return {
                "outcome": "LOSS_SL",
                "pnl_pct": round(_pnl_pct(direction, entry, sl), 4),
                "closed_at": ts.isoformat(),
            }

        for i, (label, tp) in enumerate(tp_targets):
            reached = (high >= tp) if direction == "LONG" else (low <= tp)
            if reached and (best_tp_reached is None or i > best_tp_reached):
                best_tp_reached = i

    if best_tp_reached is not None:
        label, tp = tp_targets[best_tp_reached]
        return {
            "outcome": label,
            "pnl_pct": round(_pnl_pct(direction, entry, tp), 4),
            "closed_at": df.index[-1].isoformat(),
        }

    if age_hours >= settings.outcome_max_age_hours:
        last_close = float(df["close"].iloc[-1])
        return {
            "outcome": "OPEN_EXPIRED",
            "pnl_pct": round(_pnl_pct(direction, entry, last_close), 4),
            "closed_at": df.index[-1].isoformat(),
        }

    return None  # masih open, belum expired - cek lagi nanti


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
        supabase_store.update_signal_outcome(row["id"], result["outcome"], result["pnl_pct"], result["closed_at"])
        logger.info(f"[{row['symbol']}] id={row['id']} -> {result['outcome']} ({result['pnl_pct']:+.2f}%)")
        updated += 1

    logger.info(f"Outcome tracking selesai: {updated}/{len(open_signals)} signal di-update.")
    return updated


if __name__ == "__main__":
    logger.remove()
    logger.add(sys.stdout, level=settings.log_level,
                format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level}</level> | {message}")
    track_outcomes()
