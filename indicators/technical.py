"""
indicators/technical.py
-------------------------
Implementasi indikator teknikal secara manual (pandas/numpy saja, tanpa TA-Lib)
supaya dependency tetap ringan dan mudah dipahami/di-debug per baris.
"""

import pandas as pd
import numpy as np


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def sma(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(window=period).mean()


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    result = 100 - (100 / (1 + rs))
    return result.fillna(50)


def macd(series: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
    ema_fast = ema(series, fast)
    ema_slow = ema(series, slow)
    macd_line = ema_fast - ema_slow
    signal_line = ema(macd_line, signal)
    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False).mean()


def atr_pct(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """ATR dinyatakan sebagai persentase dari harga close, memudahkan perbandingan antar coin."""
    return atr(df, period) / df["close"] * 100


# ---------- Price x Open Interest directional model (lihat layer9_scoring.py::_score_oi
# & core/exchange_client.py::fetch_oi_price_model) ----------
# Matrix umum yang dipakai desk futures untuk membaca OI: OI naik/turun sendirian TIDAK
# bermakna apa pun tanpa dibaca BERSAMA arah harga pada periode yang sama - "OI naik" bisa
# berarti posisi baru dibangun (bullish kalau price juga naik) ATAU cuma noise data. Empat
# kombinasi di bawah adalah SATU-SATUNYA klasifikasi yang dipakai, tidak ada tebakan lain.
OI_LONG_BUILDUP = "LONG_BUILDUP"        # price naik + OI naik   -> posisi LONG baru dibangun
OI_SHORT_BUILDUP = "SHORT_BUILDUP"      # price turun + OI naik  -> posisi SHORT baru dibangun
OI_SHORT_COVERING = "SHORT_COVERING"    # price naik + OI turun  -> short menutup posisi
OI_LONG_LIQUIDATION = "LONG_LIQUIDATION"  # price turun + OI turun -> long dipaksa keluar/liquidated
OI_NEUTRAL = "NEUTRAL"                  # price dan/atau OI belum melewati threshold masing2

ALL_OI_CLASSIFICATIONS = (OI_LONG_BUILDUP, OI_SHORT_BUILDUP, OI_SHORT_COVERING,
                           OI_LONG_LIQUIDATION, OI_NEUTRAL)


def classify_price_oi_direction(price_change_pct: float | None, oi_change_pct: float | None,
                                 price_threshold_pct: float, oi_threshold_pct: float) -> str:
    """
    Klasifikasikan pergerakan price x Open Interest antar-scan jadi salah satu dari
    OI_LONG_BUILDUP / OI_SHORT_BUILDUP / OI_SHORT_COVERING / OI_LONG_LIQUIDATION / OI_NEUTRAL.

    `price_change_pct` dan `oi_change_pct` HARUS diukur pada periode/interval yang SAMA
    (mis. antar-scan yang sama) - membandingkan periode berbeda akan menghasilkan klasifikasi
    yang tidak bermakna. Kalau salah satu None, atau tidak ada satu pun dari kedua metrik yang
    melewati threshold-nya masing-masing (pergerakan terlalu kecil untuk diklasifikasi dengan
    yakin), return OI_NEUTRAL - fungsi ini SENGAJA tidak menebak arah dari noise kecil.
    """
    if price_change_pct is None or oi_change_pct is None:
        return OI_NEUTRAL

    price_up = price_change_pct >= price_threshold_pct
    price_down = price_change_pct <= -price_threshold_pct
    oi_up = oi_change_pct >= oi_threshold_pct
    oi_down = oi_change_pct <= -oi_threshold_pct

    if price_up and oi_up:
        return OI_LONG_BUILDUP
    if price_down and oi_up:
        return OI_SHORT_BUILDUP
    if price_up and oi_down:
        return OI_SHORT_COVERING
    if price_down and oi_down:
        return OI_LONG_LIQUIDATION
    return OI_NEUTRAL


def percentile_of_last(series: pd.Series, lookback: int | None = None, min_history: int = 100):
    """
    Percentile rank (0-100) dari nilai TERAKHIR suatu series relatif terhadap histori series
    itu SENDIRI (bukan dibandingkan ke coin lain). Dipakai untuk threshold adaptif per-coin,
    misalnya "apakah ATR% sekarang termasuk rendah dibanding kondisi normal coin ini sendiri
    selama N candle terakhir" - alih-alih memakai angka absolut yang sama untuk semua coin.

    Return None kalau histori belum cukup (< min_history titik data valid) - dipakai supaya
    pemanggil bisa graceful-skip cek relatif untuk symbol yang baru listing / data terbatas.
    """
    s = series.dropna()
    if lookback:
        s = s.tail(lookback)
    if len(s) < min_history:
        return None
    last = s.iloc[-1]
    rank = (s <= last).sum() / len(s) * 100
    return float(rank)
