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
from core.metrics import scan_metrics
from indicators.technical import classify_price_oi_direction

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
        # Cache OI + price terakhir per symbol {symbol: (timestamp, oi_value, price)} supaya
        # bisa hitung % perubahan OI *dan* price antar-scan tanpa perlu endpoint historical OI
        # (yang belum tentu didukung ccxt untuk MEXC) - price ikut disimpan supaya OI bisa
        # dibaca sebagai price x OI directional model (lihat fetch_oi_price_model()), bukan
        # cuma "OI naik/turun" sendirian.
        self._oi_history: dict = {}

    @staticmethod
    def _call_with_retry(fn, *args, **kwargs):
        """
        Panggil fn(*args, **kwargs) dengan retry + exponential backoff untuk error transient
        (lihat RETRYABLE_ERRORS). Error non-transient langsung dilempar ulang tanpa retry.
        Percobaan & delay diatur lewat settings.api_max_retries / api_retry_base_delay_sec.

        Instrumentasi (Phase 8 - lihat core/metrics.py & ringkasan_perbaikan.md P2):
        SETIAP percobaan (termasuk retry) dihitung sebagai satu request_count, supaya
        watchlist_stress_test.py bisa membandingkan beban API riil di watchlist 100 vs
        150 vs 200 symbol - bukan cuma menduga dari elapsed time saja.
        """
        last_error = None
        for attempt in range(settings.api_max_retries + 1):
            scan_metrics.record_request()
            try:
                return fn(*args, **kwargs)
            except RETRYABLE_ERRORS as e:
                last_error = e
                if attempt >= settings.api_max_retries:
                    scan_metrics.record_failed_request()
                    break
                scan_metrics.record_retry()
                delay = settings.api_retry_base_delay_sec * (2 ** attempt)
                logger.warning(f"Retryable error ({type(e).__name__}: {e}), "
                                f"percobaan {attempt + 1}/{settings.api_max_retries}, retry dalam {delay:.1f}s")
                time.sleep(delay)
            except Exception:
                # Error non-transient (BadSymbol, AuthenticationError, dst) - tidak di-retry,
                # tapi tetap dihitung sebagai failed_requests sebelum dilempar ulang, supaya
                # metrics tidak diam-diam kehilangan kegagalan definitif ini.
                scan_metrics.record_failed_request()
                raise
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

    def fetch_ohlcv_range_df(self, symbol: str, timeframe: str, since_ms: int,
                              until_ms: int = None, page_limit: int = 1000) -> pd.DataFrame:
        """
        Sama seperti fetch_ohlcv_since_df, tapi dengan PAGINASI - dipakai backtest.py untuk
        mengambil data timeframe granular (mis. 15m) mencakup periode yang panjang (mis. 60
        hari = ~5760 candle 15m), yang biasanya melebihi limit satu request exchange.
        Berhenti begitu tidak ada candle baru lagi, atau sudah melewati until_ms/waktu sekarang.
        """
        until_ms = until_ms if until_ms is not None else int(time.time() * 1000)
        tf_ms = self.exchange.parse_timeframe(timeframe) * 1000
        all_rows = []
        cursor = since_ms
        seen_ts = set()

        while cursor < until_ms:
            raw = self._call_with_retry(self.exchange.fetch_ohlcv, symbol, timeframe=timeframe,
                                         since=cursor, limit=page_limit)
            if not raw:
                break
            new_rows = [r for r in raw if r[0] not in seen_ts]
            if not new_rows:
                break
            all_rows.extend(new_rows)
            for r in new_rows:
                seen_ts.add(r[0])
            last_ts = raw[-1][0]
            next_cursor = last_ts + tf_ms
            if next_cursor <= cursor:  # exchange tidak maju, hindari infinite loop
                break
            cursor = next_cursor
            if len(raw) < page_limit:
                break  # halaman terakhir (exchange mengembalikan lebih sedikit dari limit)

        df = pd.DataFrame(all_rows, columns=["timestamp", "open", "high", "low", "close", "volume"])
        if df.empty:
            return df
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
        df.set_index("timestamp", inplace=True)
        df = df[~df.index.duplicated(keep="last")].sort_index()
        return df[df.index <= pd.to_datetime(until_ms, unit="ms", utc=True)]

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

    def fetch_oi_price_model(self, symbol: str, ticker: dict = None):
        """
        Ambil Open Interest + price SAAT INI, lalu bandingkan KEDUANYA dengan nilai OI/price
        symbol ini yang tercatat pada scan sebelumnya, untuk menghasilkan price x OI
        directional model (lihat indicators.technical.classify_price_oi_direction) - BUKAN
        cuma "% perubahan OI" mentah seperti skema lama (yang menganggap "OI naik" selalu
        bullish tanpa peduli arah harga, padahal OI naik + price turun justru SHORT_BUILDUP,
        bukan konfirmasi bullish). Return None kalau data tidak tersedia atau ini scan
        pertama untuk symbol tsb (belum ada baseline pembanding).

        PENTING: ccxt.mexc TIDAK meng-implementasikan fetch_open_interest() (selalu raise
        NotSupported untuk MEXC per ccxt 4.5.x), jadi endpoint itu sengaja TIDAK dipakai.
        Sebagai gantinya, OI diambil dari field `holdVol` yang dikembalikan MEXC pada
        endpoint publik GET /api/v1/contract/ticker (satuan: jumlah kontrak/lot yang masih
        open, bukan nilai notional USD), dan price diambil dari `ticker["last"]` (endpoint
        yang sama, tanpa request tambahan). ccxt menaruh response mentah holdVol tsb di
        ticker["info"], jadi ticker yang sudah difetch di safe_fetch_all() bisa dipakai ulang
        di sini - kalau tidak diberikan, baru fetch_ticker() sendiri sebagai fallback.

        Return dict {"oi_change_pct": float, "price_change_pct": float, "classification": str}
        atau None (lihat di atas).
        """
        try:
            ticker = ticker if ticker is not None else self._call_with_retry(self.exchange.fetch_ticker, symbol)
            hold_vol = ticker.get("info", {}).get("holdVol")
            last_price = ticker.get("last")
            if hold_vol is None or last_price is None:
                return None
            oi_value = float(hold_vol)
            price_value = float(last_price)
        except Exception as e:
            logger.warning(f"[{symbol}] Open interest/price (holdVol/last) tidak tersedia ({e}), "
                            f"OI directional model di-skip untuk symbol ini")
            return None

        now = time.time()
        prev = self._oi_history.get(symbol)
        self._oi_history[symbol] = (now, oi_value, price_value)

        if prev is None or len(prev) < 3 or prev[1] == 0 or prev[2] == 0:
            return None

        _, prev_oi, prev_price = prev
        oi_change_pct = (oi_value - prev_oi) / prev_oi * 100
        price_change_pct = (price_value - prev_price) / prev_price * 100
        classification = classify_price_oi_direction(
            price_change_pct, oi_change_pct,
            settings.oi_price_min_change_pct, settings.oi_confirmation_min_change_pct,
        )
        return {
            "oi_change_pct": round(oi_change_pct, 4),
            "price_change_pct": round(price_change_pct, 4),
            "classification": classification,
        }

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
                # ohlcv_structure (default 1h) -> Layer 1/3/4 (ATR, struktur, SMC).
                # ohlcv_entry     (default 15m)-> Layer 5/6/7/8 (momentum, volume, trigger, entry price).
                # Lihat config.py untuk kenapa keduanya dipisah (P1 - Pisahkan Timeframe).
                "ohlcv_structure": self.fetch_ohlcv_df(symbol, settings.tf_structure, limit=300),
                "ohlcv_entry": self.fetch_ohlcv_df(symbol, settings.tf_entry, limit=300),
                # None kalau tidak didukung/gagal - masing-masing layer wajib menangani None
                # secara graceful (skip check), bukan menganggapnya sebagai kegagalan fetch total.
                "funding_rate_pct": self.fetch_funding_rate_pct(symbol),
                # Teruskan ticker yang sudah difetch di atas supaya holdVol (proxy OI)
                # diambil dari response yang sama, tanpa request tambahan ke exchange.
                # None kalau data OI/price pembanding belum ada (lihat fetch_oi_price_model),
                # dict {"oi_change_pct", "price_change_pct", "classification"} kalau berhasil.
                "oi_price_model": self.fetch_oi_price_model(symbol, ticker=ticker),
            }
            return data
        except Exception as e:
            logger.error(f"[{symbol}] Gagal fetch data exchange: {e}")
            return {}


exchange_client = ExchangeClient()
