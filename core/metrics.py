"""
core/metrics.py
------------------
Instrumentasi ringan untuk satu siklus scan_watchlist() (lihat ringkasan_perbaikan.md
P2 - Watchlist & API):

    "Tambahkan metric: scan_duration_ms, request_count, failed_requests, retry_count,
    symbols_processed, signals_generated. Dengan ini kita tahu apakah bottleneck
    berasal dari API atau computation."

Ini BUKAN metrics framework generik (Prometheus/StatsD dsb) secara sengaja - watchlist
bot ini masih kecil (rencana 100 -> 150 -> 200 symbol, satu proses, satu scan pada satu
waktu lewat APScheduler max_instances=1), jadi counter in-memory sederhana yang di-reset
tiap awal scan_watchlist() sudah cukup dan jauh lebih mudah diaudit daripada menambah
dependency baru untuk kebutuhan ini.

Dipakai oleh:
- core/exchange_client.py  -> record_request()/record_retry()/record_failed_request()
  di dalam _call_with_retry(), supaya SEMUA panggilan exchange (OHLCV, ticker, dst)
  otomatis terhitung tanpa perlu instrumentasi manual di tiap fungsi fetch_*.
- pipeline.py               -> reset()/start()/stop() di scan_watchlist(), plus
  record_symbol_processed()/record_signal_generated() per symbol.
- watchlist_stress_test.py  -> baca as_dict() setelah tiap scan untuk membandingkan
  ukuran watchlist yang berbeda (100 vs 150 vs 200).
- main.py                   -> log & simpan hasil akhir tiap scan ke Supabase (opsional,
  lihat core/supabase_client.py::save_scan_metrics).
"""

import time
from dataclasses import dataclass, field


@dataclass
class ScanMetrics:
    scan_duration_ms: float = 0.0
    request_count: int = 0
    failed_requests: int = 0
    retry_count: int = 0
    symbols_processed: int = 0
    signals_generated: int = 0
    _start_ts: float = field(default=0.0, repr=False)

    def reset(self) -> None:
        """Dipanggil di AWAL tiap scan_watchlist() - metrics ini per-siklus, bukan kumulatif
        selamanya (kumulatif lintas siklus sebaiknya dianalisis dari tabel scan_metrics di
        Supabase, bukan dari counter in-memory yang di-reset)."""
        self.scan_duration_ms = 0.0
        self.request_count = 0
        self.failed_requests = 0
        self.retry_count = 0
        self.symbols_processed = 0
        self.signals_generated = 0
        self._start_ts = time.perf_counter()

    def stop(self) -> None:
        self.scan_duration_ms = round((time.perf_counter() - self._start_ts) * 1000, 1)

    def record_request(self) -> None:
        self.request_count += 1

    def record_retry(self) -> None:
        self.retry_count += 1

    def record_failed_request(self) -> None:
        self.failed_requests += 1

    def record_symbol_processed(self) -> None:
        self.symbols_processed += 1

    def record_signal_generated(self) -> None:
        self.signals_generated += 1

    def as_dict(self) -> dict:
        return {
            "scan_duration_ms": self.scan_duration_ms,
            "request_count": self.request_count,
            "failed_requests": self.failed_requests,
            "retry_count": self.retry_count,
            "symbols_processed": self.symbols_processed,
            "signals_generated": self.signals_generated,
        }


# Singleton per-proses, sama seperti core/watchlist.py::watchlist_manager dan
# core/correlation_tracker.py::correlation_tracker - satu instance dipakai bersama
# oleh exchange_client (recorder) dan pipeline (reader/reset).
scan_metrics = ScanMetrics()
