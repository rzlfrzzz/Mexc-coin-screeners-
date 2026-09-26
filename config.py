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

    tf_htf: str = os.getenv("TF_HTF", "4h")   # Layer 2 (trend besar) & Layer 0 (BTC regime)

    # ---------- Pemisahan timeframe V2 (lihat ringkasan_perbaikan.md P1) ----------
    # Sebelumnya satu timeframe (TF_MTF, default 1h) dipakai untuk SEMUANYA: struktur,
    # SMC, momentum, DAN volume/entry trigger - padahal secara konsep struktur besar
    # (BOS/CHoCH/OB/FVG) semestinya dibaca di timeframe lebih besar daripada trigger
    # entry aktual. V2 memisahkannya jadi dua:
    #   TF_STRUCTURE (default 1h) -> Layer 1 (ATR/pump-dump), Layer 3 (struktur/BOS/CHoCH),
    #                                 Layer 4 (SMC: OB/FVG/liquidity sweep)
    #   TF_ENTRY     (default 15m)-> Layer 5 (momentum), Layer 6 (volume), Layer 7 (entry
    #                                 trigger/displacement), dan harga "entry" aktual di Layer 8
    # TF_MTF (nama lama) tetap dibaca sebagai FALLBACK kalau TF_STRUCTURE belum diset di
    # .env, supaya .env lama tidak langsung rusak setelah upgrade - tapi TF_STRUCTURE adalah
    # nama yang seharusnya dipakai mulai sekarang.
    tf_structure: str = os.getenv("TF_STRUCTURE", os.getenv("TF_MTF", "1h"))
    tf_entry: str = os.getenv("TF_ENTRY", "15m")

    # ---------- Outcome evaluation (trade_outcome.py) ----------
    # Timeframe untuk MENGECEK apakah SL/TP tersentuh - HARUS lebih granular daripada
    # tf_structure (timeframe sinyal), supaya urutan kejadian SL-vs-TP di dalam satu candle
    # sinyal bisa dibedakan (lihat ringkasan_perbaikan.md P0: "Signal = 15M, Outcome
    # checking = 1M/5M", di sini digeneralisasi relatif terhadap tf_structure).
    # Default DIUBAH ke 5m (sebelumnya 15m = sama dengan tf_entry, jadi tidak benar2
    # granular - lihat instruksi perbaikan "Ubah TF_OUTCOME ke 5M supaya mengurangi ambiguity
    # TP vs SL"). Dengan 5m, urutan SL-vs-TP di dalam satu candle sinyal (1H) ATAU di dalam
    # satu candle entry (15M) bisa dibedakan lebih presisi - candle 15M yang menyentuh SL & TP
    # sekaligus dulunya tetap ambigu (evaluate_trade_path butuh candle LEBIH KECIL dari
    # candle sinyal DAN dari candle entry, bukan cuma lebih kecil dari salah satunya).
    tf_outcome: str = os.getenv("TF_OUTCOME", "5m")
    # Kalau SL & TP tersentuh di CANDLE tf_outcome YANG SAMA (masih ambigu walau sudah
    # granular), urutan mana yang diasumsikan menang:
    # "conservative_sl_first" (default, tidak melebih-lebihkan win rate) atau
    # "conservative_tp_first" (upper-bound optimistis, dipakai hanya untuk sensitivity check).
    outcome_same_bar_policy: str = os.getenv("OUTCOME_SAME_BAR_POLICY", "conservative_sl_first")

    scan_interval_seconds: int = _get_int("SCAN_INTERVAL_SECONDS", 300)

    # Supabase
    supabase_url: str = os.getenv("SUPABASE_URL", "")
    supabase_key: str = os.getenv("SUPABASE_KEY", "")
    supabase_signals_table: str = os.getenv("SUPABASE_SIGNALS_TABLE", "signals")
    supabase_layer_log_table: str = os.getenv("SUPABASE_LAYER_LOG_TABLE", "layer_logs")

    # Telegram
    telegram_bot_token: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    telegram_chat_id: str = os.getenv("TELEGRAM_CHAT_ID", "")

    # ---------- Reliabilitas: retry & candle-closed check ----------
    # Retry-with-backoff untuk panggilan API yang gagal sesaat (network blip, rate limit,
    # exchange sedang maintenance singkat) - HANYA untuk error transient, bukan error
    # definitif (symbol salah, dsb) supaya tidak menunda-nunda kegagalan yang memang pasti.
    api_max_retries: int = _get_int("API_MAX_RETRIES", 3)
    api_retry_base_delay_sec: float = _get_float("API_RETRY_BASE_DELAY_SEC", 0.5)

    # Kalau true, candle terakhir yang masih "live"/belum closed dibuang dari hasil fetch
    # OHLCV, supaya sinyal (terutama Layer 7 entry trigger) tidak berubah-ubah di antara
    # scan karena candle yang dievaluasi masih terus terbentuk (repaint risk).
    drop_unclosed_candle: bool = _get_bool("DROP_UNCLOSED_CANDLE", True)

    # ---------- Layer 1 - Threshold relatif per-coin (percentile historis) ----------
    # Selain floor absolut (min_volume_24h_usd, min_atr_pct di atas), tambahkan cek relatif
    # terhadap distribusi historis coin itu SENDIRI - supaya threshold tidak "satu ukuran
    # untuk semua" (BTC vs low-cap altcoin punya karakteristik volume/ATR sangat berbeda).
    enable_relative_atr_filter: bool = _get_bool("ENABLE_RELATIVE_ATR_FILTER", True)
    min_atr_percentile: float = _get_float("MIN_ATR_PERCENTILE", 20)
    enable_relative_volume_filter: bool = _get_bool("ENABLE_RELATIVE_VOLUME_FILTER", True)
    min_volume_percentile: float = _get_float("MIN_VOLUME_PERCENTILE", 25)
    # Minimum jumlah data point historis yang dibutuhkan sebelum cek relatif diaktifkan;
    # kalau data belum cukup (symbol baru listing dsb), cek relatif di-skip, hanya floor
    # absolut yang berlaku.
    percentile_min_history: int = _get_int("PERCENTILE_MIN_HISTORY", 100)

    # ---------- Outcome tracking otomatis ----------
    enable_outcome_tracking: bool = _get_bool("ENABLE_OUTCOME_TRACKING", True)
    # Interval tracking outcome dalam DETIK (default 1 jam - tidak perlu secepat scan_interval)
    outcome_tracking_interval_seconds: int = _get_int("OUTCOME_TRACKING_INTERVAL_SECONDS", 3600)
    # Berapa lama (jam) sinyal yang belum tersentuh SL/TP dianggap "expired"/stale dan
    # ditandai OPEN_EXPIRED daripada terus dipantau tanpa batas waktu
    outcome_max_age_hours: float = _get_float("OUTCOME_MAX_AGE_HOURS", 72)

    # Layer 1 thresholds
    min_volume_24h_usd: float = _get_float("MIN_VOLUME_24H_USD", 5_000_000)
    max_spread_pct: float = _get_float("MAX_SPREAD_PCT", 0.15)
    min_atr_pct: float = _get_float("MIN_ATR_PCT", 0.2)
    max_1h_pump_dump_pct: float = _get_float("MAX_1H_PUMP_DUMP_PCT", 15)

    # Layer 1b - Funding Rate (khusus futures, bagian dari Market Health)
    # Funding rate ekstrem (baik positif maupun negatif) menandakan crowded trade
    # (satu sisi terlalu ramai leverage) -> risiko liquidation cascade / squeeze tinggi.
    enable_funding_filter: bool = _get_bool("ENABLE_FUNDING_FILTER", True)
    max_funding_rate_abs_pct: float = _get_float("MAX_FUNDING_RATE_ABS_PCT", 0.75)

    # Layer 0 - BTC Market Regime
    # Cek trend 4H BTC sebelum evaluasi altcoin manapun. Kalau BTC sedang bearish jelas,
    # altcoin dalam mode LONG akan di-skip (dan sebaliknya) - altcoin umumnya sangat
    # berkorelasi dengan BTC, trading berlawanan arah BTC risikonya jauh lebih tinggi.
    enable_btc_regime_filter: bool = _get_bool("ENABLE_BTC_REGIME_FILTER", True)
    btc_regime_symbol: str = os.getenv("BTC_REGIME_SYMBOL", "BTC/USDT:USDT")
    # Refresh cache regime BTC tiap N menit (4H candle tidak perlu dicek ulang tiap symbol/tiap scan)
    btc_regime_refresh_minutes: float = _get_float("BTC_REGIME_REFRESH_MINUTES", 30)

    # Layer 5/6 thresholds
    volume_spike_multiplier: float = _get_float("VOLUME_SPIKE_MULTIPLIER", 1.5)
    rsi_long_min: float = _get_float("RSI_LONG_MIN", 55)
    rsi_short_max: float = _get_float("RSI_SHORT_MAX", 45)

    # Layer 3 - Adaptive swing/fractal lookback (menggantikan konstanta tetap N=3)
    # Lookback fractal disesuaikan dengan volatilitas (ATR%) coin itu sendiri:
    # coin ber-ATR tinggi (noisy) pakai lookback lebih besar supaya swing tidak palsu,
    # coin ber-ATR rendah (calm) pakai lookback lebih kecil supaya tetap sensitif.
    swing_lookback_min: int = _get_int("SWING_LOOKBACK_MIN", 2)
    swing_lookback_default: int = _get_int("SWING_LOOKBACK_DEFAULT", 3)
    swing_lookback_max: int = _get_int("SWING_LOOKBACK_MAX", 6)
    swing_lookback_low_atr_pct: float = _get_float("SWING_LOOKBACK_LOW_ATR_PCT", 0.5)
    swing_lookback_high_atr_pct: float = _get_float("SWING_LOOKBACK_HIGH_ATR_PCT", 1.5)

    # ---------- Layer 4 - Smart Money (SMC lifecycle) ----------
    # Lihat ringkasan_perbaikan.md P1.5 - FVG/OB/Liquidity Sweep sekarang punya STATE,
    # bukan cuma "ketemu atau tidak".

    # FVG dianggap EXPIRED (tidak lagi dipakai sebagai sinyal) kalau sudah berumur lebih
    # dari sekian candle timeframe structure sejak terbentuk tanpa pernah tersentuh sama
    # sekali secara berarti (masih FRESH) - gap yang terlalu lama biasanya sudah tidak
    # relevan lagi secara psikologis pasar.
    fvg_max_age_bars: int = _get_int("FVG_MAX_AGE_BARS", 60)
    # FVG dianggap MITIGATED begitu candle manapun sejak terbentuk sudah menembus zona
    # sejauh >= persentase ini (0-1) dari tinggi gap - bukan cuma "wick nyentuh sedikit".
    fvg_mitigation_fill_ratio: float = _get_float("FVG_MITIGATION_FILL_RATIO", 0.5)

    # Order Block hanya dianggap valid ("confirmed") kalau benar2 diikuti displacement yang
    # cukup kuat (net move beberapa candle setelah OB >= sekian x ATR saat itu) DAN diikuti
    # BOS searah dalam jumlah candle tertentu - bukan cuma "body candle besar = OB"
    # (lihat P1.5: "OB candle -> displacement -> BOS", bukan "big candle = OB").
    ob_displacement_min_atr_mult: float = _get_float("OB_DISPLACEMENT_MIN_ATR_MULT", 1.2)
    ob_displacement_lookforward_bars: int = _get_int("OB_DISPLACEMENT_LOOKFORWARD_BARS", 3)
    ob_bos_confirm_max_bars: int = _get_int("OB_BOS_CONFIRM_MAX_BARS", 30)

    # Toleransi (dalam % harga) untuk menganggap dua swing high/low sebagai "equal
    # high/low" - liquidity pool yang lebih kuat daripada satu swing tunggal, karena
    # ada 2+ level stop yang menumpuk di harga yang hampir sama.
    equal_level_tolerance_pct: float = _get_float("EQUAL_LEVEL_TOLERANCE_PCT", 0.15)

    # ---------- Layer 7 - Entry Trigger: displacement (lihat ringkasan_perbaikan.md P1.8) ----------
    # Trigger (engulfing/breakout) HARUS didukung displacement nyata di timeframe entry,
    # bukan cuma pola candle formasi tanpa tenaga di belakangnya - skema "15M displacement
    # -> 15M close confirmation -> ENTRY", bukan cuma salah satu.
    entry_displacement_min_atr_mult: float = _get_float("ENTRY_DISPLACEMENT_MIN_ATR_MULT", 0.8)
    entry_displacement_lookback_bars: int = _get_int("ENTRY_DISPLACEMENT_LOOKBACK_BARS", 3)

    # ---------- Layer 7 - Entry Trigger: context-awareness (perbaikan) ----------
    # Trigger 15M (displacement + engulfing/breakout di atas) TIDAK LAGI cukup berdiri
    # sendiri - HARUS punya hubungan nyata dengan area SMC 1H (Order Block/FVG/Liquidity
    # Sweep dari Layer 4), supaya "context-aware": trigger yang kebetulan lolos displacement
    # & pattern candle di 15M tapi terjadi di tengah ruang kosong (bukan reaksi dari OB/FVG
    # atau lanjutan liquidity sweep 1H) tidak lagi dianggap valid.
    # `trigger_context_lookback_bars` = jumlah candle ENTRY (15M) tepat SEBELUM window
    # displacement (lihat entry_displacement_lookback_bars) yang dicek APAKAH range harganya
    # (wick, bukan cuma close) bersinggungan dengan OB/FVG valid searah - window "pendekatan"
    # sebelum harga displacement menjauh dari zona itu.
    trigger_context_lookback_bars: int = _get_int("TRIGGER_CONTEXT_LOOKBACK_BARS", 6)
    # Untuk konteks liquidity sweep (bukan OB/FVG): sweep 1H dianggap masih "relevan" sebagai
    # alasan trigger 15M ini kalau umurnya (dalam jumlah candle STRUCTURE/1H sejak sweep
    # terbentuk sampai candle structure terakhir) tidak lebih dari ini - sweep yang sudah
    # terlalu lama tidak lagi dianggap penyebab pergerakan 15M sekarang.
    trigger_context_sweep_max_age_bars: int = _get_int("TRIGGER_CONTEXT_SWEEP_MAX_AGE_BARS", 12)

    # ---------- Layer 8 - Risk Management V2 (lihat ringkasan_perbaikan.md P1.9/P1.10) ----------
    # SL = level invalidasi struktural (liquidity sweep low/high, atau swing structure
    # terakhir dari Layer 3 kalau tidak ada sweep aktif) ± buffer dalam satuan ATR -
    # bukan lagi persentase tetap kecil, supaya buffer otomatis menyesuaikan volatilitas
    # coin (coin volatile butuh buffer lebih lebar dari wick noise-nya sendiri).
    sl_atr_buffer_mult: float = _get_float("SL_ATR_BUFFER_MULT", 0.15)  # disarankan 0.1-0.25
    # TP1 = target struktural terdekat (swing berlawanan arah terdekat) kalau ada & RR-nya
    # cukup; TP1 ditolak (setup gagal) kalau target struktural itu ADA tapi RR-nya di bawah
    # minimum ini (resistance/support terlalu dekat untuk ditradingkan secara masuk akal).
    # Kalau TIDAK ada target struktural terlihat, fallback ke baseline RR 1:1/1:2/1:3.
    min_rr: float = _get_float("MIN_RR", 1.5)

    # Layer 8 - Risk Management sanity check.
    # Batas atas jarak SL dari entry (dalam % dari harga entry). Kalau swing
    # reference yang ditemukan terlalu jauh dari harga sekarang (misal karena
    # swing high/low lama sebelum crash/pump besar), risk yang dihasilkan bisa
    # tidak masuk akal (bahkan bikin TP jadi harga negatif untuk SHORT). Sinyal
    # dengan risk > max_risk_pct dari entry akan di-FAIL, bukan diteruskan.
    max_risk_pct: float = _get_float("MAX_RISK_PCT", 20.0)

    # Layer 6b - Open Interest: price x OI directional model (soft/scoring, bukan hard
    # block - data OI via ccxt/MEXC tidak selalu tersedia/stabil, jadi tidak dijadikan syarat
    # wajib). Diperbaiki dari skema lama ("OI naik = bullish" tanpa peduli arah harga) jadi
    # matrix price x OI standar (lihat indicators.technical.classify_price_oi_direction):
    #   price naik + OI naik   -> LONG_BUILDUP     (posisi long baru, breakout genuine)
    #   price turun + OI naik  -> SHORT_BUILDUP    (posisi short baru, breakdown genuine)
    #   price naik + OI turun  -> SHORT_COVERING   (short tutup posisi, bukan minat beli baru)
    #   price turun + OI turun -> LONG_LIQUIDATION (long dipaksa keluar, bukan tekanan jual baru)
    # Kedua threshold di bawah ini HARUS terlewati (bukan cuma salah satu) sebelum pergerakan
    # price/OI dianggap cukup berarti untuk diklasifikasi - di bawah itu dianggap NEUTRAL/noise.
    oi_confirmation_min_change_pct: float = _get_float("OI_CONFIRMATION_MIN_CHANGE_PCT", 2.0)
    oi_price_min_change_pct: float = _get_float("OI_PRICE_MIN_CHANGE_PCT", 0.15)

    # Layer 9
    score_min_to_send: int = _get_int("SCORE_MIN_TO_SEND", 70)

    # ---------- Layer 9 - Scoring V2 (lihat ringkasan_perbaikan.md P1.11 / Phase 7) ----------
    # REBALANCE (Phase 7): skema lama memberi bobot besar ke "trend_aligned" (20) dan "bos" (15)
    # padahal Layer 2 (trend) & Layer 3 (structure) adalah HARD GATE di pipeline.py - begitu
    # pipeline sampai ke Layer 9, kedua status itu SUDAH PASTI PASS, jadi bobot itu jadi bonus
    # tetap yang tidak pernah membedakan sinyal satu sama lain (dead weight). Skema baru
    # mengikuti tabel confluence score persis di ringkasan_perbaikan.md P1.11:
    #   4H regime            10
    #   1H structure         15
    #   Liquidity event      15   <- BARU: sebelumnya liquidity sweep (Layer 4) terdeteksi &
    #                                dipakai Layer 8 (SL) tapi TIDAK PERNAH ikut skor sama sekali
    #   SMC location         15
    #   15M trigger          15
    #   Volume               10
    #   Momentum              5
    #   OI                    5
    #   Risk quality         10   <- BARU: kualitas RR & sumber SL (structural vs fallback)
    #   ------------------------
    #   TOTAL                100
    # "atr_high" & "not_near_resistance" (skema lama) DIHAPUS dari skor - sesuai instruksi
    # eksplisit P1.11 ("aku sengaja mengurangi ketergantungan pada RSI/MACD/ATR"), dan karena
    # "not_near_resistance" pada dasarnya sudah redundan dengan validasi MIN_RR di Layer 8
    # (RR minimum sudah menolak setup yang resistance-nya terlalu dekat, tidak perlu skor
    # tambahan untuk hal yang sama). Setiap kategori sekarang digradasi (bukan cuma 0/penuh)
    # berdasarkan data mentah yang sudah tersedia di masing-masing layer - lihat
    # layers/layer9_scoring.py untuk detail gradasinya per kategori.
    scoring_weights: dict = field(default_factory=lambda: {
        "regime_4h": 10,
        "structure_1h": 15,
        "liquidity_event": 15,
        "smc_location": 15,
        "entry_trigger_15m": 15,
        "volume": 10,
        "momentum": 5,
        "oi": 5,
        "risk_quality": 10,
    })

    # ---------- Layer 9 - Correlation Awareness (Phase 7) ----------
    # CATATAN: item "Correlation awareness" di ringkasan_perbaikan.md Phase 7 checklist tidak
    # punya spesifikasi detail (tidak ada section tersendiri seperti P1.1-P1.11) - jadi ini
    # interpretasi saya: karena mayoritas altcoin di watchlist sangat berkorelasi dengan BTC
    # (lihat layer0_btc_regime.py), banyak sinyal SEARAH yang muncul BERSAMAAN dalam satu siklus
    # scan kemungkinan besar bukan N edge independen, melainkan 1 pergerakan market yang
    # kebetulan lolos syarat teknikal di banyak coin sekaligus ("BTC-beta", bukan alpha
    # per-coin). Ini TIDAK memblokir/mengurangi skor (skor tetap confluence score per-setup,
    # bukan win probability - sesuai filosofi P1.11) - hanya ditambahkan sebagai METADATA
    # kesadaran risiko di signal & Telegram (lihat core/correlation_tracker.py), supaya user
    # tidak salah membaca "5 sinyal LONG A+ sekaligus" sebagai 5x independent conviction.
    correlation_warn_threshold: int = _get_int("CORRELATION_WARN_THRESHOLD", 3)

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
