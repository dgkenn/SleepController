"""Per-awakening root-cause forensics.

Turns each detected awakening into an explained event: the thermal + physiological state at
the moment, the context for that night (alcohol, caffeine, stress, late work, hot room), and a
ranked list of *likely causes*. This is the human-readable substrate the predictive
pre-emption + n-of-1 engine learn from — and it directly answers "why did I wake up at 3am?".
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import List, Optional


def _parse(ts: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return None


def _cluster_events(rows, gap_min: float = 6.0):
    """Collapse consecutive wake_event samples into discrete awakenings (earliest sample of
    each cluster). rows are newest-first."""
    events = []
    anchor = None
    for r in rows:
        ts = _parse(r["ts"])
        if ts is None:
            continue
        if anchor is None or abs((anchor - ts).total_seconds()) > gap_min * 60:
            events.append(r)          # new cluster -> this (newer) sample starts it
            anchor = ts
        else:
            anchor = ts               # same cluster, keep walking back; keep the earliest later
    return events


def _night_bed_median(repo, night_date: str) -> Optional[float]:
    rows = repo.conn.execute(
        "SELECT bed_temp_f FROM raw_samples WHERE night_date=? AND bed_temp_f IS NOT NULL",
        (night_date,)).fetchall()
    vals = sorted(r["bed_temp_f"] for r in rows)
    if not vals:
        return None
    return vals[len(vals) // 2]


def _causes(ev, ctx, bed_median, warm_threshold) -> List[dict]:
    causes: List[dict] = []
    bed = ev["bed_temp_f"]
    room = ev["room_temp_f"]
    ts = _parse(ev["ts"])

    if bed is not None:
        if warm_threshold is not None and bed >= warm_threshold:
            causes.append({"factor": "warm_bed", "weight": 0.9,
                           "detail": f"Bed at {bed:.1f}°F — at/above your personal warm threshold."})
        elif bed_median is not None and bed >= bed_median + 1.5:
            causes.append({"factor": "warm_bed", "weight": 0.7,
                           "detail": f"Bed at {bed:.1f}°F — {bed - bed_median:.1f}°F above the night's median."})
    if room is not None and room >= 72:
        causes.append({"factor": "hot_room", "weight": 0.6,
                       "detail": f"Room was warm ({room:.0f}°F)."})
    if ts is not None and (3 * 60) <= (ts.hour * 60 + ts.minute) <= (5 * 60 + 30):
        causes.append({"factor": "circadian", "weight": 0.5,
                       "detail": "In the 3:00–5:30am circadian vulnerability window."})
    if ctx is not None:
        if getattr(ctx, "alcohol", None):
            causes.append({"factor": "alcohol", "weight": 0.7,
                           "detail": "Alcohol that evening — fragments second-half sleep."})
        if getattr(ctx, "caffeine", None):
            causes.append({"factor": "caffeine", "weight": 0.5, "detail": "Caffeine logged."})
        stress = getattr(ctx, "stress", None)
        if stress and stress >= 6:
            causes.append({"factor": "stress", "weight": 0.5, "detail": "High stress day."})
        if getattr(ctx, "late_night_work", None):
            causes.append({"factor": "late_work", "weight": 0.4,
                           "detail": "Late-night work before bed."})
    hr = ev["heart_rate"]
    if hr is not None and hr >= 70:
        causes.append({"factor": "hr_surge", "weight": 0.4,
                       "detail": f"Elevated heart rate ({hr:.0f} bpm) at the awakening."})
    causes.sort(key=lambda c: c["weight"], reverse=True)
    return causes


def awakening_forensics(repo, limit: int = 20, profile=None) -> List[dict]:
    warm_threshold = getattr(profile, "warm_temp_threshold_f", None) if profile else None
    rows = repo.conn.execute(
        "SELECT ts, night_date, bed_temp_f, room_temp_f, heart_rate, hrv, movement, stage "
        "FROM raw_samples WHERE wake_event=1 ORDER BY ts DESC LIMIT ?", (limit * 5,)
    ).fetchall()
    events = _cluster_events(rows)[:limit]
    out = []
    medians: dict = {}
    for ev in events:
        nd = ev["night_date"]
        if nd not in medians:
            medians[nd] = _night_bed_median(repo, nd)
        ctx = repo.get_context(nd) if nd else None
        ts = _parse(ev["ts"])
        causes = _causes(ev, ctx, medians[nd], warm_threshold)
        out.append({
            "night_date": nd,
            "time": ts.strftime("%H:%M") if ts else None,
            "bed_temp_f": ev["bed_temp_f"],
            "room_temp_f": ev["room_temp_f"],
            "heart_rate": ev["heart_rate"],
            "hrv": ev["hrv"],
            "stage_before": ev["stage"],
            "likely_causes": causes,
            "top_cause": causes[0]["factor"] if causes else "unexplained",
        })
    return out


def forensics_summary(events: List[dict]) -> dict:
    """Aggregate the top causes across recent awakenings (what to attack first)."""
    tally: dict = {}
    for e in events:
        for c in e.get("likely_causes", []):
            tally[c["factor"]] = tally.get(c["factor"], 0) + 1
    ranked = sorted(tally.items(), key=lambda kv: kv[1], reverse=True)
    return {"n_awakenings": len(events),
            "top_factors": [{"factor": f, "count": n} for f, n in ranked[:5]]}
