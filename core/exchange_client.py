"""
core/exchange_client.py
------------------------
Wrapper tipis di atas ccxt.mexc untuk mengambil data market (OHLCV, orderbook, ticker)
dari MEXC Futures (USDT-M Perpetual). Bot ini sengaja dikunci hanya untuk MEXC:
- semua threshold di layers/ ditala untuk karakteristik market MEXC futures
- symbol HARUS pakai notasi perpetual ccxt: "BASE/QUOTE:QUOTE", contoh "BTC/USDT:USDT"
Semua layer memanggil fungsi di sini, bukan ccxt langsung, supaya mudah di-mock saat testing.
"""

import ccxt
import pandas as pd
from loguru import logger

from config import settings


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
            self.exchange.load_markets()
            self._markets_loaded = True

    def fetch_ohlcv_df(self, symbol: str, timeframe: str, limit: int = 300) -> pd.DataFrame:
        """Ambil candlestick data dan kembalikan sebagai DataFrame pandas."""
        raw = self.exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
        df = pd.DataFrame(raw, columns=["timestamp", "open", "high", "low", "close", "volume"])
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
        df.set_index("timestamp", inplace=True)
        return df

    def fetch_ticker(self, symbol: str) -> dict:
        return self.exchange.fetch_ticker(symbol)

    def fetch_order_book_spread_pct(self, symbol: str) -> float:
        """Spread bid-ask dalam persen relatif terhadap mid price."""
        ob = self.exchange.fetch_order_book(symbol, limit=5)
        if not ob["bids"] or not ob["asks"]:
            return float("inf")
        best_bid = ob["bids"][0][0]
        best_ask = ob["asks"][0][0]
        mid = (best_bid + best_ask) / 2
        if mid == 0:
            return float("inf")
        return (best_ask - best_bid) / mid * 100

    def fetch_top_volume_symbols(self, top_n: int = 20, quote: str = "USDT") -> list:
        """
        Ambil top-N symbol MEXC Futures (USDT-M perpetual) berdasarkan volume transaksi
        24 jam terakhir (quoteVolume), pakai endpoint publik fetch_tickers() - tidak butuh
        API key/secret. Dipakai untuk watchlist dinamis (lihat core/watchlist.py).
        """
        self.load_markets()
        tickers = self.exchange.fetch_tickers()

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
            data = {
                "symbol": symbol,
                "ticker": self.fetch_ticker(symbol),
                "spread_pct": self.fetch_order_book_spread_pct(symbol),
                "ohlcv_htf": self.fetch_ohlcv_df(symbol, settings.tf_htf, limit=300),
                "ohlcv_mtf": self.fetch_ohlcv_df(symbol, settings.tf_mtf, limit=300),
            }
            return data
        except Exception as e:
            logger.error(f"[{symbol}] Gagal fetch data exchange: {e}")
            return {}


exchange_client = ExchangeClient()
