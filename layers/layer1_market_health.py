"""
Layer 1 - Market Health
------------------------
Verifikasi apakah coin layak ditradingkan sama sekali, sebelum layer lain dievaluasi:
- Volume 24 jam > threshold absolut, DAN (kalau data cukup) tidak sedang berada di
  percentile rendah dibanding histori volume coin itu sendiri (relatif per-coin)
- Spread bid-ask kecil
- ATR 1H cukup besar (volatilitas memadai) secara absolut, DAN (kalau data cukup) tidak
  sedang berada di percentile rendah dibanding histori ATR coin itu sendiri (relatif per-coin)
- Tidak sedang pump/dump ekstrem dalam 1 jam terakhir
- Funding rate tidak ekstrem (khusus futures - funding ekstrem = crowded trade satu sisi,
  risiko liquidation cascade/squeeze tinggi). Kalau data funding tidak tersedia dari
  exchange, cek ini di-skip secara graceful (tidak memblokir coin karena keterbatasan data).

Kenapa threshold relatif per-coin? Angka absolut tetap seperti "ATR% >= 0.2%" atau
"volume 24h >= $5jt" sama untuk BTC dan low-cap altcoin, padahal karakteristik keduanya
sangat berbeda (BTC bisa saja "tenang secara absolut" tapi itu justru NORMAL untuknya,
sementara $5jt volume mungkin sudah termasuk tinggi untuk altcoin kecil tapi rendah untuk
coin besar). Floor absolut tetap dipertahankan sebagai jaring pengaman dasar (anti coin
mati/dimanipulasi), tapi percentile relatif terhadap histori coin itu sendiri dipakai
sebagai lapisan tambahan supaya "sehat" diukur relatif terhadap kondisi normal coin
tersebut, bukan angka yang sama untuk semua.
"""

from config import settings
from models import LayerResult, LayerStatus
from indicators.technical import atr_pct, percentile_of_last


def run(raw_data: dict) -> LayerResult:
    symbol = raw_data["symbol"]
    ticker = raw_data["ticker"]
    spread_pct = raw_data["spread_pct"]
    df_mtf = raw_data["ohlcv_mtf"]  # 1H

    volume_24h_usd = ticker.get("quoteVolume") or (ticker.get("baseVolume", 0) * (ticker.get("last") or 0))
    atr_series = atr_pct(df_mtf, period=14)
    current_atr_pct = float(atr_series.iloc[-1]) if len(atr_series) else 0.0

    # Volume rolling 24h (24 candle 1H) dari OHLCV yang sudah di-fetch, dipakai untuk
    # percentile relatif - tidak butuh API call tambahan.
    rolling_vol_24h = df_mtf["volume"].rolling(window=24).sum()
    atr_percentile = percentile_of_last(atr_series, min_history=settings.percentile_min_history)
    volume_percentile = percentile_of_last(rolling_vol_24h, min_history=settings.percentile_min_history)

    # pump/dump check: perubahan harga dalam 1 jam terakhir (1 candle 1H)
    last_close = df_mtf["close"].iloc[-1]
    prev_close = df_mtf["close"].iloc[-2] if len(df_mtf) >= 2 else last_close
    change_1h_pct = abs((last_close - prev_close) / prev_close * 100) if prev_close else 0.0

    funding_rate_pct = raw_data.get("funding_rate_pct")

    data = {
        "volume_24h_usd": volume_24h_usd,
        "spread_pct": spread_pct,
        "atr_pct_1h": current_atr_pct,
        "atr_percentile": atr_percentile,
        "volume_percentile": volume_percentile,
        "change_1h_pct": change_1h_pct,
        "funding_rate_pct": funding_rate_pct,
    }

    if volume_24h_usd < settings.min_volume_24h_usd:
        return LayerResult(1, "Market Health", LayerStatus.FAIL,
                            f"Volume 24h ${volume_24h_usd:,.0f} < ${settings.min_volume_24h_usd:,.0f} (floor absolut)", data)

    if settings.enable_relative_volume_filter and volume_percentile is not None:
        if volume_percentile < settings.min_volume_percentile:
            return LayerResult(1, "Market Health", LayerStatus.FAIL,
                                f"Volume 24h berada di percentile ke-{volume_percentile:.0f} dari histori coin "
                                f"ini sendiri (< {settings.min_volume_percentile}) - sedang jauh lebih sepi "
                                f"dari kondisi normalnya", data)

    if spread_pct > settings.max_spread_pct:
        return LayerResult(1, "Market Health", LayerStatus.FAIL,
                            f"Spread {spread_pct:.3f}% > {settings.max_spread_pct}%", data)

    if current_atr_pct < settings.min_atr_pct:
        return LayerResult(1, "Market Health", LayerStatus.FAIL,
                            f"ATR 1H {current_atr_pct:.3f}% < {settings.min_atr_pct}% (volatilitas terlalu rendah, floor absolut)", data)

    if settings.enable_relative_atr_filter and atr_percentile is not None:
        if atr_percentile < settings.min_atr_percentile:
            return LayerResult(1, "Market Health", LayerStatus.FAIL,
                                f"ATR 1H berada di percentile ke-{atr_percentile:.0f} dari histori coin ini "
                                f"sendiri (< {settings.min_atr_percentile}) - volatilitas sedang tertekan "
                                f"dibanding kondisi normalnya", data)

    if change_1h_pct > settings.max_1h_pump_dump_pct:
        return LayerResult(1, "Market Health", LayerStatus.FAIL,
                            f"Perubahan 1H {change_1h_pct:.2f}% > {settings.max_1h_pump_dump_pct}% (pump/dump ekstrem)", data)

    if settings.enable_funding_filter and funding_rate_pct is not None:
        if abs(funding_rate_pct) > settings.max_funding_rate_abs_pct:
            return LayerResult(1, "Market Health", LayerStatus.FAIL,
                                f"Funding rate {funding_rate_pct:+.3f}% melewati batas "
                                f"±{settings.max_funding_rate_abs_pct}% (crowded trade, risiko squeeze/liquidation)",
                                data)

    return LayerResult(1, "Market Health", LayerStatus.PASS, "Coin layak ditradingkan", data)
