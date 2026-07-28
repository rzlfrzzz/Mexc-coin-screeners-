"""
Layer 2 - Trend Besar (4H)
---------------------------
Menentukan mode bot memakai EMA200 pada timeframe 4H:
- Bullish: harga di atas EMA200 AND EMA200 mulai naik  -> mode LONG only
- Bearish: harga di bawah EMA200 AND EMA200 mulai turun -> mode SHORT only
- Selain itu (sideways/tidak jelas) -> tidak ada mode, skip.

compute_trend_direction() diekstrak jadi fungsi terpisah supaya bisa dipakai ulang
oleh layer lain yang butuh logika trend EMA200 4H yang identik - contoh: Layer 0
(BTC Market Regime) memakai fungsi yang sama persis untuk menilai trend BTC.
"""

from models import LayerResult, LayerStatus, Direction
from indicators.technical import ema


def compute_trend_direction(df_htf) -> dict:
    """
    Hitung arah trend 4H berbasis EMA200 dari OHLCV manapun (dipakai untuk symbol
    yang sedang di-scan MAUPUN untuk BTC di Layer 0). Return dict data mentah +
    trend_direction (Direction.LONG / SHORT / NONE), TANPA membungkusnya jadi LayerResult
    supaya pemanggil bebas menentukan sendiri bagaimana hasil ini dipakai (hard gate,
    scoring, dsb).
    """
    if len(df_htf) < 210:
        return {
            "insufficient_data": True,
            "trend_direction": Direction.NONE.value,
        }

    ema200 = ema(df_htf["close"], 200)
    price = df_htf["close"].iloc[-1]
    ema_now = ema200.iloc[-1]
    ema_prev = ema200.iloc[-6]  # slope dicek dari 6 candle 4H lalu (~1 hari)

    ema_rising = ema_now > ema_prev
    ema_falling = ema_now < ema_prev

    data = {
        "insufficient_data": False,
        "price": float(price),
        "ema200": float(ema_now),
        "ema200_prev": float(ema_prev),
        "ema_rising": bool(ema_rising),
        "ema_falling": bool(ema_falling),
    }

    if price > ema_now and ema_rising:
        data["trend_direction"] = Direction.LONG.value
    elif price < ema_now and ema_falling:
        data["trend_direction"] = Direction.SHORT.value
    else:
        data["trend_direction"] = Direction.NONE.value

    return data


def run(raw_data: dict) -> LayerResult:
    df_htf = raw_data["ohlcv_htf"]  # 4H
    data = compute_trend_direction(df_htf)

    if data.get("insufficient_data"):
        return LayerResult(2, "Trend Besar 4H", LayerStatus.FAIL,
                            "Data 4H tidak cukup untuk hitung EMA200 (butuh >=210 candle)", {})

    direction = data["trend_direction"]

    if direction == Direction.LONG.value:
        return LayerResult(2, "Trend Besar 4H", LayerStatus.PASS, "Trend 4H Bullish -> mode LONG only", data)

    if direction == Direction.SHORT.value:
        return LayerResult(2, "Trend Besar 4H", LayerStatus.PASS, "Trend 4H Bearish -> mode SHORT only", data)

    return LayerResult(2, "Trend Besar 4H", LayerStatus.FAIL,
                        "Trend 4H tidak jelas (sideways/transisi) - skip", data)
