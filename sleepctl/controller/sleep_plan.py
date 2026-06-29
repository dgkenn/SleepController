"""Wake-aware nightly sleep plan.

Turns the user's wake time + recent history into a concrete plan the whole stack reasons
about: which night mode is in effect, how much sleep opportunity exists, how many NREM-REM
cycles fit, the running sleep debt, the smart-wake window, and a phased thermal strategy.

This is the bridge between the schedule and the controller objective:

  NORMAL      -> NightObjective.OPTIMIZE
  CONSTRAINED -> NightObjective.DAMAGE_CONTROL   (short work night)
  RECOVERY    -> NightObjective.RECOVERY         (off day / repaying sleep debt)

The plan is intentionally explainable — every field can be shown on the dashboard so the
user sees *why* tonight is being run a particular way.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import List, Optional

from sleepctl.benchmarks import (
    CONSTRAINED_OPPORTUNITY_MIN,
    CYCLE_LEN_MIN,
    SLEEP_NEED_MIN,
    NightMode,
    Targets,
    chronic_shortfall,
    sleep_debt_min,
    targets_for,
)
from sleepctl.models import NightObjective

_OBJECTIVE_BY_MODE = {
    NightMode.NORMAL: NightObjective.OPTIMIZE,
    NightMode.CONSTRAINED: NightObjective.DAMAGE_CONTROL,
    NightMode.RECOVERY: NightObjective.RECOVERY,
}


@dataclass
class ThermalPhase:
    name: str
    intent: str        # cool_fast | deep_cool | maintain | rem_protect | wake_ramp
    note: str


@dataclass
class SleepPlan:
    mode: NightMode
    objective: NightObjective
    sleep_opportunity_min: Optional[float]   # time in bed (bedtime -> wake)
    est_cycles: Optional[float]
    sleep_debt_min: float
    smart_wake_window_min: int
    required_wake_time: Optional[datetime]
    targets: Targets
    # actual sleep accounting: cycles/quality are judged from when you FALL ASLEEP, not
    # from when you get into bed (so lying awake doesn't inflate the plan).
    est_onset_latency_min: float = 0.0
    est_sleep_min: Optional[float] = None    # opportunity minus expected onset latency
    thermal_phases: List[ThermalPhase] = field(default_factory=list)
    # bias hints the controller / setpoint can apply (°F deltas; bounded downstream)
    deep_bias_delta_f: float = 0.0
    rem_warm_delta_f: float = 0.0
    strategy: str = ""
    bedtime: Optional["BedtimeGuidance"] = None   # when to be asleep + structural shortfall

    def to_dict(self) -> dict:
        return {
            "mode": self.mode.value,
            "objective": self.objective.value,
            "sleep_opportunity_min": self.sleep_opportunity_min,
            "est_onset_latency_min": round(self.est_onset_latency_min, 1),
            "est_sleep_min": round(self.est_sleep_min, 1) if self.est_sleep_min else None,
            "est_cycles": self.est_cycles,
            "sleep_debt_min": round(self.sleep_debt_min, 1),
            "smart_wake_window_min": self.smart_wake_window_min,
            "required_wake_time": self.required_wake_time.isoformat()
            if self.required_wake_time else None,
            "deep_bias_delta_f": self.deep_bias_delta_f,
            "rem_warm_delta_f": self.rem_warm_delta_f,
            "thermal_phases": [
                {"name": p.name, "intent": p.intent, "note": p.note}
                for p in self.thermal_phases
            ],
            "targets": {
                "sol_max_min": self.targets.sol_max_min,
                "efficiency_min": self.targets.efficiency_min,
                "waso_max_min": self.targets.waso_max_min,
                "awakenings_max": self.targets.awakenings_max,
                "deep_pct_min": self.targets.deep_pct_min,
                "deep_pct_ideal": self.targets.deep_pct_ideal,
                "rem_pct_min": self.targets.rem_pct_min,
                "rem_pct_ideal": self.targets.rem_pct_ideal,
                "total_sleep_target_min": self.targets.total_sleep_target_min,
                "rationale": self.targets.rationale,
            },
            "strategy": self.strategy,
            "bedtime": self.bedtime.to_dict() if self.bedtime else None,
        }


def decide_mode(
    sleep_opportunity_min: Optional[float],
    required_wake_time: Optional[datetime],
    debt_min: float,
    hint: Optional[str] = None,
    debt_threshold_min: float = 120.0,
) -> NightMode:
    """Pick the night mode from an explicit hint, the wake schedule, and sleep debt."""
    if hint:
        h = hint.lower()
        if h in ("recovery", "off", "off_day", "rest"):
            return NightMode.RECOVERY
        if h in ("work", "constrained", "short"):
            if sleep_opportunity_min is not None and \
                    sleep_opportunity_min < CONSTRAINED_OPPORTUNITY_MIN:
                return NightMode.CONSTRAINED
            return NightMode.NORMAL
        if h in ("normal", "balanced"):
            return NightMode.NORMAL
        # "auto" falls through to inference

    # Inference: no alarm + meaningful debt -> recovery; short opportunity -> constrained.
    if required_wake_time is None:
        return NightMode.RECOVERY if debt_min >= debt_threshold_min else NightMode.NORMAL
    if sleep_opportunity_min is not None and \
            sleep_opportunity_min < CONSTRAINED_OPPORTUNITY_MIN:
        return NightMode.CONSTRAINED
    return NightMode.NORMAL


def plan_night(
    now: datetime,
    required_wake_time: Optional[datetime],
    recent_summaries=None,
    bedtime: Optional[datetime] = None,
    hint: Optional[str] = None,
    need_min: int = SLEEP_NEED_MIN,
    base_window_min: int = 30,
) -> SleepPlan:
    """Build tonight's plan. ``bedtime`` defaults to ``now`` (planning at lights-out)."""
    bedtime = bedtime or now
    opportunity = None
    if required_wake_time is not None:
        opportunity = max(0.0, (required_wake_time - bedtime).total_seconds() / 60.0)

    debt = sleep_debt_min(recent_summaries or [], need_min=need_min)
    # Cycles + quality are judged from when you actually FALL ASLEEP, so lying awake in bed
    # doesn't inflate the plan. Use your recent typical onset latency.
    onset = median_onset_latency(recent_summaries or [])
    est_sleep = max(0.0, opportunity - onset) if opportunity is not None else None
    mode = decide_mode(est_sleep if est_sleep is not None else opportunity,
                       required_wake_time, debt, hint=hint)
    objective = _OBJECTIVE_BY_MODE[mode]

    # Recovery extends the duration target to repay (capped) debt.
    total_target = need_min
    if mode == NightMode.RECOVERY:
        total_target = int(min(need_min + 120, need_min + debt))
    targets = targets_for(mode, total_sleep_target_min=total_target)

    est_cycles = round(est_sleep / CYCLE_LEN_MIN, 1) if est_sleep else None

    # Smart-wake window + thermal phasing + setpoint bias per mode.
    phases: List[ThermalPhase] = [
        ThermalPhase("induction", "cool_fast",
                     "Cool quickly to drop core temperature and shorten sleep onset."),
        ThermalPhase("early_deep", "deep_cool",
                     "Aggressive cooling through the first cycles — deep sleep is "
                     "front-loaded and the most restorative."),
        ThermalPhase("maintenance", "maintain",
                     "Hold a stable, cool setpoint; minimise swings to protect "
                     "sleep maintenance (your #1 problem)."),
    ]

    if mode == NightMode.CONSTRAINED:
        window = min(base_window_min, 20)
        deep_bias_delta = -1.0   # cooler -> protect/boost deep
        rem_warm_delta = 0.0     # don't spend the short night chasing REM warmth
        # only add a REM-protect phase if at least ~3.5 cycles fit
        if est_cycles and est_cycles >= 3.5:
            phases.append(ThermalPhase("late_rem", "rem_protect",
                          "One late cycle fits — a small warm nudge to protect REM."))
            rem_warm_delta = 0.5
        phases.append(ThermalPhase("wake", "wake_ramp",
                      f"Wake you in light sleep within {window} min before "
                      f"{_fmt(required_wake_time)} — heat + gentle vibration, no audio."))
        strategy = (
            f"Short night (~{_hrs(opportunity)}). Prioritising quality per hour: fast "
            f"onset, deep sleep protected early, awakenings minimised, and waking you in "
            f"light sleep so you avoid grogginess. Duration is not chased."
        )
    elif mode == NightMode.RECOVERY:
        window = max(base_window_min, 45)
        deep_bias_delta = -0.5
        rem_warm_delta = 1.0     # support REM rebound (back-loaded, rebounds after debt)
        phases.append(ThermalPhase("late_rem", "rem_protect",
                      "Extended late cycles with a gentle warm bias to support REM "
                      "rebound; SWS rebound supported by early cooling."))
        phases.append(ThermalPhase("wake", "wake_ramp",
                      "Soft wake ceiling only — let sleep complete naturally to repay "
                      "sleep debt; smart wake fires late, in light sleep."))
        extra = max(0, total_target - need_min)
        strategy = (
            f"Off day / recovery. Repaying ~{_hrs(debt)} of sleep debt: extending sleep "
            f"toward {_hrs(total_target)} (+{extra} min), supporting REM and deep-sleep "
            f"rebound, and prioritising autonomic (HRV) recovery. No hard wake cutoff."
        )
    else:  # NORMAL
        window = base_window_min
        deep_bias_delta = 0.0
        rem_warm_delta = 0.5
        phases.append(ThermalPhase("late_rem", "rem_protect",
                      "Small warm bias in later cycles to support REM."))
        phases.append(ThermalPhase("wake", "wake_ramp",
                      f"Wake you in light sleep within {window} min before "
                      f"{_fmt(required_wake_time)}."))
        strategy = (
            f"Balanced night (~{_hrs(opportunity)} opportunity). Targeting full "
            f"architecture: deep 16-20%, REM 20-25%, efficiency ≥90%, minimal awakenings."
        )

    return SleepPlan(
        mode=mode,
        objective=objective,
        sleep_opportunity_min=opportunity,
        est_onset_latency_min=onset,
        est_sleep_min=est_sleep,
        est_cycles=est_cycles,
        sleep_debt_min=debt,
        smart_wake_window_min=window,
        required_wake_time=required_wake_time,
        targets=targets,
        thermal_phases=phases,
        deep_bias_delta_f=deep_bias_delta,
        rem_warm_delta_f=rem_warm_delta,
        strategy=strategy,
        bedtime=bedtime_guidance(required_wake_time, recent_summaries, need_min, onset),
    )


def _clock_min(dt) -> int:
    """Minutes past midnight for a datetime/time."""
    return int(dt.hour) * 60 + int(dt.minute)


def _fmt_clock(minutes) -> str:
    minutes = int(round(minutes)) % 1440
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def median_bedtime_clock(recent_summaries) -> Optional[int]:
    """Your habitual lights-out as minutes-past-midnight, robust across the midnight wrap
    (late bedtimes like 00:30 are folded so the median doesn't jump to noon). None if unknown."""
    vals = []
    for s in (recent_summaries or []):
        bt = getattr(s, "bedtime", None)
        if bt is None:
            continue
        m = _clock_min(bt)
        if m < 720:          # after-midnight bedtime -> treat as 24:xx for a stable evening median
            m += 1440
        vals.append(m)
    if not vals:
        return None
    vals.sort()
    n = len(vals)
    med = vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) // 2
    return med % 1440


@dataclass
class BedtimeGuidance:
    """The inverse of the wake time: when to be ASLEEP to hit your need, and — for fixed early
    wakes — how structurally short your habitual bedtime leaves you. Earlier bedtime is the only
    lever when you can't wake later."""
    recommended_lights_out: str                  # be ASLEEP by this clock time to hit need
    target_in_bed: str                           # ...so be IN BED by here (allowing onset)
    need_min: int
    est_onset_latency_min: float
    habitual_bedtime: Optional[str] = None
    achievable_sleep_min: Optional[float] = None
    structural_shortfall_min: Optional[float] = None
    go_earlier_min: Optional[int] = None         # how much earlier than habitual to hit need
    avg_tst_min: Optional[float] = None
    is_chronic_short: bool = False
    message: str = ""

    def to_dict(self) -> dict:
        return {
            "recommended_lights_out": self.recommended_lights_out,
            "target_in_bed": self.target_in_bed,
            "need_min": self.need_min, "need_h": round(self.need_min / 60, 1),
            "est_onset_latency_min": round(self.est_onset_latency_min, 1),
            "habitual_bedtime": self.habitual_bedtime,
            "achievable_sleep_min": round(self.achievable_sleep_min)
            if self.achievable_sleep_min is not None else None,
            "structural_shortfall_min": round(self.structural_shortfall_min)
            if self.structural_shortfall_min is not None else None,
            "go_earlier_min": self.go_earlier_min,
            "avg_tst_min": self.avg_tst_min, "is_chronic_short": self.is_chronic_short,
            "message": self.message,
        }


def bedtime_guidance(required_wake_time: Optional[datetime], recent_summaries=None,
                     need_min: int = SLEEP_NEED_MIN,
                     onset_min: Optional[float] = None) -> Optional[BedtimeGuidance]:
    """Compute when to be asleep/in bed to hit ``need_min`` before ``required_wake_time``, and the
    structural shortfall your habitual bedtime leaves on the table. None without a wake time."""
    if required_wake_time is None:
        return None
    onset = onset_min if onset_min is not None else median_onset_latency(recent_summaries or [])
    wake_clock = _clock_min(required_wake_time)
    asleep_by = (wake_clock - need_min) % 1440          # be ASLEEP by here to bank the full need
    in_bed_by = (wake_clock - need_min - onset) % 1440  # ...get in bed earlier to allow onset

    chronic = chronic_shortfall(recent_summaries or [], need_min=need_min)
    habitual = median_bedtime_clock(recent_summaries or [])
    achievable = shortfall = go_earlier = hab_str = None
    if habitual is not None:
        in_bed = (wake_clock - habitual) % 1440         # time in bed at your usual bedtime
        achievable = max(0.0, in_bed - onset)
        shortfall = max(0.0, need_min - achievable)
        gap = (habitual - in_bed_by) % 1440             # how much later than ideal you turn in
        go_earlier = int(gap) if gap <= 720 else 0      # >12 h -> you're already earlier than ideal
        hab_str = _fmt_clock(habitual)

    if shortfall and shortfall >= 45:
        msg = (f"To get your {round(need_min/60,1)} h before {_fmt_clock(wake_clock)}, be asleep by "
               f"{_fmt_clock(asleep_by)} (in bed ~{_fmt_clock(in_bed_by)}). At your usual "
               f"{hab_str} you only get ~{round(achievable/60,1)} h — about {round(shortfall/60,1)} h "
               f"short. Moving lights-out ~{go_earlier} min earlier is the highest-leverage fix.")
    else:
        msg = (f"Be asleep by {_fmt_clock(asleep_by)} (in bed ~{_fmt_clock(in_bed_by)}) to bank your "
               f"{round(need_min/60,1)} h before {_fmt_clock(wake_clock)}.")

    return BedtimeGuidance(
        recommended_lights_out=_fmt_clock(asleep_by), target_in_bed=_fmt_clock(in_bed_by),
        need_min=need_min, est_onset_latency_min=onset, habitual_bedtime=hab_str,
        achievable_sleep_min=achievable, structural_shortfall_min=shortfall,
        go_earlier_min=go_earlier, avg_tst_min=chronic["avg_tst_min"],
        is_chronic_short=chronic["is_chronic"], message=msg)


def median_onset_latency(recent_summaries, default_min: float = 15.0,
                         lo: float = 5.0, hi: float = 45.0) -> float:
    """Your typical time to fall asleep, from recent nights' measured onset latency.

    Lets the plan judge cycles from when you ACTUALLY fall asleep rather than when you get
    into bed, so lying awake doesn't inflate the schedule. Bounded for robustness.
    """
    vals = [float(getattr(s, "sleep_onset_latency_min", None))
            for s in (recent_summaries or [])
            if getattr(s, "sleep_onset_latency_min", None) is not None]
    if not vals:
        return default_min
    vals.sort()
    n = len(vals)
    med = vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) / 2.0
    return max(lo, min(hi, med))


def _hrs(minutes: Optional[float]) -> str:
    if not minutes:
        return "—"
    h = int(minutes // 60)
    m = int(minutes % 60)
    return f"{h}h{m:02d}m" if m else f"{h}h"


def _fmt(dt: Optional[datetime]) -> str:
    return dt.strftime("%H:%M") if isinstance(dt, datetime) else "your wake time"
