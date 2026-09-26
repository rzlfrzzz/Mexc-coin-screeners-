"""
setup_identity.py
------------------
Fungsi TERPUSAT untuk membangun `setup_id` - identitas unik satu "setup" trading,
dipakai oleh pipeline.py (live) DAN backtest.py, supaya definisinya sama persis di
kedua sisi (pola yang sama dengan trade_outcome.py di Phase 1).

Definisi (lihat ringkasan_perbaikan.md P2 - Anti-Repeated Signal):
    setup_id = symbol + direction + structure_event_id + zone_id

Contoh: BTCUSDT_LONG_BOS_bullish_115_138_FVG_20260115T0300Z

Kegunaan:
1. Anti-duplikasi: kalau scanner menemukan kondisi yang PERSIS SAMA di scan berikutnya
   (structure event yang sama + zona yang sama), setup_id-nya identik -> tidak perlu
   kirim signal baru untuk setup yang sama (lihat pipeline.py: perbandingan dengan
   setup_id sinyal open sebelumnya).
2. Analitik: dengan setup_id tersimpan di kolom `signals`, nanti bisa dianalisis
   "signal dengan liquidity sweep + FVG performanya bagaimana?" dst (lihat
   ringkasan_perbaikan.md P2 - Logging) tanpa perlu join manual ke layer_logs.

`structure_event_id` diambil dari Layer 3 (`bos_id` kalau ada BOS aktif, kalau tidak
`choch_id` kalau ada CHoCH aktif) - keduanya sudah stabil/immutable sejak Phase 3.
`zone_id` diambil dari Layer 4 (`active_zone_id` - zona OB/FVG tempat harga saat ini
berada) - sudah stabil sejak Phase 4 (dibangun dari timestamp absolut candle pembentuk
zona, bukan posisi relatif yang bisa bergeser antar scan).

Kalau salah satu komponen tidak ada (mis. tidak ada BOS/CHoCH aktif, atau tidak ada zona
OB/FVG aktif - Layer 3/4 statusnya sendiri bisa saja tetap PASS/soft-PASS tanpa itu),
dipakai placeholder eksplisit ("STRUCT_NONE"/"ZONE_NONE") supaya tetap deterministik &
kelihatan jelas di data kalau komponen itu kosong, bukan diam-diam di-skip.
"""

import re

_SAFE_CHARS = re.compile(r"[^A-Za-z0-9_]")


def _slug(text: str) -> str:
    """Bersihkan karakter yang tidak aman untuk id (mis. '/' di 'BTC/USDT:USDT')."""
    return _SAFE_CHARS.sub("", text.replace("/", "").replace(":", "").replace("-", ""))


def build_setup_id(symbol: str, direction, lr3_data: dict, lr4_data: dict) -> str:
    """
    symbol    : mis. "BTC/USDT:USDT"
    direction : Direction.LONG / Direction.SHORT (atau string "LONG"/"SHORT")
    lr3_data  : LayerResult.data dari layers/layer3_structure.py (butuh bos_id/choch_id)
    lr4_data  : LayerResult.data dari layers/layer4_smart_money.py (butuh active_zone_id)
    """
    direction_str = direction.value if hasattr(direction, "value") else str(direction)

    structure_event_id = lr3_data.get("bos_id") or lr3_data.get("choch_id") or "STRUCT_NONE"
    zone_id = lr4_data.get("active_zone_id") or "ZONE_NONE"

    return f"{_slug(symbol)}_{direction_str}_{structure_event_id}_{zone_id}"
  
