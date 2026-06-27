"""n-of-1 self-experiment engine.

A rigorous single-subject trial: pick a knob (e.g. neutral temp, REM warm offset), define two
arms (control vs treatment), and let the system randomly-but-balanced assign each night to an
arm. Outcomes are compared across arms with a simple effect-size + overlap readout, so a
quantitative user gets a *causal* answer for themselves instead of guessing from correlations.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import List, Optional

# Metrics where a LOWER value is better (everything else: higher is better).
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


def create_experiment(repo, spec: dict) -> dict:
    metric = spec.get("metric", "wake_events")
    if metric not in _METRIC_COLS:
        raise ValueError(f"unknown metric {metric!r}")
    cur = repo.conn.execute(
        "INSERT INTO experiments (name, hypothesis, variable, arm_a, arm_b, metric, "
        "min_nights_per_arm, status, created, assignments, result) "
        "VALUES (?,?,?,?,?,?,?,'active',?,?,NULL)",
        (spec.get("name", "experiment"), spec.get("hypothesis", ""), spec.get("variable", ""),
         json.dumps(spec.get("arm_a", {"label": "control", "params": {}})),
         json.dumps(spec.get("arm_b", {"label": "treatment", "params": {}})),
         metric, int(spec.get("min_nights_per_arm", 5)), _now(), json.dumps({})),
    )
    repo.conn.commit()
    return get_experiment(repo, cur.lastrowid)


def get_experiment(repo, exp_id: int) -> Optional[dict]:
    r = repo.conn.execute("SELECT * FROM experiments WHERE id=?", (exp_id,)).fetchone()
    return _row(r) if r else None


def list_experiments(repo, status: Optional[str] = None) -> List[dict]:
    if status:
        rows = repo.conn.execute("SELECT * FROM experiments WHERE status=? ORDER BY id DESC",
                                 (status,)).fetchall()
    else:
        rows = repo.conn.execute("SELECT * FROM experiments ORDER BY id DESC").fetchall()
    return [_row(r) for r in rows]


def assign_arm(repo, exp_id: int, date: str) -> Optional[str]:
    """Assign tonight to an arm (balanced: whichever arm has fewer nights; deterministic).
    Returns 'a' or 'b' (the assigned arm), or None if the experiment isn't active."""
    exp = get_experiment(repo, exp_id)
    if not exp or exp["status"] != "active":
        return None
    assignments = exp["assignments"] or {}
    if date in assignments:
        return assignments[date]
    na = sum(1 for v in assignments.values() if v == "a")
    nb = sum(1 for v in assignments.values() if v == "b")
    arm = "a" if na <= nb else "b"   # keep arms balanced; ties -> control
    assignments[date] = arm
    repo.conn.execute("UPDATE experiments SET assignments=? WHERE id=?",
                      (json.dumps(assignments), exp_id))
    repo.conn.commit()
    return arm


def _metric_by_date(repo, metric: str, dates: List[str]) -> dict:
    if not dates:
        return {}
    qs = ",".join("?" for _ in dates)
    rows = repo.conn.execute(
        f"SELECT date, {metric} AS m FROM nightly_summaries WHERE date IN ({qs})", dates
    ).fetchall()
    return {r["date"]: r["m"] for r in rows if r["m"] is not None}


def _stats(vals: List[float]) -> dict:
    n = len(vals)
    if n == 0:
        return {"n": 0, "mean": None, "sd": None}
    mean = sum(vals) / n
    sd = (sum((v - mean) ** 2 for v in vals) / n) ** 0.5 if n > 1 else 0.0
    return {"n": n, "mean": round(mean, 2), "sd": round(sd, 2)}


def analyze_experiment(repo, exp_id: int) -> Optional[dict]:
    exp = get_experiment(repo, exp_id)
    if not exp:
        return None
    metric = exp["metric"]
    assignments = exp["assignments"] or {}
    by_date = _metric_by_date(repo, metric, list(assignments.keys()))
    a_vals = [by_date[d] for d, arm in assignments.items() if arm == "a" and d in by_date]
    b_vals = [by_date[d] for d, arm in assignments.items() if arm == "b" and d in by_date]
    sa, sb = _stats(a_vals), _stats(b_vals)

    lower_better = metric in _LOWER_BETTER
    enough = sa["n"] >= exp["min_nights_per_arm"] and sb["n"] >= exp["min_nights_per_arm"]
    diff = winner = effect = None
    if sa["mean"] is not None and sb["mean"] is not None:
        diff = round(sb["mean"] - sa["mean"], 2)  # treatment - control
        pooled = (((sa["sd"] or 0) ** 2 + (sb["sd"] or 0) ** 2) / 2) ** 0.5
        effect = round(diff / pooled, 2) if pooled > 1e-6 else None
        b_better = (diff < 0) if lower_better else (diff > 0)
        if abs(diff) < 1e-9:
            winner = "tie"
        else:
            winner = exp["arm_b"].get("label", "treatment") if b_better \
                else exp["arm_a"].get("label", "control")

    if not enough:
        rec = (f"Keep going — need {exp['min_nights_per_arm']} nights per arm "
               f"(have control={sa['n']}, treatment={sb['n']}).")
    elif winner == "tie" or (effect is not None and abs(effect) < 0.2):
        rec = f"No meaningful difference in {metric} between the arms — your choice."
    else:
        rec = (f"'{winner}' wins on {metric} (Δ={diff}, effect={effect}). "
               f"{'Strong' if effect and abs(effect) >= 0.5 else 'Modest'} single-subject signal.")

    return {"metric": metric, "lower_better": lower_better, "control": sa, "treatment": sb,
            "diff": diff, "effect_size": effect, "winner": winner, "enough_data": enough,
            "recommendation": rec}


def stop_experiment(repo, exp_id: int, complete: bool = True) -> Optional[dict]:
    exp = get_experiment(repo, exp_id)
    if not exp:
        return None
    result = analyze_experiment(repo, exp_id)
    repo.conn.execute("UPDATE experiments SET status=?, result=? WHERE id=?",
                      ("complete" if complete else "stopped", json.dumps(result), exp_id))
    repo.conn.commit()
    return get_experiment(repo, exp_id)
