"""Data-quality gate — a pure, explainable trust score for one sensor frame.

The Pod senses HR/HRV/respiratory-rate via ballistocardiography (BCG), which is only
reliable when the sleeper is still, presence is confirmed, and the cloud telemetry is fresh.
``SleepController`` already refuses to act on STALE data (``frame.is_stale``) and discounts
confidence for movement (``_biometric_reliability``). This module generalizes that "do no
harm on untrustworthy data" principle into one pure, testable assessment that also accounts
for missing vitals, uncertain/absent presence, and low stage-classification confidence — so
the control path can down-weight confidence and bias toward HOLD proportionally to how much
we actually trust the frame, rather than only at the hard stale/movement edges.

Deliberately pure (no I/O, no mutation) and conservative: it only ever *lowers* trust, never
raises it above 1.0, and every point lost is tied to one explainable reason string.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional

from sleepctl.models import SensorFrame


@dataclass
class DataQuality:
    """0..1 trust score for a single ``SensorFrame``, plus the reasons it was docked."""

    score: float
    reasons: List[str] = field(default_factory=list)

    @property
    def top_reason(self) -> Optional[str]:
        return self.reasons[0] if self.reasons else None

    def to_dict(self) -> dict:
        return {
            "score": round(self.score, 3),
            "reasons": list(self.reasons),
            "top_reason": self.top_reason,
        }


def assess_data_quality(frame: SensorFrame, cfg, now: Optional[datetime] = None) -> DataQuality:
    """Score how much the control loop should trust ``frame`` right now.

    Penalties are additive deductions from 1.0 (floored at 0), each logged with a short,
    human-readable reason (worst offender first) so the Decision/UI can explain a low score
    without a second lookup. Ordering of ``reasons`` is worst-first by construction below.
    """
    t = getattr(cfg, "tunables", cfg)
    penalties: List[tuple] = []  # (deduction, reason) — appended worst-first as we go

    # --- staleness: the freshest and most safety-critical signal --------------------------
    stale_limit = getattr(t, "telemetry_stale_seconds", 30.0)
    age = frame.data_age_seconds
    if age is None:
        penalties.append((0.5, "data_age_unknown"))
    elif age > stale_limit:
        # Scale beyond the limit: 1x over -> -0.35, 3x+ over -> floor at -0.6.
        over = (age - stale_limit) / max(stale_limit, 1e-6)
        penalties.append((min(0.6, 0.3 + 0.15 * over), "data_stale"))

    # --- movement: BCG (HR/HRV/RR) needs stillness -----------------------------------------
    if frame.movement is not None and frame.movement > 0.2:
        # Mirrors the spirit of _biometric_reliability's floor, expressed as a penalty.
        penalties.append((min(0.4, 0.8 * min(frame.movement, 0.5)), "high_movement"))

    # --- missing vitals ---------------------------------------------------------------------
    missing_vitals = [name for name, v in (
        ("heart_rate", frame.heart_rate),
        ("hrv", frame.hrv),
        ("respiratory_rate", frame.respiratory_rate),
    ) if v is None]
    if missing_vitals:
        # Each missing vital chips away at trust; capped so a single frame can't go to zero
        # from missing vitals alone (staleness/presence are the harder gates for that).
        penalties.append((min(0.3, 0.1 * len(missing_vitals)),
                          f"missing_vitals:{','.join(missing_vitals)}"))

    # --- presence: uncertain or confirmed-absent -------------------------------------------
    if frame.presence is None:
        penalties.append((0.15, "presence_unknown"))
    elif frame.presence is False:
        penalties.append((0.4, "presence_absent"))

    # --- stage-classification confidence ----------------------------------------------------
    min_stage_conf = getattr(t, "onset_min_stage_conf", 0.4)
    if frame.stage_confidence is None:
        penalties.append((0.15, "stage_confidence_unknown"))
    elif frame.stage_confidence < min_stage_conf:
        penalties.append((min(0.3, 0.3 * (min_stage_conf - frame.stage_confidence) / max(min_stage_conf, 1e-6)),
                          "low_stage_confidence"))

    # Worst offender first (most explainable "top reason"), stable order otherwise.
    penalties.sort(key=lambda p: p[0], reverse=True)
    score = 1.0
    reasons: List[str] = []
    for deduction, reason in penalties:
        if deduction <= 0:
            continue
        score -= deduction
        reasons.append(reason)
    score = max(0.0, min(1.0, score))
    return DataQuality(score=score, reasons=reasons)
