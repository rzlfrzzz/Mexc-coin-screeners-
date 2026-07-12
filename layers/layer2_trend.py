"""
Layer 2 - Trend Besar (4H)
---------------------------
Menentukan mode bot memakai EMA200 pada timeframe 4H:
- Bullish: harga di atas EMA200 AND EMA200 mulai naik  -> mode LONG only
- Bearish: harga di bawah EMA200 AND EMA200 mulai turun -> mode SHORT only
- Selain itu (sideways/tidak jelas) -> tidak ada mode, skip.
"""

from models import LayerResult, LayerStatus, Direction
from indicators.technical import ema


def run(raw_data: dict) -> LayerResult:
    df_htf = raw_data["ohlcv_htf"]  # 4H

    if len(df_htf) < 210:
        return LayerResult(2, "Trend Besar 4H", LayerStatus.FAIL,
                            "Data 4H tidak cukup untuk hitung EMA200 (butuh >=210 candle)", {})

    ema200 = ema(df_htf["close"], 200)
    price = df_htf["close"].iloc[-1]
    ema_now = ema200.iloc[-1]
    ema_prev = ema200.iloc[-6]  # slope dicek dari 6 candle 4H lalu (~1 hari)

    ema_rising = ema_now > ema_prev
    ema_falling = ema_now < ema_prev

    data = {
        "price": float(price),
        "ema200": float(ema_now),
        "ema200_prev": float(ema_prev),
        "ema_rising": bool(ema_rising),
        "ema_falling": bool(ema_falling),
    }

    if price > ema_now and ema_rising:
        data["trend_direction"] = Direction.LONG.value
        return LayerResult(2, "Trend Besar 4H", LayerStatus.PASS, "Trend 4H Bullish -> mode LONG only", data)

    if price < ema_now and ema_falling:
        data["trend_direction"] = Direction.SHORT.value
        return LayerResult(2, "Trend Besar 4H", LayerStatus.PASS, "Trend 4H Bearish -> mode SHORT only", data)

    data["trend_direction"] = Direction.NONE.value
    return LayerResult(2, "Trend Besar 4H", LayerStatus.FAIL,
                        "Trend 4H tidak jelas (sideways/transisi) - skip", data)
