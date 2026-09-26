"""
models.py
---------
Struktur data yang dipakai bersama di seluruh pipeline (antar layer).
Menggunakan dataclass supaya mudah di-debug (print/log isi objeknya)
dan mudah di-serialize ke dict untuk disimpan ke Supabase.
"""

from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from typing import Optional


class Direction(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"
    NONE = "NONE"


class LayerStatus(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    SKIPPED = "SKIPPED"  # layer tidak dievaluasi karena layer sebelumnya sudah gagal


@dataclass
class LayerResult:
    """Hasil evaluasi satu layer. Independen & mudah di-debug per layer."""
    layer_number: int
    layer_name: str
    status: LayerStatus
    reason: str = ""
    data: dict = field(default_factory=dict)  # nilai-nilai mentah yang dipakai untuk keputusan

    def to_dict(self):
        d = asdict(self)
        d["status"] = self.status.value
        return d


@dataclass
class SmartMoneyZone:
    zone_type: str          # "order_block" | "fvg" | "liquidity_sweep"
    direction: Direction
    top: float
    bottom: float
    index: int               # index candle tempat zona terbentuk (posisi RELATIF di slice
                              # yang dipakai detector - JANGAN dipakai untuk id stabil lintas
                              # scan, pakai `formed_at` + zone_id lewat setup_identity.py)
    valid: bool = True
    formed_at: str | None = None  # timestamp ISO ABSOLUT candle pembentuk zona - stabil
                                   # lintas scan (dipakai untuk membangun zone_id, lihat
                                   # layers/layer4_smart_money.py::make_zone_id())
    meta: dict = field(default_factory=dict)


@dataclass
class RiskPlan:
    entry: float
    sl: float
    tp1: float
    tp2: float
    tp3: float
    risk_amount: float
    rr1: float = 1.0
    rr2: float = 2.0
    rr3: float = 3.0


@dataclass
class SignalScore:
    total: int
    breakdown: dict
    stars: int
    grade: str  # "A+", "A", "B", "REJECTED"


@dataclass
class TradeSignal:
    symbol: str
    direction: Direction
    generated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    layer_results: list = field(default_factory=list)   # list[LayerResult]
    smart_money_zones: list = field(default_factory=list)  # list[SmartMoneyZone]
    risk_plan: Optional[RiskPlan] = None
    score: Optional[SignalScore] = None
    indicators_snapshot: dict = field(default_factory=dict)
    sent: bool = False
    fail_layer: Optional[str] = None  # diisi jika pipeline berhenti (hard-stop) di tengah jalan
    soft_fail_layers: list = field(default_factory=list)  # layer 4-6 yang FAIL tapi tidak hard-stop
    setup_id: Optional[str] = None  # identitas unik setup (symbol+direction+structure_event+zone) -
                                     # lihat setup_identity.py. Dipakai untuk anti-duplikasi & analitik.
    correlation_meta: dict = field(default_factory=dict)  # lihat core/correlation_tracker.py (Phase 7)

    def to_supabase_row(self) -> dict:
        # Score breakdown (Phase 7 - lihat layers/layer9_scoring.py) DIRATAKAN jadi kolom
        # top-level score_* di sini (bukan cuma terkubur di dalam layer_results[9].data),
        # supaya bisa langsung di-query untuk analisis tanpa unwrap JSON, sesuai contoh
        # pertanyaan di ringkasan_perbaikan.md P2: "signal dengan liquidity sweep + FVG + OI
        # naik sebenarnya performanya bagaimana?".
        breakdown = self.score.breakdown if self.score else {}

        return {
            "symbol": self.symbol,
            "direction": self.direction.value,
            "generated_at": self.generated_at,
            "setup_id": self.setup_id,
            "score": self.score.total if self.score else None,
            "grade": self.score.grade if self.score else None,
            "score_regime_4h": breakdown.get("regime_4h"),
            "score_structure_1h": breakdown.get("structure_1h"),
            "score_liquidity_event": breakdown.get("liquidity_event"),
            "score_smc_location": breakdown.get("smc_location"),
            "score_entry_trigger_15m": breakdown.get("entry_trigger_15m"),
            "score_volume": breakdown.get("volume"),
            "score_momentum": breakdown.get("momentum"),
            "score_oi": breakdown.get("oi"),
            "score_risk_quality": breakdown.get("risk_quality"),
            "entry": self.risk_plan.entry if self.risk_plan else None,
            "sl": self.risk_plan.sl if self.risk_plan else None,
            "tp1": self.risk_plan.tp1 if self.risk_plan else None,
            "tp2": self.risk_plan.tp2 if self.risk_plan else None,
            "tp3": self.risk_plan.tp3 if self.risk_plan else None,
            "layer_results": [lr.to_dict() for lr in self.layer_results],
            "smart_money_zones": [
                {**asdict(z), "direction": z.direction.value} for z in self.smart_money_zones
            ],
            "indicators_snapshot": self.indicators_snapshot,
            "sent": self.sent,
            "fail_layer": self.fail_layer,
            "soft_fail_layers": self.soft_fail_layers,
            # Correlation awareness (Phase 7, lihat core/correlation_tracker.py) - metadata,
            # TIDAK mempengaruhi score di atas.
            "correlation_same_direction_count": self.correlation_meta.get("same_direction_signals_this_scan"),
            "correlation_high_risk": self.correlation_meta.get("high_correlation_risk", False),
            # outcome & backtest fields diisi belakangan oleh proses tracking terpisah
            "outcome": None,
            "closed_at": None,
            "pnl_pct": None,
        }
