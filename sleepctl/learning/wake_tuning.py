"""Personalized wake tuning — learn YOUR grogginess curve and auto-tune the alarm to it.

The orchestrator wakes in light sleep within a window; but the right window width and how eagerly
to lift on a light signal are personal. This closes the loop with the morning check-in: it joins
each night's wake conditions (the window used, and whether you were woken from a light moment or
forced near the deadline) with that morning's reported grogginess, and nudges two knobs:

  • window_min — if WIDER windows track with MORE grogginess for you (woken too early, losing
    sleep), narrow it; if wider tracks with LESS grogginess (more chance to catch a light moment),
    widen it.
  • p_wake_liftable — if your forced/deep wakes are much groggier than your light wakes, lower the
    bar so the orchestrator lifts you on a light moment more readily (catches more of them).

Conservative by construction: needs enough nights, shrinks the adjustment toward the evidence-
based defaults by sample size, and clamps to safe bounds. Revealed-preference, like the perfect-
sleep weights and the wake-ramp learners.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional


def _norm_grog(g: float) -> float:
    """Grogginess to 0..1 (higher = worse). Engine convention is a 0-10 scale (see ml/reward.py),
    so normalize by 10. (Direction is scale-invariant for the correlation regardless.)"""
    return max(0.0, min(1.0, float(g) / 10.0))


def _pearson(xs: List[float], ys: List[float]) -> float:
    n = len(xs)
    if n < 3:
        return 0.0
    mx = sum(xs) / n
    my = sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx <= 0 or syy <= 0:
        return 0.0
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    return max(-1.0, min(1.0, sxy / (sxx ** 0.5 * syy ** 0.5)))


@dataclass
class WakeTuning:
    window_min: int
    p_wake_liftable: float
    n: int
    is_personalized: bool
    rationale: str

    def to_dict(self) -> dict:
        return {"window_min": self.window_min, "p_wake_liftable": round(self.p_wake_liftable, 3),
                "n": self.n, "is_personalized": self.is_personalized, "rationale": self.rationale}


def learn_wake_tuning(records: List[dict], base_window: int = 30, base_liftable: float = 0.45,
                      min_nights: int = 8, max_window_shift: int = 10,
                      max_lift_shift: float = 0.15) -> WakeTuning:
    """records: [{'window_min': int, 'grogginess': float, 'forced': bool}, ...] (one per night
    that has a grogginess check-in). Returns the tuned window + liftable bar for this user."""
    usable = [r for r in records
              if r.get("grogginess") is not None and r.get("window_min")]
    n = len(usable)
    if n < min_nights:
        return WakeTuning(base_window, base_liftable, n, False,
                          f"learning — {n}/{min_nights} nights with a grogginess check-in")

    grog = [_norm_grog(r["grogginess"]) for r in usable]
    wins = [float(r["window_min"]) for r in usable]
    shrink = min(1.0, (n - min_nights + 1) / 10.0)

    # Window: correlate window width with grogginess. corr>0 (wider→groggier) => narrow.
    corr = _pearson(wins, grog)
    window = base_window - corr * max_window_shift * shrink
    window = int(round(max(10, min(45, window))))

    # Liftable bar: if forced wakes are groggier than light wakes, lower the bar (lift earlier).
    forced = [g for g, r in zip(grog, usable) if r.get("forced")]
    light = [g for g, r in zip(grog, usable) if not r.get("forced")]
    liftable = base_liftable
    gap = 0.0
    if forced and light:
        gap = (sum(forced) / len(forced)) - (sum(light) / len(light))
        liftable = base_liftable - max(-1.0, min(1.0, gap)) * max_lift_shift * shrink
    liftable = max(0.30, min(0.70, liftable))

    is_pers = abs(window - base_window) >= 1 or abs(liftable - base_liftable) >= 0.02
    bits = []
    if abs(window - base_window) >= 1:
        bits.append(f"window {'widened' if window > base_window else 'narrowed'} to {window} min "
                    f"({'wider helps you' if corr < 0 else 'you wake groggy when woken too early'})")
    if abs(liftable - base_liftable) >= 0.02:
        bits.append(f"lift bar {'lowered' if liftable < base_liftable else 'raised'} to "
                    f"{liftable:.2f} (forced wakes are {'groggier' if gap > 0 else 'fine'} for you)")
    rationale = "; ".join(bits) if bits else "your defaults already fit — no change"
    return WakeTuning(window, liftable, n, is_pers, rationale)


def wake_tuning_records(repo, nights: int = 30) -> List[dict]:
    """Join the wake log with the morning check-in grogginess for the learner."""
    try:
        from app import bridge
        logs = bridge.read_wake_logs(repo.conn, nights)
    except Exception:
        return []
    out: List[dict] = []
    for row in logs:
        ctx = repo.get_context(row["date"]) if hasattr(repo, "get_context") else None
        g = getattr(ctx, "grogginess", None) if ctx else None
        if g is None:
            continue
        out.append({"window_min": row.get("window_min"), "grogginess": g,
                    "forced": bool(row.get("forced"))})
    return out
