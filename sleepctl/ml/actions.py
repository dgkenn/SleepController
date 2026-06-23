"""Discrete candidate actions and how each maps to a bounded SetpointProfile change.

The action-value learner scores these against the response models. Each action is a small,
interpretable nudge to the learnable setpoint knobs; ``magnitude`` orders them so the
selector can prefer the *smallest effective* one. Induction/wake-window actions are reserved
for future per-routine response models (they don't change the thermal setpoint the current
model predicts from) and are intentionally omitted from v1 scoring.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Optional

from sleepctl.models import SetpointProfile

# Safety bounds per knob (effective comfort °F / offsets / blend weight).
KNOB_BOUNDS = {
    "neutral_f": (62.0, 78.0),
    "deep_bias_f": (58.0, 78.0),
    "rem_warm_offset_f": (0.0, 4.0),
    "wake_ramp_f": (70.0, 86.0),
    "composite_bed_weight": (0.55, 0.95),
}


@dataclass
class Action:
    name: str
    deltas: dict          # knob -> additive change
    magnitude: int        # 0 = no change; higher = larger intervention
    kind: str = "thermal"


# Ordered smallest-first. "skin_more"/"skin_less" tune the body-vs-exposed-skin blend.
ACTIONS = [
    Action("no_change", {}, 0),
    Action("slight_cool", {"deep_bias_f": -1.0, "neutral_f": -0.5}, 1),
    Action("slight_warm", {"deep_bias_f": 1.0, "neutral_f": 0.5}, 1),
    Action("rem_warm_more", {"rem_warm_offset_f": 1.0}, 1),
    Action("skin_more", {"composite_bed_weight": -0.05}, 1),
    Action("skin_less", {"composite_bed_weight": 0.05}, 1),
    Action("strong_cool", {"deep_bias_f": -2.0, "neutral_f": -1.0}, 2),
]


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def apply_action(profile: SetpointProfile, action: Action) -> SetpointProfile:
    """Return a new (bounded, version-bumped) profile with the action's deltas applied."""
    knobs = {k: getattr(profile, k) for k in KNOB_BOUNDS}
    for knob, dv in action.deltas.items():
        lo, hi = KNOB_BOUNDS[knob]
        knobs[knob] = _clamp(knobs[knob] + dv, lo, hi)
    bumped = action.name != "no_change"
    return replace(
        profile,
        neutral_f=knobs["neutral_f"],
        deep_bias_f=knobs["deep_bias_f"],
        rem_warm_offset_f=knobs["rem_warm_offset_f"],
        wake_ramp_f=knobs["wake_ramp_f"],
        composite_bed_weight=knobs["composite_bed_weight"],
        version=profile.version + (1 if bumped else 0),
        source=("ml" if bumped else profile.source),
        updated=datetime.now() if bumped else profile.updated,
    )


@dataclass
class ActionScore:
    action: Action
    profile: SetpointProfile
    predicted: dict = field(default_factory=dict)
    reward: float = 0.0
    confidence: float = 0.0
    reason: str = ""

    @property
    def name(self) -> str:
        return self.action.name
