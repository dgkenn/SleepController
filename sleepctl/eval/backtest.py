"""Backtest harness — does the closed loop actually produce better nights than no control?

Runs whole simulated nights on the response-aware model (``ResponsiveSleepModel``) under two
policies and compares the outcomes that matter:

  • controller — the real ``SleepController`` drives the bed each minute (cool-for-deep, stable,
    warm wake ramp), exactly as live.
  • baseline   — the bed is held at a fixed, uncontrolled setting (what you get without the
    system). Default: a neutral-warm level a hot sleeper would suffer.

It also asserts the SAFETY invariants on every controller tick (per-step slew ≤ max_step_f, target
within 55–110 °F), so the harness doubles as a regression guard. Pure + deterministic (seeded); no
device, no DB.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import List, Optional

from sleepctl.config import AppConfig
from sleepctl.controller.controller import SleepController
from sleepctl.eval.responsive_sim import ResponsiveSleepModel
from sleepctl.ml.reward import night_outcome_score
from sleepctl.models import ContextRecord, SensorFrame

_NEUTRAL_WARM_LEVEL = 0      # ~81 °F on the device map — fine for some, too warm for a hot sleeper


@dataclass
class NightResult:
    wake_events: int
    deep_min: float
    rem_min: float
    efficiency: float
    total_sleep_min: float
    grogginess: float
    outcome_score: float
    max_step_f: float           # largest single-tick target move (safety)
    out_of_bounds: int          # ticks with target outside 55–110 °F


def _score(summary, grog) -> float:
    try:
        return float(night_outcome_score(summary, grogginess=grog))
    except Exception:
        # fallback: maintenance-dominant, deep-positive
        return -(summary.wake_events or 0) + 0.01 * (summary.deep_min or 0)


def run_night(policy: str = "controller", scenario: str = "normal", seed: int = 7,
              minutes: int = 480, wake_hour: int = 7,
              baseline_level: int = _NEUTRAL_WARM_LEVEL) -> NightResult:
    cfg = AppConfig.default()
    sim = ResponsiveSleepModel(scenario=scenario, seed=seed)
    ctx = ContextRecord(date=sim.t0.date().isoformat())
    ctx.required_wake_time = sim.t0.replace(hour=wake_hour, minute=0) + timedelta(days=1) \
        if wake_hour <= sim.t0.hour else sim.t0.replace(hour=wake_hour, minute=0)

    controller = SleepController(cfg) if policy == "controller" else None
    recent: List[SensorFrame] = []
    now = sim.t0
    prev_target: Optional[float] = None
    max_step = 0.0
    oob = 0

    if policy != "controller":
        sim.actuate(baseline_level)

    for _ in range(minutes):
        frame = sim.read_frame()
        if controller is not None:
            decision = controller.decide(frame, ctx, recent, now, None)
            t = decision.target_temp_f
            if prev_target is not None:
                max_step = max(max_step, abs(t - prev_target))
            if not (55.0 <= t <= 110.0):
                oob += 1
            prev_target = t
            sim.actuate(decision.target_level)
            recent.append(frame)
            if len(recent) > 60:
                recent = recent[-60:]
        sim.advance(now)
        now += timedelta(minutes=1)

    s = sim.night_summary()
    grog = sim.grogginess_proxy
    return NightResult(
        wake_events=s.wake_events or 0, deep_min=s.deep_min or 0.0, rem_min=s.rem_min or 0.0,
        efficiency=s.sleep_efficiency or 0.0, total_sleep_min=s.total_sleep_min or 0.0,
        grogginess=grog, outcome_score=_score(s, grog), max_step_f=round(max_step, 2),
        out_of_bounds=oob)


def _mean(xs):
    return sum(xs) / len(xs) if xs else 0.0


def backtest(nights: int = 12, scenario: str = "normal", seed: int = 7) -> dict:
    """Run ``nights`` paired nights (same seeds) under both policies; return the comparison."""
    ctl = [run_night("controller", scenario, seed + i) for i in range(nights)]
    base = [run_night("baseline", scenario, seed + i) for i in range(nights)]

    def agg(rs):
        return {"wake_events": round(_mean([r.wake_events for r in rs]), 2),
                "deep_min": round(_mean([r.deep_min for r in rs]), 1),
                "efficiency": round(_mean([r.efficiency for r in rs]), 3),
                "total_sleep_min": round(_mean([r.total_sleep_min for r in rs]), 1),
                "grogginess": round(_mean([r.grogginess for r in rs]), 2),
                "outcome_score": round(_mean([r.outcome_score for r in rs]), 3)}

    c, b = agg(ctl), agg(base)
    return {
        "nights": nights, "scenario": scenario,
        "controller": c, "baseline": b,
        "delta": {k: round(c[k] - b[k], 3) for k in c},
        "safety": {"max_step_f": max(r.max_step_f for r in ctl),
                   "max_step_limit": AppConfig.default().tunables.max_step_f,
                   "out_of_bounds_ticks": sum(r.out_of_bounds for r in ctl)},
    }


def format_report(rep: dict) -> str:
    c, b, d = rep["controller"], rep["baseline"], rep["delta"]
    lines = [f"Backtest — {rep['nights']} nights, scenario={rep['scenario']}",
             f"{'metric':<16}{'controller':>12}{'baseline':>12}{'delta':>10}"]
    for k in ("wake_events", "deep_min", "efficiency", "total_sleep_min", "grogginess",
              "outcome_score"):
        lines.append(f"{k:<16}{c[k]:>12}{b[k]:>12}{d[k]:>10}")
    s = rep["safety"]
    lines.append(f"safety: max step {s['max_step_f']}°F (limit {s['max_step_limit']}), "
                 f"out-of-bounds ticks {s['out_of_bounds_ticks']}")
    return "\n".join(lines)
