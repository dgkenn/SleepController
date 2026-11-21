"""Apple Watch data from the iPhone: Health Auto Export's REST automation -> this box.

Apple has no cloud API for HealthKit; the data lives on the phone. Health Auto Export (iOS) runs an
automation that POSTs its JSON export to a URL on a schedule, which is the only hands-off way to
get it here. Its format (help.healthyapps.dev/en/health-auto-export/export-format/):

    {"data": {"metrics": [{"name": "heart_rate", "units": "count/min",
                           "data": [{"date": "2026-11-21 03:14:00 -0500",
                                     "Min": 52, "Avg": 55, "Max": 58, "source": "..."}]},
                          {"name": "sleep_analysis", "data": [...]}, ...],
              "workouts": [...]}}

Point fields vary: ``qty`` for single-valued metrics, ``Avg``/``Min``/``Max`` (any case) for
heart rate. Sleep comes in two shapes depending on the app's "aggregate sleep" setting:

* unaggregated -- one entry per stage segment: ``startDate``/``endDate`` + ``value`` ("Core",
  "Deep", "REM", "Awake", "In Bed", "Asleep");
* aggregated -- one entry per night with stage totals (``core``, ``deep``, ``rem``, ``awake``,
  ``asleep``, ``inBed`` in hours) and ``sleepStart``/``sleepEnd``.

Both are stored: segments in ``apple_sleep``, nightly totals as ``sleep_<stage>_h`` metrics in
``apple_health``. Everything is an idempotent upsert because the app re-sends overlapping windows.
Nothing here is published: the health snapshot carries counts and times only.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

#: The metrics worth keeping for sleep. Anything else in an export is counted and ignored.
KEPT = {
    "heart_rate", "resting_heart_rate", "heart_rate_variability", "respiratory_rate",
    "blood_oxygen_saturation", "apple_sleeping_wrist_temperature", "sleeping_wrist_temperature",
    "walking_heart_rate_average",
}
_STAGES = {"core": "core", "deep": "deep", "rem": "rem", "awake": "awake", "asleep": "asleep",
           "in bed": "in_bed", "inbed": "in_bed", "in_bed": "in_bed", "unspecified": "asleep"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_ts(value: Any) -> Optional[str]:
    """Health Auto Export stamps ``yyyy-MM-dd HH:mm:ss Z`` (``-0500``); accept ISO too.
    Returned as UTC ISO-8601."""
    if not isinstance(value, str) or not value.strip():
        return None
    v = value.strip()
    for fmt in ("%Y-%m-%d %H:%M:%S %z", "%Y-%m-%d %H:%M:%S%z", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            return _utc(datetime.strptime(v, fmt))
        except ValueError:
            pass
    try:
        dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        return None   # a wall-clock time with no offset cannot be placed on the night
    return _utc(dt)


def _utc(dt: datetime) -> str:
    """Stored in UTC so the TEXT columns order and compare correctly across offsets (DST)."""
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def _num(x: Any) -> Optional[float]:
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    return f if f == f else None


def _get(point: dict, *names: str):
    low = {str(k).lower(): v for k, v in point.items()}
    for n in names:
        if n.lower() in low and low[n.lower()] is not None:
            return low[n.lower()]
    return None


def _stage(value: Any) -> Optional[str]:
    return _STAGES.get(str(value or "").strip().lower())


def ingest(conn: sqlite3.Connection, payload: dict) -> dict:
    data = payload.get("data", payload) if isinstance(payload, dict) else {}
    metrics = data.get("metrics") if isinstance(data, dict) else None
    if not isinstance(metrics, list):
        return {"ok": False, "error": "no data.metrics array in the payload"}
    received = _now()
    stored: dict = {}
    segments = 0
    skipped = 0
    ignored: set = set()
    for m in metrics:
        if not isinstance(m, dict):
            continue
        name = str(m.get("name") or "").strip().lower()
        points = m.get("data") if isinstance(m.get("data"), list) else []
        if name == "sleep_analysis":
            for p in points:
                if not isinstance(p, dict):
                    continue
                n_seg, n_tot = _ingest_sleep_point(conn, p, received)
                segments += n_seg
                if n_tot:
                    stored["sleep_totals"] = stored.get("sleep_totals", 0) + n_tot
                if not n_seg and not n_tot:
                    skipped += 1
            continue
        if name not in KEPT:
            ignored.add(name)
            continue
        units = m.get("units")
        for p in points:
            if not isinstance(p, dict):
                continue
            ts = parse_ts(_get(p, "date"))
            val = _num(_get(p, "qty", "avg"))
            if ts is None or val is None:
                skipped += 1
                continue
            extra = {k: _get(p, k) for k in ("min", "max", "source")}
            extra = {k: v for k, v in extra.items() if v is not None}
            if units:
                extra["units"] = units
            conn.execute(
                "INSERT INTO apple_health (ts, metric, value, extra, received) VALUES (?,?,?,?,?) "
                "ON CONFLICT(ts, metric) DO UPDATE SET value=excluded.value, extra=excluded.extra, "
                "received=excluded.received",
                (ts, name, val, json.dumps(extra), received))
            stored[name] = stored.get(name, 0) + 1
    conn.commit()
    return {"ok": True, "stored": stored, "sleep_segments": segments, "skipped": skipped,
            "ignored_metrics": sorted(x for x in ignored if x)}


def _ingest_sleep_point(conn, p: dict, received: str) -> tuple[int, int]:
    start = parse_ts(_get(p, "startDate", "start"))
    end = parse_ts(_get(p, "endDate", "end"))
    stage = _stage(_get(p, "value", "stage"))
    source = _get(p, "source")
    if start and end and stage:
        conn.execute(
            "INSERT INTO apple_sleep (start, end, stage, source, received) VALUES (?,?,?,?,?) "
            "ON CONFLICT(start, end, stage) DO UPDATE SET source=excluded.source, "
            "received=excluded.received", (start, end, stage, source, received))
        return 1, 0
    # Aggregated: one row per night with stage totals in hours.
    night_end = parse_ts(_get(p, "sleepEnd", "inBedEnd", "date"))
    if not night_end:
        return 0, 0
    n = 0
    for key in ("core", "deep", "rem", "awake", "asleep", "inBed", "totalSleep"):
        val = _num(_get(p, key))
        if val is None:
            continue
        metric = "sleep_" + {"inBed": "in_bed", "totalSleep": "total"}.get(key, key) + "_h"
        extra = {"sleep_start": parse_ts(_get(p, "sleepStart", "inBedStart")), "source": source}
        conn.execute(
            "INSERT INTO apple_health (ts, metric, value, extra, received) VALUES (?,?,?,?,?) "
            "ON CONFLICT(ts, metric) DO UPDATE SET value=excluded.value, extra=excluded.extra, "
            "received=excluded.received", (night_end, metric, val, json.dumps(extra), received))
        n += 1
    return 0, n


def summary(conn: sqlite3.Connection, days: int = 7) -> dict:
    """Counts and times only -- safe for the public health snapshot (no values)."""
    try:
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        last = conn.execute("SELECT MAX(received) FROM ("
                            "SELECT received FROM apple_health UNION ALL "
                            "SELECT received FROM apple_sleep)").fetchone()[0]
        rows = conn.execute("SELECT metric, COUNT(*) FROM apple_health WHERE received >= ? "
                            "GROUP BY metric", (since,)).fetchall()
        segs = conn.execute("SELECT COUNT(*), COUNT(DISTINCT substr(end, 1, 10)) FROM apple_sleep "
                            "WHERE received >= ?", (since,)).fetchone()
    except sqlite3.Error as exc:
        return {"error": str(exc)[:120]}
    return {"last_received": last, "points_7d": {m: n for m, n in rows},
            "sleep_segments_7d": segs[0] or 0, "sleep_nights_7d": segs[1] or 0}


def night_stages(conn: sqlite3.Connection, start_iso: str, end_iso: str) -> list[dict]:
    """Apple's stage segments overlapping [start, end): the reference to score our stager against.
    Bounds must be UTC ISO strings, like the stored values."""
    rows = conn.execute("SELECT start, end, stage, source FROM apple_sleep WHERE end > ? AND "
                        "start < ? ORDER BY start", (start_iso, end_iso)).fetchall()
    return [{"start": r[0], "end": r[1], "stage": r[2], "source": r[3]} for r in rows]
