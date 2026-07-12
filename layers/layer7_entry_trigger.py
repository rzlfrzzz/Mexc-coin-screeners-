"""
Layer 7 - Entry Trigger
-------------------------
Entry hanya dikirim ketika semua layer sebelumnya (1-6) lolos DAN ada
pattern konfirmasi candlestick pada candle terakhir 1H:
- Bullish/Bearish Engulfing, atau
- Close menembus level resistance/support terdekat (breakout confirmation)
"""

from models import LayerResult, LayerStatus, Direction


def _is_bullish_engulfing(df) -> bool:
    if len(df) < 2:
        return False
    prev, cur = df.iloc[-2], df.iloc[-1]
    prev_bearish = prev["close"] < prev["open"]
    cur_bullish = cur["close"] > cur["open"]
    engulf = cur["close"] > prev["open"] and cur["open"] < prev["close"]
    return prev_bearish and cur_bullish and engulf


def _is_bearish_engulfing(df) -> bool:
    if len(df) < 2:
        return False
    prev, cur = df.iloc[-2], df.iloc[-1]
    prev_bullish = prev["close"] > prev["open"]
    cur_bearish = cur["close"] < cur["open"]
    engulf = cur["close"] < prev["open"] and cur["open"] > prev["close"]
    return prev_bullish and cur_bearish and engulf


def _closed_beyond_recent_extreme(df, direction: Direction, window: int = 20) -> bool:
    recent = df.iloc[-(window + 1):-1]
    if recent.empty:
        return False
    last_close = df["close"].iloc[-1]
    if direction == Direction.LONG:
        return last_close > recent["high"].max()
    return last_close < recent["low"].min()


def run(raw_data: dict, direction: Direction, prior_layers_passed: bool) -> LayerResult:
    df_mtf = raw_data["ohlcv_mtf"]

    if not prior_layers_passed:
        return LayerResult(7, "Entry Trigger", LayerStatus.FAIL,
                            "Salah satu layer sebelumnya gagal, entry trigger tidak dievaluasi", {})

    if direction == Direction.LONG:
        pattern = _is_bullish_engulfing(df_mtf)
        pattern_name = "Bullish Engulfing"
    else:
        pattern = _is_bearish_engulfing(df_mtf)
        pattern_name = "Bearish Engulfing"

    breakout_confirm = _closed_beyond_recent_extreme(df_mtf, direction)

    data = {
        "pattern_detected": pattern,
        "pattern_name": pattern_name,
        "breakout_confirm": breakout_confirm,
    }

    if not pattern and not breakout_confirm:
        return LayerResult(7, "Entry Trigger", LayerStatus.FAIL,
                            "Tidak ada pattern konfirmasi (engulfing / breakout close)", data)

    reason = "Konfirmasi entry: "
    reason += f"{pattern_name} terdeteksi. " if pattern else ""
    reason += "Close breakout di luar range terakhir." if breakout_confirm else ""

    return LayerResult(7, "Entry Trigger", LayerStatus.PASS, reason.strip(), data)
