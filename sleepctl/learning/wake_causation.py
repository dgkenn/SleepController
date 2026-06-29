"""Failure-mode audit: which thermal adjustments tend to WAKE you — learned with rigor.

The user's concern: "if any temperature adjustment causes me to wake up, learn it and stop doing
it — but it could have been unrelated; I might have woken anyway." That is exactly a causal
question, and the honest answer has two tiers:

  1. **Gold standard (confound-free): the randomized control.** Where a maneuver runs an n-of-1
     control arm (the deepen / lighten steerer's `applied=0` shadow nights), we already compare
     P(wake | actuated) to P(wake | not actuated) and DISABLE the maneuver if it raises the
     awakening rate (`learning/deepening.py`). That removes "you'd have woken anyway."

  2. **Observational audit (this module): everything else.** For maneuvers we can't randomize
     (most are reactive), we still audit every adjustment in the `interventions` ledger: did a wake
     follow within a short horizon, and how does that compare to the night's BASE wake rate over an
     equivalent window? The excess over base rate controls for "you'd have woken anyway." But
     reactive maneuvers (a settle nudge fires *because* a wake is brewing) are inherently confounded
     — they will always precede wakes — so those are LABELLED confounded and never auto-blamed; only
     proactive maneuvers with a statistically clear excess are flagged `suspect` for action.

So: rigorous causal disable where we have a randomized control; a clearly-labelled, base-rate-
controlled, confounder-aware audit everywhere else. Pure-python, conservative.
"""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Optional

# Reactive maneuvers fire in anticipation of / response to a disturbance, so they are EXPECTED to
# precede awakenings — their wake association is confounded and must never be auto-blamed.
_CONFOUNDED_INTENTS = {"settle_cool", "wake_ramp"}
_CONFOUNDED_STATES = {"wake_recovery", "wake_window"}
_MIN_EVENTS = 8            # need this many of a maneuver before flagging it
_MIN_EXCESS = 0.10         # post-wake rate must exceed base by this much to even consider suspect


def _parse_maneuver(reason: Optional[str], action: Optional[str]) -> str:
    """The maneuver key: the thermal intent token from the decision reason ('… -> deep_bias_cool'),
    else the coarse action direction (cooler/warmer/hold)."""
    if reason and "->" in reason:
        tok = reason.split("->")[-1].strip().split()[0]
        if tok:
            return tok
    return (action or "unknown").lower()


def _iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat()


def wake_causation_audit(repo, horizon_min: float = 15.0, nights: int = 30) -> dict:
    """Audit every mid-sleep thermal adjustment for its association with an awakening, controlled
    for the night's base wake rate. Returns {horizon_min, base_wake_rate, n, maneuvers: {key: {...}},
    note}. ``suspect`` is only ever set for non-confounded (proactive) maneuvers with a clear excess.
    """
    conn = repo.conn
    summaries = repo.recent_nights(nights) if hasattr(repo, "recent_nights") else []
    dates = [getattr(s, "date", None) for s in summaries if getattr(s, "date", None)]
    if not dates:
        return {"horizon_min": horizon_min, "base_wake_rate": None, "n": 0,
                "maneuvers": {}, "note": "no nights yet"}
    placeholders = ",".join("?" * len(dates))

    # Base rate: per-minute wake hazard during SLEEP states -> chance an H-min window contains a
    # wake by chance alone (the "you'd have woken anyway" reference).
    sleep_samples = conn.execute(
        f"SELECT COUNT(*) c FROM raw_samples WHERE night_date IN ({placeholders}) "
        f"AND controller_state IN ('maintenance','wake_recovery')", dates).fetchone()["c"] or 0
    sleep_wakes = conn.execute(
        f"SELECT COUNT(*) c FROM raw_samples WHERE night_date IN ({placeholders}) "
        f"AND controller_state IN ('maintenance','wake_recovery') AND wake_event = 1",
        dates).fetchone()["c"] or 0
    if sleep_samples <= 0:
        base_rate = None
    else:
        per_min = sleep_wakes / sleep_samples
        base_rate = max(0.0, min(1.0, 1.0 - (1.0 - per_min) ** horizon_min))

    # Each mid-sleep adjustment -> did a wake follow within the horizon?
    ivs = conn.execute(
        f"SELECT ts, controller_state, action, reason FROM interventions "
        f"WHERE night_date IN ({placeholders}) "
        f"AND controller_state IN ('maintenance','wake_recovery') ORDER BY ts", dates).fetchall()
    buckets: dict = defaultdict(lambda: {"n": 0, "woke": 0, "confounded": False})
    n_total = 0
    for iv in ivs:
        try:
            t0 = datetime.fromisoformat(iv["ts"])
        except (TypeError, ValueError):
            continue
        key = _parse_maneuver(iv["reason"], iv["action"])
        end = t0 + timedelta(minutes=horizon_min)
        woke = conn.execute(
            "SELECT COUNT(*) c FROM raw_samples WHERE wake_event = 1 AND ts > ? AND ts <= ?",
            (_iso(t0), _iso(end))).fetchone()["c"]
        b = buckets[key]
        b["n"] += 1
        b["woke"] += 1 if woke else 0
        if key in _CONFOUNDED_INTENTS or (iv["controller_state"] in _CONFOUNDED_STATES):
            b["confounded"] = True
        n_total += 1

    maneuvers = {}
    for key, b in buckets.items():
        n, woke = b["n"], b["woke"]
        post = woke / n if n else None
        excess = (post - base_rate) if (post is not None and base_rate is not None) else None
        # one-sided lower 95% bound on the post-wake rate (normal approx) — rigor against small n
        se = math.sqrt(post * (1 - post) / n) if (post is not None and n) else None
        lower = (post - 1.64 * se) if (post is not None and se is not None) else None
        suspect = bool(
            not b["confounded"] and base_rate is not None and n >= _MIN_EVENTS
            and excess is not None and excess >= _MIN_EXCESS
            and lower is not None and lower > base_rate)
        maneuvers[key] = {
            "n": n, "woke": woke,
            "post_wake_rate": round(post, 3) if post is not None else None,
            "excess_over_base": round(excess, 3) if excess is not None else None,
            "confounded": b["confounded"],
            "suspect": suspect,
            "note": ("reactive maneuver — wake association is confounded (it fires because a wake is "
                     "already brewing), not blamed" if b["confounded"]
                     else "proactive — flagged: wakes you above base rate" if suspect
                     else "no clear excess over base rate"),
        }

    return {
        "horizon_min": horizon_min,
        "base_wake_rate": round(base_rate, 3) if base_rate is not None else None,
        "n": n_total,
        "maneuvers": maneuvers,
        "note": ("Causal disable for randomized maneuvers (deepen/lighten) lives in the response "
                 "learners; this observational audit controls for the base rate and never blames a "
                 "confounded reactive maneuver."),
    }


def suspect_maneuvers(repo, horizon_min: float = 15.0, nights: int = 30) -> set:
    """The set of proactive maneuver keys the audit flags as waking you above the base rate — the
    controller can use this to soften/avoid them (do-no-harm), corroborated by the randomized arm
    where one exists."""
    audit = wake_causation_audit(repo, horizon_min, nights)
    return {k for k, v in audit["maneuvers"].items() if v.get("suspect")}


# ---------------------------------------------------------------------------------------------
# Personalized awakening-PREDICTION: learn the sensor trajectory that precedes YOUR awakenings.
# The flip side of the causation audit — instead of "what adjustment woke me?", this asks "what do
# my own sensors do in the minutes BEFORE I wake?", so the controller can predict and pre-empt an
# incoming awakening earlier and more accurately than fixed thresholds. Feeds the precursor detector.
# ---------------------------------------------------------------------------------------------

# The comprehensive trajectory feature set, with the physiological DIRECTION that precedes an
# arousal (Busek 2005: HRV decay is the earliest sign; HR creep + restlessness/tossing + breathing
# irregularity + bed warming follow). ``sign`` = +1 if a RISE precedes a wake, -1 if a FALL does.
# Movement is treated richly (the user's emphasis): not just its level but its *trend*, its *peak*,
# and the *count of tossing/turning bursts* — discrete position shifts that fragment sleep.
_PRECURSOR_FEATURES = {
    # autonomic — level AND trend (BCG, valid when still)
    "hr_slope": +1,        # bpm/min — HR creeps up
    "hr_mean": +1,         # HR running elevated vs calm sleep
    "hrv_slope": -1,       # ms/min — HRV decays (earliest sign)
    "hrv_mean": -1,        # HRV depressed
    "rr_slope": +1,        # respiratory rate rising
    "rr_cv": +1,           # breathing loses regularity
    # movement / tossing-and-turning — the restlessness signature of an impending arousal
    "move_mean": +1,       # average restlessness
    "move_slope": +1,      # restlessness BUILDING (rising trend)
    "move_max": +1,        # a big positional shift / jerk
    "move_burst": +1,      # COUNT of tossing/turning bursts in the window (discrete shifts)
    # thermal — heat fragments sleep (hot sleeper)
    "bed_slope": +1,       # the bed warms
    "bed_mean": +1,        # the bed running warm
}
_BURST_MOVEMENT = 0.25     # movement at/above this counts as a tossing/turning burst


def _slope(xs, ys) -> Optional[float]:
    """Least-squares slope of ys vs xs (per unit x). None if degenerate."""
    n = len(xs)
    if n < 2:
        return None
    mx = sum(xs) / n
    my = sum(ys) / n
    den = sum((x - mx) ** 2 for x in xs)
    if den <= 1e-9:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den


def _window_features(win) -> dict:
    """Comprehensive trajectory features for a lead-up window: list of (minute, hr, hrv, rr, move,
    bed). Columns: 1=hr, 2=hrv, 3=rr, 4=move, 5=bed."""
    if len(win) < 2:
        return {}

    def slope_of(i):
        pts = [(w[0], w[i]) for w in win if w[i] is not None]
        return _slope([p[0] for p in pts], [p[1] for p in pts]) if len(pts) >= 2 else None

    def mean_of(i):
        vals = [w[i] for w in win if w[i] is not None]
        return (sum(vals) / len(vals)) if vals else None

    def max_of(i):
        vals = [w[i] for w in win if w[i] is not None]
        return max(vals) if vals else None

    rr = [w[3] for w in win if w[3] is not None]
    rr_cv = None
    if len(rr) >= 2:
        m = sum(rr) / len(rr)
        if m > 1e-6:
            sd = (sum((x - m) ** 2 for x in rr) / len(rr)) ** 0.5
            rr_cv = sd / m
    moves = [w[4] for w in win if w[4] is not None]
    move_burst = float(sum(1 for v in moves if v >= _BURST_MOVEMENT)) if moves else None
    return {
        "hr_slope": slope_of(1), "hr_mean": mean_of(1),
        "hrv_slope": slope_of(2), "hrv_mean": mean_of(2),
        "rr_slope": slope_of(3), "rr_cv": rr_cv,
        "move_mean": mean_of(4), "move_slope": slope_of(4), "move_max": max_of(4),
        "move_burst": move_burst,
        "bed_slope": slope_of(5), "bed_mean": mean_of(5),
    }


def _mean_sd(vals):
    vals = [v for v in vals if v is not None]
    if not vals:
        return None, None, 0
    m = sum(vals) / len(vals)
    sd = (sum((x - m) ** 2 for x in vals) / len(vals)) ** 0.5 if len(vals) > 1 else 0.0
    return m, sd, len(vals)


def awakening_precursor_profile(repo, lead_min: float = 6.0, nights: int = 30,
                                min_events: int = 5) -> dict:
    """Learn the per-person sensor trajectory that PREDICTS an incoming awakening.

    For each logged awakening we take the ``lead_min`` window just before it; for control we take
    sleep windows not near any awakening. Each feature's separation between the two (a standardized
    mean difference, signed by the physiological direction) tells us which of YOUR signals actually
    lead your arousals, and a personalized threshold (mid-point between the pre-wake and baseline
    means) is where the controller should start pre-empting. Returns a profile the precursor detector
    can consume; honest about confidence when events are few."""
    conn = repo.conn
    summaries = repo.recent_nights(nights) if hasattr(repo, "recent_nights") else []
    dates = [getattr(s, "date", None) for s in summaries if getattr(s, "date", None)]
    pre_feats = defaultdict(list)
    base_feats = defaultdict(list)
    n_wakes = 0
    for d in dates:
        rows = conn.execute(
            "SELECT ts, heart_rate, hrv, respiratory_rate, movement, bed_temp_f, wake_event "
            "FROM raw_samples WHERE night_date = ? AND controller_state IN "
            "('maintenance','wake_recovery') ORDER BY ts", (d,)).fetchall()
        series = []
        for r in rows:
            try:
                t = datetime.fromisoformat(r["ts"])
            except (TypeError, ValueError):
                continue
            series.append((t, r["heart_rate"], r["hrv"], r["respiratory_rate"],
                           r["movement"], r["bed_temp_f"], r["wake_event"]))
        if not series:
            continue
        wake_times = [s[0] for s in series if s[6] == 1]
        # pre-wake windows
        for tw in wake_times:
            win = [((s[0] - tw).total_seconds() / 60.0 + lead_min, s[1], s[2], s[3], s[4], s[5])
                   for s in series if 0 <= (tw - s[0]).total_seconds() / 60.0 < lead_min]
            f = _window_features(win)
            if f:
                n_wakes += 1
                for k, v in f.items():
                    if v is not None:
                        pre_feats[k].append(v)
        # control windows: tiled lead_min windows with no wake nearby
        t0 = series[0][0]
        span = (series[-1][0] - t0).total_seconds() / 60.0
        step = lead_min
        k = 0
        while (k + 2) * step <= span:
            w_start = t0 + timedelta(minutes=k * step)
            w_end = w_start + timedelta(minutes=lead_min)
            guard_end = w_end + timedelta(minutes=lead_min)
            near_wake = any(w_start <= tw <= guard_end for tw in wake_times)
            if not near_wake:
                win = [((s[0] - w_start).total_seconds() / 60.0, s[1], s[2], s[3], s[4], s[5])
                       for s in series if w_start <= s[0] < w_end]
                f = _window_features(win)
                for kk, v in f.items():
                    if v is not None:
                        base_feats[kk].append(v)
            k += 1

    features = {}
    predictive = []
    for key, sign in _PRECURSOR_FEATURES.items():
        pm, psd, pn = _mean_sd(pre_feats.get(key, []))
        bm, bsd, bn = _mean_sd(base_feats.get(key, []))
        if pm is None or bm is None:
            features[key] = {"predictive": False, "n_pre": pn, "n_base": bn, "reason": "no data"}
            continue
        pooled = max(1e-6, ((psd or 0) ** 2 + (bsd or 0) ** 2) ** 0.5)
        # signed standardized separation (positive = leads a wake); capped so a near-constant
        # control window can't blow the score up to a meaningless magnitude.
        sep = max(-10.0, min(10.0, sign * (pm - bm) / pooled))
        is_pred = bool(n_wakes >= min_events and sep >= 0.3)
        features[key] = {
            "pre_wake_mean": round(pm, 3), "baseline_mean": round(bm, 3),
            "separation": round(sep, 2), "threshold": round((pm + bm) / 2.0, 3),
            "predictive": is_pred, "n_pre": pn, "n_base": bn,
        }
        if is_pred:
            predictive.append(key)

    confidence = max(0.0, min(1.0, (n_wakes - min_events) / 20.0)) if n_wakes >= min_events else 0.0
    return {
        "lead_min": lead_min, "n_awakenings": n_wakes,
        "features": features, "predictive_signals": predictive,
        "is_personalized": n_wakes >= min_events and bool(predictive),
        "confidence": round(confidence, 2),
        "rationale": (f"your awakenings are preceded by {', '.join(predictive)} (learned from "
                      f"{n_wakes} awakenings)" if predictive
                      else f"learning — {n_wakes}/{min_events} awakenings before a personalized "
                           f"precursor trajectory emerges"),
    }
