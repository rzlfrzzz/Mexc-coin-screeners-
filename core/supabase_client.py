"""
core/supabase_client.py
------------------------
Wrapper penyimpanan history signal & layer log ke Supabase.
Lihat supabase_schema.sql untuk skema tabel yang dibutuhkan.
"""

from datetime import datetime, date
from enum import Enum

import numpy as np
import pandas as pd
from loguru import logger
from supabase import create_client, Client

from config import settings


def _json_safe(value):
    """
    Konversi rekursif nilai numpy/pandas/Enum menjadi tipe Python native,
    supaya bisa di-JSON-serialize saat insert ke Supabase.

    BUG FIX: sebelumnya lr.data (berisi numpy.float64/numpy.bool_/pandas.Timestamp
    hasil perhitungan indikator) dikirim langsung ke Supabase. Client Supabase gagal
    serialize itu ke JSON, tapi errornya cuma ditangkap 'except Exception: logger.error(...)'
    lalu diabaikan - jadi log/signal tidak pernah benar-benar tersimpan tanpa disadari.
    """
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(v) for v in value]
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (np.bool_,)):
        return bool(value)
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        val = float(value)
        return None if np.isnan(val) or np.isinf(val) else val
    if isinstance(value, np.ndarray):
        return _json_safe(value.tolist())
    if isinstance(value, (pd.Timestamp, datetime, date)):
        return value.isoformat()
    if isinstance(value, float) and (value != value or value in (float("inf"), float("-inf"))):
        return None  # NaN/inf murni Python, JSON tidak punya representasi untuk ini
    return value


class SupabaseStore:
    def __init__(self):
        self.client: Client | None = None
        if settings.supabase_url and settings.supabase_key:
            self.client = create_client(settings.supabase_url, settings.supabase_key)
        else:
            logger.warning("Supabase belum dikonfigurasi (SUPABASE_URL/SUPABASE_KEY kosong).")

    def save_signal(self, signal_row: dict) -> dict | None:
        if not self.client:
            logger.warning("Supabase tidak terkoneksi, signal tidak disimpan.")
            return None
        try:
            res = self.client.table(settings.supabase_signals_table).insert(_json_safe(signal_row)).execute()
            return res.data[0] if res.data else None
        except Exception as e:
            logger.error(f"Gagal menyimpan signal ke Supabase: {e}")
            return None

    def save_layer_log(self, symbol: str, layer_results: list) -> None:
        """Simpan log tiap layer (untuk debugging/refinement), independen dari tabel signals."""
        if not self.client:
            return
        rows = [
            {
                "symbol": symbol,
                "layer_number": lr.layer_number,
                "layer_name": lr.layer_name,
                "status": lr.status.value,
                "reason": lr.reason,
                "data": _json_safe(lr.data),
            }
            for lr in layer_results
        ]
        try:
            self.client.table(settings.supabase_layer_log_table).insert(rows).execute()
        except Exception as e:
            logger.error(f"Gagal menyimpan layer log ke Supabase: {e}")

    def update_signal_outcome(self, signal_id, result: dict) -> None:
        """
        Dipanggil oleh outcome_tracker.py / backtest.py (lewat trade_outcome.evaluate_trade_path)
        setelah outcome bisa ditentukan. `result` minimal berisi: outcome, pnl_pct, closed_at.
        Field opsional (mfe_pct, mae_pct, time_to_outcome_hours, ambiguous_same_bar) diikutkan
        kalau ada - kolomnya ditambahkan lewat migrasi di supabase_schema.sql.
        """
        if not self.client:
            return
        payload = {
            "outcome": result["outcome"],
            "pnl_pct": result["pnl_pct"],
            "closed_at": result["closed_at"],
        }
        for optional_key in ("mfe_pct", "mae_pct", "time_to_outcome_hours", "ambiguous_same_bar"):
            if optional_key in result and result[optional_key] is not None:
                payload[optional_key] = result[optional_key]
        try:
            self.client.table(settings.supabase_signals_table).update(_json_safe(payload)).eq("id", signal_id).execute()
        except Exception as e:
            logger.error(f"Gagal update outcome signal {signal_id}: {e}")

    def has_open_signal(self, symbol: str) -> bool:
        """
        Cek apakah symbol ini masih punya trade 'open' - yaitu signal yang sudah
        pernah dikirim (sent=True) tapi outcome-nya belum ditentukan (outcome IS NULL,
        sama seperti kriteria fetch_open_signals()/outcome_tracker.py).

        FIX DUPLIKASI SIGNAL: dipanggil oleh pipeline.process_and_dispatch() sebelum
        kirim ke Telegram. Sebelumnya bot mengevaluasi ulang & mengirim signal baru
        untuk symbol yang sama di SETIAP scan interval (default 5 menit) selama
        indikatornya masih lolos semua layer - karena trend/struktur biasanya tidak
        berubah secepat itu, symbol yang sama sering lolos lagi & terkirim lagi
        (terlihat seperti "double signal"). Dengan cek ini, symbol yang tradenya
        masih open (belum SL/TP/expired) tidak akan dikirim signal baru sampai
        outcome_tracker.py menutup trade sebelumnya.

        CATATAN: fitur ini butuh ENABLE_OUTCOME_TRACKING=true supaya trade lama
        benar-benar bisa "closed" (SL/TP/expired) - kalau outcome tracking mati,
        symbol yang sudah punya open signal akan terus terblokir dari signal baru
        tanpa batas waktu.
        """
        if not self.client:
            logger.warning(
                f"[{symbol}] Supabase tidak terkoneksi, tidak bisa cek duplikasi open signal "
                "- signal akan tetap dikirim (fail-open)."
            )
            return False
        try:
            res = (
                self.client.table(settings.supabase_signals_table)
                .select("id")
                .eq("symbol", symbol)
                .eq("sent", True)
                .is_("outcome", "null")
                .limit(1)
                .execute()
            )
            return bool(res.data)
        except Exception as e:
            logger.error(f"Gagal cek open signal untuk {symbol}: {e}")
            return False  # fail-open: kalau cek gagal, jangan blokir pengiriman signal

    def get_open_signal_setup_id(self, symbol: str) -> str | None:
        """
        Ambil `setup_id` dari open signal (kalau ada) untuk symbol ini - dipakai
        process_and_dispatch() untuk membedakan dua kasus saat symbol masih punya
        posisi open (lihat has_open_signal()):
        1. setup_id BARU == setup_id yang sedang open -> murni repeat scan, setup yang
           sama belum invalid/consumed (kasus NORMAL, memang harus di-skip diam-diam).
        2. setup_id BARU != setup_id yang sedang open -> setup yang BERBEDA muncul
           (BOS/zona baru) sementara posisi lama masih open - tetap di-skip (bot ini
           satu posisi per symbol), TAPI ini informasi berharga untuk log/analisis,
           bukan sekadar "signal duplikat biasa".
        Return None kalau tidak ada open signal, Supabase tidak terkoneksi, atau baris
        lama belum punya kolom setup_id (data sebelum migrasi Phase 5).
        """
        if not self.client:
            return None
        try:
            res = (
                self.client.table(settings.supabase_signals_table)
                .select("setup_id")
                .eq("symbol", symbol)
                .eq("sent", True)
                .is_("outcome", "null")
                .order("generated_at", desc=True)
                .limit(1)
                .execute()
            )
            return res.data[0].get("setup_id") if res.data else None
        except Exception as e:
            logger.error(f"Gagal ambil setup_id open signal untuk {symbol}: {e}")
            return None

    def save_scan_metrics(self, metrics_row: dict) -> None:
        """
        Simpan metrics satu siklus scan (Phase 8 - lihat core/metrics.py &
        ringkasan_perbaikan.md P2) ke tabel terpisah `scan_metrics` - BUKAN tabel signals,
        supaya histori metrics tetap ada walau siklus scan itu tidak menghasilkan sinyal
        sama sekali. Dipakai untuk menjawab "apakah bottleneck watchlist 150/200 symbol
        berasal dari API (request_count/retry_count/failed_requests naik) atau dari
        computation (scan_duration_ms naik tapi request tetap wajar)?" secara historis,
        bukan cuma sekali lihat waktu itu saja.
        """
        if not self.client:
            return
        try:
            self.client.table("scan_metrics").insert(_json_safe(metrics_row)).execute()
        except Exception as e:
            logger.error(f"Gagal menyimpan scan metrics ke Supabase: {e}")

    def fetch_closed_signals(self, limit: int = 2000) -> list:
        """
        Ambil signal yang SUDAH punya outcome (outcome IS NOT NULL, dan bukan
        SKIPPED_DUPLICATE) - dipakai untuk analisis post-hoc parameter yang TIDAK bisa
        divalidasi lewat backtest.py karena datanya tidak disimulasikan secara historis
        (funding_rate_pct, oi_change_pct - lihat backtest.py docstring & optimize.py /
        analyze_live_params.py Phase 8). Analisis ini baru bermakna setelah data live
        terkumpul cukup banyak (lihat ringkasan_perbaikan.md P3: "kita kumpulkan data dulu").
        """
        if not self.client:
            logger.warning("Supabase tidak terkoneksi, tidak bisa ambil closed signals.")
            return []
        try:
            res = (
                self.client.table(settings.supabase_signals_table)
                .select("*")
                .not_.is_("outcome", "null")
                .neq("outcome", "SKIPPED_DUPLICATE")
                .order("generated_at", desc=False)
                .limit(limit)
                .execute()
            )
            return res.data or []
        except Exception as e:
            logger.error(f"Gagal ambil closed signals dari Supabase: {e}")
            return []

    def fetch_open_signals(self, limit: int = 200) -> list:
        """
        Ambil signal yang sudah terkirim (sent=True) tapi belum punya outcome (outcome IS NULL)
        - dipakai oleh outcome_tracker.py untuk menentukan apakah SL/TP sudah tersentuh sejak
        signal digenerate, supaya win-rate riil bisa dihitung otomatis (bukan manual/kosong).
        """
        if not self.client:
            logger.warning("Supabase tidak terkoneksi, tidak bisa ambil open signals.")
            return []
        try:
            res = (
                self.client.table(settings.supabase_signals_table)
                .select("*")
                .eq("sent", True)
                .is_("outcome", "null")
                .order("generated_at", desc=False)
                .limit(limit)
                .execute()
            )
            return res.data or []
        except Exception as e:
            logger.error(f"Gagal ambil open signals dari Supabase: {e}")
            return []


supabase_store = SupabaseStore()
