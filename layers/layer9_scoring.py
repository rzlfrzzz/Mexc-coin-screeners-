"""
Layer 9 - Scoring System (V2 - Phase 7 Rebalance)
----------------------------------------------------
Menghitung skor total (maks 100) dari seluruh hasil layer, lalu klasifikasi:
90-100 -> A+ (kirim), 80-89 -> A (kirim), 70-79 -> B (kirim + note), <70 -> tidak kirim

Bobot per kategori (lihat config.py::scoring_weights) mengikuti tabel confluence score di
ringkasan_perbaikan.md P1.11:
    4H regime            10
    1H structure         15
    Liquidity event      15
    SMC location         15
    15M trigger          15
    Volume               10
    Momentum              5
    OI                    5
    Risk quality         10
    ------------------------
    TOTAL                100

PRINSIP PENTING (kenapa ini bukan cuma "0 atau penuh" seperti skema lama):
Layer 2 (trend), Layer 0 (BTC regime, kalau aktif), Layer 3 (structure), Layer 7 (entry
trigger), dan Layer 8 (risk management) adalah HARD GATE di pipeline.py - begitu eksekusi
sampai ke Layer 9, status layer-layer itu SUDAH PASTI PASS. Kalau skor dihitung dari
"apakah status-nya PASS", hasilnya SELALU nilai penuh yang sama untuk semua sinyal yang
lolos - tidak ada daya pembeda sama sekali (ini bug nyata di skema lama untuk komponen
trend_aligned & bos). Skema V2 ini sengaja menggali data MENTAH di balik status PASS itu
(seberapa KUAT konfirmasinya - event BOS langsung vs cuma CHoCH, displacement seberapa
besar dari minimum, RR seberapa jauh di atas minimum, dst) supaya skor tetap punya variasi
berguna sebagai "confluence score", bukan sekadar re-konfirmasi bahwa hard gate lulus.
"""

from models import LayerStatus, SignalScore
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


def _scaled(value: float, floor: float, full: float, cap: float = 1.0) -> float:
    """Interpolasi linear 0..1: <= floor -> 0, >= full -> cap, di antaranya linear. Dipakai
    untuk menggradasi metrik kontinu (displacement strength, RR, dst) jadi fraksi skor,
    bukan biner."""
    if full <= floor:
        return cap if value >= full else 0.0
    frac = (value - floor) / (full - floor)
    return max(0.0, min(cap, frac))


def _score_regime_4h(w: float, layer_by_number: dict, snapshot: dict) -> float:
    """
    4H regime (BTC macro alignment, Layer 0).
    - PASS + searah confirmed (btc_direction == direction)      -> penuh
    - PASS + regime netral/sideways (tidak memblokir, tidak searah confirmed) -> setengah
    - SKIPPED (filter dimatikan) atau symbol yang di-scan = BTC itu sendiri (data kosong)
      -> penuh (tidak ada informasi berlawanan; untuk BTC sendiri, "regime 4H" = trend
      4H-nya sendiri yang sudah pasti searah karena Layer 2 hard gate).
    - Layer 0 tidak tersedia sama sekali sebagai LayerResult (mis. backtest) -> fallback ke
      snapshot["btc_regime_aligned"] (biner, tanpa tingkat netral).
    """
    l0 = layer_by_number.get(0)
    if l0 is None:
        return w if snapshot.get("btc_regime_aligned") else 0.0

    if l0.status == LayerStatus.SKIPPED or not l0.data:
        return w
    if l0.status == LayerStatus.PASS and l0.data.get("btc_direction") == l0.data.get("symbol_direction"):
        return w
    # PASS tapi netral (btc_direction NONE, tidak memblokir tapi juga belum confirmed)
    return w * 0.5


def _score_structure_1h(w: float, layer_by_number: dict) -> float:
    """
    1H structure (Layer 3). BOS event langsung searah = konfirmasi paling kuat (continuation
    yang benar-benar event, bukan status statis - lihat layer3_structure.py). CHoCH/structure_bias
    saja (tanpa BOS event terbaru) = struktur baru berbalik arah, valid tapi lebih "muda" -> partial.
    """
    l3 = layer_by_number.get(3)
    if not l3:
        return 0.0
    if l3.data.get("bos_bullish") or l3.data.get("bos_bearish"):
        return w
    if l3.data.get("choch") or l3.data.get("structure_bias") in ("bullish", "bearish"):
        return w * 0.6
    return 0.0


def _score_liquidity_event(w: float, layer_by_number: dict) -> float:
    """
    Liquidity event (Layer 4 - liquidity sweep). BARU di V2: sebelumnya sweep terdeteksi &
    dipakai Layer 8 untuk SL, tapi tidak pernah menyumbang skor sama sekali.
    EQUAL_HIGH/LOW_SWEEP (2+ level menumpuk - liquidity pool lebih kuat) = penuh.
    SWING_SWEEP (swing tunggal) = partial. Tidak ada sweep = 0.
    """
    l4 = layer_by_number.get(4)
    if not l4 or not l4.data.get("liquidity_sweep"):
        return 0.0
    sweep_type = l4.data.get("liquidity_sweep_type") or ""
    if "EQUAL" in sweep_type:
        return w
    return w * 0.65


def _score_smc_location(w: float, layer_by_number: dict) -> float:
    """
    SMC location (Layer 4 - OB/FVG). OB valid (sudah displacement+BOS confirmed di Layer 4,
    lihat layer4_smart_money.py) = referensi paling kuat -> penuh. FVG valid saja (tanpa OB)
    = partial. Kedua-duanya di-cap di `w` (tidak dobel).
    """
    l4 = layer_by_number.get(4)
    if not l4:
        return 0.0
    if l4.data.get("order_block_in_range"):
        return w
    if l4.data.get("fvg_in_range"):
        return w * 0.65
    return 0.0


def _score_entry_trigger_15m(w: float, layer_by_number: dict) -> float:
    """
    15M trigger (Layer 7). Displacement & confirmation SAMA-SAMA sudah wajib lolos sebagai
    hard gate (lihat layer7_entry_trigger.py) - jadi biner "lolos atau tidak" tidak berguna
    di sini (selalu lolos di titik ini). Digradasi dari 2 sumber:
    - displacement_strength relatif terhadap minimum (settings.entry_displacement_min_atr_mult):
      70% dari bobot, penuh kalau >= 2x minimum.
    - konfirmasi ganda (pattern engulfing DAN breakout close, bukan cuma salah satu):
      30% dari bobot.
    """
    l7 = layer_by_number.get(7)
    if not l7:
        return 0.0
    d = l7.data
    min_mult = settings.entry_displacement_min_atr_mult
    disp_frac = _scaled(d.get("displacement_strength", 0.0), floor=0.0, full=min_mult * 2)
    dual_confirm = bool(d.get("pattern_detected")) and bool(d.get("breakout_confirm"))
    confirm_frac = 1.0 if dual_confirm else 0.6
    return w * (0.7 * disp_frac + 0.3 * confirm_frac)


def _score_volume(w: float, layer_by_number: dict) -> float:
    """
    Volume (Layer 6, soft - boleh FAIL tanpa hard-stop pipeline). Digradasi dari
    volume_pct_of_avg relatif terhadap threshold spike (settings.volume_spike_multiplier),
    penuh kalau >= 2x threshold.
    """
    l6 = layer_by_number.get(6)
    if not l6:
        return 0.0
    pct = l6.data.get("volume_pct_of_avg", 0.0)
    threshold_pct = settings.volume_spike_multiplier * 100
    return w * _scaled(pct, floor=0.0, full=threshold_pct * 2)


def _score_momentum(w: float, layer_by_number: dict) -> float:
    """
    Momentum (Layer 5, soft). RSI adalah syarat utama (lebih besar bobotnya di dalam
    kategori ini), MACD histogram-growing sebagai pendukung tambahan - sesuai P1.6/P1.11
    ("jangan RSI 55 dianggap signal tersendiri, gunakan sebagai confirmation score").
    """
    l5 = layer_by_number.get(5)
    if not l5:
        return 0.0
    d = l5.data
    frac = 0.0
    if d.get("rsi_ok"):
        frac += 0.7
    if d.get("macd_ok"):
        frac += 0.3
    return w * frac


def _score_oi(w: float, layer_by_number: dict, snapshot: dict) -> float:
    """
    OI (soft/scoring only, data OI via ccxt/MEXC tidak selalu tersedia) - price x OI
    directional model (lihat indicators.technical.classify_price_oi_direction &
    core/exchange_client.py::fetch_oi_price_model), BUKAN lagi "OI naik = bullish" naif:
    - Klasifikasi SEARAH & berupa "buildup" (posisi baru dibangun searah trade) -> penuh.
      LONG_BUILDUP untuk setup LONG, SHORT_BUILDUP untuk setup SHORT.
    - Klasifikasi SEARAH tapi berupa "unwind" (short covering / long liquidation - harga
      bergerak searah trade tapi BUKAN karena minat baru, cuma posisi berlawanan menutup
      diri) -> partial, tetap ada informasi mendukung tapi lebih lemah dari buildup genuine.
    - Klasifikasi berlawanan arah, NEUTRAL, atau data tidak tersedia -> 0.
    Fallback ke flag biner lama `snapshot["oi_confirmation"]` kalau `snapshot["oi_direction"]`
    tidak ada sama sekali (mis. backtest.py yang tidak mensimulasikan data OI/price historis
    - lihat backtest.py docstring) supaya tidak crash/berubah perilaku di jalur itu.
    """
    classification = snapshot.get("oi_direction")
    if classification is None:
        return w if snapshot.get("oi_confirmation") else 0.0

    l2 = layer_by_number.get(2)
    direction = l2.data.get("trend_direction") if l2 else None

    if direction == "LONG":
        if classification == "LONG_BUILDUP":
            return w
        if classification == "SHORT_COVERING":
            return w * 0.5
        return 0.0
    if direction == "SHORT":
        if classification == "SHORT_BUILDUP":
            return w
        if classification == "LONG_LIQUIDATION":
            return w * 0.5
        return 0.0
    return 0.0


def _score_risk_quality(w: float, layer_by_number: dict) -> float:
    """
    Risk quality (Layer 8) - BARU di V2: kualitas structural SL/TP tidak pernah dinilai
    sama sekali di skema lama. Dipecah 2 sub-komponen:
    - RR TP1 relatif terhadap settings.min_rr (60% bobot): penuh kalau RR >= 2x min_rr
      (setup yang cuma pas-pasan lolos MIN_RR tidak dapat bonus penuh - target masih
      relatif dekat, meski tetap valid untuk lolos hard gate Layer 8).
    - Sumber referensi SL (40% bobot): referensi TERKAIT KONTEKS trigger 15M (lihat audit
      di layers/layer8_risk_management.py: liquidity_sweep, order_block_zone/fvg_zone yang
      PERSIS SAMA dengan alasan Layer 7 lolos - "trigger context") = penuh, karena SL-nya
      terikat langsung ke zona yang benar-benar membenarkan entry ini. swing_fallback (swing
      mentah tanpa hubungan ke trigger context - dipakai hanya kalau Layer 7/4 tidak
      menyediakan referensi apa pun) = partial.
    """
    l8 = layer_by_number.get(8)
    if not l8:
        return 0.0
    d = l8.data
    rr_frac = _scaled(d.get("tp1_rr", 0.0), floor=0.0, full=settings.min_rr * 2)
    sl_source = d.get("sl_source") or ""
    sl_frac = 1.0 if sl_source == "liquidity_sweep" or "trigger context" in sl_source else 0.6
    return w * (0.6 * rr_frac + 0.4 * sl_frac)


def run(layer_results_by_number: dict, indicators_snapshot: dict) -> SignalScore:
    """
    layer_results_by_number: dict {layer_number: LayerResult}. Layer 0/7/8 OPSIONAL (kalau
    tidak ada, misalnya di backtest yang belum melewatkannya, kategori terkait fallback ke
    indicators_snapshot atau dianggap 0 - lihat masing-masing fungsi _score_*).
    indicators_snapshot: dict berisi flag2 tambahan (btc_regime_aligned, oi_confirmation, dsb)
    yang dikumpulkan pipeline dari data layer 0/1/6.
    """
    w = settings.scoring_weights
    breakdown = {
        "regime_4h": round(_score_regime_4h(w["regime_4h"], layer_results_by_number, indicators_snapshot), 1),
        "structure_1h": round(_score_structure_1h(w["structure_1h"], layer_results_by_number), 1),
        "liquidity_event": round(_score_liquidity_event(w["liquidity_event"], layer_results_by_number), 1),
        "smc_location": round(_score_smc_location(w["smc_location"], layer_results_by_number), 1),
        "entry_trigger_15m": round(_score_entry_trigger_15m(w["entry_trigger_15m"], layer_results_by_number), 1),
        "volume": round(_score_volume(w["volume"], layer_results_by_number), 1),
        "momentum": round(_score_momentum(w["momentum"], layer_results_by_number), 1),
        "oi": round(_score_oi(w["oi"], layer_results_by_number, indicators_snapshot), 1),
        "risk_quality": round(_score_risk_quality(w["risk_quality"], layer_results_by_number), 1),
    }

    total = int(round(min(sum(breakdown.values()), 100)))

    return SignalScore(total=total, breakdown=breakdown, stars=_stars_for(total), grade=_grade_for(total))
    
