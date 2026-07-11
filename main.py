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


def configure_logging():
    logger.remove()
    logger.add(sys.stdout, level=settings.log_level,
                format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level}</level> | {message}")
    logger.add("logs/bot_{time:YYYY-MM-DD}.log", rotation="1 day", retention="14 days", level="DEBUG")


def job():
    logger.info(f"=== Mulai scan watchlist: {settings.watchlist} ===")
    start = time.time()
    signals = scan_watchlist()
    elapsed = time.time() - start
    logger.info(f"=== Scan selesai dalam {elapsed:.1f}s, {len(signals)} signal terkirim ===")


def main():
    configure_logging()

    problems = validate_settings()
    if problems:
        logger.warning("Konfigurasi belum lengkap:")
        for p in problems:
            logger.warning(f"  - {p}")
        logger.warning("Bot tetap jalan, tapi fitur terkait mungkin tidak berfungsi penuh. "
                        "Lengkapi file .env sesuai .env.example.")

    logger.info(f"Bot mulai. Interval scan: {settings.scan_interval_seconds}s. "
                f"Watchlist: {settings.watchlist}")

    # jalankan sekali di awal, lalu terjadwal berkala
    job()

    scheduler = BlockingScheduler(timezone="UTC")
    scheduler.add_job(job, "interval", seconds=settings.scan_interval_seconds,
                       id="scan_watchlist", max_instances=1, coalesce=True)
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot dihentikan oleh user.")


if __name__ == "__main__":
    main()
