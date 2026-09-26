"""
Layer 7 - Entry Trigger
-------------------------
Entry hanya dikirim ketika semua layer sebelumnya (1-6) lolos DAN ada DISPLACEMENT nyata
DI BELAKANG trigger, dikonfirmasi pattern candlestick pada candle terakhir timeframe ENTRY
(default 15m, settings.tf_entry) - bukan timeframe structure - karena trigger entry aktual
harus setepat mungkin, tidak menunggu candle 1H/4H selesai (lihat ringkasan_perbaikan.md
P1.8, "15M Trigger" di paling bawah hierarki HTF Bias -> Structure -> Liquidity -> SMC
Location -> 15M Trigger):
- Displacement  : net move `settings.entry_displacement_lookback_bars` candle terakhir >=
  `settings.entry_displacement_min_atr_mult` x ATR(14) entry-timeframe - memastikan ADA
  TENAGA di belakang trigger, bukan cuma pola candle formasi tanpa follow-through.
- Confirmation  : Bullish/Bearish Engulfing, ATAU close menembus level resistance/support
  terdekat (breakout confirmation).
- Context (PERBAIKAN)  : displacement & confirmation di atas SEBELUMNYA bisa lolos tanpa
  peduli sama sekali apakah trigger 15M ini punya hubungan dengan area SMC 1H (Order
  Block/FVG/Liquidity Sweep dari Layer 4) - dua coin dengan pola candle 15M yang identik
  akan dinilai SAMA walau satu terjadi sebagai reaksi dari OB/FVG/sweep 1H yang jelas dan
  satu lagi terjadi di ruang kosong tanpa struktur apa pun di baliknya. Sekarang trigger
  HANYA dianggap valid kalau WINDOW PENDEKATAN (`settings.trigger_context_lookback_bars`
  candle 15M tepat SEBELUM window displacement) benar-benar bersinggungan (wick, bukan cuma
  close) dengan salah satu dari:
    1. Order Block 1H valid searah (raw_data["smc_active_obs"], dari Layer 4).
    2. FVG 1H valid searah (raw_data["smc_active_fvgs"], dari Layer 4).
    3. Level liquidity sweep 1H searah (raw_data["liquidity_sweep_zone"], dari Layer 4) YANG
       MASIH SEGAR (umur <= settings.trigger_context_sweep_max_age_bars candle structure
       sejak sweep terbentuk) dan sudah terjadi SEBELUM window pendekatan ini (urutan waktu
       harus make sense: sweep dulu, baru reaksi/displacement 15M, bukan sebaliknya).
  Lihat `_find_smc_context()`. Zona/sweep yang jadi alasan trigger ini disimpan ke
  `raw_data["trigger_context"]` supaya Layer 8 (Risk Management) bisa memakai REFERENSI
  STRUKTURAL YANG SAMA untuk SL - bukan menghitung ulang secara independen dan bisa berakhir
  memakai zona lain yang tidak relevan dengan alasan entry ini sebenarnya terjadi (lihat
  audit di layers/layer8_risk_management.py).
Displacement, confirmation, DAN context SAMA-SAMA harus terpenuhi (skema: 15M displacement
-> 15M close confirmation -> konteks SMC 1H -> ENTRY, bukan cuma sebagian).
"""

import pandas as pd

from models import LayerResult, LayerStatus, Direction
from config import settings

CTX_ORDER_BLOCK = "order_block"
CTX_FVG = "fvg"
CTX_LIQUIDITY_SWEEP = "liquidity_sweep"


def _measure_entry_displacement(df_entry, direction: Direction) -> float:
    """
    Net move (dalam satuan ATR) selama `settings.entry_displacement_lookback_bars` candle
    TERAKHIR di timeframe entry - "displacement" asli di belakang trigger, bukan cuma 1
    candle formasi tanpa tenaga. ATR referensi dihitung dari 14 candle SEBELUM window
    displacement ini (tidak termasuk pergerakan yang sedang diukur, supaya tidak bias).
    """
    lookback = settings.entry_displacement_lookback_bars
    if len(df_entry) < lookback + 15:
        return 0.0

    segment = df_entry.iloc[-lookback:]
    start_price = float(df_entry["close"].iloc[-(lookback + 1)])
    if direction == Direction.LONG:
        net_move = float(segment["high"].max()) - start_price
    else:
        net_move = start_price - float(segment["low"].min())

    atr_window = df_entry.iloc[-(lookback + 15):-(lookback + 1)]
    tr = (atr_window["high"] - atr_window["low"]).abs()
    atr_ref = float(tr.mean()) if len(tr) else 0.0
    if atr_ref <= 0:
        return 0.0
    return max(net_move, 0.0) / atr_ref


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


def _range_overlaps_zone(low: float, high: float, zone) -> bool:
    """True kalau range satu/beberapa candle [low, high] (gabungan wick, bukan cuma close)
    BERSINGGUNGAN dengan zona [zone.bottom, zone.top] - dipakai untuk cek apakah harga
    benar-benar SEMPAT masuk ke zona itu, bukan cuma lewat jauh di luarnya."""
    return low <= zone.top and high >= zone.bottom


def _sweep_is_fresh(raw_data: dict, sweep) -> bool:
    """
    True kalau umur `sweep` (dalam jumlah candle STRUCTURE/1H sejak terbentuk sampai candle
    structure terakhir yang tersedia) masih di dalam batas settings.trigger_context_sweep_max_age_bars
    - sweep yang sudah terlalu lama tidak lagi dianggap penyebab pergerakan 15M sekarang.
    Kalau umur tidak bisa dihitung (data tidak lengkap), anggap TIDAK segar (fail-safe: lebih
    baik menolak context yang tidak bisa diverifikasi daripada menerimanya diam-diam).
    """
    df_structure = raw_data.get("ohlcv_structure")
    if df_structure is None or not sweep.formed_at or not isinstance(df_structure.index, pd.DatetimeIndex):
        return False
    try:
        sweep_ts = pd.Timestamp(sweep.formed_at)
        pos = df_structure.index.searchsorted(sweep_ts)
        age_bars = (len(df_structure.index) - 1) - pos
    except Exception:
        return False
    return 0 <= age_bars <= settings.trigger_context_sweep_max_age_bars


def _find_smc_context(raw_data: dict, direction: Direction, df_entry) -> dict | None:
    """
    Cari hubungan antara trigger 15M (entry-timeframe) dengan area SMC 1H (structure-
    timeframe) dari Layer 4 - Order Block / FVG / Liquidity Sweep. Lihat docstring modul di
    atas untuk aturan lengkapnya. Return dict {"type", "zone", "detail"} kalau ketemu, None
    kalau trigger ini tidak berhubungan dengan struktur 1H manapun.
    """
    lookback_bars = settings.trigger_context_lookback_bars
    if len(df_entry) < lookback_bars + 1:
        return None

    window = df_entry.iloc[-lookback_bars:]
    window_low = float(window["low"].min())
    window_high = float(window["high"].max())
    window_start_time = window.index[0] if isinstance(df_entry.index, pd.DatetimeIndex) else None

    for ob in raw_data.get("smc_active_obs", []) or []:
        if ob.direction == direction and _range_overlaps_zone(window_low, window_high, ob):
            return {
                "type": CTX_ORDER_BLOCK, "zone": ob,
                "detail": f"Window pendekatan 15M ({lookback_bars} candle) menyentuh Order "
                          f"Block 1H @ {ob.bottom:.6g}-{ob.top:.6g}",
            }

    for fvg in raw_data.get("smc_active_fvgs", []) or []:
        if fvg.direction == direction and _range_overlaps_zone(window_low, window_high, fvg):
            return {
                "type": CTX_FVG, "zone": fvg,
                "detail": f"Window pendekatan 15M ({lookback_bars} candle) menyentuh FVG 1H "
                          f"@ {fvg.bottom:.6g}-{fvg.top:.6g}",
            }

    sweep = raw_data.get("liquidity_sweep_zone")
    if sweep is not None and sweep.direction == direction:
        sweep_ts = None
        try:
            sweep_ts = pd.Timestamp(sweep.formed_at) if sweep.formed_at else None
        except Exception:
            sweep_ts = None
        sweep_already_happened = (
            sweep_ts is not None and window_start_time is not None and sweep_ts <= window_start_time
        )
        if sweep_already_happened and _sweep_is_fresh(raw_data, sweep):
            swept_level = sweep.meta.get("swept_level")
            return {
                "type": CTX_LIQUIDITY_SWEEP, "zone": sweep,
                "detail": f"Trigger 15M terjadi setelah liquidity sweep 1H "
                          f"({sweep.meta.get('sweep_type')}) @ {swept_level:.6g}" if swept_level
                          else "Trigger 15M terjadi setelah liquidity sweep 1H",
            }

    return None


def run(raw_data: dict, direction: Direction, prior_layers_passed: bool) -> LayerResult:
    df_entry = raw_data["ohlcv_entry"]

    if not prior_layers_passed:
        return LayerResult(7, "Entry Trigger", LayerStatus.FAIL,
                            "Salah satu layer sebelumnya gagal, entry trigger tidak dievaluasi", {})

    if direction == Direction.LONG:
        pattern = _is_bullish_engulfing(df_entry)
        pattern_name = "Bullish Engulfing"
    else:
        pattern = _is_bearish_engulfing(df_entry)
        pattern_name = "Bearish Engulfing"

    breakout_confirm = _closed_beyond_recent_extreme(df_entry, direction)
    displacement_strength = _measure_entry_displacement(df_entry, direction)
    displacement_ok = displacement_strength >= settings.entry_displacement_min_atr_mult
    context = _find_smc_context(raw_data, direction, df_entry)
    raw_data["trigger_context"] = context  # dipakai Layer 8 untuk SL - lihat audit di sana

    data = {
        "pattern_detected": pattern,
        "pattern_name": pattern_name,
        "breakout_confirm": breakout_confirm,
        "displacement_strength": round(displacement_strength, 3),
        "displacement_ok": displacement_ok,
        "context_type": context["type"] if context else None,
        "context_detail": context["detail"] if context else None,
    }

    confirmation = pattern or breakout_confirm

    if not displacement_ok:
        return LayerResult(7, "Entry Trigger", LayerStatus.FAIL,
                            f"Displacement 15M kurang kuat ({displacement_strength:.2f}x ATR, "
                            f"minimum {settings.entry_displacement_min_atr_mult}x)", data)
    if not confirmation:
        return LayerResult(7, "Entry Trigger", LayerStatus.FAIL,
                            "Displacement cukup tapi tidak ada pattern konfirmasi (engulfing / breakout close)", data)
    if context is None:
        return LayerResult(
            7, "Entry Trigger", LayerStatus.FAIL,
            "Displacement & konfirmasi cukup tapi trigger 15M TIDAK berhubungan dengan Order "
            "Block/FVG/Liquidity Sweep 1H manapun (searah & masih relevan) - kemungkinan cuma "
            "pola candle kebetulan tanpa konteks struktur besar, bukan reaksi genuine dari SMC 1H", data
        )

    reason = f"Displacement {displacement_strength:.2f}x ATR + konfirmasi: "
    reason += f"{pattern_name} terdeteksi. " if pattern else ""
    reason += "Close breakout di luar range terakhir. " if breakout_confirm else ""
    reason += context["detail"]

    return LayerResult(7, "Entry Trigger", LayerStatus.PASS, reason.strip(), data)
