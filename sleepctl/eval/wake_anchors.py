"""Gate 2: recall-free objective wake anchors.

People cannot report when they fell asleep or how many times they woke -- sleep-onset
misperception is well documented, and awakenings under a couple of minutes generally do not form
memories at all. So subjective recall is the wrong instrument for validating staging, and asking
for it produces confident numbers built on nothing.

Timestamped activity is not subject to that problem. A phone unlock, a sent message, a logged bed
exit -- each is high-specificity evidence that the user was AWAKE at a known instant, requiring
zero recall. That does not validate REM vs N2 vs N3 (nothing at home does), but it catches
catastrophic staging errors, which is what actually matters for a controller driven by stage.

This module already has a precedent in this codebase. From ``controller/state_estimator.py``:

    "the learned stager scored 2/6 against message-timestamp ground truth on a real night,
     calling three of the misses REM"

That single measurement is worth more than any amount of hypnogram eyeballing: it says four
objectively-evidenced awakenings were missed, three of them labelled REM -- a concrete, named
failure mode rather than "the chart looks implausible". This module turns that one-off analysis
into something reproducible on every night.

What it computes, given known-awake instants:

    miss_rate = P(inferred asleep | objectively awake)

plus the distribution of what the estimator called those moments instead, because WHICH wrong
label it picks is diagnostic (systematically calling awake moments REM is a different bug from
calling them light).

Deliberately asymmetric: anchors evidence WAKE only. There is no equivalent objective evidence
for "definitely asleep", so this can measure the estimator's misses but not its false alarms, and
it does not pretend otherwise.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta
from typing import Optional

#: How close a staged sample must be to an anchor to be considered the estimator's verdict at
#: that instant. Samples arrive ~1/min; 3 min tolerates a gap without matching an unrelated epoch.
DEFAULT_MATCH_TOLERANCE_MIN = 3.0

#: Labels that mean "the estimator thought the user was asleep".
_ASLEEP_LABELS = ("light", "deep", "rem")


def _parse(v) -> Optional[datetime]:
    if isinstance(v, datetime):
        return v
    try:
        return datetime.fromisoformat(str(v))
    except Exception:
        return None


def evaluate_wake_anchors(rows, awake_events,
                          tolerance_min: float = DEFAULT_MATCH_TOLERANCE_MIN) -> dict:
    """Score staged ``rows`` against objectively-known-awake instants.

    ``rows``: raw_samples-shaped mappings with ``ts`` and ``stage``.
    ``awake_events``: datetimes (or ISO strings) at which the user was demonstrably awake.

    Returns miss counts, the miss rate, and what the estimator called each missed anchor.
    Never raises; an anchor with no nearby sample is reported ``unmatched`` rather than scored,
    so a sensor gap is never counted as a staging error.
    """
    out: dict = {"n_anchors": 0, "matched": 0, "unmatched": 0, "hits": 0, "misses": 0,
                 "miss_rate": None, "missed_as": {}, "detail": []}

    samples = []
    for r in rows or []:
        get = r.get if hasattr(r, "get") else (lambda k: r[k])
        t = _parse(get("ts"))
        if t is None:
            continue
        samples.append((t, (get("stage") or "unknown")))
    samples.sort(key=lambda x: x[0])
    if not samples:
        out["reason"] = "no staged samples to score against"
        return out

    anchors = [a for a in (_parse(x) for x in (awake_events or [])) if a is not None]
    anchors.sort()
    out["n_anchors"] = len(anchors)
    if not anchors:
        out["reason"] = "no objective wake anchors supplied"
        return out

    tol = timedelta(minutes=tolerance_min)
    missed_labels: Counter = Counter()
    for a in anchors:
        # nearest sample in time
        best, best_gap = None, None
        for t, stage in samples:
            gap = abs((t - a).total_seconds())
            if best_gap is None or gap < best_gap:
                best, best_gap = (t, stage), gap
        if best is None or best_gap is None or best_gap > tol.total_seconds():
            out["unmatched"] += 1
            out["detail"].append({"anchor": a.isoformat(), "matched": False})
            continue
        out["matched"] += 1
        t, stage = best
        inferred_asleep = stage in _ASLEEP_LABELS
        if inferred_asleep:
            out["misses"] += 1
            missed_labels[stage] += 1
        else:
            out["hits"] += 1
        out["detail"].append({
            "anchor": a.isoformat(), "matched": True, "sample_ts": t.isoformat(),
            "inferred_stage": stage, "miss": inferred_asleep,
        })

    if out["matched"]:
        out["miss_rate"] = round(out["misses"] / out["matched"], 3)
    out["missed_as"] = dict(missed_labels)
    return out


def format_report(res: dict, label: str = "") -> str:
    lines = [f"WAKE ANCHORS{(' - ' + label) if label else ''}"]
    if res.get("reason"):
        lines.append(f"  {res['reason']}")
        return "\n".join(lines)
    lines.append(f"  {res['matched']}/{res['n_anchors']} anchors matched a staged sample "
                 f"({res['unmatched']} unmatched -- sensor gaps, not scored)")
    lines.append(f"  correctly awake: {res['hits']}    MISSED (called asleep): {res['misses']}")
    if res.get("miss_rate") is not None:
        lines.append(f"  miss rate P(inferred asleep | objectively awake) = {res['miss_rate']}")
    if res.get("missed_as"):
        lines.append(f"  misses were labelled: {res['missed_as']}")
        if res["missed_as"].get("rem"):
            lines.append("    (misses concentrated in REM is the documented failure mode -- "
                         "2/6 with three misses called REM, state_estimator.py)")
    return "\n".join(lines)
