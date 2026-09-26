"""
core/correlation_tracker.py
-----------------------------
"Correlation awareness" (lihat ringkasan_perbaikan.md Phase 7 checklist).

CATATAN SCOPE: item ini di checklist Phase 7 TIDAK punya section spesifikasi detail
seperti P1.1-P1.11 (cuma satu baris checklist tanpa penjelasan). Ini adalah interpretasi
saya: mayoritas altcoin di watchlist bot ini sangat berkorelasi dengan BTC (lihat rationale
di layers/layer0_btc_regime.py) - jadi kalau dalam SATU SIKLUS SCAN banyak symbol sekaligus
menghasilkan sinyal SEARAH, itu sinyal kuat bahwa yang terjadi adalah "BTC/market bergerak,
banyak altcoin ikut lolos syarat teknikal bersamaan" (satu sumber gerak yang sama), BUKAN N
edge/setup yang independen satu sama lain. Confluence score per-setup (Layer 9) sengaja TIDAK
diturunkan oleh hal ini (skor tetap murni tentang kualitas setup itu sendiri, sesuai filosofi
P1.11: "confluence score, bukan win probability") - fungsi modul ini murni METADATA kesadaran
risiko yang ditempelkan ke signal & pesan Telegram, supaya user tidak salah baca "3 sinyal LONG
A+ muncul bersamaan" sebagai 3x independent conviction saat itu mungkin cuma 1x BTC-beta.

State di-reset SETIAP SIKLUS SCAN (lihat pipeline.py::scan_watchlist()) - jadi ini BUKAN
korelasi statistik historis antar-coin (butuh data harga historis multi-symbol, di luar
scope Phase 7 ini), cuma penghitung sederhana "berapa symbol lain di scan yang sama sudah
menghasilkan sinyal ke arah yang sama sebelum symbol ini".
"""

from dataclasses import dataclass, field


@dataclass
class _ScanState:
    # direction.value ("LONG"/"SHORT") -> list of symbol yang sudah qualifying di scan ini
    symbols_by_direction: dict = field(default_factory=dict)


class CorrelationTracker:
    def __init__(self):
        self._state = _ScanState()

    def reset(self):
        """Panggil di awal setiap siklus scan (pipeline.py::scan_watchlist())."""
        self._state = _ScanState()

    def register_and_check(self, symbol: str, direction_value: str) -> dict:
        """
        Daftarkan satu sinyal QUALIFYING (sudah lolos score_min_to_send, terlepas dari nanti
        jadi di-skip karena duplikasi open-trade atau tidak) ke state scan saat ini, lalu
        kembalikan metadata korelasi UNTUK sinyal ini (dihitung dari symbol2 LAIN yang sudah
        terdaftar SEBELUM simbol ini di scan yang sama - bukan termasuk dirinya sendiri).
        """
        bucket = self._state.symbols_by_direction.setdefault(direction_value, [])
        same_direction_before = [s for s in bucket if s != symbol]
        count_before = len(same_direction_before)

        if symbol not in bucket:
            bucket.append(symbol)

        from config import settings
        high_risk = count_before >= settings.correlation_warn_threshold

        return {
            "same_direction_signals_this_scan": count_before,
            "same_direction_symbols_this_scan": same_direction_before,
            "high_correlation_risk": high_risk,
        }


correlation_tracker = CorrelationTracker()
