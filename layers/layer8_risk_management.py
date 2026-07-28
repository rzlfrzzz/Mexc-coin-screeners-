"""
Layer 8 - Risk Management
---------------------------
Hitung Entry, SL, TP1/TP2/TP3 otomatis berdasarkan swing low/high terdekat.
SL LONG  : di bawah swing low terdekat
SL SHORT : di atas swing high terdekat
TP1/2/3  : kelipatan risk (RR 1:1, 1:2, 1:3)
"""

from models import LayerResult, LayerStatus, Direction, RiskPlan
from layers.layer3_structure import find_swings

SL_BUFFER_PCT = 0.05  # buffer kecil di bawah/atas swing supaya tidak kena wick tipis


def run(raw_data: dict, direction: Direction) -> tuple[LayerResult, RiskPlan | None]:
    df_mtf = raw_data["ohlcv_mtf"]
    entry = float(df_mtf["close"].iloc[-1])
    swing_lookback = raw_data.get("swing_lookback")
    swings = find_swings(df_mtf) if swing_lookback is None else find_swings(df_mtf, lookback=swing_lookback)

    if direction == Direction.LONG:
        lows = [s for s in swings if s["type"] == "low" and s["price"] < entry]
        if not lows:
            return LayerResult(8, "Risk Management", LayerStatus.FAIL,
                                "Tidak ditemukan swing low valid untuk SL", {}), None
        swing_ref = lows[-1]["price"]
        sl = swing_ref * (1 - SL_BUFFER_PCT / 100)
        risk = entry - sl
        if risk <= 0:
            return LayerResult(8, "Risk Management", LayerStatus.FAIL,
                                "Risk <= 0, SL tidak valid (entry sudah di bawah swing low)", {}), None
        tp1, tp2, tp3 = entry + risk, entry + risk * 2, entry + risk * 3
    else:
        highs = [s for s in swings if s["type"] == "high" and s["price"] > entry]
        if not highs:
            return LayerResult(8, "Risk Management", LayerStatus.FAIL,
                                "Tidak ditemukan swing high valid untuk SL", {}), None
        swing_ref = highs[-1]["price"]
        sl = swing_ref * (1 + SL_BUFFER_PCT / 100)
        risk = sl - entry
        if risk <= 0:
            return LayerResult(8, "Risk Management", LayerStatus.FAIL,
                                "Risk <= 0, SL tidak valid (entry sudah di atas swing high)", {}), None
        tp1, tp2, tp3 = entry - risk, entry - risk * 2, entry - risk * 3

    risk_plan = RiskPlan(entry=entry, sl=sl, tp1=tp1, tp2=tp2, tp3=tp3, risk_amount=risk)

    data = {
        "entry": entry, "sl": sl, "tp1": tp1, "tp2": tp2, "tp3": tp3,
        "risk_amount": risk, "swing_ref": swing_ref,
    }

    return LayerResult(8, "Risk Management", LayerStatus.PASS,
                        f"SL @ {sl:.6g}, Risk {risk:.6g}, Target RR 1:3", data), risk_plan
