"""
core/timeframes.py
-------------------
Helper waktu TERPUSAT untuk urusan "candle open time vs close time".

OHLCV dari exchange (ccxt) diberi timestamp = waktu OPEN candle, BUKAN waktu close-nya.
Candle 1H berlabel 11:00 baru selesai (closed) pukul 12:00, dan baru saat itu datanya
boleh dianggap "sudah diketahui". Semua perbandingan urutan waktu antar timeframe
(backtest slicing, Layer 7 temporal ordering) HARUS memakai close time, kalau tidak
candle yang belum closed ikut terhitung (look-ahead) atau candle yang valid malah
terbuang (desinkronisasi backtest vs live).
"""

import re

import pandas as pd

_TF_PATTERN = re.compile(r"^\s*(\d+)\s*([mhdw])\s*$", re.IGNORECASE)
_UNIT_SECONDS = {"m": 60, "h": 3600, "d": 86400, "w": 604800}


def timeframe_to_timedelta(timeframe: str) -> pd.Timedelta:
    """'15m' -> 15 menit, '1h' -> 1 jam, '4h' -> 4 jam, '1d' -> 1 hari. Raise ValueError kalau format tidak dikenal."""
    match = _TF_PATTERN.match(str(timeframe))
    if not match:
        raise ValueError(f"Timeframe '{timeframe}' tidak dikenal (harus seperti 5m/15m/1h/4h/1d)")
    amount, unit = int(match.group(1)), match.group(2).lower()
    if amount <= 0:
        raise ValueError(f"Timeframe '{timeframe}' harus > 0")
    return pd.Timedelta(seconds=amount * _UNIT_SECONDS[unit])


def closed_candles_until(df: pd.DataFrame, tf_delta: pd.Timedelta, cutoff) -> pd.DataFrame:
    """
    Kembalikan hanya candle yang SUDAH CLOSED pada waktu `cutoff`:
        candle_open + tf_delta <= cutoff
    (`cutoff` = waktu close candle timeframe lain yang lebih besar, mis. close candle 1H).
    Ekuivalen dengan `df[df.index + tf_delta <= cutoff]`, tapi memakai searchsorted kalau
    index terurut (jauh lebih cepat untuk backtest bar-by-bar).
    """
    if df.empty:
        return df
    cutoff = pd.Timestamp(cutoff)
    if df.index.is_monotonic_increasing:
        end = int(df.index.searchsorted(cutoff - tf_delta, side="right"))
        return df.iloc[:end]
    return df[df.index + tf_delta <= cutoff]
