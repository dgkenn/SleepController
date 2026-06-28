"""Gym-vs-sleep morning advisor — a togglable "should I get up early to train, or do I need
the sleep?" call, tuned to a RESIDENT's reality, not a competitive athlete's.

Framing (why this isn't the athlete model):
  • The workout is BINARY — now or never. If you can't train before the shift, it usually
    doesn't happen at all. So the gym is weighed as exercise-vs-nothing, and exercise has real
    standalone value for a resident: a pilot RCT of a flexible fitness program in residents cut
    anxiety (d≈1.07) with trends toward less depression/loneliness (Yang 2026,
    doi:10.1007/s40596-026-02384-y). That's an "opportunity value" that biases toward GOING when
    sleep is merely adequate.
  • The cost that should pull you back to bed is CLINICAL READINESS + your own recovery, not
    athletic injury. Sleep extension improves function and under-slept days are worse (Mah 2011,
    doi:10.5665/SLEEP.1132); cumulative debt degrades vigilance dose-dependently (Van Dongen 2003,
    doi:10.1093/sleep/26.2.117). Sleep maintenance is your #1 problem, so a fragmented night also
    argues for protecting recovery.

Net policy: default toward GO when projected sleep is adequate (the window won't come back), and
flip to SLEEP-IN only when you'd be genuinely short — below a safe floor, deep in debt, badly
fragmented, or facing a demanding shift on too little sleep. Advisory, and tunable (``lean`` sets
how the close calls break; the floor/target are personalizable and learnable).

NOTE: there is no RCT on "resident gym-vs-sleep at 5 a.m." — the weights below are judgment calls
informed by the evidence above and the user's own revealed preferences over time, not a measured
threshold. They're meant to be tuned to you.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import List, Optional

from sleepctl.benchmarks import NightMode, sleep_debt_min
from sleepctl.readiness import morning_readiness

_LEAN_THRESHOLD = {"protect": 0.58, "balanced": 0.50, "push": 0.42}


def _clamp(x: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


@dataclass
class GymConfig:
    """User-tunable policy for the advisor."""
    enabled: bool = False
    early_offset_min: int = 75            # how much earlier the gym alarm is vs the normal one
    sufficient_sleep_h: float = 7.0       # the "training without a deficit" target (learnable)
    min_safe_sleep_h: float = 6.0         # clinical-readiness floor: below this, protect sleep
    opportunity_value: float = 0.6        # now-or-never + mood/health value of the only window
    lean: str = "push"                    # protect | balanced | push — default leans toward the
                                          # gym (the window is now-or-never); flip to protect/balanced
                                          # if you'd rather guard sleep more
    gym_days: Optional[List[int]] = None  # weekday ints Mon=0..Sun=6; None = any day

    def to_dict(self) -> dict:
        return {"enabled": self.enabled, "early_offset_min": self.early_offset_min,
                "sufficient_sleep_h": self.sufficient_sleep_h,
                "min_safe_sleep_h": self.min_safe_sleep_h,
                "opportunity_value": self.opportunity_value, "lean": self.lean,
                "gym_days": self.gym_days}

    @staticmethod
    def from_dict(d: Optional[dict]) -> "GymConfig":
        d = d or {}
        c = GymConfig()
        for k in ("enabled", "early_offset_min", "sufficient_sleep_h", "min_safe_sleep_h",
                  "opportunity_value", "lean", "gym_days"):
            if k in d and d[k] is not None:
                setattr(c, k, d[k])
        if c.lean not in _LEAN_THRESHOLD:
            c.lean = "balanced"
        return c


@dataclass
class GymDecision:
    recommend: str                         # "go" | "sleep_in" | "off" | "rest_day"
    go_score: float                        # 0..1 (higher = train)
    confidence: float                      # 0..1
    headline: str
    early_wake_time: Optional[str] = None
    normal_wake_time: Optional[str] = None
    projected_gym_sleep_h: Optional[float] = None
    projected_sleepin_sleep_h: Optional[float] = None
    reasons: List[str] = field(default_factory=list)
    signals: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "recommend": self.recommend, "go_score": round(self.go_score, 3),
            "confidence": round(self.confidence, 2), "headline": self.headline,
            "early_wake_time": self.early_wake_time, "normal_wake_time": self.normal_wake_time,
            "projected_gym_sleep_h": (round(self.projected_gym_sleep_h, 1)
                                      if self.projected_gym_sleep_h is not None else None),
            "projected_sleepin_sleep_h": (round(self.projected_sleepin_sleep_h, 1)
                                          if self.projected_sleepin_sleep_h is not None else None),
            "reasons": self.reasons, "signals": self.signals,
        }


def _hhmm(dt: Optional[datetime]) -> Optional[str]:
    return dt.strftime("%H:%M") if dt else None


def gym_decision(now: datetime, normal_wake: Optional[datetime], recent_nights,
                 *, cfg: GymConfig = GymConfig(), sleep_onset: Optional[datetime] = None,
                 planned_bedtime: Optional[datetime] = None, last_night=None,
                 baseline_hrv: Optional[float] = None, day_demanding: bool = False,
                 mode: NightMode = NightMode.NORMAL) -> GymDecision:
    """Decide GO (train) vs SLEEP-IN. ``sleep_onset`` (when you actually fell asleep) gives the
    most accurate projection; otherwise ``planned_bedtime`` is used to plan the night before."""
    if not cfg.enabled:
        return GymDecision("off", 0.0, 0.0, "Gym advisor is off.")
    if cfg.gym_days is not None and now.weekday() not in cfg.gym_days:
        return GymDecision("rest_day", 0.0, 1.0, "Not a scheduled gym day — rest.")

    early_wake = (normal_wake - timedelta(minutes=cfg.early_offset_min)) if normal_wake else None

    # --- projected sleep on each branch -------------------------------------------------
    start = sleep_onset or planned_bedtime
    proj_gym = proj_sleepin = None
    if start and normal_wake and early_wake:
        proj_gym = max(0.0, (early_wake - start).total_seconds() / 3600.0)
        proj_sleepin = max(0.0, (normal_wake - start).total_seconds() / 3600.0)

    # --- signals (each toward GO positive / SLEEP-IN negative) --------------------------
    signals: dict = {}
    reasons: List[str] = []

    # opportunity: the window is now-or-never and exercise has standalone value for a resident.
    opportunity = float(cfg.opportunity_value)
    signals["opportunity"] = round(opportunity, 2)

    # sufficiency: hours above/below the personal training target (±1 h = ±1.0)
    # Gentle slope (1.5 h per unit): a little under target shouldn't beat the only-window value;
    # the hard stop is the safe floor below.
    suff = 0.0
    if proj_gym is not None:
        suff = _clamp((proj_gym - cfg.sufficient_sleep_h) / 1.5)
        signals["sufficiency"] = round(suff, 2)

    floor_breach = proj_gym is not None and proj_gym < cfg.min_safe_sleep_h

    # recovery / debt / continuity — reuse the morning-readiness engine when a night exists
    debt = sleep_debt_min(recent_nights) if recent_nights else 0.0
    recovery = continuity = None
    if last_night is not None:
        r = morning_readiness(last_night, recent_nights, mode=mode, baseline_hrv=baseline_hrv)
        debt = r.debt_min
        recovery = r.components.get("recovery")
        continuity = r.components.get("continuity")

    debt_term = -_clamp(debt / 360.0, 0.0, 1.0)            # ~6 h debt -> full negative
    signals["debt"] = round(debt_term, 2)
    rec_term = _clamp((recovery - 60.0) / 40.0) if recovery is not None else 0.0
    cont_term = _clamp((continuity - 60.0) / 40.0) if continuity is not None else 0.0
    if recovery is not None:
        signals["recovery"] = round(rec_term, 2)
    if continuity is not None:
        signals["continuity"] = round(cont_term, 2)
    demand_term = -0.8 if day_demanding else 0.0
    if day_demanding:
        signals["day_demand"] = demand_term

    # --- weighted fusion ----------------------------------------------------------------
    net = (opportunity + 1.0 * suff + 0.8 * debt_term + 0.7 * rec_term
           + 0.5 * cont_term + 0.7 * demand_term)
    if floor_breach:
        net -= 1.6                                          # too short to be safe -> protect sleep
    go_score = 1.0 / (1.0 + math.exp(-1.3 * net))
    threshold = _LEAN_THRESHOLD.get(cfg.lean, 0.50)
    recommend = "go" if go_score >= threshold else "sleep_in"

    # --- reasons (ranked, human-readable) ----------------------------------------------
    if proj_gym is not None:
        reasons.append(f"You'd get ~{proj_gym:.1f} h if you train"
                       f"{f' vs ~{proj_sleepin:.1f} h sleeping in' if proj_sleepin else ''}.")
    if floor_breach:
        reasons.append(f"That drops you under your {cfg.min_safe_sleep_h:.0f} h safe floor — too "
                       "short to head into a shift; protect the sleep today.")
    elif recommend == "go":
        reasons.append("It's your only window today, and the workout's mood/stress payoff is worth "
                       "it when your sleep is adequate (Yang 2026).")
    if suff <= -0.25 and not floor_breach:
        reasons.append(f"You'd train a bit under your ~{cfg.sufficient_sleep_h:.1f} h target — fine "
                       "for an easier session, ease off the intensity.")
    elif suff >= 0.25:
        reasons.append(f"Comfortably above your ~{cfg.sufficient_sleep_h:.1f} h target.")
    if debt >= 240:
        reasons.append(f"Carrying ~{debt/60:.1f} h sleep debt — recovery is competing for that hour.")
    if rec_term <= -0.25:
        reasons.append("HRV/recovery is below your baseline — keep it light or rest.")
    elif rec_term >= 0.25:
        reasons.append("Well recovered (HRV at/above baseline).")
    if cont_term <= -0.25:
        reasons.append("Last night was fragmented — your maintenance problem flared, so sleep has "
                       "extra value.")
    if day_demanding:
        reasons.append("Demanding shift ahead — if you go, keep it easy and protect alertness.")

    # confidence: distance from the decision line + a penalty for thin data
    conf = min(1.0, abs(go_score - threshold) * 3.0 + 0.3)
    if proj_gym is None:
        conf *= 0.6
    if last_night is None:
        conf *= 0.8

    if recommend == "go":
        headline = "Go train — it's your window and you can spare the sleep."
    else:
        headline = "Sleep in — you're too short today for the workout to be worth it."

    return GymDecision(
        recommend=recommend, go_score=go_score, confidence=conf, headline=headline,
        early_wake_time=_hhmm(early_wake), normal_wake_time=_hhmm(normal_wake),
        projected_gym_sleep_h=proj_gym, projected_sleepin_sleep_h=proj_sleepin,
        reasons=reasons, signals=signals)
