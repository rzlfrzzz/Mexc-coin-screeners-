"""
config.py
---------
Memuat semua konfigurasi dari environment variables (.env).
Semua modul lain mengambil konfigurasi dari sini, bukan dari os.environ langsung,
supaya default value & validasi terpusat di satu tempat.
"""

import os
from dataclasses import dataclass, field
from dotenv import load_dotenv

load_dotenv()


def _get_bool(key: str, default: bool = False) -> bool:
    val = os.getenv(key)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


def _get_float(key: str, default: float) -> float:
    val = os.getenv(key)
    return float(val) if val not in (None, "") else default


def _get_int(key: str, default: int) -> int:
    val = os.getenv(key)
    return int(val) if val not in (None, "") else default


def _get_list(key: str, default: list) -> list:
    val = os.getenv(key)
    if not val:
        return default
    return [x.strip() for x in val.split(",") if x.strip()]


@dataclass
class Settings:
    # Exchange - bot ini HANYA untuk MEXC Futures (USDT-M Perpetual), lewat ccxt.
    # exchange_id sengaja di-hardcode (bukan dari .env) supaya tidak ada yang tidak sengaja
    # mengarahkan bot ini ke exchange lain - semua layer/threshold di bot ini di-tuning untuk MEXC.
    exchange_id: str = "mexc"
    exchange_market_type: str = "swap"  # "swap" = USDT-M perpetual futures di ccxt
    # Tidak ada exchange_api_key/exchange_api_secret di sini secara sengaja:
    # bot ini hanya memanggil endpoint publik (OHLCV, ticker, order book),
    # jadi tidak butuh API key/secret exchange sama sekali.
    # CATATAN: MEXC tidak menyediakan sandbox/testnet di ccxt (ex.urls['test'] kosong).
    # Kalau di-set True, bot akan crash saat start. Dibiarkan False permanen, opsi di .env diabaikan
    # dan hanya dipakai untuk menampilkan warning ke user.
    exchange_sandbox_requested: bool = _get_bool("EXCHANGE_SANDBOX", False)

    # Format symbol WAJIB pakai notasi perpetual ccxt: "BASE/QUOTE:QUOTE", contoh "BTC/USDT:USDT"
    watchlist: list = field(default_factory=lambda: _get_list(
        "WATCHLIST_SYMBOLS", ["BTC/USDT:USDT", "ETH/USDT:USDT"]
    ))

    # Mode watchlist:
    # - "static"  -> selalu pakai WATCHLIST_SYMBOLS apa adanya
    # - "dynamic" -> auto top-N symbol by volume 24h (quoteVolume), refresh berkala.
    #                WATCHLIST_SYMBOLS tetap dipakai sebagai fallback awal/kalau fetch gagal.
    watchlist_mode: str = os.getenv("WATCHLIST_MODE", "static")
    watchlist_top_n: int = _get_int("WATCHLIST_TOP_N", 20)
    watchlist_refresh_hours: float = _get_float("WATCHLIST_REFRESH_HOURS", 12)
    watchlist_quote: str = os.getenv("WATCHLIST_QUOTE", "USDT")

    tf_htf: str = os.getenv("TF_HTF", "4h")   # untuk Layer 2 (trend besar)
    tf_mtf: str = os.getenv("TF_MTF", "1h")   # untuk Layer 3-7 (struktur, SMC, momentum, volume)

    scan_interval_seconds: int = _get_int("SCAN_INTERVAL_SECONDS", 300)

    # Supabase
    supabase_url: str = os.getenv("SUPABASE_URL", "")
    supabase_key: str = os.getenv("SUPABASE_KEY", "")
    supabase_signals_table: str = os.getenv("SUPABASE_SIGNALS_TABLE", "signals")
    supabase_layer_log_table: str = os.getenv("SUPABASE_LAYER_LOG_TABLE", "layer_logs")

    # Telegram
    telegram_bot_token: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    telegram_chat_id: str = os.getenv("TELEGRAM_CHAT_ID", "")

    # Layer 1 thresholds
    min_volume_24h_usd: float = _get_float("MIN_VOLUME_24H_USD", 5_000_000)
    max_spread_pct: float = _get_float("MAX_SPREAD_PCT", 0.15)
    min_atr_pct: float = _get_float("MIN_ATR_PCT", 0.2)
    max_1h_pump_dump_pct: float = _get_float("MAX_1H_PUMP_DUMP_PCT", 15)

    # Layer 5/6 thresholds
    volume_spike_multiplier: float = _get_float("VOLUME_SPIKE_MULTIPLIER", 1.5)
    rsi_long_min: float = _get_float("RSI_LONG_MIN", 55)
    rsi_short_max: float = _get_float("RSI_SHORT_MAX", 45)

    # Layer 9
    score_min_to_send: int = _get_int("SCORE_MIN_TO_SEND", 70)

    # Scoring weights - bisa diubah user tanpa mengubah source code layer lain
    scoring_weights: dict = field(default_factory=lambda: {
        "trend_aligned": 25,
        "bos": 20,
        "order_block": 15,
        "fvg": 10,
        "volume_spike": 10,
        "rsi": 5,
        "macd": 5,
        "atr_high": 5,
        "not_near_resistance": 5,
    })

    log_level: str = os.getenv("LOG_LEVEL", "INFO")


settings = Settings()


def validate_settings() -> list:
    """Return a list of missing/invalid required settings. Empty list = OK."""
    problems = []
    if not settings.supabase_url:
        problems.append("SUPABASE_URL belum diisi")
    if not settings.supabase_key:
        problems.append("SUPABASE_KEY belum diisi")
    if not settings.telegram_bot_token:
        problems.append("TELEGRAM_BOT_TOKEN belum diisi")
    if not settings.telegram_chat_id:
        problems.append("TELEGRAM_CHAT_ID belum diisi")
    if not settings.watchlist:
        problems.append("WATCHLIST_SYMBOLS kosong")
    for sym in settings.watchlist:
        if ":" not in sym:
            problems.append(
                f"WATCHLIST_SYMBOLS '{sym}' bukan format perpetual futures ccxt yang valid "
                f"(harus 'BASE/QUOTE:QUOTE', contoh 'BTC/USDT:USDT')"
            )
    if settings.watchlist_mode not in ("static", "dynamic"):
        problems.append(
            f"WATCHLIST_MODE '{settings.watchlist_mode}' tidak valid, harus 'static' atau 'dynamic'"
        )
    if settings.watchlist_mode == "dynamic" and settings.watchlist_top_n <= 0:
        problems.append("WATCHLIST_TOP_N harus > 0 kalau WATCHLIST_MODE=dynamic")
    if settings.exchange_sandbox_requested:
        problems.append(
            "EXCHANGE_SANDBOX=true diabaikan: MEXC Futures tidak punya sandbox/testnet di ccxt, "
            "bot tetap jalan ke live market MEXC"
        )
    return problems
