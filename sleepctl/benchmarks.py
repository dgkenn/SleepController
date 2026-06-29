"""Literature-backed sleep benchmarks and the "perfect sleep" index.

The controller optimises against *evidence-based* targets rather than raw duration.
Three night modes carry different targets and scoring weights, because the right
goal depends on the schedule:

  NORMAL      adequate opportunity, alarm-bounded   -> hit all benchmarks
  CONSTRAINED short night, hard early work wake      -> maximise quality PER HOUR
                                                        (protect deep, fast onset,
                                                         low WASO, wake in light sleep);
                                                        duration is de-emphasised
  RECOVERY    off day / repaying sleep debt          -> maximise total recovery:
                                                        extend sleep, REM + SWS rebound,
                                                        autonomic (HRV) recovery

Sources (via PubMed):
  - Ohayon M, et al. National Sleep Foundation's sleep quality recommendations.
    Sleep Health 2017;3(1):6-19. doi:10.1016/j.sleh.2016.11.006
    Consensus continuity indicators of good sleep: sleep-onset latency (SOL),
    awakenings >5 min, wake-after-sleep-onset (WASO), and sleep efficiency.
    Appropriate (young/working adults): SOL <=15 min, efficiency >=85%,
    WASO <=20 min, awakenings >5 min <=1.
  - Ohayon M, Carskadon M, Guilleminault C, Vitiello M. Meta-analysis of
    quantitative sleep parameters from childhood to old age in healthy individuals.
    Sleep 2004;27(7):1255-73. doi:10.1093/sleep/27.7.1255
    Normative architecture for healthy adults: slow-wave (deep, N3) ~16-20% of
    total sleep in young adults (declines with age); REM ~20-25%; N1 ~5%.
  - Libert J-P. Thermal regulation during sleep. Rev Neurol 2003;159(11 Suppl):6S30-4.
    Thermoregulation persists during slow-wave sleep but is largely suspended during
    REM -> cooling protects/promotes deep sleep; REM is thermosensitive (a small late
    warm bias supports REM, bounded for a hot sleeper).
  - Eight Sleep Autopilot RCT (SLEEP 2024, abs. 0462): cooler offset -> more deep;
    warmer offset -> more REM; escalate if prior night deep <15% or REM <20%.

These are population references, not clinical thresholds; the per-user ML layer
personalises around them.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class NightMode(str, Enum):
    NORMAL = "normal"
    CONSTRAINED = "constrained"   # short, work-bounded night
    RECOVERY = "recovery"         # off day / sleep-debt payback


# Default sleep need for a healthy working adult (AASM/SRS >=7h; NSF 7-9h).
SLEEP_NEED_MIN = 480
# A constrained night is one whose opportunity falls below this (≈ < 6.5 h).
CONSTRAINED_OPPORTUNITY_MIN = 390
# Mean adult NREM-REM cycle length used for cycle counting / wake alignment.
CYCLE_LEN_MIN = 90


@dataclass(frozen=True)
class Targets:
    """Per-mode benchmark targets (fractions are of total sleep time)."""

    # continuity (Ohayon 2017 NSF)
    sol_max_min: float
    efficiency_min: float
    waso_max_min: float
    awakenings_max: int
    # architecture (Ohayon 2004)
    deep_pct_min: float
    deep_pct_ideal: float
    rem_pct_min: float
    rem_pct_ideal: float
    # duration / recovery
    total_sleep_target_min: int
    hrv_recovery_weighted: bool
    # scoring weights (sum need not be 1; normalised in the index)
    weights: dict = field(default_factory=dict)
    # human-readable one-liner describing the objective
    rationale: str = ""


def targets_for(mode: NightMode, total_sleep_target_min: int = SLEEP_NEED_MIN) -> Targets:
    """Return the literature-anchored targets + scoring weights for a night mode."""
    if mode == NightMode.CONSTRAINED:
        # Short work night: protect the homeostatically-defended, front-loaded deep
        # sleep, fall asleep fast (don't waste opportunity), minimise WASO, and end on
        # a light-sleep cycle boundary. Duration is intentionally de-weighted.
        return Targets(
            sol_max_min=12, efficiency_min=0.92, waso_max_min=15, awakenings_max=1,
            deep_pct_min=0.18, deep_pct_ideal=0.22,
            rem_pct_min=0.16, rem_pct_ideal=0.20,
            total_sleep_target_min=total_sleep_target_min,
            hrv_recovery_weighted=False,
            weights={"waso": 0.28, "awakenings": 0.18, "efficiency": 0.18,
                     "deep": 0.16, "sol": 0.12, "rem": 0.05, "total": 0.03},
            rationale="Short night: quality per hour. Protect deep sleep early, fall "
                      "asleep fast, minimise awakenings, wake in light sleep.",
        )
    if mode == NightMode.RECOVERY:
        # Off day / sleep-debt payback: extend sleep, support REM rebound (back-loaded,
        # rebounds after restriction) and SWS rebound, weight autonomic (HRV) recovery.
        return Targets(
            sol_max_min=20, efficiency_min=0.86, waso_max_min=25, awakenings_max=2,
            deep_pct_min=0.18, deep_pct_ideal=0.23,
            rem_pct_min=0.22, rem_pct_ideal=0.26,
            total_sleep_target_min=total_sleep_target_min,
            hrv_recovery_weighted=True,
            weights={"total": 0.30, "rem": 0.20, "deep": 0.16, "hrv": 0.14,
                     "waso": 0.10, "efficiency": 0.06, "awakenings": 0.04},
            rationale="Off day: maximise recovery. Extend sleep to repay debt, support "
                      "REM and deep-sleep rebound, prioritise autonomic recovery.",
        )
    # NORMAL
    return Targets(
        sol_max_min=15, efficiency_min=0.90, waso_max_min=20, awakenings_max=1,
        deep_pct_min=0.16, deep_pct_ideal=0.20,
        rem_pct_min=0.20, rem_pct_ideal=0.25,
        total_sleep_target_min=total_sleep_target_min,
        hrv_recovery_weighted=False,
        weights={"total": 0.18, "efficiency": 0.16, "deep": 0.16, "rem": 0.16,
                 "waso": 0.18, "awakenings": 0.10, "sol": 0.06},
        rationale="Balanced night: meet duration, architecture and continuity targets.",
    )


def _ramp(value: float, floor: float, ideal: float) -> float:
    """0 at/below floor, 1 at/above ideal, linear between (higher is better)."""
    if ideal == floor:
        return 1.0 if value >= ideal else 0.0
    return max(0.0, min(1.0, (value - floor) / (ideal - floor)))


def _ramp_down(value: float, ideal: float, ceiling: float) -> float:
    """1 at/below ideal, 0 at/above ceiling, linear between (lower is better)."""
    if ceiling == ideal:
        return 1.0 if value <= ideal else 0.0
    return max(0.0, min(1.0, (ceiling - value) / (ceiling - ideal)))


def _safe_float(value, default: float = 0.0) -> float:
    """Tolerate None / non-numeric / NaN / inf inputs — the scorer must never crash or NaN out."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return default
    return default if (math.isnan(f) or math.isinf(f)) else f


def _norm_efficiency(value) -> float:
    """Accept sleep efficiency as either a fraction (0.92) or a percentage (92) and return a
    bounded 0..1 fraction. Removes a silent saturation bug when sources disagree on units."""
    e = _safe_float(value, 0.0)
    if e > 1.5:           # clearly given as a percent
        e = e / 100.0
    return max(0.0, min(1.0, e))


def _debt_factor(debt_min: float) -> float:
    """0 at no debt → 1 at severe (~6 h). Monotonic + clamped (no score discontinuities)."""
    return max(0.0, min(1.0, _safe_float(debt_min) / 360.0))


def _debt_adjust_targets(t: "Targets", df: float, mode: "NightMode" = NightMode.NORMAL) -> "Targets":
    """Make the benchmarks debt-aware (PubMed-grounded), with conflicts resolved by mode.

    As debt rises a GOOD recovery night is deep-heavy and long, with onset/efficiency expected
    tighter. The high-confidence moves:
      - up-weight DEEP (SWS rebounds first/most — Van Dongen 2003 doi:10.1093/sleep/26.2.117);
        a deep rebound already isn't penalized (the deep ramp caps at 1.0), so this *rewards* it;
      - up-weight TOTAL SLEEP (recovery is dose-driven — Banks 2010 doi:10.1093/sleep/33.8.1013)
        — but ONLY when the opportunity allows recovery. On a CONSTRAINED (short, work-bounded)
        night you physically cannot repay debt, so up-weighting total would unfairly penalize an
        unavoidably short night. (mode × debt conflict resolved.)
      - tighten onset latency (high sleep pressure → faster onset), floored so it can never
        collapse to ~0. Efficiency is intentionally NOT tightened: its ramp window is narrow, so
        a small floor shift would wrongly zero-out a perfectly good ~92% night, and the evidence
        for tightening it is the weakest of the set (age/timing-gated).

    REM is deliberately NOT touched: its direction is night-dependent (suppressed on recovery
    night 1, rebounds nights 2+ — De Gennaro 2009 doi:10.1016/j.bbr.2009.09.030; Buguet 1995
    doi:10.1111/j.1365-2869.1995.tb00173.x) and RECOVERY mode already rewards the rebound, so a
    blunt debt down-weight would conflict with both (and REM was the least-certain finding).
    Continuity weights (waso/awakenings) are left intact so sleep MAINTENANCE — the user's #1
    priority — is never buried by the recovery boost. Magnitudes are conservative; direction is
    well-supported.
    """
    from dataclasses import replace
    w = dict(t.weights)
    if "deep" in w:
        w["deep"] = w["deep"] * (1.0 + 0.5 * df)
    if "total" in w and mode != NightMode.CONSTRAINED:
        w["total"] = w["total"] * (1.0 + 0.6 * df)
    return replace(t, weights=w, sol_max_min=max(4.0, t.sol_max_min * (1.0 - 0.3 * df)))


def perfect_sleep_index(summary, mode: NightMode = NightMode.NORMAL,
                        targets: Optional[Targets] = None, debt_min: float = 0.0) -> dict:
    """Score a NightSummary 0-100 against the active mode's benchmarks.

    Returns {score, mode, components: {metric: 0..1}, targets_met: [...], notes}.
    ``summary`` needs total_sleep_min, deep_min, rem_min, sleep_efficiency,
    wake_events, and optionally waso_min / sleep_onset_latency_min / avg_hrv.
    """
    t = targets or targets_for(mode)
    if debt_min and debt_min > 0:
        t = _debt_adjust_targets(t, _debt_factor(debt_min), mode)

    # --- input hardening: tolerate missing / mis-typed / out-of-range data ---------------
    tst_raw = getattr(summary, "total_sleep_min", None)
    tst = max(1.0, _safe_float(tst_raw))
    # architecture fractions are physically bounded to [0, 1] (guards bad data where a stage
    # exceeds total sleep, or a near-zero tst inflating the ratio).
    deep_pct = max(0.0, min(1.0, _safe_float(getattr(summary, "deep_min", 0)) / tst))
    rem_pct = max(0.0, min(1.0, _safe_float(getattr(summary, "rem_min", 0)) / tst))
    eff = _norm_efficiency(getattr(summary, "sleep_efficiency", 0))
    waso = getattr(summary, "waso_min", None)
    if waso is None:
        waso = _safe_float(getattr(summary, "wake_events", 0)) * 7.0  # rough proxy if WASO absent
    sol = getattr(summary, "sleep_onset_latency_min", None)
    awak = max(0.0, _safe_float(getattr(summary, "wake_events", 0)))
    hrv = getattr(summary, "avg_hrv", None)

    comp = {
        "deep": _ramp(deep_pct, t.deep_pct_min, t.deep_pct_ideal),
        "rem": _ramp(rem_pct, t.rem_pct_min, t.rem_pct_ideal),
        "efficiency": _ramp(eff, t.efficiency_min, max(t.efficiency_min + 0.01, 0.95)),
        "waso": _ramp_down(max(0.0, _safe_float(waso)), t.waso_max_min, t.waso_max_min + 30),
        "awakenings": _ramp_down(awak, t.awakenings_max, t.awakenings_max + 3),
        "total": _ramp(tst, t.total_sleep_target_min * 0.6, t.total_sleep_target_min),
    }
    if sol is not None:
        comp["sol"] = _ramp_down(max(0.0, _safe_float(sol)), t.sol_max_min, t.sol_max_min + 25)
    if hrv is not None and t.hrv_recovery_weighted:
        # relative: 70 ms is a reasonable healthy nighttime average anchor
        comp["hrv"] = max(0.0, min(1.0, _safe_float(hrv) / 70.0))

    total_w = sum(t.weights.get(k, 0) for k in comp)
    if total_w <= 0:
        score = 0.0
    else:
        score = 100.0 * sum(comp[k] * t.weights.get(k, 0) for k in comp) / total_w
    score = max(0.0, min(100.0, score))  # invariant: always a clean 0..100

    # Honesty signal: distinguish a genuinely bad night from one we couldn't score. <1 h of
    # recorded sleep (or no total at all) means the architecture %s aren't trustworthy.
    missing = [name for name, ok in (
        ("total_sleep_min", tst_raw not in (None, 0)),
        ("deep_min", getattr(summary, "deep_min", None) is not None),
        ("rem_min", getattr(summary, "rem_min", None) is not None),
    ) if not ok]
    insufficient = (tst_raw is None) or (_safe_float(tst_raw) < 60.0)

    met = [k for k, v in comp.items() if v >= 0.999]
    return {
        "score": round(score, 1),
        "mode": mode.value,
        "components": {k: round(v, 3) for k, v in comp.items()},
        "targets_met": met,
        "insufficient_data": insufficient,
        "missing": missing,
        "rationale": t.rationale,
    }


def chronic_shortfall(recent_summaries, need_min: int = SLEEP_NEED_MIN,
                      horizon_nights: int = 14) -> dict:
    """Is the user *structurally* short — averaging well under their need night after night —
    as opposed to carrying acute debt from one or two bad nights?

    For someone with fixed very-early wakes this is the dominant regime: the only real fix is an
    earlier bedtime (Rupp 2009 banking aside, you can't wake later). Recovery from chronic
    restriction also spans multiple nights, so a sustained deficit matters more than its day-to-day
    swings (Van Dongen 2003, doi:10.1093/sleep/26.2.117; Rupp 2009, doi:10.1093/sleep/32.3.311).

    Returns the trailing-average total sleep, the mean nightly shortfall vs need, the fraction of
    short nights, and an ``is_chronic`` flag (averaging >~1 h under need across enough nights).
    """
    nights = [s for s in list(recent_summaries)[-horizon_nights:]
              if getattr(s, "total_sleep_min", None)]
    n = len(nights)
    if n == 0:
        return {"avg_tst_min": None, "mean_shortfall_min": 0.0, "short_nights_frac": 0.0,
                "n_nights": 0, "is_chronic": False}
    tsts = [float(s.total_sleep_min) for s in nights]
    avg = sum(tsts) / n
    mean_short = max(0.0, need_min - avg)
    short_frac = sum(1 for t in tsts if t < need_min - 30) / n
    # Chronic = a real sustained deficit (>1 h under need on average) over a few nights, not a blip.
    is_chronic = bool(n >= 3 and mean_short >= 60 and short_frac >= 0.5)
    return {"avg_tst_min": round(avg), "mean_shortfall_min": round(mean_short),
            "short_nights_frac": round(short_frac, 2), "n_nights": n,
            "is_chronic": is_chronic}


def sleep_debt_min(recent_summaries, need_min: int = SLEEP_NEED_MIN,
                   horizon_nights: int = 14, cap_min: int = 600) -> float:
    """Rolling cumulative sleep debt over the last ``horizon_nights`` (capped).

    Debt = sum of max(0, need - total_sleep) across recent nights, with mild decay so
    older deficits matter less. Used to set the recovery-night extension target.
    """
    debt = 0.0
    nights = list(recent_summaries)[-horizon_nights:]
    n = len(nights)
    for i, s in enumerate(nights):
        tst = float(getattr(s, "total_sleep_min", 0) or 0)
        deficit = max(0.0, need_min - tst)
        # recency weight: most recent night full weight, linearly down to ~0.4
        w = 0.4 + 0.6 * (i + 1) / max(1, n)
        debt += deficit * w
    return min(cap_min, debt)
