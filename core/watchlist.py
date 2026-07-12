"""
core/watchlist.py
-------------------
Kelola daftar symbol yang di-scan bot.

Dua mode (lihat WATCHLIST_MODE di .env):
- "static"  : selalu pakai WATCHLIST_SYMBOLS apa adanya, tidak pernah berubah sendiri.
- "dynamic" : otomatis top-N symbol MEXC Futures berdasarkan volume 24 jam
              (quoteVolume), di-refresh berkala tiap WATCHLIST_REFRESH_HOURS jam
              (bukan tiap scan - supaya tidak boros API call fetch_tickers()).
              WATCHLIST_SYMBOLS tetap dipakai sebagai watchlist awal sebelum
              refresh pertama berhasil, dan sebagai fallback kalau fetch gagal
              (misal exchange down) - watchlist lama tetap dipertahankan.
"""

import time
from loguru import logger

from config import settings
from core.exchange_client import exchange_client


class WatchlistManager:
    def __init__(self):
        # fallback awal = watchlist statis dari .env, dipakai sampai refresh pertama sukses
        self._symbols = list(settings.watchlist)
        self._last_refresh_ts = 0.0

    def _is_stale(self) -> bool:
        if self._last_refresh_ts == 0.0:
            return True
        elapsed_hours = (time.time() - self._last_refresh_ts) / 3600
        return elapsed_hours >= settings.watchlist_refresh_hours

    def refresh(self, force: bool = False) -> None:
        """Refresh watchlist dari exchange kalau mode dynamic dan sudah waktunya (atau force=True)."""
        if settings.watchlist_mode != "dynamic":
            return
        if not force and not self._is_stale():
            return

        try:
            top_symbols = exchange_client.fetch_top_volume_symbols(
                top_n=settings.watchlist_top_n,
                quote=settings.watchlist_quote,
            )
            if top_symbols:
                old = self._symbols
                self._symbols = top_symbols
                self._last_refresh_ts = time.time()
                logger.info(
                    f"Watchlist dinamis di-refresh (top {len(top_symbols)} by volume 24h): "
                    f"{top_symbols}"
                )
                added = sorted(set(top_symbols) - set(old))
                removed = sorted(set(old) - set(top_symbols))
                if added:
                    logger.info(f"  + masuk watchlist: {added}")
                if removed:
                    logger.info(f"  - keluar watchlist: {removed}")
            else:
                logger.warning(
                    "Refresh watchlist dinamis gagal (hasil kosong), watchlist lama dipertahankan: "
                    f"{self._symbols}"
                )
        except Exception as e:
            logger.error(
                f"Error saat refresh watchlist dinamis: {e}. Watchlist lama dipertahankan: "
                f"{self._symbols}"
            )

    def get_symbols(self) -> list:
        """Symbol yang dipakai untuk scan saat ini. Auto-refresh dulu kalau mode dynamic & sudah stale."""
        self.refresh()
        return list(self._symbols)

    def current_symbols(self) -> list:
        """Symbol watchlist yang sedang aktif TANPA memicu pengecekan refresh.
        Dipakai untuk logging setelah scan_watchlist() sudah refresh, supaya tidak
        double-check staleness dalam satu siklus scan yang sama."""
        return list(self._symbols)

    def last_refresh_info(self) -> str:
        if settings.watchlist_mode != "dynamic":
            return "mode static"
        if self._last_refresh_ts == 0.0:
            return "belum pernah refresh"
        elapsed_min = (time.time() - self._last_refresh_ts) / 60
        return f"terakhir refresh {elapsed_min:.0f} menit lalu"


watchlist_manager = WatchlistManager()
