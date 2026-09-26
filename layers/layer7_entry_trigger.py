"""
Layer 8 - Risk Management
---------------------------
Hitung Entry, SL, TP1/TP2/TP3 berbasis STRUKTUR, bukan "nearest swing + RR tetap"
(lihat ringkasan_perbaikan.md P1.9 & P1.10):

SL (P1.9 - Structural SL, DIAUDIT ULANG setelah Layer 7 jadi context-aware)
----------------------------------------------------------------------------
Sebelum Layer 7 context-aware (lihat layers/layer7_entry_trigger.py), SL di sini dihitung
INDEPENDEN dari alasan trigger 15M sebenarnya terjadi: Layer 8 selalu memakai sweep TERBARU
yang ditemukan Layer 4 (`raw_data["liquidity_sweep_zone"]`) tanpa peduli apakah trigger 15M
itu benar reaksi dari sweep tsb, atau justru dari OB/FVG lain, atau bahkan tidak berhubungan
sama sekali. Akibatnya SL bisa merujuk ke level yang TIDAK RELEVAN dengan setup yang
sebenarnya terjadi. Sekarang Layer 7 SUDAH menetapkan dengan pasti zona/sweep mana yang jadi
alasan trigger ini (`raw_data["trigger_context"]`), jadi urutan prioritas referensi
invalidasi struktural diaudit ulang jadi:
1. Zona/level PERSIS SAMA yang jadi alasan Layer 7 lolos (`raw_data["trigger_context"]`) -
   PALING akurat karena ini bukan cuma "ada sweep/OB/FVG di suatu tempat", tapi ZONA YANG
   BENAR-BENAR MEMBENARKAN kenapa entry ini terjadi:
   - liquidity_sweep -> swept_level (sama seperti perilaku versi sebelumnya).
   - order_block/fvg -> sisi zona yang BERLAWANAN dari arah trade (bottom untuk LONG, top
     untuk SHORT) - kalau harga balik menembus habis OB/FVG yang jadi alasan entry ini,
     thesis-nya sudah tidak valid.
2. Fallback (dipertahankan untuk pemanggilan di luar pipeline normal, mis. unit test, yang
   belum sempat melewati Layer 7 sehingga `trigger_context` belum ada): sweep TERBARU
   (`raw_data["liquidity_sweep_zone"]`) SEARAH setup, seperti perilaku versi sebelumnya.
3. Fallback terakhir: swing low/high TERDEKAT secara HARGA dari entry (perilaku versi
   sebelumnya, dipertahankan sebagai fallback yang wajar - bukan dihapus).
SL = level referensi di atas -+ buffer dalam satuan ATR (settings.sl_atr_buffer_mult,
disarankan 0.1-0.25x ATR) - BUKAN persentase tetap kecil, supaya buffer otomatis
menyesuaikan volatilitas coin (coin volatile butuh buffer lebih lebar dari wick
noise-nya sendiri dibanding coin tenang).

TP (audit): TP1/2/3 di bawah TETAP memakai target struktural (swing berlawanan arah)
TERDEKAT dari entry, TIDAK diubah oleh audit ini - target TP secara konsep memang harus
forward-looking (ke mana harga BISA menuju), bukan terikat ke zona asal entry seperti SL
(invalidasi arah BALIK). Audit ini mengonfirmasi tidak ada bug di logika TP yang perlu
diperbaiki terkait perubahan Layer 7.

TP (P1.10 - Structural TP)
----------------------------
TP1 = target struktural (liquidity/swing berlawanan arah) TERDEKAT dari entry.
TP2 = target struktural berikutnya (lebih jauh).
TP3 = target struktural ketiga, ATAU ekstensi RR 3x kalau itu lebih jauh (extension
      di luar struktur yang sudah diketahui).
Kalau salah satu levelnya tidak ada target struktural yang cocok, fallback ke baseline
RR (1R/2R/3R) - bukan dipaksa mengada-ada.
VALIDASI MINIMUM RR: kalau target struktural TERDEKAT (TP1) sudah ada tapi RR-nya di
bawah `settings.min_rr`, setup ini DITOLAK (bukan diteruskan dengan RR kecil) - resistance
strategy jelas benar : "resistance/support terlalu dekat untuk ditradingkan secara masuk
akal" (persis kata-kata di ringkasan_perbaikan.md P1.10).
"""

from models import LayerResult, LayerStatus, Direction, RiskPlan
from layers.layer3_structure import find_swings
from indicators.technical import atr
from config import settings


def _structural_sl_reference(raw_data: dict, direction: Direction, entry: float, swings: list):
    """
    Return (level, source_label) - lihat urutan prioritas di docstring modul (P1.9, diaudit
    ulang supaya konsisten dengan raw_data["trigger_context"] dari Layer 7).
    (None, None) kalau tidak ada referensi valid sama sekali.
    """
    context = raw_data.get("trigger_context")
    if context and context.get("zone") is not None:
        zone = context["zone"]
        if context["type"] == "liquidity_sweep" and zone.direction == direction:
            ref = zone.meta.get("swept_level", zone.bottom if direction == Direction.LONG else zone.top)
            return float(ref), "liquidity_sweep"
        if context["type"] in ("order_block", "fvg") and zone.direction == direction:
            ref = zone.bottom if direction == Direction.LONG else zone.top
            return float(ref), f"{context['type']}_zone (trigger context)"

    sweep = raw_data.get("liquidity_sweep_zone")
    if sweep is not None:
        if direction == Direction.LONG and sweep.direction == Direction.LONG:
            return float(sweep.meta.get("swept_level", sweep.bottom)), "liquidity_sweep"
        if direction == Direction.SHORT and sweep.direction == Direction.SHORT:
            return float(sweep.meta.get("swept_level", sweep.top)), "liquidity_sweep"

    if direction == Direction.LONG:
        lows = [s for s in swings if s["type"] == "low" and s["price"] < entry]
        if not lows:
            return None, None
        nearest = min(lows, key=lambda s: entry - s["price"])
        return float(nearest["price"]), "swing_fallback"
    else:
        highs = [s for s in swings if s["type"] == "high" and s["price"] > entry]
        if not highs:
            return None, None
        nearest = min(highs, key=lambda s: s["price"] - entry)
        return float(nearest["price"]), "swing_fallback"


def _opposing_structural_targets(swings: list, direction: Direction, entry: float, tolerance_pct: float) -> list:
    """
    List harga target struktural BERLAWANAN arah dari entry, terurut dari yang PALING
    DEKAT dulu. Level yang hampir sama (dalam `tolerance_pct`) di-dedup jadi satu -
    dianggap satu level struktural yang sama, bukan dua target terpisah.
    """
    if direction == Direction.LONG:
        candidates = [s for s in swings if s["type"] == "high" and s["price"] > entry]
    else:
        candidates = [s for s in swings if s["type"] == "low" and s["price"] < entry]
    if not candidates:
        return []

    candidates = sorted(candidates, key=lambda s: abs(s["price"] - entry))
    levels = []
    for s in candidates:
        price = float(s["price"])
        if any(abs(price - lv) / lv * 100 <= tolerance_pct for lv in levels if lv != 0):
            continue
        levels.append(price)
    return levels


def run(raw_data: dict, direction: Direction) -> tuple[LayerResult, RiskPlan | None]:
    # Harga ENTRY dari timeframe entry (default 15m, settings.tf_entry) - lebih fresh
    # daripada close candle structure (1H) yang bisa ketinggalan sampai ~1 jam. SL/TP
    # tetap dari swing/sweep di timeframe STRUCTURE (invalidasi struktural, bukan noise 15m).
    df_structure = raw_data["ohlcv_structure"]
    entry = float(raw_data["ohlcv_entry"]["close"].iloc[-1])
    swing_lookback = raw_data.get("swing_lookback")
    swings = find_swings(df_structure) if swing_lookback is None else find_swings(df_structure, lookback=swing_lookback)

    sl_ref, sl_source = _structural_sl_reference(raw_data, direction, entry, swings)
    if sl_ref is None:
        label = "swing low" if direction == Direction.LONG else "swing high"
        return LayerResult(8, "Risk Management", LayerStatus.FAIL,
                            f"Tidak ditemukan {label} valid untuk referensi SL", {}), None

    atr_series = atr(df_structure, period=14)
    atr_abs = float(atr_series.iloc[-1]) if len(atr_series) else 0.0
    buffer = atr_abs * settings.sl_atr_buffer_mult

    if direction == Direction.LONG:
        sl = sl_ref - buffer
        risk = entry - sl
    else:
        sl = sl_ref + buffer
        risk = sl - entry

    if risk <= 0:
        return LayerResult(8, "Risk Management", LayerStatus.FAIL,
                            "Risk <= 0, SL tidak valid (entry sudah melewati level referensi)", {}), None

    # --- Sanity check: risk tidak boleh terlalu besar dibanding entry ---
    risk_pct_of_entry = (risk / entry) * 100 if entry > 0 else float("inf")
    if risk_pct_of_entry > settings.max_risk_pct:
        return LayerResult(
            8, "Risk Management", LayerStatus.FAIL,
            f"Risk {risk_pct_of_entry:.1f}% dari entry melebihi batas "
            f"{settings.max_risk_pct:.1f}% (referensi SL @ {sl_ref:.6g} [{sl_source}] terlalu jauh "
            f"dari entry {entry:.6g}) - SL/TP tidak realistis, sinyal di-skip", {}
        ), None

    # --- TP struktural (P1.10) ---
    targets = _opposing_structural_targets(swings, direction, entry, settings.equal_level_tolerance_pct)
    sign = 1 if direction == Direction.LONG else -1

    def _rr_of(price: float) -> float:
        return abs(price - entry) / risk if risk > 0 else 0.0

    if targets:
        tp1 = targets[0]
        tp1_source = "structural"
        tp1_rr = _rr_of(tp1)
        if tp1_rr < settings.min_rr:
            return LayerResult(
                8, "Risk Management", LayerStatus.FAIL,
                f"Target struktural terdekat @ {tp1:.6g} cuma RR 1:{tp1_rr:.2f} (minimum 1:{settings.min_rr}) "
                "- resistance/support terlalu dekat untuk ditradingkan, setup ditolak", {}
            ), None
    else:
        tp1 = entry + sign * risk
        tp1_source = "rr_fallback"
        tp1_rr = 1.0

    tp2_candidate = targets[1] if len(targets) >= 2 else entry + sign * risk * 2
    tp2_source = "structural" if len(targets) >= 2 else "rr_fallback"
    # Jaga urutan monoton (TP2 harus lebih jauh dari TP1 searah `sign`) - bisa saja
    # fallback RR*2 secara kebetulan lebih DEKAT daripada TP1 struktural yang sudah jauh.
    if sign * (tp2_candidate - tp1) <= 0:
        tp2_candidate = tp1 + sign * risk
        tp2_source = "rr_fallback (disesuaikan agar > TP1)"
    tp2 = tp2_candidate

    rr3_baseline = entry + sign * risk * 3
    if len(targets) >= 3 and sign * (targets[2] - tp2) > 0 and sign * (targets[2] - rr3_baseline) >= 0:
        tp3 = targets[2]
        tp3_source = "structural"
    else:
        tp3 = rr3_baseline
        tp3_source = "rr_extension"
    if sign * (tp3 - tp2) <= 0:
        tp3 = tp2 + sign * risk
        tp3_source = "rr_extension (disesuaikan agar > TP2)"

    # Untuk SHORT, TP tidak boleh menyentuh atau melewati nol (harga tidak bisa negatif).
    if min(tp1, tp2, tp3) <= 0:
        return LayerResult(
            8, "Risk Management", LayerStatus.FAIL,
            "TP hasil perhitungan <= 0 (harga mustahil), sinyal di-skip", {}
        ), None

    risk_plan = RiskPlan(entry=entry, sl=sl, tp1=tp1, tp2=tp2, tp3=tp3, risk_amount=risk)

    data = {
        "entry": entry, "sl": sl, "tp1": tp1, "tp2": tp2, "tp3": tp3,
        "risk_amount": risk, "risk_pct_of_entry": risk_pct_of_entry,
        "sl_ref": sl_ref, "sl_source": sl_source, "atr_buffer": buffer,
        "tp1_source": tp1_source, "tp2_source": tp2_source, "tp3_source": tp3_source,
        "tp1_rr": round(tp1_rr, 3),
    }

    reason = (
        f"SL @ {sl:.6g} ({sl_source}, buffer {settings.sl_atr_buffer_mult}x ATR), "
        f"Risk {risk:.6g} ({risk_pct_of_entry:.1f}% dari entry). "
        f"TP1 {tp1_source} RR 1:{tp1_rr:.2f}, TP2 {tp2_source}, TP3 {tp3_source}."
    )

    return LayerResult(8, "Risk Management", LayerStatus.PASS, reason, data), risk_plan
      
