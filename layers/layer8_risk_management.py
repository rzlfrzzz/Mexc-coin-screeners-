"""
Layer 8 - Risk Management
---------------------------
Hitung Entry, SL, TP1/TP2/TP3 otomatis berdasarkan swing low/high TERDEKAT
SECARA HARGA (bukan terdekat secara waktu) dari entry.
SL LONG  : di bawah swing low terdekat (harga)
SL SHORT : di atas swing high terdekat (harga)
TP1/2/3  : kelipatan risk (RR 1:1, 1:2, 1:3)

Catatan penting soal pemilihan swing_ref:
`find_swings()` menyisir seluruh candle yang di-fetch (bisa mundur berhari-hari),
jadi bisa saja ada beberapa swing high/low valid di atas/bawah entry. Kalau kita
ambil yang PALING BARU SECARA WAKTU (index terakhir), itu bisa kebetulan sangat
jauh secara HARGA dari entry (misal swing high lama dari sebelum coin crash
tajam) -> risk jadi sangat besar -> TP bisa sampai negatif/mustahil. Karena itu
kita pilih swing_ref dengan jarak harga PALING KECIL ke entry, lalu tetap
divalidasi lagi dengan sanity check `max_risk_pct` di bawah sebagai lapisan
pengaman kedua.
"""

from models import LayerResult, LayerStatus, Direction, RiskPlan
from layers.layer3_structure import find_swings
from config import settings

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
        # Ambil swing low dengan jarak harga paling dekat ke entry, bukan yang
        # paling baru secara waktu.
        nearest = min(lows, key=lambda s: entry - s["price"])
        swing_ref = nearest["price"]
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
        # Ambil swing high dengan jarak harga paling dekat ke entry, bukan yang
        # paling baru secara waktu.
        nearest = min(highs, key=lambda s: s["price"] - entry)
        swing_ref = nearest["price"]
        sl = swing_ref * (1 + SL_BUFFER_PCT / 100)
        risk = sl - entry
        if risk <= 0:
            return LayerResult(8, "Risk Management", LayerStatus.FAIL,
                                "Risk <= 0, SL tidak valid (entry sudah di atas swing high)", {}), None
        tp1, tp2, tp3 = entry - risk, entry - risk * 2, entry - risk * 3

    # --- Sanity check kedua (independen dari cara swing_ref dipilih di atas) ---
    # Kalau risk (jarak entry->SL) terlalu besar dibanding harga entry, setup ini
    # tidak realistis untuk ditradingkan (size jadi sangat kecil untuk risk yang
    # wajar) dan berisiko menghasilkan TP yang mustahil (negatif atau nyaris nol).
    # Daripada meneruskan RiskPlan yang cacat, layer ini FAIL total.
    risk_pct_of_entry = (risk / entry) * 100 if entry > 0 else float("inf")
    if risk_pct_of_entry > settings.max_risk_pct:
        return LayerResult(
            8, "Risk Management", LayerStatus.FAIL,
            f"Risk {risk_pct_of_entry:.1f}% dari entry melebihi batas "
            f"{settings.max_risk_pct:.1f}% (swing_ref @ {swing_ref:.6g} terlalu jauh dari entry "
            f"{entry:.6g}) - SL/TP tidak realistis, sinyal di-skip", {}
        ), None

    # Untuk SHORT, TP tidak boleh menyentuh atau melewati nol (harga tidak bisa negatif).
    if min(tp1, tp2, tp3) <= 0:
        return LayerResult(
            8, "Risk Management", LayerStatus.FAIL,
            "TP hasil perhitungan <= 0 (harga mustahil), sinyal di-skip", {}
        ), None

    risk_plan = RiskPlan(entry=entry, sl=sl, tp1=tp1, tp2=tp2, tp3=tp3, risk_amount=risk)

    data = {
        "entry": entry, "sl": sl, "tp1": tp1, "tp2": tp2, "tp3": tp3,
        "risk_amount": risk, "swing_ref": swing_ref, "risk_pct_of_entry": risk_pct_of_entry,
    }

    return LayerResult(8, "Risk Management", LayerStatus.PASS,
                        f"SL @ {sl:.6g}, Risk {risk:.6g} ({risk_pct_of_entry:.1f}% dari entry), Target RR 1:3",
                        data), risk_plan
