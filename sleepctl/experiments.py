"""n-of-1 self-experiment engine (multi-cycle, washout, paired analysis).

A rigorous single-subject trial. Following the n-of-1 evidence (Blackston 2019,
DOI 10.3390/healthcare7040137; Vrinten 2015, DOI 10.1136/bmjopen-2015-007863), this uses:
  - **multiple crossover cycles** with **counterbalanced** arm order (controls slow drift),
  - **washout nights** between periods (controls carryover — the #1 false-positive source),
  - a **paired within-cycle analysis** (each cycle is its own control) instead of pooling all
    nights, which mitigates the serial autocorrelation of nightly sleep metrics.

A night is assigned to arm 'a', 'b', or 'washout' by a deterministic schedule; outcomes are
compared as the mean of per-cycle (B-A) contrasts, with a credible interval.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import List, Optional

_LOWER_BETTER = {"wake_events", "waso_min", "sleep_onset_latency_min"}
_METRIC_COLS = {
    "wake_events", "waso_min", "sleep_efficiency", "deep_min", "rem_min",
    "total_sleep_min", "sleep_onset_latency_min", "avg_hrv", "outcome_score",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row(r) -> dict:
    d = dict(r)
    for k in ("arm_a", "arm_b", "assignments", "result"):
        d[k] = json.loads(d[k]) if d.get(k) else ({} if k != "result" else None)
    return d


def _schedule_slot(n: int, period: int, washout: int) -> str:
    """The planned slot for the n-th assigned night (0-based): 'a' | 'b' | 'washout'.

    One cycle = [armX]*period, [washout], [armY]*period, [washout]; arm order is
    counterbalanced (cycle 0 -> A first, cycle 1 -> B first, ...)."""
    cycle_len = 2 * period + 2 * washout
    cycle, pos = divmod(n, cycle_len)
    first, second = ("a", "b") if cycle % 2 == 0 else ("b", "a")
    if pos < period:
        return first
    if pos < period + washout:
        return "washout"
    if pos < 2 * period + washout:
        return second
    return "washout"


def _cycle_of(n: int, period: int, washout: int) -> int:
    return n // (2 * period + 2 * washout)


def create_experiment(repo, spec: dict) -> dict:
    metric = spec.get("metric", "wake_events")
    if metric not in _METRIC_COLS:
        raise ValueError(f"unknown metric {metric!r}")
    cur = repo.conn.execute(
        "INSERT INTO experiments (name, hypothesis, variable, arm_a, arm_b, metric, "
        "min_nights_per_arm, washout_nights, status, created, assignments, result) "
        "VALUES (?,?,?,?,?,?,?,?,'active',?,?,NULL)",
        (spec.get("name", "experiment"), spec.get("hypothesis", ""), spec.get("variable", ""),
         json.dumps(spec.get("arm_a", {"label": "control", "params": {}})),
         json.dumps(spec.get("arm_b", {"label": "treatment", "params": {}})),
         metric, int(spec.get("min_nights_per_arm", 3)), int(spec.get("washout_nights", 1)),
         _now(), json.dumps({})),
    )
    repo.conn.commit()
    return get_experiment(repo, cur.lastrowid)


def get_experiment(repo, exp_id: int) -> Optional[dict]:
    r = repo.conn.execute("SELECT * FROM experiments WHERE id=?", (exp_id,)).fetchone()
    return _row(r) if r else None


def list_experiments(repo, status: Optional[str] = None) -> List[dict]:
    q = "SELECT * FROM experiments" + (" WHERE status=?" if status else "") + " ORDER BY id DESC"
    rows = repo.conn.execute(q, (status,) if status else ()).fetchall()
    return [_row(r) for r in rows]


def assign_arm(repo, exp_id: int, date: str) -> Optional[str]:
    """Assign tonight per the multi-cycle washout schedule. Returns 'a'|'b'|'washout' or None."""
    exp = get_experiment(repo, exp_id)
    if not exp or exp["status"] != "active":
        return None
    assignments = exp["assignments"] or {}
    if date in assignments:
        return assignments[date]
    slot = _schedule_slot(len(assignments), int(exp["min_nights_per_arm"]),
                          int(exp.get("washout_nights", 1)))
    assignments[date] = slot
    repo.conn.execute("UPDATE experiments SET assignments=? WHERE id=?",
                      (json.dumps(assignments), exp_id))
    repo.conn.commit()
    return slot


def _metric_by_date(repo, metric: str, dates: List[str]) -> dict:
    if not dates:
        return {}
    qs = ",".join("?" for _ in dates)
    rows = repo.conn.execute(
        f"SELECT date, {metric} AS m FROM nightly_summaries WHERE date IN ({qs})", dates
    ).fetchall()
    return {r["date"]: r["m"] for r in rows if r["m"] is not None}


def _mean(xs):
    return sum(xs) / len(xs) if xs else None


def _stats(vals: List[float]) -> dict:
    n = len(vals)
    if n == 0:
        return {"n": 0, "mean": None, "sd": None}
    m = sum(vals) / n
    sd = (sum((v - m) ** 2 for v in vals) / n) ** 0.5 if n > 1 else 0.0
    return {"n": n, "mean": round(m, 2), "sd": round(sd, 2)}


def analyze_experiment(repo, exp_id: int) -> Optional[dict]:
    exp = get_experiment(repo, exp_id)
    if not exp:
        return None
    metric = exp["metric"]
    period = int(exp["min_nights_per_arm"])
    washout = int(exp.get("washout_nights", 1))
    assignments = exp["assignments"] or {}
    by_date = _metric_by_date(repo, metric, list(assignments.keys()))

    # Reconstruct each night's cycle from its insertion order (dict preserves order).
    cycles: dict = {}
    a_all, b_all = [], []
    for n, (date, slot) in enumerate(assignments.items()):
        if slot == "washout" or date not in by_date:
            continue
        c = _cycle_of(n, period, washout)
        cycles.setdefault(c, {"a": [], "b": []})[slot].append(by_date[date])
        (a_all if slot == "a" else b_all).append(by_date[date])

    # Paired within-cycle contrasts (B - A): each cycle is its own control.
    cycle_diffs = []
    for c, arms in sorted(cycles.items()):
        ma, mb = _mean(arms["a"]), _mean(arms["b"])
        if ma is not None and mb is not None:
            cycle_diffs.append(round(mb - ma, 3))

    lower_better = metric in _LOWER_BETTER
    sa, sb = _stats(a_all), _stats(b_all)
    n_cycles = len(cycle_diffs)
    diff = effect = winner = ci = None
    if n_cycles >= 1:
        diff = round(sum(cycle_diffs) / n_cycles, 3)
        if n_cycles >= 2:
            sd = (sum((d - diff) ** 2 for d in cycle_diffs) / n_cycles) ** 0.5
            se = sd / (n_cycles ** 0.5)
            ci = [round(diff - 1.96 * se, 3), round(diff + 1.96 * se, 3)]
            effect = round(diff / sd, 2) if sd > 1e-6 else None
        b_better = (diff < 0) if lower_better else (diff > 0)
        if abs(diff) < 1e-9:
            winner = "tie"
        else:
            winner = exp["arm_b"].get("label", "treatment") if b_better \
                else exp["arm_a"].get("label", "control")

    enough = n_cycles >= 2
    # A credible interval that excludes 0 is the single-subject signal.
    ci_excludes_zero = bool(ci and (ci[0] > 0 or ci[1] < 0))
    if not enough:
        rec = (f"Keep going — need ≥2 completed cycles (have {n_cycles}). Each cycle pairs an "
               f"A and B period with a {washout}-night washout between them.")
    elif winner == "tie" or not ci_excludes_zero:
        rec = (f"No clear winner on {metric}: the cycle-paired difference's 95% interval "
               f"{ci} still includes 0. Your choice, or run more cycles.")
    else:
        strength = "strong" if (effect and abs(effect) >= 0.8) else "moderate"
        rec = (f"'{winner}' wins on {metric} (mean cycle Δ={diff}, 95% CI {ci}, excludes 0). "
               f"A {strength} single-subject signal across {n_cycles} cycles.")

    return {"metric": metric, "lower_better": lower_better, "control": sa, "treatment": sb,
            "diff": diff, "effect_size": effect, "winner": winner, "enough_data": enough,
            "n_cycles": n_cycles, "cycle_diffs": cycle_diffs, "ci": ci,
            "washout_nights": washout, "recommendation": rec}


# Arm params that map to a learnable SetpointProfile knob (additive °F deltas). These are the
# thermal-trajectory experiments the controller can actually run live. Behavioral params
# (variability_cap_f, induction_minutes_delta, am_warm_nudge_f, anchor_bedtime) are scheduled +
# analyzed but their application needs controller-config support (a follow-up).
_ARM_SETPOINT_DELTAS = {
    "deep_bias_delta_f": "deep_bias_f",
    "neutral_delta_f": "neutral_f",
    "rem_warm_offset_delta_f": "rem_warm_offset_f",
    "wake_ramp_delta_f": "wake_ramp_f",
}


def apply_experiment_arm(repo, date: str, profile):
    """Apply the active experiment's assigned arm to tonight's SetpointProfile (CLOSES the n-of-1
    loop: arms were scheduled but never applied). Returns ``(profile_for_tonight, arm_info)``.

    The arm delta rides on top of the current learned setpoint and is NOT persisted as a new
    version — it's a transient per-night override so A vs B is compared against the same base.
    On washout / no active experiment, the profile is returned unchanged.
    """
    if profile is None:
        return profile, None
    active = list_experiments(repo, status="active")
    if not active:
        return profile, None
    exp = active[0]  # one active thermal experiment at a time
    arm = assign_arm(repo, exp["id"], date)
    info = {"exp_id": exp["id"], "name": exp.get("name"), "arm": arm, "applied": False}
    if arm not in ("a", "b"):
        return profile, info
    armspec = exp["arm_a"] if arm == "a" else exp["arm_b"]
    params = (armspec or {}).get("params", {}) or {}
    from dataclasses import replace

    from sleepctl.ml.actions import KNOB_BOUNDS, _clamp
    knobs = {}
    for pkey, knob in _ARM_SETPOINT_DELTAS.items():
        if pkey in params:
            lo, hi = KNOB_BOUNDS[knob]
            knobs[knob] = _clamp(getattr(profile, knob) + float(params[pkey]), lo, hi)
    info.update({"label": (armspec or {}).get("label"), "applied": bool(knobs), "params": params})
    return (replace(profile, **knobs) if knobs else profile), info


def stop_experiment(repo, exp_id: int, complete: bool = True) -> Optional[dict]:
    exp = get_experiment(repo, exp_id)
    if not exp:
        return None
    result = analyze_experiment(repo, exp_id)
    repo.conn.execute("UPDATE experiments SET status=?, result=? WHERE id=?",
                      ("complete" if complete else "stopped", json.dumps(result), exp_id))
    repo.conn.commit()
    return get_experiment(repo, exp_id)
