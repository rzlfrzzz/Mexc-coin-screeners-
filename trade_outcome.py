"""
trade_outcome.py
-----------------
Fungsi evaluasi outcome trade TERPUSAT - satu-satunya definisi "apa hasil trade ini",
dipakai oleh outcome_tracker.py (live) DAN backtest.py (historis). Sebelum modul ini
ada, kedua file punya implementasi sendiri-sendiri yang bisa memberi jawaban berbeda
untuk kejadian yang sama persis (lihat ringkasan_perbaikan.md P0 & P0.2).

Bug yang diperbaiki di sini
----------------------------
Implementasi lama (di kedua file) mengecek SL di setiap candle dan langsung
`return LOSS` begitu SL tersentuh - TANPA peduli apakah TP1 sudah tersentuh di
candle-candle SEBELUMNYA. Akibatnya:

    Entry -> TP1 tersentuh (candle #5) -> ... -> SL tersentuh (candle #12)
    Hasil lama : LOSS_SL   (salah - mengabaikan bahwa TP1 sudah tercapai duluan)
    Hasil benar: TP1_FIRST (event pertama yang tersentuh menentukan outcome)

Prinsip desain
--------------
1. Outcome ditentukan oleh EVENT PERTAMA yang tersentuh secara kronologis. Begitu
   TP1 (atau TP2/TP3) tersentuh sebelum SL, tracking berhenti - apa pun yang terjadi
   di candle-candle sesudahnya tidak mengubah outcome ini lagi.
2. Kalau di satu candle TP2 tersentuh (artinya TP1 otomatis sudah lewat juga, karena
   TP1 < TP2 < TP3 jaraknya dari entry), label yang dipakai adalah TP TERTINGGI yang
   valid di candle itu (TP2_FIRST), bukan TP1_FIRST.
3. MFE (max favorable excursion) & MAE (max adverse excursion) dihitung dari SETIAP
   candle sejak entry sampai outcome ditentukan (atau sampai kehabisan data), bukan
   hanya di titik exit - dipakai nanti untuk analisis kualitas SL/TP placement.
4. Kalau SL & TP tersentuh di CANDLE YANG SAMA, urutan kejadian sebenarnya TIDAK BISA
   diketahui dari candle sebesar itu saja - modul ini TIDAK menebak diam-diam.
   Kasus ini ditandai `ambiguous_same_bar=True` dan diselesaikan mengikuti
   `same_bar_policy` (default konservatif: SL dianggap duluan, supaya win-rate tidak
   over-estimate). Cara paling benar untuk MENGHINDARI ambiguitas ini adalah memberi
   `candles` pada timeframe yang lebih KECIL dari timeframe sinyal (mis. sinyal di 1H,
   outcome dicek di candle 15M/5M) - itulah kenapa baik outcome_tracker.py maupun
   backtest.py memakai `settings.tf_outcome` yang sengaja lebih granular daripada
   `settings.tf_structure`/`settings.tf_entry`.
5. Kalau sampai candle terakhir yang diberikan belum ada SL/TP tersentuh, function ini
   TIDAK memutuskan sendiri apakah itu artinya "OPEN_EXPIRED" atau "masih harus ditunggu"
   - itu keputusan CALLER (tergantung umur signal utk live, atau holding-period utk
   backtest). Function ini hanya melaporkan state apa adanya lewat `still_open=True`.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import pandas as pd

TP1_FIRST = "TP1_FIRST"
TP2_FIRST = "TP2_FIRST"
TP3_FIRST = "TP3_FIRST"
SL_FIRST = "SL_FIRST"
OPEN_EXPIRED = "OPEN_EXPIRED"
STILL_OPEN = "OPEN"  # internal - dipakai still_open=True, caller yang menerjemahkan jadi OPEN_EXPIRED atau diabaikan

ALL_OUTCOMES = (TP1_FIRST, TP2_FIRST, TP3_FIRST, SL_FIRST, OPEN_EXPIRED)


def _pnl_pct(direction: str, entry: float, exit_price: float) -> float:
    if direction == "LONG":
        return (exit_price - entry) / entry * 100
    return (entry - exit_price) / entry * 100


@dataclass
class TradeOutcome:
    outcome: str                              # TP1_FIRST | TP2_FIRST | TP3_FIRST | SL_FIRST | "OPEN"
    exit_price: float                         # harga exit (realized) atau mark-to-market terakhir kalau still_open
    pnl_pct: float
    closed_at: Optional[str]                  # ISO timestamp candle exit (atau candle terakhir kalau still_open)
    bars_to_outcome: int                      # jumlah candle sejak entry sampai exit/akhir data
    time_to_outcome_seconds: Optional[float]  # None kalau entry_time tidak diberikan
    mfe_pct: float                             # max favorable excursion, selalu >= 0
    mae_pct: float                             # max adverse excursion, selalu >= 0 (magnitude, bukan signed)
    ambiguous_same_bar: bool = False
    still_open: bool = False                  # True = belum SL/TP tersentuh di sepanjang `candles` yang diberikan


def evaluate_trade_path(
    candles: pd.DataFrame,
    direction: str,
    entry: float,
    sl: float,
    tp1: Optional[float],
    tp2: Optional[float],
    tp3: Optional[float],
    entry_time: Optional[datetime] = None,
    same_bar_policy: str = "conservative_sl_first",
) -> Optional[TradeOutcome]:
    """
    candles : DataFrame OHLCV (index=timestamp UTC ascending, kolom high/low/close),
              HANYA candle SETELAH entry terbentuk (candle entry sendiri tidak perlu
              diikutkan - ini tanggung jawab caller supaya tidak ada look-ahead:
              caller memotong slice-nya sendiri sebelum memanggil function ini).
    same_bar_policy : "conservative_sl_first" (default) atau "conservative_tp_first" -
              hanya berlaku pada kasus ambigu (SL & TP tersentuh di candle yang sama).

    Return None kalau `candles` kosong (tidak ada apa pun untuk dievaluasi - caller
    sebaiknya coba lagi nanti/skip, BUKAN menganggap trade sudah closed).
    """
    if candles.empty:
        return None

    tp_targets = [(TP1_FIRST, tp1), (TP2_FIRST, tp2), (TP3_FIRST, tp3)]
    tp_targets = [(label, tp) for label, tp in tp_targets if tp is not None]
    tp_price_by_label = dict(tp_targets)

    mfe_pct = 0.0
    mae_pct = 0.0

    for bars_seen, (ts, candle) in enumerate(candles.iterrows(), start=1):
        low, high = float(candle["low"]), float(candle["high"])

        # --- excursion dihitung dari SETIAP candle (termasuk candle exit itu sendiri) ---
        if direction == "LONG":
            favorable = (high - entry) / entry * 100
            adverse = (entry - low) / entry * 100
        else:
            favorable = (entry - low) / entry * 100
            adverse = (high - entry) / entry * 100
        mfe_pct = max(mfe_pct, favorable, 0.0)
        mae_pct = max(mae_pct, adverse, 0.0)

        sl_hit = (low <= sl) if direction == "LONG" else (high >= sl)

        tp_hit_label = None
        for label, tp in reversed(tp_targets):  # cek TP terjauh dulu -> label TERTINGGI yang tercapai dipilih
            reached = (high >= tp) if direction == "LONG" else (low <= tp)
            if reached:
                tp_hit_label = label
                break

        if sl_hit or tp_hit_label:
            ambiguous = bool(sl_hit and tp_hit_label)
            if ambiguous and same_bar_policy == "conservative_tp_first":
                out_label, out_price = tp_hit_label, tp_price_by_label[tp_hit_label]
            elif sl_hit:
                # kasus non-ambigu (hanya SL), atau ambigu dgn policy default (SL menang)
                out_label, out_price = SL_FIRST, sl
            else:
                out_label, out_price = tp_hit_label, tp_price_by_label[tp_hit_label]

            time_to_outcome = None
            if entry_time is not None:
                ts_dt = ts.to_pydatetime() if isinstance(ts, pd.Timestamp) else ts
                time_to_outcome = (ts_dt - entry_time).total_seconds()

            return TradeOutcome(
                outcome=out_label,
                exit_price=out_price,
                pnl_pct=round(_pnl_pct(direction, entry, out_price), 4),
                closed_at=ts.isoformat() if isinstance(ts, pd.Timestamp) else str(ts),
                bars_to_outcome=bars_seen,
                time_to_outcome_seconds=time_to_outcome,
                mfe_pct=round(mfe_pct, 4),
                mae_pct=round(mae_pct, 4),
                ambiguous_same_bar=ambiguous,
                still_open=False,
            )

    # kehabisan candle tanpa SL/TP tersentuh - belum tentu "expired", tergantung caller
    last_ts = candles.index[-1]
    last_close = float(candles["close"].iloc[-1])
    time_to_outcome = None
    if entry_time is not None:
        last_ts_dt = last_ts.to_pydatetime() if isinstance(last_ts, pd.Timestamp) else last_ts
        time_to_outcome = (last_ts_dt - entry_time).total_seconds()

    return TradeOutcome(
        outcome=STILL_OPEN,
        exit_price=last_close,
        pnl_pct=round(_pnl_pct(direction, entry, last_close), 4),
        closed_at=last_ts.isoformat() if isinstance(last_ts, pd.Timestamp) else str(last_ts),
        bars_to_outcome=len(candles),
        time_to_outcome_seconds=time_to_outcome,
        mfe_pct=round(mfe_pct, 4),
        mae_pct=round(mae_pct, 4),
        ambiguous_same_bar=False,
        still_open=True,
    )
