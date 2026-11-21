"""Independent night outcomes and a personal recovery score, from Apple's measurements.

Everything the controller learns from -- experiments, the deepen/lighten learners -- used to be
graded by the controller's OWN stager. That is circular in a way that matters: cooling the bed
lowers heart rate, and a lower heart rate is exactly what a heart-rate stager reads as deep sleep,
so a cooling nudge can "deepen" the stager's output without deepening the sleeper. Apple's
stages (PSG-validated, computed by Apple from the watch's own signals, untouched by our
controller) are the independent judge. This module turns them into:

* ``night_reference`` -- per night (keyed by the controller's ``night_date``): deep / REM / core
  minutes, total sleep, efficiency, WASO and awakenings from Apple's hypnogram, plus overnight
  physiology from Apple Health (lowest sustained heart rate, mean HRV (SDNN), resting heart rate,
  mean sleeping breathing rate, mean blood oxygen);
* ``recovery_score`` -- one number per night, 50 = your usual, +10 per typical-spread better:
  the mean of robust z-scores against your own previous nights (median / MAD, up to 30), weighted
  half physiology (HRV up, overnight HR down, sleeping breathing rate down) and half architecture
  (deep and REM up, WASO down, efficiency up). Needs :data:`MIN_HISTORY` earlier nights before it
  scores at all;
* :func:`regrade_steer_events` -- re-labels each resolved deepen / REM nudge with whether Apple
  saw the target stage in its response window, so the learners judge the maneuver by an outside
  measurement when one exists.

Nothing here is published: the health snapshot carries counts only.
"""

from __future__ import annotations

import json
import statistics
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Sequence, Tuple

from sleepctl.eval.eeg_agreement import _to_unix
from sleepctl.learning.wearable_consensus import (_iso_unix, recent_nights, source_kind)

MIN_HISTORY = 5            # earlier reference nights before a recovery score is given
HISTORY_N = 30
MIN_SLEEP_MIN = 120.0      # a "night" with less sleep than this is not scored
AWAKE_EVENT_MIN = 1.0      # an awake segment at least this long, after onset, is an awakening
#: component: (column, +1 if higher is better, weight)
#: Physiology half: HRV up, overnight heart rate down, overnight breathing rate down (a raised
#: sleeping breathing rate against your own baseline is one of the earliest strain / illness
#: signs a watch sees). Architecture half: deep and REM up, WASO down, efficiency up.
COMPONENTS = (
    ("hrv_mean", +1, 0.25), ("hr_min", -1, 0.15), ("resp_mean", -1, 0.10),
    ("deep_min", +1, 0.20), ("rem_min", +1, 0.10), ("waso_min", -1, 0.10),
    ("sleep_efficiency", +1, 0.10),
)
METRICS = ("deep_min", "rem_min", "light_min", "total_sleep_min", "sleep_efficiency",
           "waso_min", "wake_events", "hr_min", "hrv_mean", "resting_hr", "resp_mean",
           "spo2_mean", "recovery_score")

DDL = """
CREATE TABLE IF NOT EXISTS night_reference (
    date TEXT PRIMARY KEY,            -- the controller's night_date
    deep_min REAL, rem_min REAL, light_min REAL, total_sleep_min REAL,
    sleep_efficiency REAL, waso_min REAL, wake_events INTEGER,
    hr_min REAL, hrv_mean REAL, resting_hr REAL,
    resp_mean REAL, spo2_mean REAL,
    recovery_score REAL,
    source TEXT, updated TEXT
);
"""


def ensure_schema(conn) -> None:
    conn.executescript(DDL)
    try:   # columns added after the first cut of the table
        have = {r[1] for r in conn.execute("PRAGMA table_info(night_reference)").fetchall()}
        for col in ("resp_mean", "spo2_mean"):
            if col not in have:
                conn.execute(f"ALTER TABLE night_reference ADD COLUMN {col} REAL")
    except Exception:
        pass
    for col in ("ref_deepened", "ref_succeeded"):
        try:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(steer_events)").fetchall()}
            if cols and col not in cols:
                conn.execute(f"ALTER TABLE steer_events ADD COLUMN {col} INTEGER")
        except Exception:
            pass


def _apple_segments(conn, lo: float, hi: float) -> List[Tuple[float, float, str]]:
    a = datetime.fromtimestamp(lo, timezone.utc).isoformat()
    b = datetime.fromtimestamp(hi, timezone.utc).isoformat()
    try:
        rows = conn.execute("SELECT start, end, stage, source FROM health_sleep "
                            "WHERE end > ? AND start < ?", (a, b)).fetchall()
    except Exception:
        return []
    out = []
    for st, en, stage, src in rows:
        if source_kind(src) != "apple":
            continue
        x, y = _iso_unix(st), _iso_unix(en)
        if x is not None and y is not None and y > x:
            out.append((max(x, lo), min(y, hi), str(stage)))
    out.sort()
    return out


def _health_values(conn, metric: str, lo: float, hi: float) -> List[Tuple[float, float]]:
    a = datetime.fromtimestamp(lo, timezone.utc).isoformat()
    b = datetime.fromtimestamp(hi, timezone.utc).isoformat()
    try:
        rows = conn.execute("SELECT ts, value FROM apple_health WHERE metric = ? AND ts >= ? "
                            "AND ts <= ? ORDER BY ts", (metric, a, b)).fetchall()
    except Exception:
        return []
    return [(t, float(v)) for ts, v in rows if (t := _iso_unix(ts)) is not None and v is not None]


def night_outcomes(conn, lo: float, hi: float) -> Optional[dict]:
    """Apple-measured outcomes for the night inside [lo, hi] (unix), or None if Apple has none."""
    segs = _apple_segments(conn, lo, hi)
    sleep = [(a, b, s) for a, b, s in segs if s in ("core", "deep", "rem", "asleep")]
    if not sleep:
        return None
    onset, final = min(a for a, _, _ in sleep), max(b for _, b, _ in sleep)
    mins = lambda st: sum(b - a for a, b, s in segs if s == st) / 60.0  # noqa: E731
    deep, rem, core, unspec = mins("deep"), mins("rem"), mins("core"), mins("asleep")
    total = deep + rem + core + unspec
    if total < MIN_SLEEP_MIN:
        return None
    awake = [(a, b) for a, b, s in segs if s == "awake" and a >= onset and b <= final]
    waso = sum(b - a for a, b in awake) / 60.0
    wakes = sum(1 for a, b in awake if (b - a) / 60.0 >= AWAKE_EVENT_MIN)
    in_bed = [(a, b) for a, b, s in segs if s == "in_bed"]
    start = min([onset] + [a for a, _ in in_bed])
    end = max([final] + [b for _, b in in_bed])
    eff = total / ((end - start) / 60.0) if end > start else None
    hr = [v for _, v in _health_values(conn, "heart_rate", onset, final)]
    # lowest SUSTAINED rate: the 10th percentile, so one artefactual low reading cannot set it
    hr_min = (sorted(hr)[max(0, int(len(hr) * 0.1) - 1)] if len(hr) >= 5 else None)
    hrv = [v for _, v in _health_values(conn, "heart_rate_variability", onset - 3600, final + 3600)]
    rhr = [v for _, v in _health_values(conn, "resting_heart_rate", final - 6 * 3600,
                                        final + 18 * 3600)]
    resp = [v for _, v in _health_values(conn, "respiratory_rate", onset, final)]
    spo2 = [v for _, v in _health_values(conn, "blood_oxygen_saturation", onset, final)]
    return {"deep_min": round(deep, 1), "rem_min": round(rem, 1), "light_min": round(core, 1),
            "total_sleep_min": round(total, 1),
            "sleep_efficiency": round(min(1.0, eff), 3) if eff else None,
            "waso_min": round(waso, 1), "wake_events": wakes,
            "hr_min": round(hr_min, 1) if hr_min else None,
            "hrv_mean": round(statistics.fmean(hrv), 1) if hrv else None,
            "resting_hr": round(rhr[-1], 1) if rhr else None,
            "resp_mean": round(statistics.fmean(resp), 2) if resp else None,
            "spo2_mean": round(statistics.fmean(spo2), 1) if spo2 else None,
            "onset_unix": onset, "final_unix": final}


def _robust_z(x: float, hist: Sequence[float]) -> Optional[float]:
    if len(hist) < MIN_HISTORY:
        return None
    med = statistics.median(hist)
    mad = statistics.median(abs(h - med) for h in hist) * 1.4826
    # A very steady history must not turn a real change into a huge (or, with zero spread, a
    # zero) score: the spread is floored at 5% of the usual value, about the night-to-night
    # noise of any of these measurements.
    scale = max(mad, statistics.pstdev(hist) if mad <= 1e-9 else 0.0, 0.05 * abs(med))
    if scale <= 1e-9:
        return 0.0
    return max(-3.0, min(3.0, (x - med) / scale))


def recovery_score(row: dict, history: Sequence[dict]) -> Optional[float]:
    """50 = your usual night; +10 per robust SD better, from the weighted components present."""
    tot = wsum = 0.0
    for col, sign, w in COMPONENTS:
        x = row.get(col)
        hist = [h[col] for h in history if h.get(col) is not None]
        if x is None:
            continue
        z = _robust_z(float(x), hist)
        if z is None:
            continue
        tot += w * sign * z
        wsum += w
    if wsum < 0.4:            # too few components to call it a recovery score
        return None
    return round(max(0.0, min(100.0, 50.0 + 10.0 * tot / wsum)), 1)


def build(conn, nights: Optional[Sequence[Tuple[str, float, float]]] = None) -> dict:
    """Recompute every recent night's reference outcomes and recovery scores (oldest first, so
    each night is scored against the ones before it)."""
    ensure_schema(conn)
    nights = sorted(nights if nights is not None else recent_nights(conn, limit=60))
    now = datetime.now().replace(microsecond=0).isoformat()
    done: List[dict] = []
    for nd, lo, hi in nights:
        out = night_outcomes(conn, lo, hi)
        if out is None:
            continue
        out["recovery_score"] = recovery_score(out, done[-HISTORY_N:])
        out["date"] = nd
        done.append(out)
        cols = ("date",) + METRICS + ("source", "updated")
        conn.execute(
            f"INSERT INTO night_reference ({', '.join(cols)}) VALUES "
            f"({', '.join('?' for _ in cols)}) ON CONFLICT(date) DO UPDATE SET "
            + ", ".join(f"{c}=excluded.{c}" for c in cols[1:]),
            (nd, *(out.get(c) for c in METRICS), "apple", now))
    conn.commit()
    regraded = regrade_steer_events(conn)
    return {"nights": len(done), "scored": sum(1 for d in done if d["recovery_score"] is not None),
            "steer_events_regraded": regraded}


def reference_metric(conn, metric: str, dates: Sequence[str]) -> Dict[str, float]:
    if metric not in METRICS or not dates:
        return {}
    try:
        ensure_schema(conn)
        qs = ",".join("?" for _ in dates)
        rows = conn.execute(f"SELECT date, {metric} FROM night_reference WHERE date IN ({qs})",
                            list(dates)).fetchall()
    except Exception:
        return {}
    return {r[0]: float(r[1]) for r in rows if r[1] is not None}


def regrade_steer_events(conn) -> int:
    """Label each resolved deepen / rem_warm event with whether APPLE saw the target stage in
    its response window (``ref_succeeded``) and any deep at all (``ref_deepened``)."""
    try:
        rows = conn.execute("SELECT id, ts, horizon_min, maneuver FROM steer_events "
                            "WHERE resolved = 1 AND ref_succeeded IS NULL").fetchall()
    except Exception:
        return 0
    n = 0
    for rid, ts, horizon, maneuver in rows:
        t0 = _to_unix(ts)
        if t0 is None:
            continue
        t1 = t0 + float(horizon or 0) * 60.0
        segs = _apple_segments(conn, t0 - 6 * 3600, t1 + 6 * 3600)
        if not segs or not any(a <= t0 <= b for a, b, _ in segs) and not (
                segs[0][0] <= t0 <= segs[-1][1]):
            continue                      # Apple did not cover this moment: leave it ungraded
        inside = [s for a, b, s in segs if b > t0 and a < t1]
        target = "rem" if maneuver == "rem_warm" else "deep"
        conn.execute("UPDATE steer_events SET ref_deepened = ?, ref_succeeded = ? WHERE id = ?",
                     (int("deep" in inside), int(target in inside), rid))
        n += 1
    if n:
        conn.commit()
    return n


def snapshot(conn) -> dict:
    """Counts only (the health branch is public)."""
    try:
        ensure_schema(conn)
        n, scored = conn.execute("SELECT COUNT(*), COUNT(recovery_score) FROM night_reference"
                                 ).fetchone()
        g = conn.execute("SELECT COUNT(ref_succeeded) FROM steer_events").fetchone()[0]
    except Exception as exc:
        return {"error": type(exc).__name__}
    return {"reference_nights": n, "recovery_scored_nights": scored,
            "steer_events_graded_by_apple": g, "needs_nights_for_score": MIN_HISTORY}


__all__ = ["night_outcomes", "recovery_score", "build", "reference_metric",
           "regrade_steer_events", "snapshot", "METRICS"]
