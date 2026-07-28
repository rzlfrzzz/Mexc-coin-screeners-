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
