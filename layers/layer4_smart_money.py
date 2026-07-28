"""
Layer 4 - Smart Money Area
----------------------------
Mencari 3 area penting pada 1H:
1. Order Block   : candle impulsif terakhir sebelum pergerakan besar berlawanan arah candle itu.
2. Fair Value Gap: gap 3-candle (high candle[i-1] vs low candle[i+1], atau sebaliknya).
3. Liquidity Sweep: swing high/low yang disapu (wick menembus) lalu candle close kembali
   ke dalam range -> indikasi stop hunt sebelum reversal.
"""

import pandas as pd
from models import LayerResult, LayerStatus, SmartMoneyZone, Direction
from layers.layer3_structure import find_swings

IMPULSE_MULTIPLIER = 1.5  # candle dianggap 'impulsif' jika body > rata2 body * multiplier


def _body(df_row):
    return abs(df_row["close"] - df_row["open"])


def find_order_blocks(df: pd.DataFrame, direction: Direction, lookback: int = 40):
    """
    Order block bullish: candle bearish terakhir sebelum rangkaian candle bullish impulsif.
    Order block bearish: candle bullish terakhir sebelum rangkaian candle bearish impulsif.
    """
    recent = df.iloc[-lookback:].reset_index(drop=True)
    avg_body = (recent["close"] - recent["open"]).abs().mean()
    zones = []

    for i in range(1, len(recent) - 1):
        body = abs(recent["close"].iloc[i] - recent["open"].iloc[i])
        is_impulsive = body > avg_body * IMPULSE_MULTIPLIER
        if not is_impulsive:
            continue
        bullish_impulse = recent["close"].iloc[i] > recent["open"].iloc[i]
        prev = recent.iloc[i - 1]
        prev_bearish = prev["close"] < prev["open"]
        prev_bullish = prev["close"] > prev["open"]

        if direction == Direction.LONG and bullish_impulse and prev_bearish:
            zones.append(SmartMoneyZone(
                zone_type="order_block", direction=Direction.LONG,
                top=float(prev["open"]), bottom=float(prev["close"]),
                index=i, meta={"impulse_body": float(body)}
            ))
        if direction == Direction.SHORT and not bullish_impulse and prev_bullish:
            zones.append(SmartMoneyZone(
                zone_type="order_block", direction=Direction.SHORT,
                top=float(prev["close"]), bottom=float(prev["open"]),
                index=i, meta={"impulse_body": float(body)}
            ))
    return zones


def find_fvgs(df: pd.DataFrame, direction: Direction, lookback: int = 40):
    """FVG 3-candle: gap antara high[i-1] dan low[i+1] (bullish) atau low[i-1] dan high[i+1] (bearish)."""
    recent = df.iloc[-lookback:].reset_index(drop=True)
    zones = []
    for i in range(1, len(recent) - 1):
        prev_high, prev_low = recent["high"].iloc[i - 1], recent["low"].iloc[i - 1]
        next_high, next_low = recent["high"].iloc[i + 1], recent["low"].iloc[i + 1]

        if direction == Direction.LONG and next_low > prev_high:
            zones.append(SmartMoneyZone(
                zone_type="fvg", direction=Direction.LONG,
                top=float(next_low), bottom=float(prev_high), index=i
            ))
        if direction == Direction.SHORT and next_high < prev_low:
            zones.append(SmartMoneyZone(
                zone_type="fvg", direction=Direction.SHORT,
                top=float(prev_low), bottom=float(next_high), index=i
            ))
    return zones


def find_liquidity_sweep(df: pd.DataFrame, direction: Direction, lookback: int = None):
    """
    Cek apakah candle terakhir (atau 2 terakhir) menyapu swing low/high sebelumnya
    dengan wick, lalu close balik ke dalam range -> sweep valid.
    `lookback` idealnya adalah swing_lookback adaptif yang sama yang dipakai Layer 3
    (raw_data["swing_lookback"]) supaya definisi swing konsisten di seluruh pipeline
    untuk satu symbol yang sama.
    """
    swings = find_swings(df) if lookback is None else find_swings(df, lookback=lookback)
    if not swings or len(df) < 5:
        return None

    last_candles = df.iloc[-3:]
    if direction == Direction.LONG:
        recent_lows = [s for s in swings if s["type"] == "low"]
        if not recent_lows:
            return None
        target = recent_lows[-1]
        swept = (last_candles["low"] < target["price"]).any()
        closed_back_above = last_candles["close"].iloc[-1] > target["price"]
        if swept and closed_back_above:
            return SmartMoneyZone(
                zone_type="liquidity_sweep", direction=Direction.LONG,
                top=float(target["price"]), bottom=float(last_candles["low"].min()),
                index=target["index"], meta={"swept_level": target["price"]}
            )
    else:
        recent_highs = [s for s in swings if s["type"] == "high"]
        if not recent_highs:
            return None
        target = recent_highs[-1]
        swept = (last_candles["high"] > target["price"]).any()
        closed_back_below = last_candles["close"].iloc[-1] < target["price"]
        if swept and closed_back_below:
            return SmartMoneyZone(
                zone_type="liquidity_sweep", direction=Direction.SHORT,
                top=float(last_candles["high"].max()), bottom=float(target["price"]),
                index=target["index"], meta={"swept_level": target["price"]}
            )
    return None


def price_in_zone(price: float, zone: SmartMoneyZone, tolerance_pct: float = 0.15) -> bool:
    span = zone.top - zone.bottom
    tol = span * tolerance_pct
    return (zone.bottom - tol) <= price <= (zone.top + tol)


def run(raw_data: dict, direction: Direction) -> LayerResult:
    df_mtf = raw_data["ohlcv_mtf"]
    current_price = float(df_mtf["close"].iloc[-1])

    swing_lookback = raw_data.get("swing_lookback")

    order_blocks = find_order_blocks(df_mtf, direction)
    fvgs = find_fvgs(df_mtf, direction)
    sweep = find_liquidity_sweep(df_mtf, direction, lookback=swing_lookback)

    ob_in_range = [ob for ob in order_blocks if price_in_zone(current_price, ob)]
    fvg_in_range = [f for f in fvgs if price_in_zone(current_price, f)]

    zones = order_blocks + fvgs + ([sweep] if sweep else [])

    data = {
        "order_blocks_found": len(order_blocks),
        "fvgs_found": len(fvgs),
        "order_block_in_range": bool(ob_in_range),
        "fvg_in_range": bool(fvg_in_range),
        "liquidity_sweep": bool(sweep),
        "current_price": current_price,
    }

    has_retrace_zone = bool(ob_in_range) or bool(fvg_in_range)

    if not has_retrace_zone and not sweep:
        return LayerResult(4, "Smart Money Area", LayerStatus.FAIL,
                            "Tidak ada Order Block/FVG di harga sekarang dan tidak ada liquidity sweep", data,
                            ), zones

    reason_parts = []
    if ob_in_range:
        reason_parts.append(f"harga berada di Order Block ({len(ob_in_range)} zona)")
    if fvg_in_range:
        reason_parts.append(f"harga berada di FVG ({len(fvg_in_range)} zona)")
    if sweep:
        reason_parts.append("liquidity sweep terdeteksi & sudah rejection")

    result = LayerResult(4, "Smart Money Area", LayerStatus.PASS,
                          "Smart money zone valid: " + ", ".join(reason_parts), data)
    return result, zones
