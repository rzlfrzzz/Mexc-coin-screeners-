"""
Layer 9 - Scoring System
--------------------------
Menghitung skor total (maks 100) dari seluruh hasil layer, lalu klasifikasi:
90-100 -> A+ (kirim), 80-89 -> A (kirim), 70-79 -> B (kirim + note), <70 -> tidak kirim

Bobot skor bisa di-adjust user lewat settings.scoring_weights (lihat config.py / .env).
"""

from models import LayerResult, SignalScore
from config import settings


def _stars_for(total: int) -> int:
    if total >= 90:
        return 5
    if total >= 80:
        return 4
    if total >= 70:
        return 3
    return 0


def _grade_for(total: int) -> str:
    if total >= 90:
        return "A+"
    if total >= 80:
        return "A"
    if total >= 70:
        return "B"
    return "REJECTED"


def run(layer_results_by_number: dict, indicators_snapshot: dict) -> SignalScore:
    """
    layer_results_by_number: dict {layer_number: LayerResult}
    indicators_snapshot: dict berisi flag2 tambahan (atr_high, not_near_resistance, dsb)
    yang sudah dikumpulkan pipeline dari data layer 1/4/5.
    """
    w = settings.scoring_weights
    breakdown = {}

    l2 = layer_results_by_number.get(2)
    breakdown["trend_aligned"] = w["trend_aligned"] if l2 and l2.status.value == "PASS" else 0

    l3 = layer_results_by_number.get(3)
    bos_detected = bool(l3 and (l3.data.get("bos_bullish") or l3.data.get("bos_bearish")))
    breakdown["bos"] = w["bos"] if bos_detected else 0

    l4 = layer_results_by_number.get(4)
    ob_valid = bool(l4 and l4.data.get("order_block_in_range"))
    fvg_valid = bool(l4 and l4.data.get("fvg_in_range"))
    breakdown["order_block"] = w["order_block"] if ob_valid else 0
    breakdown["fvg"] = w["fvg"] if fvg_valid else 0

    l6 = layer_results_by_number.get(6)
    vol_spike = bool(l6 and l6.data.get("volume_spike"))
    breakdown["volume_spike"] = w["volume_spike"] if vol_spike else 0

    l5 = layer_results_by_number.get(5)
    rsi_ok = bool(l5 and l5.data.get("rsi_ok"))
    macd_ok = bool(l5 and l5.data.get("macd_ok"))
    breakdown["rsi"] = w["rsi"] if rsi_ok else 0
    breakdown["macd"] = w["macd"] if macd_ok else 0

    breakdown["atr_high"] = w["atr_high"] if indicators_snapshot.get("atr_high") else 0
    breakdown["not_near_resistance"] = w["not_near_resistance"] if indicators_snapshot.get("not_near_resistance") else 0

    # Layer 0 (BTC regime) - hard gate di pipeline sudah memblokir kasus berlawanan arah,
    # jadi field ini biasanya True di titik ini kecuali BTC regime sedang netral/sideways
    # (di mana filter tidak memblokir tapi juga tidak dianggap "confirmed align").
    breakdown["btc_regime_aligned"] = w["btc_regime_aligned"] if indicators_snapshot.get("btc_regime_aligned") else 0

    # Open Interest confirmation - soft/scoring only (data OI via ccxt/MEXC tidak selalu
    # tersedia), jadi tidak pernah memblokir sinyal, hanya menambah/tidak menambah skor.
    breakdown["oi_confirmation"] = w["oi_confirmation"] if indicators_snapshot.get("oi_confirmation") else 0

    total = sum(breakdown.values())
    total = min(total, 100)

    return SignalScore(total=total, breakdown=breakdown, stars=_stars_for(total), grade=_grade_for(total))
