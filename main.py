"""
main.py
--------
Entry point bot. Menjalankan scanning market secara berkala (interval dari .env)
menggunakan APScheduler, lalu memanggil pipeline 9-layer untuk tiap symbol di watchlist.

Jalankan:
    python main.py
"""

import sys
import time
from loguru import logger
from apscheduler.schedulers.blocking import BlockingScheduler

from config import settings, validate_settings
from pipeline import scan_watchlist
from core.watchlist import watchlist_manager
from outcome_tracker import track_outcomes


def configure_logging():
    logger.remove()
    logger.add(sys.stdout, level=settings.log_level,
                format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level}</level> | {message}")
    logger.add("logs/bot_{time:YYYY-MM-DD}.log", rotation="1 day", retention="14 days", level="DEBUG")


def job():
    logger.info(f"=== Mulai scan watchlist ({watchlist_manager.last_refresh_info()}) ===")
    start = time.time()
    signals = scan_watchlist()
    elapsed = time.time() - start
    symbols = watchlist_manager.current_symbols()
    logger.info(f"=== Scan selesai dalam {elapsed:.1f}s untuk {symbols}, {len(signals)} signal terkirim ===")


def outcome_tracking_job():
    logger.info("=== Mulai outcome tracking ===")
    try:
        track_outcomes()
    except Exception as e:
        logger.exception(f"Error saat outcome tracking: {e}")


def main():
    configure_logging()

    problems = validate_settings()
    if problems:
        logger.warning("Konfigurasi belum lengkap:")
        for p in problems:
            logger.warning(f"  - {p}")
        logger.warning("Bot tetap jalan, tapi fitur terkait mungkin tidak berfungsi penuh. "
                        "Lengkapi file .env sesuai .env.example.")

    if settings.watchlist_mode == "dynamic":
        logger.info(
            f"Watchlist mode: dynamic (top {settings.watchlist_top_n} by volume 24h, "
            f"refresh tiap {settings.watchlist_refresh_hours}h). Mengambil watchlist awal..."
        )
        watchlist_manager.refresh(force=True)
    else:
        logger.info(f"Watchlist mode: static. Watchlist: {settings.watchlist}")

    logger.info(f"Bot mulai. Interval scan: {settings.scan_interval_seconds}s.")

    # jalankan sekali di awal, lalu terjadwal berkala
    job()

    scheduler = BlockingScheduler(timezone="UTC")
    scheduler.add_job(job, "interval", seconds=settings.scan_interval_seconds,
                       id="scan_watchlist", max_instances=1, coalesce=True)

    if settings.enable_outcome_tracking:
        scheduler.add_job(outcome_tracking_job, "interval", seconds=settings.outcome_tracking_interval_seconds,
                           id="outcome_tracking", max_instances=1, coalesce=True)
        logger.info(f"Outcome tracking aktif, interval {settings.outcome_tracking_interval_seconds}s.")
    else:
        logger.info("Outcome tracking dimatikan (ENABLE_OUTCOME_TRACKING=false).")

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot dihentikan oleh user.")


if __name__ == "__main__":
    main()
