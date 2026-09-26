"""
optimize.py
------------
Phase 8 - Optimization (lihat ringkasan_perbaikan.md P3 & bagian "Urutan implementasi").

P3 eksplisit meminta:

    "Jangan lakukan ini sebelum P0/P1 selesai... Kemudian lakukan eksperimen satu
    variabel pada satu waktu... Bukan: ATR berubah, RSI berubah, score berubah, volume
    berubah, OI berubah semuanya sekaligus. Kalau semuanya berubah, kita tidak tahu
    penyebab hasilnya."

backtest.py SUDAH punya grid_search() (dibuat di fase sebelumnya), tapi grid_search()
melakukan cartesian product dari beberapa parameter SEKALIGUS dalam satu run - itu
justru pola yang P3 minta DIHINDARI untuk tuning resmi Phase 8 (grid_search() tetap
berguna untuk eksplorasi interaksi antar-parameter, tapi bukan alat utama Phase 8).
Modul ini sebaliknya: SATU parameter berubah per eksperimen, parameter lain dikunci ke
BASELINE eksplisit (bukan "dibiarkan apa adanya di .env", supaya hasil antar-eksperimen
bisa dibandingkan apple-to-apple, dan supaya eksperimen sebelumnya dalam proses Python
yang sama tidak mencemari eksperimen berikutnya).

Parameter Phase 8 & alasan cakupannya di modul ini:
    - min_atr_pct                    -> dipakai backtest (Layer 1, dari OHLCV historis)
    - volume_spike_multiplier        -> dipakai backtest (Layer 6, dari OHLCV historis)
    - rsi_long_min / rsi_short_max   -> dipakai backtest (Layer 5, dari OHLCV historis)
    - score_min_to_send              -> dipakai backtest (gate akhir Layer 9)
    Kelima ini BISA divalidasi lewat backtest karena datanya (harga, volume) tersedia
    penuh secara historis dari OHLCV.

    - max_funding_rate_abs_pct (Funding) dan oi_confirmation_min_change_pct (OI) SENGAJA
    TIDAK dimasukkan ke sweep backtest di modul ini. Lihat backtest.py docstring:
    funding_rate_pct dan oi_change_pct historis TIDAK disimulasikan sama sekali (selalu
    None di raw_data backtest) - artinya mengubah threshold funding/OI di backtest TIDAK
    akan mengubah satu pun hasil, karena Layer 1/Layer 9 akan selalu graceful-skip check
    itu. Menjalankan "sweep" untuk keduanya di sini akan menghasilkan angka yang identik
    di semua nilai dan menyesatkan (seolah-olah sudah divalidasi, padahal belum sama
    sekali). Funding & OI dianalisis terpisah dari data LIVE lewat analyze_live_params.py
    setelah data live cukup terkumpul (lihat P3: "kita kumpulkan data dulu").

    - Watchlist size BUKAN parameter yang mempengaruhi kualitas sinyal (win-rate/expectancy),
    melainkan reliability/skala infrastruktur - diuji terpisah lewat watchlist_stress_test.py
    (lihat core/metrics.py), bukan lewat backtest expectancy.

Cara pakai:
    # Satu parameter saja
    python optimize.py --symbols BTC/USDT:USDT,ETH/USDT:USDT --days 60 --param min_atr_pct

    # Semua parameter Phase 8 yang bisa divalidasi lewat backtest, berurutan
    python optimize.py --symbols BTC/USDT:USDT,ETH/USDT:USDT --days 60

PENTING - keterbatasan sandbox: sama seperti backtest.py, modul ini TIDAK bisa dijalankan
end-to-end di sini karena environment penulisan kode ini tidak punya akses network ke
api.mexc.com. Logika sweep (kunci-baseline -> ubah satu parameter -> restore) sudah diuji
dengan me-monkeypatch backtest.run_backtest() memakai data sintetis (lihat laporan chat) -
tapi angka win-rate/expectancy REALISTIS tetap harus dijalankan sendiri oleh Anda.
"""

import argparse
import sys

import pandas as pd
from loguru import logger

from config import settings
import backtest


# Baseline V2 awal (lihat ringkasan_perbaikan.md - "Konfigurasi V2 awal" & Phase 7 report) -
# titik referensi tiap eksperimen. Parameter LAIN selalu dikunci ke nilai ini sebelum tiap
# run, terlepas dari apa isi .env saat modul ini dijalankan, supaya eksperimen deterministik
# dan tidak diam-diam berubah kalau .env berubah di antara dua sesi optimasi.
BASELINE = {
    "min_atr_pct": 0.2,
    "volume_spike_multiplier": 1.5,
    "rsi_long_min": 55,
    "rsi_short_max": 45,
    "score_min_to_send": 80,
}

# Rentang eksperimen per parameter. ATR & volume multiplier persis rentang yang disebutkan
# eksplisit di ringkasan_perbaikan.md P3 ("ATR = 0.2/0.3/0.4", "1.25/1.5/1.75/2.0"); RSI &
# score_min_to_send rentang wajar di sekitar baseline (langkah ~5 poin) karena ringkasan
# tidak memberi angka eksplisit untuk keduanya.
EXPERIMENTS = {
    "min_atr_pct": [0.2, 0.3, 0.4],
    "volume_spike_multiplier": [1.25, 1.5, 1.75, 2.0],
    "rsi_long_min": [50, 55, 60, 65],
    "rsi_short_max": [50, 45, 40, 35],
    "score_min_to_send": [60, 70, 80, 90],
}

RANK_METRIC = "expectancy_pct_per_trade"


def run_single_variable_experiment(param: str, values: list, symbols: list, days: int) -> pd.DataFrame:
    """
    Jalankan backtest.run_backtest() sekali per nilai di `values`, HANYA mengubah `param` -
    semua parameter Phase 8 lainnya dikunci eksplisit ke BASELINE sebelum tiap run (bukan
    cuma "tidak disentuh"), supaya efek `param` tidak tercampur dengan sisa perubahan dari
    eksperimen lain yang mungkin dipanggil sebelumnya di proses yang sama.

    settings di-monkeypatch sementara (pola sama seperti backtest.grid_search()) dan SELALU
    dikembalikan ke nilai aslinya di finally, termasuk kalau run_backtest() melempar error.
    """
    if param not in BASELINE:
        raise ValueError(
            f"Parameter '{param}' bukan bagian dari eksperimen Phase 8 yang divalidasi lewat "
            f"backtest di modul ini. Pilihan valid: {list(BASELINE)}. (Funding & OI dianalisis "
            f"terpisah lewat analyze_live_params.py - lihat docstring modul ini kenapa.)"
        )
    if param not in EXPERIMENTS:
        raise ValueError(f"Tidak ada rentang eksperimen terdaftar untuk '{param}'.")

    original = {k: getattr(settings, k) for k in BASELINE}
    rows = []
    try:
        for value in values:
            for k, base_v in BASELINE.items():
                setattr(settings, k, base_v)
            setattr(settings, param, value)

            logger.info(f"[Phase 8] Eksperimen {param}={value} (parameter lain dikunci ke baseline: "
                        f"{ {k: v for k, v in BASELINE.items() if k != param} })")
            df = backtest.run_backtest(symbols, days=days)
            summary = backtest.summarize(df)
            rows.append({param: value, "is_baseline": bool(value == BASELINE[param]), **summary})
    finally:
        for k, v in original.items():
            setattr(settings, k, v)

    return pd.DataFrame(rows)


def run_all_experiments(symbols: list, days: int, params: list = None) -> dict:
    """Jalankan run_single_variable_experiment() untuk tiap parameter di `params`
    (default: semua parameter Phase 8 yang bisa divalidasi lewat backtest), BERURUTAN
    (bukan cartesian product) - satu Series 'hasil per parameter' berdiri sendiri."""
    params = params or list(EXPERIMENTS.keys())
    results = {}
    for param in params:
        results[param] = run_single_variable_experiment(param, EXPERIMENTS[param], symbols, days)
    return results


def _print_result(param: str, df: pd.DataFrame) -> None:
    print(f"\n=== Eksperimen Phase 8: {param} (baseline={BASELINE[param]}, parameter lain dikunci) ===")
    if df.empty:
        print("(tidak ada sinyal yang lolos untuk parameter/periode ini - coba periode lebih panjang "
              "atau symbol lebih banyak sebelum menyimpulkan apa pun)")
        return
    cols = [param, "is_baseline", "total_signals", "closed", "win_rate_pct",
            "expectancy_pct_per_trade", "avg_pnl_pct"]
    cols = [c for c in cols if c in df.columns]
    print(df[cols].sort_values(RANK_METRIC, ascending=False).to_string(index=False))
    baseline_row = df[df["is_baseline"]]
    if not baseline_row.empty:
        base_exp = baseline_row.iloc[0][RANK_METRIC]
        best_exp = df[RANK_METRIC].max()
        if best_exp > base_exp:
            best_value = df.loc[df[RANK_METRIC].idxmax(), param]
            print(f"-> Nilai terbaik ({best_value}) mengungguli baseline ({BASELINE[param]}): "
                  f"{best_exp:.3f} vs {base_exp:.3f} {RANK_METRIC.replace('_', ' ')}. "
                  f"Cek juga jumlah sinyal (closed) - jangan pindah baseline hanya dari sample kecil.")


def _cli():
    parser = argparse.ArgumentParser(
        description="Phase 8 - eksperimen tuning satu variabel per waktu (lihat ringkasan_perbaikan.md P3)")
    parser.add_argument("--symbols", type=str, default=",".join(settings.watchlist),
                         help="Comma-separated symbol, contoh: BTC/USDT:USDT,ETH/USDT:USDT")
    parser.add_argument("--days", type=int, default=60, help="Berapa hari data historis ke belakang")
    parser.add_argument("--param", type=str, default=None,
                         help=f"Nama satu parameter untuk dites. Kalau tidak diisi, semua parameter "
                              f"berikut dites berurutan (BUKAN sekaligus): {list(EXPERIMENTS)}")
    args = parser.parse_args()

    logger.remove()
    logger.add(sys.stdout, level="INFO", format="<green>{time:HH:mm:ss}</green> | {message}")

    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    params = [args.param] if args.param else None
    results = run_all_experiments(symbols, args.days, params)

    for param, df in results.items():
        _print_result(param, df)
        if not df.empty:
            out_path = f"optimize_{param}.csv"
            df.to_csv(out_path, index=False)
            print(f"Detail disimpan ke {out_path}")


if __name__ == "__main__":
    _cli()
