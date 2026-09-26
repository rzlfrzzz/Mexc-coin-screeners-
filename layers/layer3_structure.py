"""
Layer 3 - Market Structure
----------------------------
Deteksi swing (fractal) pada timeframe structure (default 1H, settings.tf_structure),
klasifikasikan sebagai HH/HL/LH/LL, lalu tentukan Break of Structure (BOS) dan
Change of Character (CHoCH) sebagai EVENT (satu candle spesifik), bukan status
"sedang di atas level" yang dicek ulang tiap kali layer ini jalan.

Definisi dipakai (lihat ringkasan_perbaikan.md P1.4):
- Swing high: candle dengan high tertinggi dibanding N candle kiri & kanan.
- Swing low : candle dengan low terendah dibanding N candle kiri & kanan.
- Swing baru "diketahui" (confirmed) setelah N candle di kanan candle swing itu terbentuk
  (fractal butuh sisi kanan untuk konfirmasi) - lihat `find_swings()`.
- BOS (event)  : candle PERTAMA yang closenya menembus level swing SEARAH bias struktur
  yang sedang berjalan (continuation) - persis:
      previous_close <= swing_level  DAN  current_close > swing_level   (bullish)
      previous_close >= swing_level  DAN  current_close < swing_level   (bearish)
  BUKAN "close saat ini > swing_high" yang dicek ulang tiap candle (itu bukan event,
  itu status - bisa true berhari-hari berturut-turut untuk swing yang sama).
- CHoCH (event): candle PERTAMA yang closenya menembus level swing di SISI BERLAWANAN
  dari bias struktur yang sedang berjalan (reversal) - mis. struktur sedang bullish
  (HH/HL) lalu close menembus ke BAWAH swing low terakhir (HL) -> CHoCH ke bearish.
  Kalau event CHoCH ini lebih baru daripada event BOS terakhir, bias struktur dianggap
  sudah berubah (structure_state ikut berubah), BOS lama tidak lagi dianggap "aktif".
"""

import pandas as pd

from models import LayerResult, LayerStatus
from config import settings
from indicators.technical import atr_pct

SWING_LOOKBACK = 3  # fallback/default kalau adaptive lookback tidak bisa dihitung


def compute_adaptive_lookback(df, period: int = 14) -> int:
    """
    Tentukan jumlah candle kiri/kanan (lookback) untuk validasi fractal secara adaptif
    berdasarkan volatilitas coin itu sendiri (rata-rata ATR% selama `period` candle
    terakhir di timeframe structure), bukan konstanta tetap N=3 untuk semua pair:
    - ATR% rendah (coin "tenang")  -> lookback lebih KECIL, supaya tetap sensitif
      mendeteksi swing (kalau tetap pakai N besar, swing asli yang kecil bisa terlewat).
    - ATR% tinggi (coin noisy/volatile) -> lookback lebih BESAR, supaya swing minor akibat
      noise tidak salah dianggap sebagai swing high/low yang valid.
    Dibatasi antara settings.swing_lookback_min dan settings.swing_lookback_max.
    """
    try:
        atr_series = atr_pct(df, period=period)
        recent_atr = atr_series.tail(50).dropna()
        if recent_atr.empty:
            return settings.swing_lookback_default
        avg_atr_pct = float(recent_atr.mean())
    except Exception:
        return settings.swing_lookback_default

    if avg_atr_pct <= settings.swing_lookback_low_atr_pct:
        lookback = settings.swing_lookback_min
    elif avg_atr_pct >= settings.swing_lookback_high_atr_pct:
        lookback = settings.swing_lookback_max
    else:
        # interpolasi linear antara min dan max di rentang low_atr_pct..high_atr_pct
        span = settings.swing_lookback_high_atr_pct - settings.swing_lookback_low_atr_pct
        ratio = (avg_atr_pct - settings.swing_lookback_low_atr_pct) / span if span > 0 else 0.5
        lookback = round(
            settings.swing_lookback_min + ratio * (settings.swing_lookback_max - settings.swing_lookback_min)
        )

    return max(settings.swing_lookback_min, min(settings.swing_lookback_max, int(lookback)))


def find_swings(df, lookback: int = SWING_LOOKBACK):
    """
    Kembalikan list of dict {index, price, type: 'high'|'low', confirmed_index, time,
    confirmed_at} terurut sesuai waktu.

    `index`           : posisi candle swing itu sendiri di `df`.
    `confirmed_index` : posisi candle PERTAMA di mana swing ini BISA diketahui (butuh
                        `lookback` candle di kanan swing untuk konfirmasi fractal) -
                        inilah timestamp yang benar dipakai untuk cek BOS/CHoCH, BUKAN
                        `index`, supaya tidak look-ahead (lihat ringkasan_perbaikan.md
                        P1.4.A: "timestamp swing menunjukkan kapan swing diketahui, bukan
                        kapan candle swing terjadi").
    `time`/`confirmed_at` : versi timestamp asli (dari df.index) dari kedua posisi di atas,
                        kalau df punya DatetimeIndex - dipakai untuk logging/Supabase.
    """
    highs = df["high"].values
    lows = df["low"].values
    has_dt_index = isinstance(df.index, pd.DatetimeIndex)
    swings = []
    for i in range(lookback, len(df) - lookback):
        window_high = highs[i - lookback:i + lookback + 1]
        window_low = lows[i - lookback:i + lookback + 1]
        confirmed_index = i + lookback
        if highs[i] == window_high.max() and highs[i] != highs[i - 1]:
            swings.append({
                "index": i, "price": highs[i], "type": "high",
                "confirmed_index": confirmed_index,
                "time": df.index[i] if has_dt_index else None,
                "confirmed_at": df.index[confirmed_index] if has_dt_index else None,
            })
        if lows[i] == window_low.min() and lows[i] != lows[i - 1]:
            swings.append({
                "index": i, "price": lows[i], "type": "low",
                "confirmed_index": confirmed_index,
                "time": df.index[i] if has_dt_index else None,
                "confirmed_at": df.index[confirmed_index] if has_dt_index else None,
            })
    swings.sort(key=lambda s: s["index"])
    # buang duplikat berurutan dengan tipe sama (ambil yang paling ekstrem)
    cleaned = []
    for s in swings:
        if cleaned and cleaned[-1]["type"] == s["type"]:
            if s["type"] == "high" and s["price"] > cleaned[-1]["price"]:
                cleaned[-1] = s
            elif s["type"] == "low" and s["price"] < cleaned[-1]["price"]:
                cleaned[-1] = s
        else:
            cleaned.append(s)
    return cleaned


def label_swings(swings: list) -> list:
    """Tambahkan label HH/HL/LH/LL berdasarkan swing sebelumnya dengan tipe sama."""
    labeled = []
    last_high = None
    last_low = None
    for s in swings:
        label = None
        if s["type"] == "high":
            if last_high is not None:
                label = "HH" if s["price"] > last_high else "LH"
            last_high = s["price"]
        else:
            if last_low is not None:
                label = "HL" if s["price"] > last_low else "LL"
            last_low = s["price"]
        labeled.append({**s, "label": label})
    return labeled


def _find_crossing_event(close: pd.Series, swing: dict, mode: str) -> dict | None:
    """
    Cari candle PERTAMA (paling awal secara waktu) SETELAH swing ini terkonfirmasi
    (`swing["confirmed_index"]`) di mana close menembus level swing searah `mode`:
      "above" : previous_close <= level  DAN  current_close > level
      "below" : previous_close >= level  DAN  current_close < level
    Scan dimulai dari confirmed_index (bukan index candle swing itu sendiri) supaya level
    ini baru "dianggap ada" sejak swing-nya benar-benar terkonfirmasi - tidak look-ahead.
    Return None kalau belum pernah tersentuh sama sekali sepanjang data yang ada.
    """
    level = swing["price"]
    start = max(int(swing.get("confirmed_index", swing["index"])), 1)
    n = len(close)
    for j in range(start, n):
        prev_c = float(close.iloc[j - 1])
        cur_c = float(close.iloc[j])
        crossed = (prev_c <= level < cur_c) if mode == "above" else (prev_c >= level > cur_c)
        if crossed:
            return {"index": j, "timestamp": close.index[j], "level": float(level)}
    return None


def run(raw_data: dict) -> LayerResult:
    df_structure = raw_data["ohlcv_structure"]  # default 1H (settings.tf_structure)

    # Adaptive lookback dihitung sekali di sini lalu disimpan ke raw_data supaya Layer 4
    # (order block / liquidity sweep) dan Layer 8 (swing ref untuk SL) memakai nilai yang
    # persis sama - konsisten satu symbol, satu lookback, bukan tiap layer hitung sendiri.
    swing_lookback = compute_adaptive_lookback(df_structure)
    raw_data["swing_lookback"] = swing_lookback

    swings = find_swings(df_structure, lookback=swing_lookback)

    if len(swings) < 4:
        return LayerResult(3, "Market Structure", LayerStatus.FAIL,
                            "Swing terlalu sedikit untuk analisis struktur",
                            {"swings": swings, "swing_lookback": swing_lookback})

    labeled = label_swings(swings)
    recent_labels = [s["label"] for s in labeled if s["label"] is not None][-4:]

    last_high_swing = next((s for s in reversed(labeled) if s["type"] == "high"), None)
    last_low_swing = next((s for s in reversed(labeled) if s["type"] == "low"), None)

    # "Tilt" mentah dari komposisi label 4 swing terakhir - dipakai sebagai bias AWAL
    # sebelum dikonfirmasi/dikoreksi oleh event BOS/CHoCH di bawah.
    label_tilt = "bullish" if recent_labels.count("HH") + recent_labels.count("HL") >= \
        recent_labels.count("LH") + recent_labels.count("LL") else "bearish"

    close = df_structure["close"]

    # BOS = break SEARAH tilt (continuation). CHoCH = break BERLAWANAN arah tilt (reversal).
    if label_tilt == "bullish":
        bos_event = _find_crossing_event(close, last_high_swing, "above") if last_high_swing else None
        choch_event = _find_crossing_event(close, last_low_swing, "below") if last_low_swing else None
        bos_swing_ref, choch_swing_ref = last_high_swing, last_low_swing
        bos_dir_if_found, choch_dir_if_found = "bullish", "bearish"
    else:
        bos_event = _find_crossing_event(close, last_low_swing, "below") if last_low_swing else None
        choch_event = _find_crossing_event(close, last_high_swing, "above") if last_high_swing else None
        bos_swing_ref, choch_swing_ref = last_low_swing, last_high_swing
        bos_dir_if_found, choch_dir_if_found = "bearish", "bullish"

    # Kalau CHoCH terjadi LEBIH BARU daripada BOS (atau BOS belum pernah terjadi),
    # struktur dianggap sudah berubah arah - BOS lama sudah tidak "aktif" lagi.
    choch_is_latest = bool(
        choch_event and (not bos_event or choch_event["timestamp"] > bos_event["timestamp"])
    )

    bos_active = bool(bos_event) and not choch_is_latest
    choch_active = choch_is_latest

    structure_bias = choch_dir_if_found if choch_is_latest else label_tilt

    bos_bullish = bos_active and bos_dir_if_found == "bullish"
    bos_bearish = bos_active and bos_dir_if_found == "bearish"
    choch = choch_active

    bos_timestamp = bos_event["timestamp"].isoformat() if bos_active and bos_event else None
    bos_level = bos_event["level"] if bos_active and bos_event else None
    bos_direction = bos_dir_if_found if bos_active else None
    bos_id = (f"BOS_{bos_direction}_{bos_swing_ref['index']}_{bos_event['index']}"
              if bos_active and bos_event and bos_swing_ref else None)

    choch_timestamp = choch_event["timestamp"].isoformat() if choch_active and choch_event else None
    choch_level = choch_event["level"] if choch_active and choch_event else None
    choch_direction = choch_dir_if_found if choch_active else None
    choch_id = (f"CHOCH_{choch_direction}_{choch_swing_ref['index']}_{choch_event['index']}"
                if choch_active and choch_event and choch_swing_ref else None)
    choch_desc = (
        f"Struktur berubah ke {choch_direction} (close menembus "
        f"{'swing low' if choch_direction == 'bearish' else 'swing high'} terakhir @ {choch_level:.6g})"
        if choch_active else ""
    )

    if bos_bullish:
        structure_state = "bullish_bos"
    elif bos_bearish:
        structure_state = "bearish_bos"
    elif choch and structure_bias == "bullish":
        structure_state = "bullish_choch"
    elif choch and structure_bias == "bearish":
        structure_state = "bearish_choch"
    else:
        structure_state = "ranging"

    data = {
        "recent_labels": recent_labels,
        "label_tilt": label_tilt,
        "structure_bias": structure_bias,
        "structure_state": structure_state,
        "bos_bullish": bos_bullish,
        "bos_bearish": bos_bearish,
        "bos_timestamp": bos_timestamp,
        "bos_level": bos_level,
        "bos_direction": bos_direction,
        "bos_id": bos_id,
        "choch": choch,
        "choch_desc": choch_desc,
        "choch_timestamp": choch_timestamp,
        "choch_level": choch_level,
        "choch_direction": choch_direction,
        "choch_id": choch_id,
        "last_high_swing": last_high_swing,
        "last_low_swing": last_low_swing,
        "swing_lookback": swing_lookback,
    }

    # Dipakai Layer 4 (order block "confirmed by BOS") supaya validasi displacement -> BOS
    # bisa membandingkan candle OB dengan event BOS yang PERSIS SAMA yang layer ini deteksi
    # sendiri - bukan Layer 4 menghitung ulang BOS dengan logika/level yang bisa berbeda.
    raw_data["structure_snapshot"] = data

    if not bos_bullish and not bos_bearish and not choch:
        return LayerResult(3, "Market Structure", LayerStatus.FAIL,
                            "Belum ada BOS maupun CHoCH yang jelas", data)

    reason = f"Struktur ({structure_state}): "
    if bos_bullish:
        reason += f"BOS Bullish @ {bos_level:.6g} ({bos_timestamp}). "
    if bos_bearish:
        reason += f"BOS Bearish @ {bos_level:.6g} ({bos_timestamp}). "
    if choch:
        reason += choch_desc

    return LayerResult(3, "Market Structure", LayerStatus.PASS, reason.strip(), data)
          
