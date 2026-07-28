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

    def update_signal_outcome(self, signal_id, outcome: str, pnl_pct: float, closed_at: str) -> None:
        """Dipanggil oleh proses tracking terpisah (mis. cron) setelah TP/SL tersentuh."""
        if not self.client:
            return
        try:
            self.client.table(settings.supabase_signals_table).update({
                "outcome": outcome,
                "pnl_pct": pnl_pct,
                "closed_at": closed_at,
            }).eq("id", signal_id).execute()
        except Exception as e:
            logger.error(f"Gagal update outcome signal {signal_id}: {e}")

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
