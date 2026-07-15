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


def format_signal_message(signal: TradeSignal) -> str:
    direction_emoji = "🟢" if signal.direction == Direction.LONG else "🔴"
    stars = STAR_MAP.get(signal.score.stars, "") if signal.score else ""
    snap = signal.indicators_snapshot

    lines = [
        f"{direction_emoji} {signal.direction.value} {signal.symbol}",
        "",
        f"Score: {signal.score.total}/100 {stars}",
        "",
        f"{_check_mark(snap.get('trend_htf_aligned'))} Trend {settings.tf_htf.upper():<8}: {snap.get('trend_label', '-')}",
        f"{_check_mark(snap.get('bos'))} BOS           : {snap.get('bos_label', '-')}",
        f"{_check_mark(snap.get('order_block_valid'))} Order Block   : {'Valid' if snap.get('order_block_valid') else 'Tidak ada'}",
        f"{_check_mark(snap.get('fvg_valid'))} FVG           : {'Valid' if snap.get('fvg_valid') else 'Tidak ada'}",
        f"{_check_mark(snap.get('volume_spike'))} Volume        : {snap.get('volume_pct_of_avg', 0):.0f}% dari rata-rata",
        f"{_check_mark(snap.get('rsi_ok'))} RSI           : {snap.get('rsi', 0):.0f}",
        f"{_check_mark(snap.get('atr_high'))} ATR           : {'Tinggi' if snap.get('atr_high') else 'Rendah'}",
        f"{_check_mark(snap.get('not_near_resistance'))} Tidak dekat R : {'Aman' if snap.get('not_near_resistance') else 'Dekat resistance'}",
        "",
    ]

    if signal.risk_plan:
        rp = signal.risk_plan
        lines += [
            f"Entry      : {rp.entry:g}",
            f"Stoplos    : {rp.sl:g}",
            "",
            f"TP1   : {rp.tp1:g}",
            f"TP2   : {rp.tp2:g}",
            f"TP3   : {rp.tp3:g}",
            "",
            "Risk/Reward: 1:3",
        ]

    if signal.score and signal.score.grade == "B":
        lines.append("\n⚠️ Note: B-setup, quality masih di bawah A. Gunakan size lebih kecil.")

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
