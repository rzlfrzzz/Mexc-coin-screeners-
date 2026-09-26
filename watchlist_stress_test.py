"""
watchlist_stress_test.py
---------------------------
Phase 8 - Optimization: Watchlist size (lihat ringkasan_perbaikan.md P2 & P3):

    "WATCHLIST_TOP_N=100 ... Setelah stabil: 100 -> 150 -> 200. Jangan langsung 200.
    Tambahkan metric: scan_duration_ms, request_count, failed_requests, retry_count,
    symbols_processed, signals_generated. Dengan ini kita tahu apakah bottleneck
    berasal dari API atau computation."

Watchlist size BUKAN parameter kualitas sinyal (win-rate/expectancy) seperti parameter
lain di optimize.py - ini parameter RELIABILITY/SKALA infrastruktur, jadi diuji dengan
cara berbeda: menjalankan pipeline.scan_watchlist() SUNGGUHAN (live, memanggil API MEXC
riil - BUKAN backtest terhadap data historis) untuk tiap ukuran watchlist, lalu
membandingkan core/metrics.py.

PERINGATAN PENTING:
- Skrip ini memanggil API MEXC SUNGGUHAN (read-only: OHLCV/ticker publik, TIDAK ada order),
  jadi tetap memakan rate-limit exchange & butuh waktu nyata sebanding jumlah symbol
  (watchlist 200 simbol bisa memakan waktu signifikan). Jangan dijalankan berulang-ulang
  dalam waktu singkat, dan jangan dijalankan bersamaan dengan bot utama (main.py) yang
  sedang scan - keduanya akan berebut rate-limit yang sama.
- Sesuai saran ringkasan_perbaikan.md, JALANKAN BERTAHAP: pastikan satu ukuran stabil
  dulu (tidak ada failed_requests/retry_count yang melonjak, scan_duration_ms masih di
  bawah scan_interval_seconds) sebelum naik ke ukuran berikutnya - jangan langsung lompat
  ke 200 (--sizes 100,150,200 dijalankan berurutan oleh skrip ini justru supaya urutan ini
  otomatis diikuti, TAPI evaluasi hasil ukuran sebelumnya secara manual sebelum menjalankan
  lagi dengan ukuran yang lebih besar, jangan cuma percaya skrip berjalan tanpa exception).
- WATCHLIST_MODE dipaksa sementara ke "dynamic" untuk durasi tes ini saja (top-N by volume
  24h) - dikembalikan ke setting asli di .env setelahnya. WATCHLIST_TOP_N asli juga
  dikembalikan.

Cara pakai:
    python watchlist_stress_test.py --sizes 100,150,200

PENTING - keterbatasan sandbox: sama seperti backtest.py, skrip ini TIDAK bisa dijalankan
end-to-end di sini (tidak ada akses network ke api.mexc.com dari environment penulisan
kode ini). Alur reset-settings/force-refresh/restore sudah diuji dengan me-monkeypatch
exchange_client & pipeline.scan_watchlist() memakai stub (lihat laporan chat) - tapi angka
scan_duration_ms/request_count REALISTIS tetap harus dijalankan sendiri terhadap MEXC asli.
"""

import argparse
import sys

import pandas as pd
from loguru import logger

from config import settings
from core.exchange_client import exchange_client
from core.watchlist import watchlist_manager
from core.metrics import scan_metrics
import pipeline


def run_one_size(size: int) -> dict:
    """Paksa watchlist dynamic top-N = `size`, jalankan SATU siklus scan_watchlist() penuh,
    kembalikan metrics-nya. Setting asli (mode & top_n) SELALU dikembalikan di finally."""
    original_mode = settings.watchlist_mode
    original_top_n = settings.watchlist_top_n
    settings.watchlist_mode = "dynamic"
    settings.watchlist_top_n = size
    try:
        # force=True: paksa fetch ulang top-N simbol SEKARANG dengan top_n baru, jangan
        # pakai watchlist lama yang mungkin masih di-cache dari ukuran sebelumnya.
        watchlist_manager.refresh(force=True)
        actual_symbols = watchlist_manager.current_symbols()
        if len(actual_symbols) < size:
            logger.warning(
                f"Watchlist dinamis hanya berisi {len(actual_symbols)} symbol (diminta {size}) - "
                f"mungkin jumlah pair USDT perpetual di MEXC memang lebih sedikit dari itu."
            )
        pipeline.scan_watchlist()
    finally:
        settings.watchlist_mode = original_mode
        settings.watchlist_top_n = original_top_n

    row = scan_metrics.as_dict()
    row["watchlist_size_requested"] = size
    row["watchlist_size_actual"] = len(watchlist_manager.current_symbols())
    row["exceeds_scan_interval"] = (row["scan_duration_ms"] / 1000.0) > settings.scan_interval_seconds
    return row


def run_stress_test(sizes: list) -> pd.DataFrame:
    exchange_client.load_markets()
    rows = []
    for size in sizes:
        logger.info(f"=== Phase 8: stress test watchlist_top_n={size} ===")
        rows.append(run_one_size(size))
    return pd.DataFrame(rows)


def _cli():
    parser = argparse.ArgumentParser(
        description="Phase 8 - uji reliability watchlist size (lihat ringkasan_perbaikan.md P2)")
    parser.add_argument("--sizes", type=str, default="100,150,200",
                         help="Comma-separated ukuran watchlist yang dites berurutan, contoh: 100,150,200")
    args = parser.parse_args()

    logger.remove()
    logger.add(sys.stdout, level="INFO", format="<green>{time:HH:mm:ss}</green> | {message}")

    sizes = [int(x.strip()) for x in args.sizes.split(",") if x.strip()]
    df = run_stress_test(sizes)

    print("\n=== HASIL STRESS TEST WATCHLIST SIZE ===")
    print(df.to_string(index=False))
    if df["exceeds_scan_interval"].any():
        bad = df[df["exceeds_scan_interval"]]["watchlist_size_requested"].tolist()
        print(f"\nPERINGATAN: ukuran watchlist {bad} membuat satu siklus scan LEBIH LAMA dari "
              f"SCAN_INTERVAL_SECONDS ({settings.scan_interval_seconds}s) - scan berikutnya bisa "
              f"tumpang tindih / tertunda. Pertimbangkan menaikkan scan_interval_seconds atau tidak "
              f"naik ke ukuran ini dulu.")

    out_path = "watchlist_stress_test.csv"
    df.to_csv(out_path, index=False)
    print(f"\nDetail disimpan ke {out_path}")


if __name__ == "__main__":
    _cli()
