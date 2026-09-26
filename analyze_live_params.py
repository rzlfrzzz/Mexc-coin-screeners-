"""
analyze_live_params.py
------------------------
Phase 8 - Optimization: Funding & OI (lihat ringkasan_perbaikan.md P3 & optimize.py).

optimize.py sengaja TIDAK mencakup max_funding_rate_abs_pct (Funding) dan
oi_confirmation_min_change_pct (OI) karena backtest.py tidak mensimulasikan data historis
funding rate / Open Interest sama sekali (selalu None) - sweep lewat backtest untuk
keduanya akan menghasilkan angka identik di semua nilai (tidak menguji apa pun secara
diam-diam). Satu-satunya sumber data funding/OI yang REAL untuk bot ini adalah histori
sinyal LIVE yang sudah dikirim & sudah closed (SL/TP/expired) - persis alasan Phase 7
"Logging" (ringkasan_perbaikan.md P2) menyimpan funding_rate_pct & oi_change_pct per
sinyal:

    "Signal dengan liquidity sweep + FVG + OI naik sebenarnya performanya bagaimana?"

Modul ini menjawab pertanyaan yang sama untuk Funding & OI: kelompokkan sinyal live yang
SUDAH closed berdasarkan bucket funding_rate_pct / oi_change_pct saat sinyal itu dibuat,
lalu bandingkan win-rate & rata-rata pnl_pct antar bucket.

PENTING - butuh data live dulu:
Sesuai P3 ("kita kumpulkan data dulu"), modul ini TIDAK berguna sampai bot benar-benar
sudah mengirim & menutup cukup banyak sinyal live (sertakan estimasi minimum sample di
bawah). Menjalankannya terlalu dini hanya akan menghasilkan bucket dengan 1-2 sinyal yang
tidak bermakna secara statistik - modul ini akan MEMPERINGATKAN (bukan menyembunyikan)
kalau sample per bucket terlalu kecil.

Cara pakai (butuh SUPABASE_URL/SUPABASE_KEY terisi di .env, dan sinyal live yang sudah closed):
    python analyze_live_params.py --field funding_rate_pct
    python analyze_live_params.py --field oi_change_pct --bins 5
"""

import argparse
import sys

import pandas as pd
from loguru import logger

from core.supabase_client import supabase_store

MIN_SAMPLE_PER_BUCKET = 20  # di bawah ini, kesimpulan per-bucket dianggap belum bermakna


def load_closed_signals() -> pd.DataFrame:
    rows = supabase_store.fetch_closed_signals()
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows)


def analyze_field(df: pd.DataFrame, field: str, bins: int = 4) -> pd.DataFrame:
    """
    Bucket `field` (mis. funding_rate_pct atau oi_change_pct) jadi `bins` kelompok
    (pandas.qcut - kuantil, supaya tiap bucket punya jumlah sample sebanding, bukan
    lebar-tetap yang bisa timpang kalau distribusinya skewed), lalu hitung win_rate &
    avg_pnl_pct per bucket.
    """
    if field not in df.columns:
        raise ValueError(f"Kolom '{field}' tidak ada di data signals. Kolom tersedia: {list(df.columns)}")

    sub = df.dropna(subset=[field, "outcome", "pnl_pct"]).copy()
    if sub.empty:
        return pd.DataFrame()

    try:
        sub["bucket"] = pd.qcut(sub[field], q=bins, duplicates="drop")
    except ValueError:
        # Terlalu sedikit nilai unik untuk qcut sebanyak `bins` - fallback ke cut biasa.
        sub["bucket"] = pd.cut(sub[field], bins=bins)

    wins = sub["outcome"].isin(["TP1_FIRST", "TP2_FIRST", "TP3_FIRST"])
    closed_mask = sub["outcome"] != "OPEN_EXPIRED"

    rows = []
    for bucket, group in sub.groupby("bucket", observed=True):
        group_closed = group[group["outcome"] != "OPEN_EXPIRED"]
        n_closed = len(group_closed)
        win_rate = (
            group_closed["outcome"].isin(["TP1_FIRST", "TP2_FIRST", "TP3_FIRST"]).mean() * 100
            if n_closed else None
        )
        rows.append({
            "bucket": str(bucket),
            "n_signals": len(group),
            "n_closed": n_closed,
            "win_rate_pct": round(win_rate, 2) if win_rate is not None else None,
            "avg_pnl_pct": round(group["pnl_pct"].mean(), 3),
            "sample_ok": n_closed >= MIN_SAMPLE_PER_BUCKET,
        })
    return pd.DataFrame(rows)


def _cli():
    parser = argparse.ArgumentParser(
        description="Phase 8 - analisis post-hoc Funding/OI dari data live Supabase")
    parser.add_argument("--field", type=str, default="funding_rate_pct",
                         choices=["funding_rate_pct", "oi_change_pct"])
    parser.add_argument("--bins", type=int, default=4)
    args = parser.parse_args()

    logger.remove()
    logger.add(sys.stdout, level="INFO", format="<green>{time:HH:mm:ss}</green> | {message}")

    df = load_closed_signals()
    if df.empty:
        print("Belum ada sinyal live yang closed di Supabase. Modul ini butuh data live "
              "terkumpul dulu (lihat P3: 'kita kumpulkan data dulu') - jalankan bot beberapa "
              "waktu (atau outcome_tracker.py) sebelum memakai analisis ini.")
        return

    result = analyze_field(df, args.field, bins=args.bins)
    if result.empty:
        print(f"Tidak ada data valid untuk kolom '{args.field}' (semua NULL, atau semua sinyal "
              f"masih OPEN_EXPIRED).")
        return

    print(f"\n=== Phase 8: {args.field} vs outcome ({len(df)} sinyal closed) ===")
    print(result.to_string(index=False))

    if not result["sample_ok"].all():
        small = result[~result["sample_ok"]]["bucket"].tolist()
        print(f"\nPERINGATAN: bucket berikut punya < {MIN_SAMPLE_PER_BUCKET} sinyal closed, "
              f"JANGAN dijadikan dasar mengubah threshold hanya dari angka ini: {small}")


if __name__ == "__main__":
    _cli()
