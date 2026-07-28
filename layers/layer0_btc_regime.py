"""
Layer 0 - BTC Market Regime
-----------------------------
Cek trend 4H BTC (pakai logika EMA200 identik dengan Layer 2) SEBELUM sinyal altcoin
manapun dianggap valid. Alasan: mayoritas altcoin di MEXC futures sangat berkorelasi
dengan pergerakan BTC - LONG altcoin saat BTC jelas bearish (dan sebaliknya) secara
historis jauh lebih rawan gagal karena "market beta" menyeret altcoin ikut arah BTC,
terlepas dari seberapa bagus setup teknikal altcoin itu sendiri.

Perilaku:
- Kalau symbol yang di-scan adalah BTC itu sendiri -> selalu dianggap align (tidak
  relevan membandingkan BTC dengan dirinya sendiri).
- Kalau regime BTC NONE (sideways/transisi, tidak jelas) -> tidak memblokir apa pun
  (netral), supaya filter ini tidak menambah beban false-negative saat market memang
  sedang tidak punya arah jelas.
- Kalau regime BTC jelas (LONG/SHORT) dan berlawanan dengan direction altcoin yang
  ditentukan Layer 2 -> FAIL (hard block, sesuai settings.enable_btc_regime_filter).

Regime BTC di-cache dalam proses (in-memory) dan di-refresh berkala (default 30 menit,
lihat settings.btc_regime_refresh_minutes) supaya tidak fetch OHLCV 4H BTC berulang
kali untuk setiap symbol di watchlist pada satu siklus scan yang sama.
"""

import time

from loguru import logger

from config import settings
from models import LayerResult, LayerStatus, Direction
from layers.layer2_trend import compute_trend_direction


class BtcRegimeCache:
    def __init__(self):
        self._direction: str = Direction.NONE.value
        self._data: dict = {}
        self._last_refresh_ts: float = 0.0

    def _is_stale(self) -> bool:
        if self._last_refresh_ts == 0.0:
            return True
        elapsed_minutes = (time.time() - self._last_refresh_ts) / 60
        return elapsed_minutes >= settings.btc_regime_refresh_minutes

    def get(self, exchange_client) -> dict:
        """Kembalikan data regime BTC saat ini, refresh dari exchange kalau sudah stale."""
        if not self._is_stale():
            return self._data

        try:
            df_htf = exchange_client.fetch_ohlcv_df(settings.btc_regime_symbol, settings.tf_htf, limit=300)
            data = compute_trend_direction(df_htf)
            self._data = data
            self._direction = data.get("trend_direction", Direction.NONE.value)
            self._last_refresh_ts = time.time()
            logger.info(f"[BTC Regime] Refresh: {self._direction} "
                        f"(price={data.get('price')}, ema200={data.get('ema200')})")
        except Exception as e:
            logger.error(f"[BTC Regime] Gagal fetch/refresh regime BTC: {e}. "
                          f"Regime lama dipertahankan: {self._direction}")
        return self._data


btc_regime_cache = BtcRegimeCache()


def run(raw_data: dict, symbol_direction: Direction, exchange_client) -> LayerResult:
    symbol = raw_data["symbol"]

    if not settings.enable_btc_regime_filter:
        return LayerResult(0, "BTC Market Regime", LayerStatus.SKIPPED,
                            "Filter BTC regime dimatikan (ENABLE_BTC_REGIME_FILTER=false)", {})

    if symbol == settings.btc_regime_symbol:
        return LayerResult(0, "BTC Market Regime", LayerStatus.PASS,
                            "Symbol yang di-scan adalah BTC itu sendiri, filter tidak relevan", {})

    btc_data = btc_regime_cache.get(exchange_client)
    btc_direction = btc_data.get("trend_direction", Direction.NONE.value)

    data = {
        "btc_direction": btc_direction,
        "symbol_direction": symbol_direction.value,
        "btc_price": btc_data.get("price"),
        "btc_ema200": btc_data.get("ema200"),
    }

    if btc_direction == Direction.NONE.value:
        return LayerResult(0, "BTC Market Regime", LayerStatus.PASS,
                            "Regime BTC 4H tidak jelas (sideways) - tidak memblokir", data)

    if btc_direction != symbol_direction.value:
        return LayerResult(0, "BTC Market Regime", LayerStatus.FAIL,
                            f"BTC regime {btc_direction}, berlawanan dengan direction {symbol} "
                            f"({symbol_direction.value}) - skip untuk kurangi risiko trading "
                            f"melawan arah pasar", data)

    return LayerResult(0, "BTC Market Regime", LayerStatus.PASS,
                        f"BTC regime {btc_direction} searah dengan direction {symbol}", data)
