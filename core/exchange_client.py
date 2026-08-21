"""
core/exchange_client.py
------------------------
Wrapper tipis di atas ccxt.mexc untuk mengambil data market (OHLCV, orderbook, ticker)
dari MEXC Futures (USDT-M Perpetual). Bot ini sengaja dikunci hanya untuk MEXC:
- semua threshold di layers/ ditala untuk karakteristik market MEXC futures
- symbol HARUS pakai notasi perpetual ccxt: "BASE/QUOTE:QUOTE", contoh "BTC/USDT:USDT"
Semua layer memanggil fungsi di sini, bukan ccxt langsung, supaya mudah di-mock saat testing.
"""

import time

import ccxt
import pandas as pd
from loguru import logger

from config import settings

# Error transient yang layak di-retry (network blip, rate limit sesaat, exchange maintenance
# singkat). Error di LUAR daftar ini (mis. BadSymbol, AuthenticationError, InvalidOrder)
# dianggap definitif - retry tidak akan membantu, jadi langsung dilempar ulang tanpa delay.
RETRYABLE_ERRORS = (
    ccxt.NetworkError,
    ccxt.RequestTimeout,
    ccxt.ExchangeNotAvailable,
    ccxt.DDoSProtection,
    ccxt.RateLimitExceeded,
)


class ExchangeClient:
    def __init__(self):
        # Hardcoded ke MEXC futures (USDT-M perpetual). defaultType="swap" memastikan
        # ccxt query ke market futures, bukan spot, walau symbol tanpa suffix ":USDT".
        # Bot ini HANYA memanggil endpoint publik (OHLCV, ticker, order book), jadi
        # sengaja TIDAK mengirim apiKey/secret sama sekali - tidak dibutuhkan dan
        # menghindari kesalahpahaman bahwa bot ini butuh akses ke akun exchange.
        self.exchange = ccxt.mexc({
            "enableRateLimit": True,
            "options": {"defaultType": settings.exchange_market_type},
        })
        # PENTING: MEXC tidak punya sandbox/testnet di ccxt (ex.urls["test"] kosong).
        # Memanggil set_sandbox_mode(True) di sini akan raise TypeError saat startup,
        # jadi sengaja TIDAK dipanggil sama sekali. Warning ke user sudah dilakukan
        # di config.validate_settings() kalau EXCHANGE_SANDBOX=true di .env.
        self._markets_loaded = False
        # Cache OI terakhir per symbol {symbol: (timestamp, oi_usd)} supaya bisa hitung
        # % perubahan OI antar-scan tanpa perlu endpoint historical OI (yang belum tentu
        # didukung ccxt untuk MEXC).
        self._oi_history: dict = {}

    @staticmethod
    def _call_with_retry(fn, *args, **kwargs):
        """
        Panggil fn(*args, **kwargs) dengan retry + exponential backoff untuk error transient
        (lihat RETRYABLE_ERRORS). Error non-transient langsung dilempar ulang tanpa retry.
        Percobaan & delay diatur lewat settings.api_max_retries / api_retry_base_delay_sec.
        """
        last_error = None
        for attempt in range(settings.api_max_retries + 1):
            try:
                return fn(*args, **kwargs)
            except RETRYABLE_ERRORS as e:
                last_error = e
                if attempt >= settings.api_max_retries:
                    break
                delay = settings.api_retry_base_delay_sec * (2 ** attempt)
                logger.warning(f"Retryable error ({type(e).__name__}: {e}), "
                                f"percobaan {attempt + 1}/{settings.api_max_retries}, retry dalam {delay:.1f}s")
                time.sleep(delay)
        raise last_error

    @staticmethod
    def normalize_symbol(symbol: str) -> str:
        """
        Terima symbol format spot ("BTC/USDT") maupun futures ("BTC/USDT:USDT"),
        selalu kembalikan format perpetual ccxt yang valid untuk MEXC futures.
        """
        symbol = symbol.strip().upper()
        if ":" in symbol:
            return symbol
        if "/" not in symbol:
            raise ValueError(f"Symbol '{symbol}' tidak valid, gunakan format 'BASE/QUOTE' atau 'BASE/QUOTE:QUOTE'")
        _, quote = symbol.split("/", 1)
        return f"{symbol}:{quote}"

    def load_markets(self):
        if not self._markets_loaded:
            self._call_with_retry(self.exchange.load_markets)
            self._markets_loaded = True

    def fetch_ohlcv_df(self, symbol: str, timeframe: str, limit: int = 300) -> pd.DataFrame:
        """
        Ambil candlestick data dan kembalikan sebagai DataFrame pandas.

        Reliabilitas:
        - Dibungkus retry-with-backoff untuk error transient (lihat _call_with_retry).
        - Kalau settings.drop_unclosed_candle=True (default), candle TERAKHIR yang masih
          "live"/belum closed dibuang. Tanpa ini, layer yang membaca candle terakhir
          (terutama Layer 7 entry trigger yang mendeteksi pattern candlestick) bisa
          menghasilkan sinyal yang "repaint" - berubah-ubah tiap scan karena candle
          tersebut masih terus terbentuk. Untuk kompensasi, fetch limit+2 candle lalu
          trim ke `limit` supaya jumlah candle CLOSED yang dikembalikan tetap konsisten.
        """
        fetch_limit = limit + 2 if settings.drop_unclosed_candle else limit
        raw = self._call_with_retry(self.exchange.fetch_ohlcv, symbol, timeframe=timeframe, limit=fetch_limit)
        df = pd.DataFrame(raw, columns=["timestamp", "open", "high", "low", "close", "volume"])
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
        df.set_index("timestamp", inplace=True)

        if settings.drop_unclosed_candle and len(df) > 0:
            try:
                tf_seconds = self.exchange.parse_timeframe(timeframe)
                last_candle_open_ms = int(df.index[-1].timestamp() * 1000)
                last_candle_close_ms = last_candle_open_ms + tf_seconds * 1000
                now_ms = int(time.time() * 1000)
                if now_ms < last_candle_close_ms:
                    df = df.iloc[:-1]
            except Exception as e:
                logger.warning(f"[{symbol}] Gagal cek status closed candle ({e}), candle terakhir tetap dipakai apa adanya")

        return df.tail(limit)

    def fetch_ohlcv_since_df(self, symbol: str, timeframe: str, since_ms: int, limit: int = 1000) -> pd.DataFrame:
        """
        Ambil candlestick sejak timestamp tertentu (ms epoch) sampai sekarang - dipakai oleh
        outcome_tracker.py (mengecek pergerakan harga sejak signal digenerate) dan backtest.py
        (mengambil data historis untuk simulasi). Sama seperti fetch_ohlcv_df tapi pakai
        parameter `since` alih-alih hanya limit candle terakhir.
        """
        raw = self._call_with_retry(self.exchange.fetch_ohlcv, symbol, timeframe=timeframe,
                                     since=since_ms, limit=limit)
        df = pd.DataFrame(raw, columns=["timestamp", "open", "high", "low", "close", "volume"])
        if df.empty:
            return df
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
        df.set_index("timestamp", inplace=True)
        return df

    def fetch_ticker(self, symbol: str) -> dict:
        return self._call_with_retry(self.exchange.fetch_ticker, symbol)

    def fetch_order_book_spread_pct(self, symbol: str) -> float:
        """Spread bid-ask dalam persen relatif terhadap mid price."""
        ob = self._call_with_retry(self.exchange.fetch_order_book, symbol, limit=5)
        if not ob["bids"] or not ob["asks"]:
            return float("inf")
        best_bid = ob["bids"][0][0]
        best_ask = ob["asks"][0][0]
        mid = (best_bid + best_ask) / 2
        if mid == 0:
            return float("inf")
        return (best_ask - best_bid) / mid * 100

    def fetch_funding_rate_pct(self, symbol: str):
        """
        Ambil funding rate saat ini (dalam persen, mis. 0.35 = 0.35% per interval funding)
        via endpoint publik ccxt fetch_funding_rate(). Return None kalau tidak didukung/gagal
        setelah retry (dipakai untuk graceful degradation - filter funding di Layer 1
        di-skip, bukan crash, kalau data tidak tersedia).
        """
        try:
            fr = self._call_with_retry(self.exchange.fetch_funding_rate, symbol)
            rate = fr.get("fundingRate")
            if rate is None:
                return None
            return float(rate) * 100
        except Exception as e:
            logger.warning(f"[{symbol}] Funding rate tidak tersedia ({e}), filter funding di-skip untuk symbol ini")
            return None

    def fetch_open_interest_change_pct(self, symbol: str, ticker: dict = None):
        """
        Ambil Open Interest saat ini, lalu bandingkan dengan nilai OI symbol ini yang
        tercatat pada scan sebelumnya untuk menghasilkan % perubahan OI antar-scan (proxy
        sederhana untuk "apakah posisi baru sedang dibangun", bukan OI history resmi).
        Return None kalau data tidak tersedia atau ini scan pertama untuk symbol tsb
        (belum ada baseline pembanding).

        PENTING: ccxt.mexc TIDAK meng-implementasikan fetch_open_interest() (selalu raise
        NotSupported untuk MEXC per ccxt 4.5.x), jadi endpoint itu sengaja TIDAK dipakai.
        Sebagai gantinya, OI diambil dari field `holdVol` yang dikembalikan MEXC pada
        endpoint publik GET /api/v1/contract/ticker (satuan: jumlah kontrak/lot yang masih
        open, bukan nilai notional USD). ccxt menaruh response mentah tsb di ticker["info"],
        jadi ticker yang sudah difetch di safe_fetch_all() bisa dipakai ulang di sini tanpa
        request tambahan - kalau tidak diberikan, baru fetch_ticker() sendiri sebagai fallback.
        """
        try:
            ticker = ticker if ticker is not None else self._call_with_retry(self.exchange.fetch_ticker, symbol)
            hold_vol = ticker.get("info", {}).get("holdVol")
            if hold_vol is None:
                return None
            oi_value = float(hold_vol)
        except Exception as e:
            logger.warning(f"[{symbol}] Open interest (holdVol) tidak tersedia ({e}), OI confirmation di-skip untuk symbol ini")
            return None

        now = time.time()
        prev = self._oi_history.get(symbol)
        self._oi_history[symbol] = (now, oi_value)

        if prev is None or prev[1] == 0:
            return None

        prev_ts, prev_oi = prev
        change_pct = (oi_value - prev_oi) / prev_oi * 100
        return change_pct

    def fetch_top_volume_symbols(self, top_n: int = 20, quote: str = "USDT") -> list:
        """
        Ambil top-N symbol MEXC Futures (USDT-M perpetual) berdasarkan volume transaksi
        24 jam terakhir (quoteVolume), pakai endpoint publik fetch_tickers() - tidak butuh
        API key/secret. Dipakai untuk watchlist dinamis (lihat core/watchlist.py).
        """
        self.load_markets()
        tickers = self._call_with_retry(self.exchange.fetch_tickers)

        candidates = []
        for symbol, market in self.exchange.markets.items():
            # hanya USDT-M perpetual swap, quote currency sesuai parameter
            if not market.get("swap") or market.get("quote") != quote:
                continue
            ticker = tickers.get(symbol)
            if not ticker:
                continue
            vol = ticker.get("quoteVolume")
            if vol is None:
                # fallback: hitung dari baseVolume * last price kalau quoteVolume kosong
                base_vol = ticker.get("baseVolume")
                last = ticker.get("last")
                vol = base_vol * last if base_vol and last else None
            if vol is None:
                continue
            candidates.append((symbol, vol))

        candidates.sort(key=lambda x: x[1], reverse=True)
        return [sym for sym, _ in candidates[:top_n]]

    def safe_fetch_all(self, symbol: str) -> dict:
        """
        Ambil semua data mentah yang dibutuhkan seluruh layer dalam satu panggilan,
        supaya pipeline tidak berulang kali hit API untuk symbol yang sama.
        """
        try:
            symbol = self.normalize_symbol(symbol)
            ticker = self.fetch_ticker(symbol)
            data = {
                "symbol": symbol,
                "ticker": ticker,
                "spread_pct": self.fetch_order_book_spread_pct(symbol),
                "ohlcv_htf": self.fetch_ohlcv_df(symbol, settings.tf_htf, limit=300),
                "ohlcv_mtf": self.fetch_ohlcv_df(symbol, settings.tf_mtf, limit=300),
                # None kalau tidak didukung/gagal - masing-masing layer wajib menangani None
                # secara graceful (skip check), bukan menganggapnya sebagai kegagalan fetch total.
                "funding_rate_pct": self.fetch_funding_rate_pct(symbol),
                # Teruskan ticker yang sudah difetch di atas supaya holdVol (proxy OI)
                # diambil dari response yang sama, tanpa request tambahan ke exchange.
                "oi_change_pct": self.fetch_open_interest_change_pct(symbol, ticker=ticker),
            }
            return data
        except Exception as e:
            logger.error(f"[{symbol}] Gagal fetch data exchange: {e}")
            return {}


exchange_client = ExchangeClient()
