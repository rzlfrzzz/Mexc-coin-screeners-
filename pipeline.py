"""
pipeline.py
------------
Orkestrasi layer filter secara berurutan untuk satu symbol.

Perubahan desain filter logic (lihat catatan masing-masing bagian di bawah):
1. Layer 4 (Smart Money Area), 5 (Momentum), 6 (Volume) TIDAK LAGI hard-stop pipeline
   kalau FAIL. Hasilnya tetap direkam & tetap masuk ke breakdown skor Layer 9 (soft/scoring),
   supaya sinyal dengan kombinasi kekuatan lain yang bagus tidak otomatis gugur hanya karena
   satu dari tiga layer "pendukung" ini gagal. Layer 1 (Market Health), 2 (Trend), Layer 0
   (BTC Regime - baru), 3 (Structure alignment), 7 (Entry Trigger), 8 (Risk Management) tetap
   hard gate karena masing-masing adalah prasyarat struktural (tanpa itu sinyal tidak valid
   atau tidak punya arah/SL sama sekali).
2. Layer 0 (baru) - BTC Market Regime: dievaluasi tepat setelah Layer 2 (begitu direction
   symbol ditentukan), supaya bisa dibandingkan dengan regime BTC.
3. Adaptive fractal lookback (dihitung di Layer 3, disimpan ke raw_data["swing_lookback"])
   otomatis dipakai ulang oleh Layer 4 & Layer 8 lewat raw_data - tidak perlu perubahan lain
   di sini, hanya urutan panggilan Layer 3 sebelum Layer 4/8 tetap dipertahankan.
4. Funding rate check ada di dalam Layer 1 (lihat layers/layer1_market_health.py).
"""

from loguru import logger

from config import settings
from models import TradeSignal, Direction, LayerStatus
from core.exchange_client import exchange_client
from core.watchlist import watchlist_manager
from core.supabase_client import supabase_store
from core.telegram_notifier import send_signal
from core.correlation_tracker import correlation_tracker
from core.metrics import scan_metrics
from setup_identity import build_setup_id

from layers import (
    layer0_btc_regime,
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
    Jalankan seluruh layer untuk satu symbol.
    Return TradeSignal jika lolos scoring minimum, None jika gagal di salah satu hard-gate
    layer atau skor di bawah threshold. Semua layer result tetap disimpan untuk debugging,
    baik lolos, gagal-hard-stop, maupun gagal-soft (layer 4-6).
    """
    layer_results = []
    layer_by_number = {}
    soft_fail_layers = []

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
        signal.soft_fail_layers = soft_fail_layers
        logger.info(f"[{symbol}] STOP di Layer {lr.layer_number} ({lr.layer_name}): {lr.reason}")
        supabase_store.save_layer_log(symbol, layer_results)
        return None

    def _record_soft(lr):
        """Untuk Layer 4/5/6: rekam hasil apa pun statusnya, TIDAK menghentikan pipeline."""
        _record(lr)
        if lr.status != LayerStatus.PASS:
            soft_fail_layers.append(f"Layer {lr.layer_number} - {lr.layer_name}")
            logger.info(f"[{symbol}] Layer {lr.layer_number} ({lr.layer_name}) FAIL (soft, lanjut): {lr.reason}")

    # ---------------- Layer 1 (hard gate) ----------------
    lr1 = layer1_market_health.run(raw_data)
    if lr1.status != LayerStatus.PASS:
        return _fail_stop(lr1)
    _record(lr1)

    # ---------------- Layer 2 (hard gate) ----------------
    lr2 = layer2_trend.run(raw_data)
    if lr2.status != LayerStatus.PASS:
        return _fail_stop(lr2)
    _record(lr2)
    direction = Direction(lr2.data["trend_direction"])
    signal.direction = direction

    # ---------------- Layer 0 (hard gate, toggleable) - BTC Market Regime ----------------
    # Dijalankan di sini (bukan sebelum Layer 1) karena butuh `direction` dari Layer 2 untuk
    # dibandingkan dengan regime BTC.
    lr0 = layer0_btc_regime.run(raw_data, direction, exchange_client)
    if lr0.status == LayerStatus.FAIL:
        return _fail_stop(lr0)
    _record(lr0)

    # ---------------- Layer 3 (hard gate) ----------------
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

    # ---------------- Layer 4 (SOFT - scoring, tidak hard-stop) ----------------
    lr4, smc_zones = layer4_smart_money.run(raw_data, direction)
    _record_soft(lr4)
    signal.smart_money_zones = smc_zones

    # ---------------- Setup ID (lihat setup_identity.py & ringkasan_perbaikan.md P2) ----------------
    # Identitas unik setup ini = symbol + direction + structure event (BOS/CHoCH dari Layer 3)
    # + zona aktif (OB/FVG dari Layer 4). Dihitung di sini (setelah Layer 3 & 4 tersedia)
    # supaya bisa dipakai untuk anti-duplikasi yang lebih presisi di process_and_dispatch()
    # - bukan cuma "symbol ini ada open trade" tapi "setup PERSIS INI sudah pernah dikirim".
    signal.setup_id = build_setup_id(symbol, direction, lr3.data, lr4.data)

    # ---------------- Layer 5 (SOFT - scoring, tidak hard-stop) ----------------
    lr5 = layer5_momentum.run(raw_data, direction)
    _record_soft(lr5)

    # ---------------- Layer 6 (SOFT - scoring, tidak hard-stop) ----------------
    lr6 = layer6_volume.run(raw_data)
    _record_soft(lr6)

    # ---------------- Layer 7 (hard gate) ----------------
    # prior_layers_passed di sini merujuk ke prasyarat HARD (1, 2, 0, 3) yang memang sudah
    # pasti PASS di titik ini kalau kode sampai sejauh ini - Layer 4/5/6 boleh FAIL (soft)
    # tanpa memblokir evaluasi entry trigger.
    lr7 = layer7_entry_trigger.run(raw_data, direction, prior_layers_passed=True)
    if lr7.status != LayerStatus.PASS:
        return _fail_stop(lr7)
    _record(lr7)

    # ---------------- Layer 8 (hard gate) ----------------
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

    # BTC regime aligned = benar-benar dikonfirmasi searah (bukan sekadar tidak diblokir).
    # Kalau regime BTC netral/sideways atau filter dimatikan, ini dianggap False (tidak dapat
    # bonus skor) walau tidak memblokir sinyal - konsisten dengan filosofi Layer 0 sebagai
    # pengurang risiko, bukan syarat kelulusan tambahan.
    btc_regime_aligned = bool(
        lr0.status == LayerStatus.PASS
        and lr0.data.get("btc_direction") == direction.value
        and lr0.data.get("btc_direction") != Direction.NONE.value
    )

    # OI: price x OI directional model (lihat indicators.technical.classify_price_oi_direction
    # & core/exchange_client.py::fetch_oi_price_model) - BUKAN lagi "OI naik = bullish" naif.
    # oi_confirmation sekarang berarti "OI bergerak searah trade DAN merupakan buildup posisi
    # baru (LONG_BUILDUP/SHORT_BUILDUP) ATAU minimal unwind yang tidak berlawanan
    # (SHORT_COVERING utk LONG, LONG_LIQUIDATION utk SHORT)" - lihat layer9_scoring.py::_score_oi
    # untuk gradasi penuh/partial-nya di skor.
    oi_model = raw_data.get("oi_price_model")
    oi_direction = oi_model.get("classification") if oi_model else None
    oi_change_pct = oi_model.get("oi_change_pct") if oi_model else None
    oi_price_change_pct = oi_model.get("price_change_pct") if oi_model else None
    oi_confirmation = bool(
        oi_direction is not None and (
            (direction == Direction.LONG and oi_direction in ("LONG_BUILDUP", "SHORT_COVERING"))
            or (direction == Direction.SHORT and oi_direction in ("SHORT_BUILDUP", "LONG_LIQUIDATION"))
        )
    )

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
        "btc_regime_aligned": btc_regime_aligned,
        "btc_direction": lr0.data.get("btc_direction"),
        "oi_confirmation": oi_confirmation,
        "oi_direction": oi_direction,
        "oi_change_pct": oi_change_pct,
        "oi_price_change_pct": oi_price_change_pct,
        "funding_rate_pct": lr1.data.get("funding_rate_pct"),
        "swing_lookback": lr3.data.get("swing_lookback"),
    }
    signal.indicators_snapshot = snapshot
    signal.soft_fail_layers = soft_fail_layers

    # ---------------- Layer 9 (scoring final) ----------------
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
        logger.info(f"[{symbol}] Score {score.total} < {settings.score_min_to_send}, tidak dikirim "
                    f"(soft-fail layers: {soft_fail_layers or 'tidak ada'})")
        supabase_store.save_signal(signal.to_supabase_row())
        return None

    # ---------------- Correlation awareness (Phase 7, lihat core/correlation_tracker.py) ----
    # Didaftarkan di sini (bukan di process_and_dispatch) supaya SETIAP sinyal yang lolos
    # score_min_to_send ikut terhitung sebagai "qualifying signal searah" di scan ini - baik
    # yang nanti benar2 terkirim maupun yang di-skip karena duplikasi open-trade (skip
    # duplikasi tetap merepresentasikan "kondisi teknikal ini muncul lagi", bukan noise acak).
    signal.correlation_meta = correlation_tracker.register_and_check(symbol, direction.value)
    if signal.correlation_meta.get("high_correlation_risk"):
        others = ", ".join(signal.correlation_meta["same_direction_symbols_this_scan"])
        logger.info(f"[{symbol}] Correlation awareness: {signal.correlation_meta['same_direction_signals_this_scan']} "
                    f"sinyal {direction.value} lain sudah lolos di scan ini ({others}) - kemungkinan "
                    f"BTC-beta/market-wide move, bukan edge independen per-coin.")

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

    # ---------------- Anti-duplikasi: skip kalau symbol ini masih ada trade open ----------------
    # Trade "open" = signal sebelumnya sudah terkirim (sent=True) tapi outcome-nya belum
    # ditentukan oleh outcome_tracker.py (belum kena SL/TP/expired). Tanpa cek ini, symbol
    # yang sama bisa lolos pipeline lagi & lagi di setiap scan interval selagi indikatornya
    # belum berubah, sehingga terkirim berkali-kali (duplikasi) untuk trade yang sebenarnya
    # masih berjalan.
    if supabase_store.has_open_signal(symbol):
        existing_setup_id = supabase_store.get_open_signal_setup_id(symbol)
        if existing_setup_id and existing_setup_id == signal.setup_id:
            logger.info(f"[{symbol}] Skip kirim: setup PERSIS SAMA ({signal.setup_id}) masih open, belum invalid/consumed.")
        else:
            logger.info(
                f"[{symbol}] Skip kirim: setup BARU terdeteksi ({signal.setup_id}) tapi masih ada "
                f"posisi open lain ({existing_setup_id or 'setup_id lama tidak tercatat'}) - bot ini "
                "satu posisi per symbol, setup baru ini diabaikan sampai posisi lama closed."
            )
        signal.sent = False
        skip_row = signal.to_supabase_row()
        skip_row["outcome"] = "SKIPPED_DUPLICATE"
        skip_row["closed_at"] = signal.generated_at
        supabase_store.save_signal(skip_row)
        return None

    sent = send_signal(signal)
    signal.sent = sent
    saved = supabase_store.save_signal(signal.to_supabase_row())
    if saved and "id" in saved:
        logger.info(f"[{symbol}] Signal tersimpan dengan id={saved['id']}")

    return signal


def scan_watchlist() -> list:
    """Scan seluruh watchlist saat ini (static atau dynamic), kembalikan list signal yang berhasil dikirim.

    Metrics siklus scan ini (scan_duration_ms, request_count, failed_requests, retry_count,
    symbols_processed, signals_generated - lihat ringkasan_perbaikan.md P2 & core/metrics.py)
    tersedia lewat core.metrics.scan_metrics SEGERA setelah fungsi ini return - dibaca oleh
    main.py (log + simpan ke Supabase) dan watchlist_stress_test.py (Phase 8, uji ukuran
    watchlist 100/150/200)."""
    exchange_client.load_markets()
    # Reset correlation tracker & scan metrics di awal SETIAP siklus scan (bukan persisten
    # antar siklus) - lihat core/correlation_tracker.py: metadata korelasi ini hanya relevan
    # untuk "berapa sinyal searah lain muncul BERSAMAAN di scan yang sama", bukan histori
    # lintas waktu. Sama halnya untuk scan_metrics: per-siklus, bukan kumulatif selamanya.
    correlation_tracker.reset()
    scan_metrics.reset()
    symbols = watchlist_manager.get_symbols()
    sent_signals = []
    for symbol in symbols:
        logger.info(f"Scanning {symbol} ...")
        scan_metrics.record_symbol_processed()
        signal = process_and_dispatch(symbol)
        if signal:
            sent_signals.append(signal)
            scan_metrics.record_signal_generated()
    scan_metrics.stop()
    logger.info(f"Scan metrics: {scan_metrics.as_dict()}")
    return sent_signals
