"""
core/telegram_notifier.py
--------------------------
Mengirim signal terformat ke Telegram (bot token + chat/channel/group id dari .env).
"""

import asyncio
from loguru import logger
from telegram import Bot
from telegram.constants import ParseMode

from config import settings
from models import TradeSignal, Direction

STAR_MAP = {5: "⭐⭐⭐⭐⭐", 4: "⭐⭐⭐⭐", 3: "⭐⭐⭐", 2: "⭐⭐", 1: "⭐", 0: ""}


def _check_mark(passed: bool) -> str:
    return "✅" if passed else "❌"


def _fmt_pct(val) -> str:
    if val is None:
        return "N/A"
    return f"{val:+.1f}%"


def _base_symbol(symbol: str) -> str:
    """Ambil kode base currency dari symbol ccxt, mis. 'ETH/USDT:USDT' -> 'ETH'.

    Khusus untuk ticker kategori Stock di MEXC (mis. 'TESLASTOCK/USDT:USDT',
    'SAMSUNGSTOCK/USDT:USDT'), nama asli exchange punya suffix 'STOCK' yang
    tidak dikenali oleh bot pencatat signal eksternal. Suffix ini HANYA
    dihapus untuk tampilan di pesan Telegram (field 'Pair:') -- tidak
    mengubah signal.symbol asli yang tetap dipakai untuk fetch candle,
    cek open-signal di Supabase, dsb.
    """
    base = symbol.split("/")[0].strip().upper()
    if base.endswith("STOCK") and len(base) > len("STOCK"):
        base = base[: -len("STOCK")]
    return base


OI_LABELS = {
    "LONG_BUILDUP": "Long Buildup",
    "SHORT_BUILDUP": "Short Buildup",
    "SHORT_COVERING": "Short Covering",
    "LONG_LIQUIDATION": "Long Liquidation",
    "NEUTRAL": "Netral",
}


def _oi_label(classification) -> str:
    if classification is None:
        return "Data tidak tersedia"
    return OI_LABELS.get(classification, classification)


DIVIDER = "▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬"

DISCLAIMER = (
    "🤖 <i>Signal ini dibuat otomatis oleh bot berdasarkan analisis teknikal. "
    "Bukan merupakan nasihat keuangan. Selalu lakukan analisis &amp; riset mandiri "
    "(DYOR) serta kelola risiko sebelum mengambil keputusan trading.</i>"
)


def format_signal_message(signal: TradeSignal) -> str:
    direction_emoji = "🟢" if signal.direction == Direction.LONG else "🔴"
    stars = STAR_MAP.get(signal.score.stars, "") if signal.score else ""
    snap = signal.indicators_snapshot

    # NOTE: baris "Pair:", "Entry :", "Stoploss  :", "TP1/2/3   :", dan
    # "Risk/Reward:" SENGAJA dibiarkan sebagai teks polos (tanpa tag HTML)
    # karena field-field ini dibaca/di-parse oleh bot lain. Hanya elemen
    # dekoratif (divider, judul section, disclaimer) yang dipercantik.
    lines = [
        DIVIDER,
        f"{direction_emoji} <b>{signal.direction.value} SIGNAL</b>",
        DIVIDER,
        "",
        f"Pair: ${_base_symbol(signal.symbol)}",
        f"Score: {signal.score.total}/100 {stars}",
        "",
        "📊 <b>Analisa Teknikal</b>",
        f"{_check_mark(snap.get('trend_htf_aligned'))} Trend {settings.tf_htf.upper():<8}: {snap.get('trend_label', '-')}",
        f"{_check_mark(snap.get('bos'))} BOS           : {snap.get('bos_label', '-')}",
        f"{_check_mark(snap.get('order_block_valid'))} Order Block   : {'Valid' if snap.get('order_block_valid') else 'Tidak ada'}",
        f"{_check_mark(snap.get('fvg_valid'))} FVG           : {'Valid' if snap.get('fvg_valid') else 'Tidak ada'}",
        f"{_check_mark(snap.get('volume_spike'))} Volume        : {snap.get('volume_pct_of_avg', 0):.0f}% dari rata-rata",
        f"{_check_mark(snap.get('rsi_ok'))} RSI           : {snap.get('rsi', 0):.0f}",
        # ATR & "tidak dekat resistance" sengaja hanya INFORMASIONAL mulai Phase 7 - tidak
        # lagi ikut dihitung ke skor Layer 9 (lihat config.py::scoring_weights, P1.11: sengaja
        # mengurangi ketergantungan pada ATR; "not_near_resistance" sudah redundan dengan
        # validasi MIN_RR di Layer 8).
        f"{_check_mark(snap.get('atr_high'))} ATR (info)    : {'Tinggi' if snap.get('atr_high') else 'Rendah'}",
        f"{_check_mark(snap.get('btc_regime_aligned'))} BTC Regime    : {snap.get('btc_direction', '-') or '-'}",
        f"{_check_mark(snap.get('oi_confirmation'))} OI            : {_oi_label(snap.get('oi_direction'))} "
        f"(OI {_fmt_pct(snap.get('oi_change_pct'))}, Price {_fmt_pct(snap.get('oi_price_change_pct'))})",
        "",
    ]

    if signal.score and signal.score.breakdown:
        b = signal.score.breakdown
        lines += [
            "🧮 <b>Score Breakdown</b>",
            f"4H Regime      : {b.get('regime_4h', 0):.0f}/{settings.scoring_weights['regime_4h']}",
            f"1H Structure   : {b.get('structure_1h', 0):.0f}/{settings.scoring_weights['structure_1h']}",
            f"Liquidity Event: {b.get('liquidity_event', 0):.0f}/{settings.scoring_weights['liquidity_event']}",
            f"SMC Location   : {b.get('smc_location', 0):.0f}/{settings.scoring_weights['smc_location']}",
            f"15M Trigger    : {b.get('entry_trigger_15m', 0):.0f}/{settings.scoring_weights['entry_trigger_15m']}",
            f"Volume         : {b.get('volume', 0):.0f}/{settings.scoring_weights['volume']}",
            f"Momentum       : {b.get('momentum', 0):.0f}/{settings.scoring_weights['momentum']}",
            f"OI             : {b.get('oi', 0):.0f}/{settings.scoring_weights['oi']}",
            f"Risk Quality   : {b.get('risk_quality', 0):.0f}/{settings.scoring_weights['risk_quality']}",
            "",
        ]

    if signal.soft_fail_layers:
        lines += [f"ℹ️ Catatan: {', '.join(signal.soft_fail_layers)} tidak lolos penuh (skor dikurangi, bukan diblokir)", ""]

    # Correlation awareness (Phase 7, lihat core/correlation_tracker.py) - metadata kesadaran
    # risiko, BUKAN bagian skor. Hanya ditampilkan kalau melewati threshold (tidak menambah
    # noise di setiap sinyal normal).
    corr = getattr(signal, "correlation_meta", None) or {}
    if corr.get("high_correlation_risk"):
        n = corr.get("same_direction_signals_this_scan", 0)
        others = ", ".join(corr.get("same_direction_symbols_this_scan", []))
        lines += [
            f"⚠️ <b>Correlation Awareness</b>: {n} sinyal {signal.direction.value} lain sudah lolos "
            f"di scan yang sama ({others}). Kemungkinan BTC-beta/market-wide move, bukan edge "
            f"independen per-coin - pertimbangkan total exposure, jangan dihitung sebagai N sinyal "
            f"yang saling independen.",
            "",
        ]

    if signal.risk_plan:
        rp = signal.risk_plan
        # Hitung RR ke TP2 langsung dari angka entry/sl/tp2 yang sebenarnya,
        # bukan label statis - supaya kalau logika perhitungan TP di layer 8
        # berubah suatu saat, pesan yang tampil ke user tetap konsisten dan
        # tidak pernah menampilkan RR yang salah/menyesatkan.
        risk = abs(rp.entry - rp.sl)
        reward_tp2 = abs(rp.tp2 - rp.entry)
        rr_tp2 = (reward_tp2 / risk) if risk > 0 else 0
        lines += [
            "💰 <b>Trade Setup</b>",
            f"Entry : {rp.entry:g}",
            f"Stoploss  : {rp.sl:g}",
            "",
            f"TP1   : {rp.tp1:g}",
            f"TP2   : {rp.tp2:g}",
            "",
            f"Risk/Reward: 1:{rr_tp2:.1f}",
            "",
            DIVIDER,
        ]

    if signal.score and signal.score.grade == "B":
        lines += ["", "⚠️ Note: B-setup, quality masih di bawah A. Gunakan size lebih kecil."]

    lines += ["", DISCLAIMER]

    return "\n".join(lines)


async def _send_async(text: str):
    bot = Bot(token=settings.telegram_bot_token)
    await bot.send_message(chat_id=settings.telegram_chat_id, text=text, parse_mode=ParseMode.HTML)


def send_signal(signal: TradeSignal) -> bool:
    if not settings.telegram_bot_token or not settings.telegram_chat_id:
        logger.warning("Telegram belum dikonfigurasi, signal tidak dikirim.")
        return False
    text = format_signal_message(signal)
    try:
        asyncio.run(_send_async(text))
        logger.info(f"Signal {signal.symbol} {signal.direction.value} terkirim ke Telegram.")
        return True
    except Exception as e:
        logger.error(f"Gagal mengirim signal ke Telegram: {e}")
        return False


def send_plain_text(text: str) -> bool:
    if not settings.telegram_bot_token or not settings.telegram_chat_id:
        return False
    try:
        asyncio.run(_send_async(text))
        return True
    except Exception as e:
        logger.error(f"Gagal mengirim pesan ke Telegram: {e}")
        return False
