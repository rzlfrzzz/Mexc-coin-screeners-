"""
Layer 6 - Volume
-----------------
Breakout/entry harus didukung volume, dihitung di timeframe ENTRY (default 15m,
settings.tf_entry) - bukan timeframe structure - karena volume spike yang relevan untuk
konfirmasi entry adalah volume candle entry itu sendiri (lihat ringkasan_perbaikan.md
P1.7):
Volume candle entry saat ini > SMA20(Volume, timeframe entry) x 1.5
Breakout tanpa volume dianggap tidak valid -> skip.
"""

from models import LayerResult, LayerStatus
from config import settings
from indicators.technical import sma


def run(raw_data: dict) -> LayerResult:
    df_entry = raw_data["ohlcv_entry"]
    volume = df_entry["volume"]

    vol_sma20 = sma(volume, 20)
    current_vol = float(volume.iloc[-1])
    avg_vol = float(vol_sma20.iloc[-1]) if not vol_sma20.empty else 0.0

    pct_of_avg = (current_vol / avg_vol * 100) if avg_vol > 0 else 0.0
    volume_spike = current_vol > avg_vol * settings.volume_spike_multiplier

    data = {
        "current_volume": current_vol,
        "sma20_volume": avg_vol,
        "volume_pct_of_avg": pct_of_avg,
        "volume_spike": volume_spike,
    }

    if not volume_spike:
        return LayerResult(6, "Volume", LayerStatus.FAIL,
                            f"Volume {pct_of_avg:.0f}% dari SMA20 (butuh > {settings.volume_spike_multiplier * 100:.0f}%)",
                            data)

    return LayerResult(6, "Volume", LayerStatus.PASS,
                        f"Volume spike {pct_of_avg:.0f}% dari rata-rata SMA20", data)
