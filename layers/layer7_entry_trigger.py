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

URUTAN WAKTU (perbaikan P0 - anti look-ahead)
----------------------------------------------
Urutan kausal yang HARUS benar, dan sekarang DIPAKSA oleh kode (bukan cuma diasumsikan):

    zona/sweep 1H terbentuk & sudah CLOSED -> harga masuk/retest zona (context window)
        -> displacement 15M -> confirmation 15M (candle terakhir) -> entry

1. Context window dan displacement window TIDAK BOLEH overlap. Window layout (n = jumlah
   candle context, d = jumlah candle displacement):
       df_entry.iloc[-(n + d):-d]   = context window      C1 C2 C3
       df_entry.iloc[-d:]           = displacement window              C4 C5 C6
   Sebelumnya context = iloc[-n:] dan displacement = iloc[-d:] saling menimpa (C4-C6 masuk
   keduanya), sehingga "harga menyentuh zona lalu displacement" bisa terlapor padahal
   displacement-nya yang lebih dulu terjadi.
2. OB/FVG hanya sah jadi context kalau SUDAH TERSEDIA sebelum context window mulai:
       zone_available_at <= context_window_start
   `zone_available_at` = waktu CLOSE candle structure yang membuat zona itu bisa diketahui
   (open time + durasi TF structure; untuk OB = candle terakhir yang dibutuhkan displacement/
   BOS-nya, bukan cuma candle OB). Timestamp OHLCV adalah waktu OPEN, jadi membandingkan
   open time saja akan meloloskan zona dari candle 1H yang masih berjalan (look-ahead).
   Kedekatan harga saja TIDAK cukup - urutan waktu wajib benar.
3. Liquidity sweep memakai `meta["sweep_event_at"]` (candle yang menyapu) dan
   `meta["reclaim_at"]` dari Layer 4 - BUKAN waktu swing yang disapu. Urutannya:
   candle sweep closed -> reclaim closed -> displacement window mulai
   (sweep -> reaksi/reclaim -> displacement). Lihat `_sweep_precedes_displacement()`.
"""

import pandas as pd

from models import LayerResult, LayerStatus, Direction
from config import settings
from core.timeframes import timeframe_to_timedelta

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


def _to_utc_timestamp(value) -> pd.Timestamp | None:
    """Parse ISO string/Timestamp jadi Timestamp UTC (tz-aware). None kalau kosong/tidak valid."""
    if value is None or value == "":
        return None
    try:
        ts = pd.Timestamp(value)
    except Exception:
        return None
    if pd.isna(ts):
        return None
    return ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")


def _zone_available_at(zone, structure_delta: pd.Timedelta) -> pd.Timestamp | None:
    """
    Waktu zona (OB/FVG) TERSEDIA = close candle structure terakhir yang dibutuhkan untuk
    menyatakannya valid. Diambil dari yang PALING AKHIR antara `formed_at` dan
    `meta["ready_candle_at"]` (Layer 4: untuk OB = akhir displacement/BOS, untuk FVG =
    candle ketiga), lalu + durasi TF structure karena timestamp adalah waktu OPEN candle.
    None kalau tidak bisa ditentukan -> pemanggil harus MENOLAK zona (fail-safe).
    """
    candidates = [
        _to_utc_timestamp(getattr(zone, "formed_at", None)),
        _to_utc_timestamp((getattr(zone, "meta", None) or {}).get("ready_candle_at")),
    ]
    candidates = [c for c in candidates if c is not None]
    if not candidates:
        return None
    return max(candidates) + structure_delta


def _sweep_event_time(sweep) -> pd.Timestamp | None:
    """Timestamp candle yang benar-benar MENYAPU level (meta['sweep_event_at']). Bukan waktu swing/pool."""
    return _to_utc_timestamp((sweep.meta or {}).get("sweep_event_at"))


def _sweep_is_fresh(raw_data: dict, sweep) -> bool:
    """
    True kalau umur `sweep` (dalam jumlah candle STRUCTURE/1H sejak candle SWEEP-nya sampai
    candle structure terakhir yang tersedia) masih di dalam batas
    settings.trigger_context_sweep_max_age_bars - sweep yang sudah terlalu lama tidak lagi
    dianggap penyebab pergerakan 15M sekarang. Umur dihitung dari `sweep_event_at` (kapan
    sweep TERJADI), bukan dari waktu swing yang disapu.
    Kalau umur tidak bisa dihitung (data tidak lengkap), anggap TIDAK segar (fail-safe: lebih
    baik menolak context yang tidak bisa diverifikasi daripada menerimanya diam-diam).
    """
    df_structure = raw_data.get("ohlcv_structure")
    sweep_ts = _sweep_event_time(sweep)
    if df_structure is None or sweep_ts is None or not isinstance(df_structure.index, pd.DatetimeIndex):
        return False
    try:
        pos = df_structure.index.searchsorted(sweep_ts)
        age_bars = (len(df_structure.index) - 1) - pos
    except Exception:
        return False
    return 0 <= age_bars <= settings.trigger_context_sweep_max_age_bars


def _sweep_precedes_displacement(sweep, structure_delta: pd.Timedelta,
                                  displacement_start: pd.Timestamp) -> bool:
    """
    Urutan sweep -> reaksi (reclaim) -> displacement: candle sweep sudah CLOSED, candle
    reclaim sudah CLOSED, dan keduanya selesai SEBELUM (<=) candle displacement pertama
    dibuka. Timestamp Layer 4 = waktu OPEN candle 1H, jadi ditambah durasi TF structure
    untuk mendapat waktu close-nya. Kalau timeline tidak lengkap -> False (fail-safe).
    """
    event_at = _sweep_event_time(sweep)
    reclaim_at = _to_utc_timestamp((sweep.meta or {}).get("reclaim_at"))
    if event_at is None or reclaim_at is None or reclaim_at < event_at:
        return False
    return (event_at + structure_delta) <= displacement_start and \
           (reclaim_at + structure_delta) <= displacement_start


def _split_windows(df_entry):
    """
    Pisahkan candle entry jadi (context_window, displacement_window) TANPA overlap:
        context      = df_entry.iloc[-(context_n + disp_n):-disp_n]
        displacement = df_entry.iloc[-disp_n:]
    Return None kalau data tidak cukup / index bukan DatetimeIndex (urutan waktu tidak bisa
    diverifikasi -> tidak ada context, fail-safe).
    """
    context_n = max(1, int(settings.trigger_context_lookback_bars))
    disp_n = max(1, int(settings.entry_displacement_lookback_bars))
    if not isinstance(df_entry.index, pd.DatetimeIndex) or len(df_entry) < context_n + disp_n:
        return None
    context_window = df_entry.iloc[-(context_n + disp_n):-disp_n]
    displacement_window = df_entry.iloc[-disp_n:]
    if context_window.empty or displacement_window.empty:
        return None
    # Guard eksplisit: context harus benar-benar berakhir SEBELUM displacement mulai.
    if not context_window.index[-1] < displacement_window.index[0]:
        return None
    return context_window, displacement_window


def _find_smc_context(raw_data: dict, direction: Direction, df_entry, diag: dict = None) -> dict | None:
    """
    Cari hubungan antara trigger 15M (entry-timeframe) dengan area SMC 1H (structure-
    timeframe) dari Layer 4 - Order Block / FVG / Liquidity Sweep. Lihat docstring modul di
    atas untuk aturan lengkapnya. Return dict {"type", "zone", "detail", "timeline"} kalau
    ketemu, None kalau trigger ini tidak berhubungan dengan struktur 1H manapun.

    Urutan yang dipaksa (lihat "URUTAN WAKTU" di docstring modul):
      OB/FVG : zone_available_at <= context_window_start  DAN  wick context window menyentuh zona
      Sweep  : sweep closed -> reclaim closed -> displacement window mulai
    `diag` (opsional, dict) diisi hitungan zona yang DITOLAK karena urutan waktu, untuk audit.
    """
    windows = _split_windows(df_entry)
    if windows is None:
        return None
    context_window, displacement_window = windows

    structure_delta = timeframe_to_timedelta(settings.tf_structure)
    context_start = context_window.index[0]
    context_end = context_window.index[-1]
    displacement_start = displacement_window.index[0]
    confirmation_at = df_entry.index[-1]
    context_n = len(context_window)

    window_low = float(context_window["low"].min())
    window_high = float(context_window["high"].max())

    timeline = {
        "context_window_start": context_start.isoformat(),
        "context_window_end": context_end.isoformat(),
        "displacement_start": displacement_start.isoformat(),
        "confirmation_at": confirmation_at.isoformat(),
    }
    rejected_temporal = 0

    def _zone_candidates(key: str):
        return [z for z in (raw_data.get(key, []) or []) if z.direction == direction]

    for zone_type, key, label in ((CTX_ORDER_BLOCK, "smc_active_obs", "Order Block"),
                                   (CTX_FVG, "smc_active_fvgs", "FVG")):
        for zone in _zone_candidates(key):
            if not _range_overlaps_zone(window_low, window_high, zone):
                continue
            available_at = _zone_available_at(zone, structure_delta)
            if available_at is None or not available_at <= context_start:
                # Harga menyentuh zona, tapi zona itu belum tersedia (belum closed/terkonfirmasi)
                # saat context window dimulai -> BUKAN context (look-ahead). Proximity saja
                # tidak cukup.
                rejected_temporal += 1
                continue
            if diag is not None:
                diag["zones_rejected_temporal"] = rejected_temporal
            return {
                "type": zone_type, "zone": zone,
                "detail": f"Window pendekatan 15M ({context_n} candle) menyentuh {label} 1H "
                          f"@ {zone.bottom:.6g}-{zone.top:.6g} (zona tersedia sejak {available_at.isoformat()})",
                "timeline": {**timeline, "zone_available_at": available_at.isoformat()},
            }

    sweep = raw_data.get("liquidity_sweep_zone")
    if sweep is not None and sweep.direction == direction:
        if not _sweep_precedes_displacement(sweep, structure_delta, displacement_start):
            rejected_temporal += 1
        elif _sweep_is_fresh(raw_data, sweep):
            swept_level = sweep.meta.get("swept_level")
            detail = (f"Trigger 15M terjadi setelah liquidity sweep 1H ({sweep.meta.get('sweep_type')}) "
                      f"@ {swept_level:.6g} (sweep {sweep.meta.get('sweep_event_at')}, "
                      f"reclaim {sweep.meta.get('reclaim_at')})") if swept_level \
                else "Trigger 15M terjadi setelah liquidity sweep 1H"
            if diag is not None:
                diag["zones_rejected_temporal"] = rejected_temporal
            return {
                "type": CTX_LIQUIDITY_SWEEP, "zone": sweep, "detail": detail,
                "timeline": {**timeline,
                              "sweep_event_at": sweep.meta.get("sweep_event_at"),
                              "reclaim_at": sweep.meta.get("reclaim_at")},
            }

    if diag is not None:
        diag["zones_rejected_temporal"] = rejected_temporal
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
    context_diag = {}
    context = _find_smc_context(raw_data, direction, df_entry, diag=context_diag)
    raw_data["trigger_context"] = context  # dipakai Layer 8 untuk SL - lihat audit di sana

    data = {
        "pattern_detected": pattern,
        "pattern_name": pattern_name,
        "breakout_confirm": breakout_confirm,
        "displacement_strength": round(displacement_strength, 3),
        "displacement_ok": displacement_ok,
        "context_type": context["type"] if context else None,
        "context_detail": context["detail"] if context else None,
        # Timeline urutan waktu (context -> displacement -> confirmation) untuk audit/debug,
        # dan jumlah zona yang DITOLAK karena urutan waktu (bukan karena harga tidak menyentuh).
        "context_timeline": context.get("timeline") if context else None,
        "context_zones_rejected_temporal": context_diag.get("zones_rejected_temporal", 0),
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
