"""
pipeline.py
------------
Orkestrasi 9 layer filter secara berurutan untuk satu symbol.
Setiap layer independen (modul terpisah di /layers) dan bisa di-debug sendiri-sendiri.
Begitu satu layer gagal, proses berhenti (fail-fast) dan failure point dicatat.
"""

from loguru import logger

from config import settings
from models import TradeSignal, Direction, LayerStatus
from core.exchange_client import exchange_client
from core.watchlist import watchlist_manager
from core.supabase_client import supabase_store
from core.telegram_notifier import send_signal

from layers import (
    layer1_market_health,
    layer2_trend,
    layer3_structure,
    layer4_smart_money,
    layer5_momentum,
    layer6_volume,
    layer7_entry_trigger,
    layer8_risk_management,
    layer9_scoring,
)


def run_pipeline_for_symbol(symbol: str) -> TradeSignal | None:
    """
    Jalankan seluruh 9 layer untuk satu symbol.
    Return TradeSignal jika lolos scoring minimum, None jika gagal di layer manapun
    atau skor di bawah threshold. Semua layer result tetap disimpan untuk debugging,
    baik lolos maupun gagal.
    """
    layer_results = []
    layer_by_number = {}

    raw_data = exchange_client.safe_fetch_all(symbol)
    if not raw_data:
        logger.warning(f"[{symbol}] Skip: gagal ambil data dari exchange")
        return None

    signal = TradeSignal(symbol=symbol, direction=Direction.NONE)

    def _record(lr):
        layer_results.append(lr)
        layer_by_number[lr.layer_number] = lr
        signal.layer_results.append(lr)

    def _fail_stop(lr):
        _record(lr)
        signal.fail_layer = f"Layer {lr.layer_number} - {lr.layer_name}"
        logger.info(f"[{symbol}] STOP di Layer {lr.layer_number} ({lr.layer_name}): {lr.reason}")
        supabase_store.save_layer_log(symbol, layer_results)
        return None

    # ---------------- Layer 1 ----------------
    lr1 = layer1_market_health.run(raw_data)
    if lr1.status != LayerStatus.PASS:
        return _fail_stop(lr1)
    _record(lr1)

    # ---------------- Layer 2 ----------------
    lr2 = layer2_trend.run(raw_data)
    if lr2.status != LayerStatus.PASS:
        return _fail_stop(lr2)
    _record(lr2)
    direction = Direction(lr2.data["trend_direction"])
    signal.direction = direction

    # ---------------- Layer 3 ----------------
    lr3 = layer3_structure.run(raw_data)
    # konsistensi: structure harus searah trend besar
    structure_aligned = (
        (direction == Direction.LONG and (lr3.data.get("bos_bullish") or lr3.data.get("structure_bias") == "bullish"))
        or
        (direction == Direction.SHORT and (lr3.data.get("bos_bearish") or lr3.data.get("structure_bias") == "bearish"))
    )
    if lr3.status != LayerStatus.PASS or not structure_aligned:
        if lr3.status == LayerStatus.PASS and not structure_aligned:
            lr3.reason += " | Struktur 1H tidak searah dengan trend 4H"
        lr3.status = LayerStatus.FAIL
        return _fail_stop(lr3)
    _record(lr3)

    # ---------------- Layer 4 ----------------
    lr4, smc_zones = layer4_smart_money.run(raw_data, direction)
    if lr4.status != LayerStatus.PASS:
        return _fail_stop(lr4)
    _record(lr4)
    signal.smart_money_zones = smc_zones

    # ---------------- Layer 5 ----------------
    lr5 = layer5_momentum.run(raw_data, direction)
    if lr5.status != LayerStatus.PASS:
        return _fail_stop(lr5)
    _record(lr5)

    # ---------------- Layer 6 ----------------
    lr6 = layer6_volume.run(raw_data)
    if lr6.status != LayerStatus.PASS:
        return _fail_stop(lr6)
    _record(lr6)

    # ---------------- Layer 7 ----------------
    lr7 = layer7_entry_trigger.run(raw_data, direction, prior_layers_passed=True)
    if lr7.status != LayerStatus.PASS:
        return _fail_stop(lr7)
    _record(lr7)

    # ---------------- Layer 8 ----------------
    lr8, risk_plan = layer8_risk_management.run(raw_data, direction)
    if lr8.status != LayerStatus.PASS or risk_plan is None:
        return _fail_stop(lr8)
    _record(lr8)
    signal.risk_plan = risk_plan

    # ---------------- indicators snapshot (dipakai layer 9 + format telegram) ----------------
    atr_pct_1h = lr1.data.get("atr_pct_1h", 0.0)
    # anggap ATR "tinggi" jika di atas 1.5x threshold minimum (bisa disesuaikan)
    atr_high = atr_pct_1h > (settings.min_atr_pct * 1.5)

    last_high_swing = lr3.data.get("last_high_swing")
    last_low_swing = lr3.data.get("last_low_swing")
    entry_price = risk_plan.entry
    not_near_resistance = True
    if direction == Direction.LONG and last_high_swing:
        distance_pct = abs(last_high_swing["price"] - entry_price) / entry_price * 100
        not_near_resistance = distance_pct > 0.3
    elif direction == Direction.SHORT and last_low_swing:
        distance_pct = abs(entry_price - last_low_swing["price"]) / entry_price * 100
        not_near_resistance = distance_pct > 0.3

    trend_label = "Bullish" if direction == Direction.LONG else "Bearish"
    bos_label = "Bullish" if lr3.data.get("bos_bullish") else ("Bearish" if lr3.data.get("bos_bearish") else "-")

    snapshot = {
        "trend_htf_aligned": True,
        "trend_label": trend_label,
        "bos": bool(lr3.data.get("bos_bullish") or lr3.data.get("bos_bearish")),
        "bos_label": bos_label,
        "order_block_valid": bool(lr4.data.get("order_block_in_range")),
        "fvg_valid": bool(lr4.data.get("fvg_in_range")),
        "volume_spike": bool(lr6.data.get("volume_spike")),
        "volume_pct_of_avg": lr6.data.get("volume_pct_of_avg", 0.0),
        "rsi": lr5.data.get("rsi", 0.0),
        "rsi_ok": bool(lr5.data.get("rsi_ok")),
        "atr_high": atr_high,
        "not_near_resistance": not_near_resistance,
    }
    signal.indicators_snapshot = snapshot

    # ---------------- Layer 9 ----------------
    score = layer9_scoring.run(layer_by_number, snapshot)
    signal.score = score

    lr9_data = {"breakdown": score.breakdown, "total": score.total, "grade": score.grade}
    lr9 = layer_results[0].__class__(9, "Scoring System",
                                      LayerStatus.PASS if score.total >= settings.score_min_to_send else LayerStatus.FAIL,
                                      f"Score {score.total}/100 ({score.grade})", lr9_data)
    _record(lr9)

    supabase_store.save_layer_log(symbol, layer_results)

    if score.total < settings.score_min_to_send:
        signal.fail_layer = "Layer 9 - Scoring System"
        logger.info(f"[{symbol}] Score {score.total} < {settings.score_min_to_send}, tidak dikirim")
        supabase_store.save_signal(signal.to_supabase_row())
        return None

    return signal


def process_and_dispatch(symbol: str) -> TradeSignal | None:
    """Jalankan pipeline lalu kirim ke Telegram + simpan ke Supabase jika lolos."""
    try:
        signal = run_pipeline_for_symbol(symbol)
    except Exception as e:
        logger.exception(f"[{symbol}] Error tak terduga di pipeline: {e}")
        return None

    if signal is None:
        return None

    sent = send_signal(signal)
    signal.sent = sent
    saved = supabase_store.save_signal(signal.to_supabase_row())
    if saved and "id" in saved:
        logger.info(f"[{symbol}] Signal tersimpan dengan id={saved['id']}")

    return signal


def scan_watchlist() -> list:
    """Scan seluruh watchlist saat ini (static atau dynamic), kembalikan list signal yang berhasil dikirim."""
    exchange_client.load_markets()
    symbols = watchlist_manager.get_symbols()
    sent_signals = []
    for symbol in symbols:
        logger.info(f"Scanning {symbol} ...")
        signal = process_and_dispatch(symbol)
        if signal:
            sent_signals.append(signal)
    return sent_signals
