"""
Layer 1 - Market Health
------------------------
Verifikasi apakah coin layak ditradingkan sama sekali, sebelum layer lain dievaluasi:
- Volume 24 jam > threshold
- Spread bid-ask kecil
- ATR 1H cukup besar (volatilitas memadai)
- Tidak sedang pump/dump ekstrem dalam 1 jam terakhir
"""

from config import settings
from models import LayerResult, LayerStatus
from indicators.technical import atr_pct


def run(raw_data: dict) -> LayerResult:
    symbol = raw_data["symbol"]
    ticker = raw_data["ticker"]
    spread_pct = raw_data["spread_pct"]
    df_mtf = raw_data["ohlcv_mtf"]  # 1H

    volume_24h_usd = ticker.get("quoteVolume") or (ticker.get("baseVolume", 0) * (ticker.get("last") or 0))
    atr_series = atr_pct(df_mtf, period=14)
    current_atr_pct = float(atr_series.iloc[-1]) if len(atr_series) else 0.0

    # pump/dump check: perubahan harga dalam 1 jam terakhir (1 candle 1H)
    last_close = df_mtf["close"].iloc[-1]
    prev_close = df_mtf["close"].iloc[-2] if len(df_mtf) >= 2 else last_close
    change_1h_pct = abs((last_close - prev_close) / prev_close * 100) if prev_close else 0.0

    data = {
        "volume_24h_usd": volume_24h_usd,
        "spread_pct": spread_pct,
        "atr_pct_1h": current_atr_pct,
        "change_1h_pct": change_1h_pct,
    }

    if volume_24h_usd < settings.min_volume_24h_usd:
        return LayerResult(1, "Market Health", LayerStatus.FAIL,
                            f"Volume 24h ${volume_24h_usd:,.0f} < ${settings.min_volume_24h_usd:,.0f}", data)

    if spread_pct > settings.max_spread_pct:
        return LayerResult(1, "Market Health", LayerStatus.FAIL,
                            f"Spread {spread_pct:.3f}% > {settings.max_spread_pct}%", data)

    if current_atr_pct < settings.min_atr_pct:
        return LayerResult(1, "Market Health", LayerStatus.FAIL,
                            f"ATR 1H {current_atr_pct:.3f}% < {settings.min_atr_pct}% (volatilitas terlalu rendah)", data)

    if change_1h_pct > settings.max_1h_pump_dump_pct:
        return LayerResult(1, "Market Health", LayerStatus.FAIL,
                            f"Perubahan 1H {change_1h_pct:.2f}% > {settings.max_1h_pump_dump_pct}% (pump/dump ekstrem)", data)

    return LayerResult(1, "Market Health", LayerStatus.PASS, "Coin layak ditradingkan", data)
