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
    index: int               # index candle tempat zona terbentuk
    valid: bool = True
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

    def to_supabase_row(self) -> dict:
        return {
            "symbol": self.symbol,
            "direction": self.direction.value,
            "generated_at": self.generated_at,
            "score": self.score.total if self.score else None,
            "grade": self.score.grade if self.score else None,
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
            # outcome & backtest fields diisi belakangan oleh proses tracking terpisah
            "outcome": None,
            "closed_at": None,
            "pnl_pct": None,
        }
