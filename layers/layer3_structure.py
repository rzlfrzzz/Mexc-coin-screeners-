"""
Layer 3 - Market Structure (1H)
---------------------------------
Deteksi swing (fractal) pada 1H, klasifikasikan sebagai HH/HL/LH/LL,
lalu tentukan Break of Structure (BOS) dan Change of Character (CHoCH).

Definisi dipakai:
- Swing high: candle dengan high tertinggi dibanding N candle kiri & kanan.
- Swing low : candle dengan low terendah dibanding N candle kiri & kanan.
- BOS bullish: harga close menembus swing high signifikan terakhir saat struktur bullish
               (atau menembus swing low signifikan terakhir saat berusaha reversal ke bearish
               -- disini kita definisikan BOS searah trend yang sedang berjalan).
- CHoCH: pola swing berubah arah, misal dari (HH,HL) ke LL, atau dari (LH,LL) ke HH.
"""

from models import LayerResult, LayerStatus
from config import settings
from indicators.technical import atr_pct

SWING_LOOKBACK = 3  # fallback/default kalau adaptive lookback tidak bisa dihitung


def compute_adaptive_lookback(df, period: int = 14) -> int:
    """
    Tentukan jumlah candle kiri/kanan (lookback) untuk validasi fractal secara adaptif
    berdasarkan volatilitas coin itu sendiri (rata-rata ATR% 1H selama `period` candle
    terakhir), bukan konstanta tetap N=3 untuk semua pair:
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
    """Kembalikan list of dict {index, price, type: 'high'|'low'} terurut sesuai waktu."""
    highs = df["high"].values
    lows = df["low"].values
    swings = []
    for i in range(lookback, len(df) - lookback):
        window_high = highs[i - lookback:i + lookback + 1]
        window_low = lows[i - lookback:i + lookback + 1]
        if highs[i] == window_high.max() and highs[i] != highs[i - 1]:
            swings.append({"index": i, "price": highs[i], "type": "high"})
        if lows[i] == window_low.min() and lows[i] != lows[i - 1]:
            swings.append({"index": i, "price": lows[i], "type": "low"})
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


def run(raw_data: dict) -> LayerResult:
    df_mtf = raw_data["ohlcv_mtf"]

    # Adaptive lookback dihitung sekali di sini lalu disimpan ke raw_data supaya Layer 4
    # (order block / liquidity sweep) dan Layer 8 (swing ref untuk SL) memakai nilai yang
    # persis sama - konsisten satu symbol, satu lookback, bukan tiap layer hitung sendiri.
    swing_lookback = compute_adaptive_lookback(df_mtf)
    raw_data["swing_lookback"] = swing_lookback

    swings = find_swings(df_mtf, lookback=swing_lookback)

    if len(swings) < 4:
        return LayerResult(3, "Market Structure 1H", LayerStatus.FAIL,
                            "Swing terlalu sedikit untuk analisis struktur",
                            {"swings": swings, "swing_lookback": swing_lookback})

    labeled = label_swings(swings)
    recent_labels = [s["label"] for s in labeled if s["label"] is not None][-4:]

    last_close = df_mtf["close"].iloc[-1]
    last_high_swing = next((s for s in reversed(labeled) if s["type"] == "high"), None)
    last_low_swing = next((s for s in reversed(labeled) if s["type"] == "low"), None)

    bos_bullish = last_high_swing is not None and last_close > last_high_swing["price"]
    bos_bearish = last_low_swing is not None and last_close < last_low_swing["price"]

    choch = False
    choch_desc = ""
    if len(recent_labels) >= 2:
        prev, cur = recent_labels[-2], recent_labels[-1]
        if prev in ("HH", "HL") and cur in ("LH", "LL"):
            choch = True
            choch_desc = f"{prev} -> {cur} (berpotensi shift ke bearish)"
        elif prev in ("LH", "LL") and cur in ("HH", "HL"):
            choch = True
            choch_desc = f"{prev} -> {cur} (berpotensi shift ke bullish)"

    structure_bias = "bullish" if recent_labels.count("HH") + recent_labels.count("HL") >= \
        recent_labels.count("LH") + recent_labels.count("LL") else "bearish"

    data = {
        "recent_labels": recent_labels,
        "bos_bullish": bos_bullish,
        "bos_bearish": bos_bearish,
        "choch": choch,
        "choch_desc": choch_desc,
        "structure_bias": structure_bias,
        "last_high_swing": last_high_swing,
        "last_low_swing": last_low_swing,
        "swing_lookback": swing_lookback,
    }

    if not bos_bullish and not bos_bearish and not choch:
        return LayerResult(3, "Market Structure 1H", LayerStatus.FAIL,
                            "Belum ada BOS maupun CHoCH yang jelas", data)

    reason = "Struktur 1H: "
    if bos_bullish:
        reason += "BOS Bullish terdeteksi. "
    if bos_bearish:
        reason += "BOS Bearish terdeteksi. "
    if choch:
        reason += f"CHoCH: {choch_desc}."

    return LayerResult(3, "Market Structure 1H", LayerStatus.PASS, reason.strip(), data)
