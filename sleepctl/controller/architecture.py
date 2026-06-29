"""In-night sleep-architecture steering — "nudge me deeper".

A bounded, awakening-risk-vetoed FAST loop that runs inside Maintenance. It compares the
*realized* cumulative stage curve against the night's *ideal* (front-loaded deep, back-loaded
REM, parameterized by the personalized per-night targets) and, when you are LIGHTER than the
ideal deep curve wants AND wake-risk is low, asks the thermal controller to drive the bed toward
the deep setpoint — biasing you deeper. The bed cannot *force* a stage; it shifts transition
probability (Eight Sleep Autopilot RCT: cooler offset -> more deep). See docs/ARCHITECTURE_STEERING.md.

Honest constraints baked in here:
  - Deep is front-loaded and barely steerable late, so deepening is gated to the front of the
    night (``steer_deepen_max_fraction``) and to a real deficit (``steer_deepen_min_deficit_min``).
  - Stage labels are noisy at 60 s; this acts on the cumulative deficit, not a single minute.
  - The maneuver is ASYMMETRIC: deepen (cool) is the workhorse; "nudge lighter" (warm) is the
    pre-wake ramp plus an optional, off-by-default back-third REM-unblock — never at the cost of
    your learned deep floor.

Pure functions + a tiny stateless evaluator; the controller owns the accrued minutes and the
awakening-risk veto. No device I/O, no persistence.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from sleepctl.models import SleepStage

# Stages we treat as "steerable toward deep" — we only ever nudge from light/unknown; we never
# pull you OUT of REM or out of deep to chase the deep curve.
_LIGHTISH = (SleepStage.LIGHT, SleepStage.UNKNOWN)


@dataclass(frozen=True)
class IdealTrajectory:
    """The ideal CUMULATIVE deep/REM minutes as a function of minutes-since-onset.

    Deep accrues front-loaded (concave: most SWS in the first cycles); REM accrues back-loaded
    (convex: REM grows in the last third). Both integrate to the night's deep/REM totals.
    """

    deep_total_min: float
    rem_total_min: float
    est_sleep_min: float
    deep_front_p: float = 0.6   # exponent < 1 -> concave -> front-loaded
    rem_back_q: float = 1.6     # exponent > 1 -> convex -> back-loaded

    def _frac(self, minutes_since_onset: float) -> float:
        if self.est_sleep_min <= 0:
            return 0.0
        return max(0.0, min(1.0, minutes_since_onset / self.est_sleep_min))

    def deep_by(self, minutes_since_onset: float) -> float:
        """Ideal cumulative deep minutes accrued by ``minutes_since_onset`` (front-loaded)."""
        f = self._frac(minutes_since_onset)
        return self.deep_total_min * (f ** self.deep_front_p)

    def rem_by(self, minutes_since_onset: float) -> float:
        """Ideal cumulative REM minutes accrued by ``minutes_since_onset`` (back-loaded)."""
        f = self._frac(minutes_since_onset)
        return self.rem_total_min * (f ** self.rem_back_q)


@dataclass
class SteerDecision:
    """One tick's steering verdict. ``maneuver`` in {'deepen','rem_warm','hold'}."""

    maneuver: str
    deep_deficit_min: float        # ideal-by-now minus realized (positive = behind on deep)
    rem_deficit_min: float
    frac_of_night: float
    on_deep_curve: bool            # realized deep is at/above its ideal-by-now
    risk_low: bool
    reason: str

    @property
    def deepen(self) -> bool:
        return self.maneuver == "deepen"

    def to_dict(self) -> dict:
        return {
            "maneuver": self.maneuver,
            "deep_deficit_min": round(self.deep_deficit_min, 1),
            "rem_deficit_min": round(self.rem_deficit_min, 1),
            "frac_of_night": round(self.frac_of_night, 3),
            "on_deep_curve": self.on_deep_curve,
            "risk_low": self.risk_low,
            "reason": self.reason,
        }


def _f(v, default: float) -> float:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return default
    return default if (x != x) else x  # drop NaN


class ArchitectureSteering:
    """Stateless evaluator. The controller feeds it the accrued architecture + risk each tick."""

    def __init__(self, cfg) -> None:
        self.cfg = cfg

    def build_trajectory(self, targets, est_sleep_min: float) -> IdealTrajectory:
        t = self.cfg.tunables
        est = max(1.0, _f(est_sleep_min, 0.0)
                  or _f(getattr(targets, "total_sleep_target_min", 0), 480.0))
        deep_ideal = _f(getattr(targets, "deep_pct_ideal", 0.20), 0.20)
        rem_ideal = _f(getattr(targets, "rem_pct_ideal", 0.22), 0.22)
        return IdealTrajectory(
            deep_total_min=deep_ideal * est,
            rem_total_min=rem_ideal * est,
            est_sleep_min=est,
            deep_front_p=_f(t.steer_deep_front_p, 0.6),
            rem_back_q=_f(t.steer_rem_back_q, 1.6),
        )

    def prewake_standoff_min(self) -> float:
        """How long before the wake deadline the steerer stops deepening and yields to the wake-up
        trajectory: the smart-wake window plus a guard (~a cycle)."""
        t = self.cfg.tunables
        return _f(t.wake_window_min, 30.0) + _f(t.steer_prewake_guard_min, 45.0)

    def evaluate(
        self,
        *,
        minutes_since_onset: float,
        est_sleep_min: float,
        deep_min_so_far: float,
        rem_min_so_far: float,
        current_stage: Optional[SleepStage],
        targets,
        risk_low: bool,
        minutes_to_wake: Optional[float] = None,
    ) -> SteerDecision:
        """Decide whether to nudge deeper (the workhorse), warm for a late REM-unblock (optional,
        off by default), or hold. This is the in-night thermal arbiter, with a strict precedence
        that RECONCILES steering against the other two sleep maneuvers:

          1. wake-PREVENTION wins — a non-low ``risk_low`` (the controller folds in rising
             wake-risk + the leading-edge precursor + a micro-arousal) always yields ``hold`` so
             maintenance (the #1 priority) owns the bed; the settle nudge runs instead of deepening.
          2. wake-UP handoff — within ``prewake_standoff_min`` of the deadline we stop deepening so
             the smart-wake ramp can lift you out of light sleep; deepening you here would just add
             sleep-inertia to the wake.
          3. only then DEEPEN (or the off-by-default REM-unblock); else hold.
        """
        t = self.cfg.tunables
        traj = self.build_trajectory(targets, est_sleep_min)
        mso = max(0.0, _f(minutes_since_onset, 0.0))
        frac = traj._frac(mso)
        deep_def = traj.deep_by(mso) - max(0.0, _f(deep_min_so_far, 0.0))
        rem_def = traj.rem_by(mso) - max(0.0, _f(rem_min_so_far, 0.0))
        on_curve = deep_def <= 0.0

        if not t.inight_steering_enabled:
            return SteerDecision("hold", deep_def, rem_def, frac, on_curve, risk_low,
                                 "in-night steering disabled")
        if not risk_low:
            # (1) Maintenance is the top priority: never deepen while a disturbance is brewing —
            # the wake-prevention settle nudge owns the bed instead.
            return SteerDecision("hold", deep_def, rem_def, frac, on_curve, risk_low,
                                 "awakening-risk not low -> hold (wake-prevention owns the bed)")

        # (2) Wake-up handoff: stand down as the deadline approaches so the wake-up trajectory can
        # surface you from LIGHT sleep — never deepen you into pre-wake inertia.
        near_wake = (minutes_to_wake is not None
                     and minutes_to_wake <= self.prewake_standoff_min())
        if near_wake:
            return SteerDecision(
                "hold", deep_def, rem_def, frac, on_curve, risk_low,
                f"within {self.prewake_standoff_min():.0f} min of wake -> stand down for the "
                f"wake-up ramp (avoid deep-sleep inertia)")

        # --- (3a) ACQUIRE: the deepen workhorse. Light-but-should-be-deep, early, real deficit. -
        if (current_stage in _LIGHTISH
                and frac <= _f(t.steer_deepen_max_fraction, 0.6)
                and deep_def >= _f(t.steer_deepen_min_deficit_min, 8.0)):
            return SteerDecision(
                "deepen", deep_def, rem_def, frac, on_curve, risk_low,
                f"deep {deep_def:.0f} min behind the ideal curve at "
                f"{frac:.0%} of the night, in light sleep -> cool toward deep")

        # --- (3b) DEFEND the favorable state you're already IN. The complement of acquiring a
        # better one: when you're in DEEP (the recovery-critical state), hold the cool, stable
        # setpoint to PROLONG the bout and never trade it away — the bed's job is to keep you here,
        # not pull you out. (The thermal action matches what maintenance already does for DEEP; the
        # explicit verdict unifies the reasoning and guards the REM-unblock below.) ---------------
        if current_stage is SleepStage.DEEP:
            return SteerDecision(
                "defend_deep", deep_def, rem_def, frac, on_curve, risk_low,
                "in deep sleep -> hold cool + stable to keep you here (defend the favorable state)")

        # --- REM-unblock (the "nudge lighter" corollary): back-third only, deep already met, REM
        # behind, in LIGHT (never mid-deep — defend_deep above guards that). OFF by default; only
        # ships per person once A/B proves it helps. Never reduces deep below its floor. ----------
        if (t.steer_rem_unblock_enabled and frac >= 0.66 and on_curve
                and current_stage is SleepStage.LIGHT
                and rem_def >= _f(t.steer_deepen_min_deficit_min, 8.0)):
            return SteerDecision(
                "rem_warm", deep_def, rem_def, frac, on_curve, risk_low,
                f"back third, deep on-curve, REM {rem_def:.0f} min behind -> small warm bias")

        # --- DEFEND REM in the back half (favorable there): hold the gentle warm, don't cool-
        # disturb it. Again matches maintenance's REM behaviour; the verdict makes it explicit. ---
        if current_stage is SleepStage.REM and frac >= 0.5:
            return SteerDecision(
                "defend_rem", deep_def, rem_def, frac, on_curve, risk_low,
                "in back-half REM -> hold steady to keep you here (defend the favorable state)")

        return SteerDecision("hold", deep_def, rem_def, frac, on_curve, risk_low,
                             "on/near the ideal curve or not steerable now -> hold")
