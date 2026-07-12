"""
Layer 5 - Konfirmasi Momentum
-------------------------------
Indikator dipakai sebagai konfirmasi tambahan, bukan penentu utama arah:
- RSI(14): LONG jika >55, SHORT jika <45
- MACD   : histogram mulai membesar searah direction
"""

from models import LayerResult, LayerStatus, Direction
from config import settings
from indicators.technical import rsi, macd


def run(raw_data: dict, direction: Direction) -> LayerResult:
    df_mtf = raw_data["ohlcv_mtf"]
    close = df_mtf["close"]

    rsi_series = rsi(close, 14)
    current_rsi = float(rsi_series.iloc[-1])

    macd_line, signal_line, hist = macd(close)
    hist_now = float(hist.iloc[-1])
    hist_prev = float(hist.iloc[-2]) if len(hist) >= 2 else 0.0
    hist_growing = abs(hist_now) > abs(hist_prev)

    if direction == Direction.LONG:
        rsi_ok = current_rsi > settings.rsi_long_min
        macd_ok = hist_now > 0 and hist_growing
    else:
        rsi_ok = current_rsi < settings.rsi_short_max
        macd_ok = hist_now < 0 and hist_growing

    data = {
        "rsi": current_rsi,
        "rsi_ok": rsi_ok,
        "macd_hist": hist_now,
        "macd_hist_prev": hist_prev,
        "macd_hist_growing": hist_growing,
        "macd_ok": macd_ok,
    }

    if not rsi_ok:
        return LayerResult(5, "Konfirmasi Momentum", LayerStatus.FAIL,
                            f"RSI {current_rsi:.1f} tidak mendukung {direction.value}", data)

    # MACD bersifat pendukung skor (bukan hard block) tapi tetap dicatat.
    reason = f"RSI {current_rsi:.1f} mendukung {direction.value}"
    reason += ", MACD histogram mendukung & membesar" if macd_ok else ", MACD belum sepenuhnya mendukung"

    return LayerResult(5, "Konfirmasi Momentum", LayerStatus.PASS, reason, data)
