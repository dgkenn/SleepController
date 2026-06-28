"""Learn the THERMAL wake maneuver per-person — what bed temperature wakes YOU least groggy.

Some people surface clearest from a warming ramp (distal warming → core-temp rise → alertness);
others wake sharper from a cooler bed (cold is arousing). For a hot sleeper it's especially
personal. Rather than guess, this learns the wake-ramp temperature that minimizes YOUR morning
grogginess — and, crucially, it ACTIVELY EXPLORES (the old direction-only learner just held when
the night-to-night temperature never varied). Each night it jitters the wake temp a little around
the current best, records what it used, and converges on the per-person optimum.

  • learn_thermal_wake(records): bins the recorded wake temps by their grogginess and picks the
    coolest-/warmest-performing setting, shrunk toward the default by sample size and clamped.
  • next_wake_f(best, night): adds a small rotating ±jitter so the grogginess-vs-temp curve gets
    sampled (deterministic by date — no randomness in the control loop).

Conservative + bounded (70–86 °F, the wake-ramp safety range); needs enough nights before it moves.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import List

WAKE_F_BOUNDS = (70.0, 86.0)
_EXPLORE_PATTERN = (0.0, 1.0, -1.0)     # rotated by night so the curve is sampled around best


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _norm_grog(g: float) -> float:
    return max(0.0, min(1.0, float(g) / 10.0))     # engine 0-10 grogginess scale


@dataclass
class ThermalWakeManeuver:
    wake_f: float                # learned best wake-ramp temperature
    direction: str               # warmer | cooler | neutral (vs the default)
    n: int
    is_personalized: bool
    rationale: str

    def to_dict(self) -> dict:
        return {"wake_f": round(self.wake_f, 1), "direction": self.direction, "n": self.n,
                "is_personalized": self.is_personalized, "rationale": self.rationale}


def learn_thermal_wake(records: List[dict], base_f: float = 74.0, min_nights: int = 8,
                       min_per_bucket: int = 2, bounds=WAKE_F_BOUNDS) -> ThermalWakeManeuver:
    """records: [{'wake_thermal_f': float, 'grogginess': float}, ...]. Returns the learned wake
    temperature that left you least groggy (shrunk toward the default, clamped to safe bounds)."""
    usable = [r for r in records
              if r.get("grogginess") is not None and r.get("wake_thermal_f") is not None]
    n = len(usable)
    if n < min_nights:
        return ThermalWakeManeuver(base_f, "neutral", n, False,
                                   f"learning — {n}/{min_nights} nights with a check-in")

    buckets = defaultdict(list)
    for r in usable:
        buckets[round(float(r["wake_thermal_f"]))].append(_norm_grog(r["grogginess"]))
    cand = [(f, sum(gs) / len(gs)) for f, gs in buckets.items() if len(gs) >= min_per_bucket]
    if not cand:
        return ThermalWakeManeuver(base_f, "neutral", n, False,
                                   "not enough repeated wake temps yet to compare")

    best_raw = min(cand, key=lambda c: c[1])[0]            # coolest-grogginess wake temp tried
    shrink = min(1.0, (n - min_nights + 1) / 8.0)
    best = round(_clamp(base_f + (best_raw - base_f) * shrink, *bounds), 1)
    is_pers = abs(best - base_f) >= 0.5
    direction = "warmer" if best > base_f else "cooler" if best < base_f else "neutral"
    rationale = (f"a {direction} wake (~{best:.0f} °F) leaves you least groggy"
                 if is_pers else "your default wake temperature already fits")
    return ThermalWakeManeuver(best, direction, n, is_pers, rationale)


def next_wake_f(best_f: float, night_index: int, bounds=WAKE_F_BOUNDS, jitter: float = 2.0) -> float:
    """Tonight's wake temperature: the learned best plus a small rotating exploration jitter so
    the grogginess-vs-temperature curve keeps getting sampled. Deterministic by night index."""
    delta = _EXPLORE_PATTERN[night_index % len(_EXPLORE_PATTERN)]
    return round(_clamp(best_f + jitter * delta, *bounds), 1)


def thermal_wake_records(repo, nights: int = 30) -> List[dict]:
    """Join the wake log's recorded wake temperature with the morning grogginess check-in."""
    try:
        from app import bridge
        logs = bridge.read_wake_logs(repo.conn, nights)
    except Exception:
        return []
    out: List[dict] = []
    for row in logs:
        ctx = repo.get_context(row["date"]) if hasattr(repo, "get_context") else None
        g = getattr(ctx, "grogginess", None) if ctx else None
        if g is None or row.get("wake_thermal_f") is None:
            continue
        out.append({"wake_thermal_f": row["wake_thermal_f"], "grogginess": g})
    return out
