"""
Layer 4 - Smart Money Area
----------------------------
Mencari 3 area penting pada timeframe structure (default 1H, settings.tf_structure),
masing-masing dengan STATE/lifecycle-nya sendiri (lihat ringkasan_perbaikan.md P1.5),
bukan cuma "ketemu atau tidak":

1. Fair Value Gap (FVG) : gap 3-candle, dengan state FRESH / PARTIALLY_FILLED /
   MITIGATED / INVALIDATED / EXPIRED. Signal hanya memakai FRESH & PARTIALLY_FILLED.
2. Order Block (OB)     : candle impulsif terakhir sebelum pergerakan besar berlawanan
   arah, TAPI hanya dianggap valid kalau diikuti DISPLACEMENT yang cukup kuat (net move
   >= X * ATR dalam beberapa candle ke depan) DAN displacement itu benar-benar berujung
   ke BOS (event dari Layer 3) dalam jangka waktu wajar - bukan cuma "body candle besar
   = OB" (skema: OB candle -> displacement -> BOS).
3. Liquidity Sweep      : diklasifikasi jadi SWING_SWEEP (swing high/low tunggal yang
   disapu) atau EQUAL_HIGH_SWEEP/EQUAL_LOW_SWEEP (2+ swing dengan harga hampir sama yang
   disapu sekaligus - liquidity pool yang lebih kuat karena stop menumpuk di level itu).
"""

import pandas as pd

from models import LayerResult, LayerStatus, SmartMoneyZone, Direction
from config import settings
from layers.layer3_structure import find_swings

IMPULSE_MULTIPLIER = 1.5  # candle dianggap 'impulsif' jika body > rata2 body * multiplier

# ---------------- FVG lifecycle states ----------------
FVG_FRESH = "FRESH"
FVG_PARTIALLY_FILLED = "PARTIALLY_FILLED"
FVG_MITIGATED = "MITIGATED"
FVG_INVALIDATED = "INVALIDATED"
FVG_EXPIRED = "EXPIRED"

# ---------------- Liquidity sweep classification ----------------
SWEEP_SWING = "SWING_SWEEP"
SWEEP_EQUAL_HIGH = "EQUAL_HIGH_SWEEP"
SWEEP_EQUAL_LOW = "EQUAL_LOW_SWEEP"


def _body(df_row):
    return abs(df_row["close"] - df_row["open"])


# =====================================================================================
# Fair Value Gap - deteksi + lifecycle
# =====================================================================================

def _fvg_lifecycle(df, start_idx: int, top: float, bottom: float, approach_from: str):
    """
    Jalan dari `start_idx` (candle SETELAH gap terbentuk) sampai akhir `df`, tentukan
    seberapa dalam candle-candle berikutnya sudah masuk ke zona gap [bottom, top]:
    - approach_from="above" (FVG bullish, price expected retrace turun ke zona)
    - approach_from="below" (FVG bearish, price expected retrace naik ke zona)
    Return (state, fill_ratio_terdalam 0..1, index candle saat fully-filled/None).
    """
    height = top - bottom
    if height <= 0:
        return FVG_INVALIDATED, 1.0, start_idx

    deepest_fill_ratio = 0.0
    touched = False

    for j in range(start_idx, len(df)):
        low_j, high_j = float(df["low"].iloc[j]), float(df["high"].iloc[j])
        if approach_from == "above":
            if low_j <= bottom:
                return FVG_INVALIDATED, 1.0, j  # gap tertembus habis - sudah tidak valid
            if low_j <= top:
                touched = True
                deepest_fill_ratio = max(deepest_fill_ratio, (top - low_j) / height)
        else:
            if high_j >= top:
                return FVG_INVALIDATED, 1.0, j
            if high_j >= bottom:
                touched = True
                deepest_fill_ratio = max(deepest_fill_ratio, (high_j - bottom) / height)

    if not touched:
        return FVG_FRESH, 0.0, None
    if deepest_fill_ratio >= settings.fvg_mitigation_fill_ratio:
        return FVG_MITIGATED, deepest_fill_ratio, None
    return FVG_PARTIALLY_FILLED, deepest_fill_ratio, None


def find_fvgs(df: pd.DataFrame, direction: Direction, lookback: int = 40):
    """FVG 3-candle: gap antara high[i-1] dan low[i+1] (bullish) atau low[i-1] dan high[i+1] (bearish)."""
    recent = df.iloc[-lookback:]
    n = len(recent)
    zones = []
    for i in range(1, n - 1):
        prev_high, prev_low = recent["high"].iloc[i - 1], recent["low"].iloc[i - 1]
        next_high, next_low = recent["high"].iloc[i + 1], recent["low"].iloc[i + 1]
        formed_idx = i + 1  # candle ketiga = saat gap resmi terbentuk & bisa mulai diisi
        age_bars = (n - 1) - formed_idx

        if direction == Direction.LONG and next_low > prev_high:
            top, bottom = float(next_low), float(prev_high)
            # Scan retracement mulai dari SETELAH candle pembentuk gap (formed_idx+1) -
            # candle formed_idx sendiri (low = `next_low` = top gap) selalu "menyentuh"
            # batas top-nya sendiri secara trivial (low_j == top by construction), itu
            # BUKAN retracement asli, cuma efek matematis dari cara gap ini didefinisikan.
            # Tanpa +1 ini, tiap FVG baru akan langsung berstatus PARTIALLY_FILLED
            # (fill_ratio=0) alih-alih FRESH begitu ada 1 candle lagi setelahnya.
            state, fill_ratio, _ = _fvg_lifecycle(recent, formed_idx + 1, top, bottom, "above")
            if state == FVG_FRESH and age_bars > settings.fvg_max_age_bars:
                state = FVG_EXPIRED
            zones.append(SmartMoneyZone(
                zone_type="fvg", direction=Direction.LONG, top=top, bottom=bottom, index=i,
                valid=state in (FVG_FRESH, FVG_PARTIALLY_FILLED),
                formed_at=recent.index[formed_idx].isoformat(),
                meta={"state": state, "fill_ratio": round(fill_ratio, 3), "age_bars": age_bars},
            ))
        if direction == Direction.SHORT and next_high < prev_low:
            top, bottom = float(prev_low), float(next_high)
            state, fill_ratio, _ = _fvg_lifecycle(recent, formed_idx + 1, top, bottom, "below")
            if state == FVG_FRESH and age_bars > settings.fvg_max_age_bars:
                state = FVG_EXPIRED
            zones.append(SmartMoneyZone(
                zone_type="fvg", direction=Direction.SHORT, top=top, bottom=bottom, index=i,
                valid=state in (FVG_FRESH, FVG_PARTIALLY_FILLED),
                formed_at=recent.index[formed_idx].isoformat(),
                meta={"state": state, "fill_ratio": round(fill_ratio, 3), "age_bars": age_bars},
            ))
    return zones


# =====================================================================================
# Order Block - deteksi + displacement + konfirmasi BOS
# =====================================================================================

def _measure_displacement(df: pd.DataFrame, i: int, move_direction: str) -> float:
    """
    Net move (dalam satuan ATR) selama `settings.ob_displacement_lookforward_bars` candle
    SETELAH candle impulsif index `i` - "displacement" asli, bukan cuma body 1 candle besar.
    ATR referensi dihitung dari 14 candle SEBELUM `i` (bukan termasuk pergerakan setelahnya,
    supaya tidak lookahead & tidak bias oleh displacement itu sendiri).
    """
    lookforward = settings.ob_displacement_lookforward_bars
    end = min(i + 1 + lookforward, len(df))
    if end <= i + 1:
        return 0.0
    segment = df.iloc[i + 1:end]
    if segment.empty:
        return 0.0

    start_price = float(df["close"].iloc[i])
    if move_direction == "up":
        net_move = float(segment["high"].max()) - start_price
    else:
        net_move = start_price - float(segment["low"].min())

    window = df.iloc[max(0, i - 14):i + 1]
    tr = (window["high"] - window["low"]).abs()
    atr_ref = float(tr.mean()) if len(tr) else 0.0
    if atr_ref <= 0:
        return 0.0
    return max(net_move, 0.0) / atr_ref


def _ob_mitigation(df: pd.DataFrame, start_i: int, zone_top: float, zone_bottom: float, side: str):
    """
    Cek dari candle SETELAH `start_i` (candle impulsif OB) sampai akhir df:
    - mitigated  : harga sudah pernah balik masuk ke zona OB (di-test ulang)
    - invalidated: harga sudah CLOSE menembus habis sisi seberang zona (OB gagal total
      sebagai support/resistance)
    """
    mitigated, invalidated = False, False
    for j in range(start_i + 1, len(df)):
        low_j, high_j, close_j = float(df["low"].iloc[j]), float(df["high"].iloc[j]), float(df["close"].iloc[j])
        if side == "long":
            if low_j <= zone_top:
                mitigated = True
            if close_j < zone_bottom:
                invalidated = True
        else:
            if high_j >= zone_bottom:
                mitigated = True
            if close_j > zone_top:
                invalidated = True
    return mitigated, invalidated


def _confirmed_by_bos(recent: pd.DataFrame, i: int, expected_direction: str, structure_snapshot: dict) -> bool:
    """
    OB dianggap "confirmed" kalau event BOS yang PERSIS SAMA yang dideteksi Layer 3
    (raw_data["structure_snapshot"] - lihat layers/layer3_structure.py) terjadi SETELAH
    candle OB ini, searah (`expected_direction`), dan dalam jarak <= settings.
    ob_bos_confirm_max_bars candle - supaya BOS itu benar masuk akal sebagai KONSEKUENSI
    dari displacement OB ini, bukan BOS lama yang tidak terkait.
    """
    if not structure_snapshot:
        return False
    bos_ts_str = structure_snapshot.get("bos_timestamp")
    bos_direction = structure_snapshot.get("bos_direction")
    if not bos_ts_str or bos_direction != expected_direction:
        return False
    try:
        bos_ts = pd.Timestamp(bos_ts_str)
        ob_ts = recent.index[i]
    except Exception:
        return False
    if bos_ts < ob_ts:
        return False
    bos_pos = int(recent.index.searchsorted(bos_ts))
    bars_between = bos_pos - i
    return 0 <= bars_between <= settings.ob_bos_confirm_max_bars


def find_order_blocks(df: pd.DataFrame, direction: Direction, lookback: int = 40,
                       structure_snapshot: dict = None):
    """
    Order block bullish: candle bearish terakhir sebelum rangkaian candle bullish impulsif
    YANG diikuti displacement kuat & BOS bullish (skema: OB -> displacement -> BOS).
    Order block bearish: kebalikannya.
    """
    recent = df.iloc[-lookback:]
    avg_body = (recent["close"] - recent["open"]).abs().mean()
    n = len(recent)
    zones = []

    for i in range(1, n - 1):
        body = abs(recent["close"].iloc[i] - recent["open"].iloc[i])
        is_impulsive = body > avg_body * IMPULSE_MULTIPLIER
        if not is_impulsive:
            continue
        bullish_impulse = recent["close"].iloc[i] > recent["open"].iloc[i]
        prev = recent.iloc[i - 1]
        prev_bearish = prev["close"] < prev["open"]
        prev_bullish = prev["close"] > prev["open"]

        if direction == Direction.LONG and bullish_impulse and prev_bearish:
            zone_top, zone_bottom = float(prev["open"]), float(prev["close"])
            displacement_strength = _measure_displacement(recent, i, "up")
            mitigated, invalidated = _ob_mitigation(recent, i, zone_top, zone_bottom, "long")
            confirmed_by_bos = _confirmed_by_bos(recent, i, "bullish", structure_snapshot)
            valid = (
                displacement_strength >= settings.ob_displacement_min_atr_mult
                and confirmed_by_bos
                and not invalidated
            )
            zones.append(SmartMoneyZone(
                zone_type="order_block", direction=Direction.LONG,
                top=zone_top, bottom=zone_bottom, index=i, valid=valid,
                formed_at=recent.index[i - 1].isoformat(),  # candle OB itu sendiri = prev (i-1)
                meta={
                    "impulse_body": float(body),
                    "displacement_strength": round(displacement_strength, 3),
                    "mitigated": mitigated,
                    "invalidated": invalidated,
                    "confirmed_by_bos": confirmed_by_bos,
                },
            ))
        if direction == Direction.SHORT and not bullish_impulse and prev_bullish:
            zone_top, zone_bottom = float(prev["close"]), float(prev["open"])
            displacement_strength = _measure_displacement(recent, i, "down")
            mitigated, invalidated = _ob_mitigation(recent, i, zone_top, zone_bottom, "short")
            confirmed_by_bos = _confirmed_by_bos(recent, i, "bearish", structure_snapshot)
            valid = (
                displacement_strength >= settings.ob_displacement_min_atr_mult
                and confirmed_by_bos
                and not invalidated
            )
            zones.append(SmartMoneyZone(
                zone_type="order_block", direction=Direction.SHORT,
                top=zone_top, bottom=zone_bottom, index=i, valid=valid,
                formed_at=recent.index[i - 1].isoformat(),
                meta={
                    "impulse_body": float(body),
                    "displacement_strength": round(displacement_strength, 3),
                    "mitigated": mitigated,
                    "invalidated": invalidated,
                    "confirmed_by_bos": confirmed_by_bos,
                },
            ))
    return zones


# =====================================================================================
# Liquidity Sweep - klasifikasi SWING_SWEEP vs EQUAL_HIGH/LOW_SWEEP
# =====================================================================================

def _cluster_equal_levels(level_swings: list, tolerance_pct: float):
    """
    Cari cluster >=2 swing (tipe sama, sudah difilter caller) dengan harga dalam
    `tolerance_pct`% satu sama lain - liquidity pool "equal high/low". Kalau ada beberapa
    cluster valid, ambil yang candle TERAKHIRNYA paling baru (paling relevan sekarang).
    """
    if len(level_swings) < 2:
        return None
    best = None
    for i in range(len(level_swings)):
        cluster = [level_swings[i]]
        for j in range(len(level_swings)):
            if j == i:
                continue
            base_price = level_swings[i]["price"]
            if base_price == 0:
                continue
            if abs(level_swings[j]["price"] - base_price) / abs(base_price) * 100 <= tolerance_pct:
                cluster.append(level_swings[j])
        if len(cluster) >= 2:
            avg_price = sum(c["price"] for c in cluster) / len(cluster)
            last_index = max(c["index"] for c in cluster)
            if best is None or last_index > best["last_index"]:
                best = {"price": avg_price, "count": len(cluster), "last_index": last_index}
    return best


def find_equal_highs(swings: list, tolerance_pct: float):
    return _cluster_equal_levels([s for s in swings if s["type"] == "high"], tolerance_pct)


def find_equal_lows(swings: list, tolerance_pct: float):
    return _cluster_equal_levels([s for s in swings if s["type"] == "low"], tolerance_pct)


def find_liquidity_sweep(df: pd.DataFrame, direction: Direction, lookback: int = None):
    """
    Cek apakah candle terakhir (2-3 terakhir) menyapu liquidity lalu close balik ke dalam
    range -> sweep valid. Diklasifikasi EQUAL_HIGH/LOW_SWEEP (diprioritaskan, liquidity pool
    lebih kuat karena 2+ swing menumpuk di level hampir sama) atau SWING_SWEEP (swing
    signifikan tunggal terakhir) sebagai fallback.
    `lookback` idealnya adalah swing_lookback adaptif yang sama yang dipakai Layer 3
    (raw_data["swing_lookback"]) supaya definisi swing konsisten di seluruh pipeline
    untuk satu symbol yang sama.
    """
    swings = find_swings(df) if lookback is None else find_swings(df, lookback=lookback)
    if not swings or len(df) < 5:
        return None

    last_candles = df.iloc[-3:]
    tol = settings.equal_level_tolerance_pct

    if direction == Direction.LONG:
        equal_lows = find_equal_lows(swings, tol)
        if equal_lows and equal_lows["count"] >= 2:
            level = equal_lows["price"]
            swept = (last_candles["low"] < level).any()
            closed_back_above = last_candles["close"].iloc[-1] > level
            if swept and closed_back_above:
                return SmartMoneyZone(
                    zone_type="liquidity_sweep", direction=Direction.LONG,
                    top=float(level), bottom=float(last_candles["low"].min()),
                    index=equal_lows["last_index"],
                    formed_at=df.index[equal_lows["last_index"]].isoformat(),
                    meta={"sweep_type": SWEEP_EQUAL_LOW, "swept_level": level, "pool_count": equal_lows["count"]},
                )
        recent_lows = [s for s in swings if s["type"] == "low"]
        if not recent_lows:
            return None
        target = recent_lows[-1]
        swept = (last_candles["low"] < target["price"]).any()
        closed_back_above = last_candles["close"].iloc[-1] > target["price"]
        if swept and closed_back_above:
            return SmartMoneyZone(
                zone_type="liquidity_sweep", direction=Direction.LONG,
                top=float(target["price"]), bottom=float(last_candles["low"].min()),
                index=target["index"], formed_at=df.index[target["index"]].isoformat(),
                meta={"sweep_type": SWEEP_SWING, "swept_level": target["price"]},
            )
    else:
        equal_highs = find_equal_highs(swings, tol)
        if equal_highs and equal_highs["count"] >= 2:
            level = equal_highs["price"]
            swept = (last_candles["high"] > level).any()
            closed_back_below = last_candles["close"].iloc[-1] < level
            if swept and closed_back_below:
                return SmartMoneyZone(
                    zone_type="liquidity_sweep", direction=Direction.SHORT,
                    top=float(last_candles["high"].max()), bottom=float(level),
                    index=equal_highs["last_index"],
                    formed_at=df.index[equal_highs["last_index"]].isoformat(),
                    meta={"sweep_type": SWEEP_EQUAL_HIGH, "swept_level": level, "pool_count": equal_highs["count"]},
                )
        recent_highs = [s for s in swings if s["type"] == "high"]
        if not recent_highs:
            return None
        target = recent_highs[-1]
        swept = (last_candles["high"] > target["price"]).any()
        closed_back_below = last_candles["close"].iloc[-1] < target["price"]
        if swept and closed_back_below:
            return SmartMoneyZone(
                zone_type="liquidity_sweep", direction=Direction.SHORT,
                top=float(last_candles["high"].max()), bottom=float(target["price"]),
                index=target["index"], formed_at=df.index[target["index"]].isoformat(),
                meta={"sweep_type": SWEEP_SWING, "swept_level": target["price"]},
            )
    return None


def make_zone_id(zone: SmartMoneyZone) -> str:
    """
    Id stabil untuk satu zona, dipakai setup_identity.py::build_setup_id() - BUKAN dari
    `zone.index` (posisi relatif yang bisa bergeser antar scan begitu window data maju),
    tapi dari `zone.formed_at` (timestamp absolut candle pembentuk zona) yang tidak berubah
    selama candle itu masih ada di data - jadi setup_id yang sama akan tetap sama persis di
    scan-scan berikutnya selama zona & structure event-nya belum berubah.
    """
    ts = (zone.formed_at or "NA").replace(":", "").replace("-", "").replace("+00:00", "Z")
    return f"{zone.zone_type.upper()}_{zone.direction.value}_{ts}"


def price_in_zone(price: float, zone: SmartMoneyZone, tolerance_pct: float = 0.15) -> bool:
    span = zone.top - zone.bottom
    tol = span * tolerance_pct
    return (zone.bottom - tol) <= price <= (zone.top + tol)


def run(raw_data: dict, direction: Direction) -> LayerResult:
    # Zona (OB/FVG/liquidity sweep) tetap dihitung dari timeframe structure (default 1H) -
    # ini area besar yang tidak berubah tiap 15 menit. Tapi "harga sekarang" untuk cek
    # apakah harga SEDANG BERADA di zona itu pakai timeframe entry (default 15m, lebih
    # fresh) - candle structure terakhir bisa sampai ~1 jam ketinggalan dari harga riil.
    df_structure = raw_data["ohlcv_structure"]
    current_price = float(raw_data["ohlcv_entry"]["close"].iloc[-1])

    swing_lookback = raw_data.get("swing_lookback")
    structure_snapshot = raw_data.get("structure_snapshot", {})

    order_blocks = find_order_blocks(df_structure, direction, structure_snapshot=structure_snapshot)
    fvgs = find_fvgs(df_structure, direction)
    sweep = find_liquidity_sweep(df_structure, direction, lookback=swing_lookback)
    # Dipakai Layer 8 (raw_data["liquidity_sweep_zone"]) sebagai referensi SL struktural
    # utama (P1.9: "Liquidity sweep low/high -> structural low/high -> SL") - pola yang
    # sama dengan raw_data["structure_snapshot"] dari Layer 3.
    raw_data["liquidity_sweep_zone"] = sweep

    # Simpan sweep ke raw_data supaya Layer 8 (Risk Management) bisa memakainya sebagai
    # referensi SL struktural yang lebih presisi (lihat ringkasan_perbaikan.md P1.9:
    # "Liquidity sweep low -> structural low -> SL") - pola yang sama seperti
    # raw_data["structure_snapshot"] dari Layer 3.
    raw_data["liquidity_sweep_zone"] = sweep

    # Hanya zona yang masih VALID yang dipakai untuk cek "harga sekarang ada di zona ini":
    # FVG valid = state FRESH/PARTIALLY_FILLED (belum MITIGATED/INVALIDATED/EXPIRED).
    # OB  valid = displacement cukup kuat + confirmed_by_bos + belum invalidated.
    active_obs = [ob for ob in order_blocks if ob.valid]
    active_fvgs = [f for f in fvgs if f.valid]

    # Simpan zona VALID (bukan cuma yang "in range" harga sekarang) ke raw_data supaya
    # Layer 7 (entry_trigger, lihat layers/layer7_entry_trigger.py) bisa mengecek apakah
    # WINDOW PENDEKATAN sebelum displacement 15M bersinggungan dengan salah satu zona ini -
    # trigger 15M jadi context-aware terhadap OB/FVG 1H, bukan cuma raw_data["ohlcv_entry"]
    # sendirian tanpa hubungan ke struktur besar (perbaikan poin 1 - lihat docstring Layer 7).
    raw_data["smc_active_obs"] = active_obs
    raw_data["smc_active_fvgs"] = active_fvgs

    ob_in_range = [ob for ob in active_obs if price_in_zone(current_price, ob)]
    fvg_in_range = [f for f in active_fvgs if price_in_zone(current_price, f)]

    zones = order_blocks + fvgs + ([sweep] if sweep else [])

    # Zona AKTIF (tempat harga sekarang berada) - dipakai setup_identity.py::build_setup_id()
    # untuk komponen `zone_id`. OB diprioritaskan sebelum FVG kalau kebetulan keduanya valid
    # & overlap di harga yang sama (OB dianggap referensi zona yang lebih "berat").
    active_zone = (ob_in_range[0] if ob_in_range else (fvg_in_range[0] if fvg_in_range else None))
    active_zone_id = make_zone_id(active_zone) if active_zone else None

    data = {
        "order_blocks_found": len(order_blocks),
        "order_blocks_valid": len(active_obs),
        "fvgs_found": len(fvgs),
        "fvgs_valid": len(active_fvgs),
        "order_block_in_range": bool(ob_in_range),
        "fvg_in_range": bool(fvg_in_range),
        "liquidity_sweep": bool(sweep),
        "liquidity_sweep_type": sweep.meta.get("sweep_type") if sweep else None,
        "current_price": current_price,
        "active_zone_id": active_zone_id,
        "active_zone_type": active_zone.zone_type if active_zone else None,
    }

    has_retrace_zone = bool(ob_in_range) or bool(fvg_in_range)

    if not has_retrace_zone and not sweep:
        return LayerResult(4, "Smart Money Area", LayerStatus.FAIL,
                            "Tidak ada Order Block/FVG valid di harga sekarang dan tidak ada liquidity sweep", data,
                            ), zones

    reason_parts = []
    if ob_in_range:
        reason_parts.append(f"harga berada di Order Block valid ({len(ob_in_range)} zona, displacement+BOS confirmed)")
    if fvg_in_range:
        states = sorted({f.meta.get("state") for f in fvg_in_range})
        reason_parts.append(f"harga berada di FVG valid ({len(fvg_in_range)} zona, state: {', '.join(states)})")
    if sweep:
        reason_parts.append(f"liquidity sweep terdeteksi ({sweep.meta.get('sweep_type')}) & sudah rejection")

    result = LayerResult(4, "Smart Money Area", LayerStatus.PASS,
                          "Smart money zone valid: " + ", ".join(reason_parts), data)
    return result, zones
