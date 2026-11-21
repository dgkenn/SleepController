"""The standing experiment program: keep testing one bed change at a time, keep what helps.

The experiment engine (``sleepctl.experiments``) runs rigorous n-of-1 crossovers -- counterbalanced
arms, washout nights, a cycle-paired 95% interval -- but only when someone starts one. This keeps
one running: when no experiment is active it starts the next entry of :data:`PROGRAM` (ordered
by strength of evidence, best-supported first), judged on
the **recovery score** (``learning.recovery``: Apple-measured, independent of the controller),
and at each night's start it checks the running one:

* winner with a 95% interval clear of zero after >= :data:`MIN_CYCLES` cycles -> it is ADOPTED:
  the treatment's change is written into the learned setpoints as a new version (only if the
  treatment won; a control win keeps the current setting) and the program moves on;
* no winner after :data:`MAX_CYCLES` cycles -> concluded "no difference", setting kept, move on.

Each entry changes one setpoint knob by a small, bounded amount (the knobs
``experiments.apply_experiment_arm`` actually applies). An experiment someone starts by hand
always takes precedence: the program only manages its own ("Auto: ..."). It does nothing until
the recovery score exists (:data:`MIN_REFERENCE_NIGHTS` Apple-measured nights), because an
experiment with no outcome to judge it by would only be noise. When the program has been through
every entry it starts over: a setting that helped last month is worth re-checking as seasons
and the sleeper change. ``SLEEPCTL_AUTO_EXPERIMENTS=0`` turns it off.
"""

from __future__ import annotations

import os
from dataclasses import replace
from datetime import datetime
from typing import Optional

from sleepctl import experiments as ex

PREFIX = "Auto: "
METRIC = "recovery_score"
MIN_CYCLES = 2
MAX_CYCLES = 4
MIN_REFERENCE_NIGHTS = 7          # MIN_HISTORY for the score + a couple of scored nights
NIGHTS_PER_ARM = 3
WASHOUT = 1

#: Ordered by strength of evidence (docs/ALGORITHM_RESEARCH.md, section 4). ``evidence`` says why.
PROGRAM = (
    {"key": "cooler_base", "name": "Cooler baseline",
     "hypothesis": "A slightly cooler bed all night increases deep sleep and lowers overnight "
                   "heart rate, improving recovery.",
     "evidence": "randomized PSG trials: enhanced conductive body-heat loss during sleep raised "
                 "N3 (+7.5 min) and lowered heart rate (-2.4 bpm) in a 3-centre study of 72 "
                 "adults (Herberger 2024), after a 15-subject crossover (Herberger 2018)",
     "variable": "neutral_f", "param": "neutral_delta_f", "delta": -1.0},
    {"key": "cooler_deep", "name": "Cooler during deep sleep",
     "hypothesis": "Extra cooling while in deep sleep lengthens deep sleep further.",
     "evidence": "extension of the heat-loss trials to stage-timed cooling; no direct trial",
     "variable": "deep_bias_f", "param": "deep_bias_delta_f", "delta": -1.5},
    {"key": "warmer_rem", "name": "Warmer during REM",
     "hypothesis": "A little warmth in REM (thermoregulation is suspended in REM) reduces REM "
                   "awakenings and improves recovery.",
     "evidence": "physiological rationale only (REM poikilothermia); no trial",
     "variable": "rem_warm_offset_f", "param": "rem_warm_offset_delta_f", "delta": 1.0},
)
#: Considered and left out: a warmer bed all night. It runs against the heat-loss trials above;
#: warmth helps at sleep ONSET (Raymann 2005; Fronczek 2008), which the onset program already does.


def enabled() -> bool:
    return os.environ.get("SLEEPCTL_AUTO_EXPERIMENTS", "1").strip().lower() not in (
        "0", "false", "no", "off")


def _spec(entry: dict) -> dict:
    return {"name": PREFIX + entry["name"], "hypothesis": entry["hypothesis"],
            "variable": entry["variable"], "metric": METRIC,
            "min_nights_per_arm": NIGHTS_PER_ARM, "washout_nights": WASHOUT,
            "arm_a": {"label": "current setting", "params": {entry["param"]: 0.0}},
            "arm_b": {"label": f"{entry['delta']:+.1f}°F", "params": {entry["param"]:
                                                                       entry["delta"]}},
            "auto_key": entry["key"]}


def _reference_nights(repo) -> int:
    try:
        from sleepctl.learning.recovery import ensure_schema
        ensure_schema(repo.conn)
        return int(repo.conn.execute("SELECT COUNT(recovery_score) FROM night_reference"
                                     ).fetchone()[0])
    except Exception:
        return 0


def _next_entry(repo) -> dict:
    done = [e["name"] for e in ex.list_experiments(repo) if e["name"].startswith(PREFIX)]
    for entry in PROGRAM:                         # first entry never run
        if PREFIX + entry["name"] not in done:
            return entry
    # all run: start over with the one run longest ago
    last_run = {n: i for i, n in enumerate(reversed(done))}     # list is newest first
    return max(PROGRAM, key=lambda e: last_run.get(PREFIX + e["name"], -1))


def _adopt(repo, exp: dict, result: dict) -> Optional[dict]:
    """Write the treatment's change into the learned setpoints (a new version)."""
    params = (exp.get("arm_b") or {}).get("params") or {}
    from sleepctl.ml.actions import KNOB_BOUNDS, _clamp
    base = repo.latest_setpoints()
    if base is None:
        return None
    knobs = {}
    for pkey, knob in ex._ARM_SETPOINT_DELTAS.items():
        if pkey in params and float(params[pkey]) != 0.0:
            lo, hi = KNOB_BOUNDS[knob]
            knobs[knob] = _clamp(getattr(base, knob) + float(params[pkey]), lo, hi)
    if not knobs:
        return None
    new = replace(base, **knobs, version=int(base.version or 0) + 1, source="experiment",
                  updated=datetime.now())
    repo.save_setpoints(new)
    return knobs


def tick(repo) -> dict:
    """Run once per night before the arm is applied. Returns what it did (for the log)."""
    if not enabled():
        return {"action": "off"}
    active = ex.list_experiments(repo, status="active")
    if active:
        exp = active[0]
        if not exp["name"].startswith(PREFIX):
            return {"action": "manual experiment running", "name": exp["name"]}
        res = ex.analyze_experiment(repo, exp["id"]) or {}
        n, ci = int(res.get("n_cycles") or 0), res.get("ci")
        decided = bool(ci and (ci[0] > 0 or ci[1] < 0))
        if n >= MIN_CYCLES and decided:
            b_label = (exp.get("arm_b") or {}).get("label")
            adopted = _adopt(repo, exp, res) if res.get("winner") == b_label else None
            ex.stop_experiment(repo, exp["id"], complete=True)
            return {"action": "concluded", "name": exp["name"], "winner": res.get("winner"),
                    "adopted": adopted}
        if n >= MAX_CYCLES:
            ex.stop_experiment(repo, exp["id"], complete=True)
            return {"action": "concluded", "name": exp["name"], "winner": "no difference",
                    "adopted": None}
        return {"action": "running", "name": exp["name"], "cycles": n}
    if _reference_nights(repo) < MIN_REFERENCE_NIGHTS:
        return {"action": "waiting", "why": f"needs {MIN_REFERENCE_NIGHTS} nights with an "
                                            f"Apple-measured recovery score"}
    entry = _next_entry(repo)
    exp = ex.create_experiment(repo, _spec(entry))
    return {"action": "started", "name": exp["name"]}


def snapshot(repo) -> dict:
    """Names, status, cycle counts and verdict labels only -- no outcome values."""
    try:
        rows = [e for e in ex.list_experiments(repo) if e["name"].startswith(PREFIX)][:6]
    except Exception as exc:
        return {"error": type(exc).__name__}
    out = []
    for e in rows:
        r = e.get("result") or {}
        out.append({"name": e["name"][len(PREFIX):], "status": e["status"],
                    "cycles": r.get("n_cycles"), "winner": r.get("winner"),
                    "nights": len(e.get("assignments") or {})})
    return {"enabled": enabled(), "experiments": out}


__all__ = ["PROGRAM", "tick", "snapshot", "enabled"]
