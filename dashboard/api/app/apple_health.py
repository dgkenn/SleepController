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

Both are stored: segments in ``health_sleep`` (keyed with the writing app, so Apple's and
Bevel's stay apart), nightly totals as ``sleep_<stage>_h`` metrics in
``apple_health``. Everything is an idempotent upsert because the app re-sends overlapping windows.
Nothing here is published: the health snapshot carries counts and times only.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
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
    out = {"ok": True, "stored": stored, "sleep_segments": segments, "skipped": skipped,
           "ignored_metrics": sorted(x for x in ignored if x)}
    if segments or stored.get("sleep_totals"):
        out["consensus"] = kick_consensus()
    return out


# ------------------------------------------------- the morning comparison, after new stages
# New sleep stages -> rebuild the three-way comparison and refit the personal calibration
# (sleepctl.learning.wearable_consensus). Run off the request on its own DB connection; the
# phone re-sends overlapping windows hourly, so a run is skipped within RERUN_MIN of the last.
RERUN_MIN = 30.0
_consensus_lock = threading.Lock()
_consensus_last = 0.0


def kick_consensus(force: bool = False) -> str:
    global _consensus_last
    now = time.monotonic()
    if not force and now - _consensus_last < RERUN_MIN * 60:
        return "ran recently"
    if not _consensus_lock.acquire(blocking=False):
        return "already running"
    _consensus_last = now

    def work():
        try:
            from app.db import get_repo
            from sleepctl.learning import wearable_consensus
            repo = get_repo()
            try:
                wearable_consensus.run(repo)
            finally:
                try:
                    repo.close()
                except Exception:
                    pass
        except Exception as exc:   # never let the background pass take the API down
            print(f"[apple_health] consensus pass failed: {exc!r}", flush=True)
        finally:
            _consensus_lock.release()

    threading.Thread(target=work, name="wearable-consensus", daemon=True).start()
    return "started"


def _ingest_sleep_point(conn, p: dict, received: str) -> tuple[int, int]:
    start = parse_ts(_get(p, "startDate", "start"))
    end = parse_ts(_get(p, "endDate", "end"))
    stage = _stage(_get(p, "value", "stage"))
    source = _get(p, "source")
    if start and end and stage:
        conn.execute(
            "INSERT INTO health_sleep (start, end, stage, source, received) VALUES (?,?,?,?,?) "
            "ON CONFLICT(start, end, stage, source) DO UPDATE SET received=excluded.received",
            (start, end, stage, str(source or "")[:80], received))
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


_ATTEMPTS_DDL = ("CREATE TABLE IF NOT EXISTS apple_ingest_attempts (at TEXT, path TEXT, kind TEXT, "
                 "ok INTEGER, stored INTEGER, skipped INTEGER, error TEXT)")


def record_attempt(conn: sqlite3.Connection, path: str, result: Optional[dict],
                   error: Optional[str] = None) -> None:
    """Every upload that reaches the box, kept or not, so "nothing arrived" can be told apart from
    "it arrived and was rejected". Counts and a short reason only -- never values."""
    try:
        r = result or {}
        st = r.get("stored")
        if isinstance(st, dict):
            st = sum(v for v in st.values() if isinstance(v, int)) + int(r.get("sleep_segments") or 0)
        conn.execute(_ATTEMPTS_DDL)
        conn.execute("INSERT INTO apple_ingest_attempts VALUES (?,?,?,?,?,?,?)",
                     (_now(), path, str(r.get("kind") or "")[:30], int(bool(r.get("ok"))),
                      int(st or 0), int(r.get("skipped") or 0),
                      (error or r.get("error") or "")[:120] or None))
        conn.execute("DELETE FROM apple_ingest_attempts WHERE at < ?",
                     ((datetime.now(timezone.utc) - timedelta(days=30)).isoformat(),))
        conn.commit()
    except Exception:
        pass


def recent_attempts(conn: sqlite3.Connection, n: int = 5) -> list:
    try:
        conn.execute(_ATTEMPTS_DDL)
        rows = conn.execute("SELECT at, path, kind, ok, stored, skipped, error FROM "
                            "apple_ingest_attempts ORDER BY rowid DESC LIMIT ?", (n,)).fetchall()
    except sqlite3.Error:
        return []
    return [{"at": r[0], "path": r[1], "kind": r[2], "ok": bool(r[3]), "stored": r[4],
             "skipped": r[5], "error": r[6]} for r in rows]


def summary(conn: sqlite3.Connection, days: int = 7) -> dict:
    """Counts and times only -- safe for the public health snapshot (no values)."""
    try:
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        last = conn.execute("SELECT MAX(received) FROM ("
                            "SELECT received FROM apple_health UNION ALL "
                            "SELECT received FROM health_sleep)").fetchone()[0]
        rows = conn.execute("SELECT metric, COUNT(*) FROM apple_health WHERE received >= ? "
                            "GROUP BY metric", (since,)).fetchall()
        segs = conn.execute("SELECT COUNT(*), COUNT(DISTINCT substr(end, 1, 10)) FROM health_sleep "
                            "WHERE received >= ?", (since,)).fetchone()
        srcs = conn.execute("SELECT source, COUNT(*) FROM health_sleep WHERE received >= ? "
                            "GROUP BY source", (since,)).fetchall()
    except sqlite3.Error as exc:
        return {"error": str(exc)[:120]}
    return {"last_received": last, "points_7d": {m: n for m, n in rows},
            "sleep_segments_7d": segs[0] or 0, "sleep_nights_7d": segs[1] or 0,
            # which apps' stages are arriving, by kind only (the raw name carries a device name)
            "sleep_sources_7d": _source_kinds(srcs),
            # every upload that reached the box, including rejected ones (counts only)
            "recent_uploads": recent_attempts(conn)}


def _source_kinds(rows) -> dict:
    from sleepctl.learning.wearable_consensus import source_kind
    out: dict = {}
    for src, n in rows:
        k = source_kind(src) or "other"
        out[k] = out.get(k, 0) + n
    return out


def night_stages(conn: sqlite3.Connection, start_iso: str, end_iso: str) -> list[dict]:
    """Apple's stage segments overlapping [start, end): the reference to score our stager against.
    Bounds must be UTC ISO strings, like the stored values."""
    rows = conn.execute("SELECT start, end, stage, source FROM health_sleep WHERE end > ? AND "
                        "start < ? ORDER BY start", (start_iso, end_iso)).fetchall()
    return [{"start": r[0], "end": r[1], "stage": r[2], "source": r[3]} for r in rows]


# ------------------------------------------------- free path: Apple's own Shortcuts app
# Health Auto Export's REST automation is paid. Shortcuts' "Find Health Samples" is free and can
# POST the samples itself, but it sends them the way Shortcuts can: one JSON field per property,
# each a list -- or, when a list is dropped into a text field, the items joined by newlines -- and
# dates in the phone's display format ("Nov 21, 2026 at 3:14 AM", with a narrow no-break space
# before AM on recent iOS) unless formatted as ISO 8601. This accepts all of that:
#
#   POST /api/apple/shortcut  {"kind": "sleep" | "heart_rate",
#                              "start": [...], "end": [...], "value": [...], "source": [...]}
#
# A date with no offset is taken as the box's local time, which is the phone's too.
_DISPLAY_FORMATS = (
    "%b %d, %Y at %I:%M %p", "%b %d, %Y at %I:%M:%S %p", "%B %d, %Y at %I:%M %p",
    "%B %d, %Y at %I:%M:%S %p", "%b %d, %Y, %I:%M %p", "%m/%d/%Y, %I:%M %p", "%m/%d/%y, %I:%M %p",
    "%m/%d/%Y %I:%M %p", "%Y-%m-%d %H:%M:%S %z", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M",
    "%d %b %Y at %H:%M", "%d %B %Y at %H:%M", "%b %d, %Y at %H:%M",
)


def parse_any_ts(value: Any) -> Optional[str]:
    """Any date the phone may send -> UTC ISO; naive means local (box == phone)."""
    if not isinstance(value, str) or not value.strip():
        return None
    v = " ".join(value.replace(" ", " ").replace(" ", " ").split())
    got = parse_ts(v)
    if got:
        return got
    dt = None
    try:
        dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        for fmt in _DISPLAY_FORMATS:
            try:
                dt = datetime.strptime(v, fmt)
                break
            except ValueError:
                continue
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.astimezone()          # naive -> this machine's local zone
    return _utc(dt)


def _column(payload: dict, *names: str) -> list:
    for n in names:
        if n in payload and payload[n] is not None:
            v = payload[n]
            if isinstance(v, list):
                return v
            if isinstance(v, str):
                return [x for x in v.splitlines() if x.strip()] if "\n" in v else [v]
            return [v]
    return []


_NUM = __import__("re").compile(r"-?\d+(?:\.\d+)?")


#: Numeric sample kinds a shortcut may send -> the stored metric (same names as Health Auto
#: Export's, so both paths land in the same rows).
_NUMERIC_KINDS = {
    "heart_rate": "heart_rate", "heartrate": "heart_rate", "hr": "heart_rate",
    "hrv": "heart_rate_variability", "heart_rate_variability": "heart_rate_variability",
    "respiratory_rate": "respiratory_rate", "breathing_rate": "respiratory_rate",
    "resting_heart_rate": "resting_heart_rate",
    "blood_oxygen": "blood_oxygen_saturation", "oxygen_saturation": "blood_oxygen_saturation",
    "blood_oxygen_saturation": "blood_oxygen_saturation",
    "wrist_temperature": "apple_sleeping_wrist_temperature",
    "sleeping_wrist_temperature": "apple_sleeping_wrist_temperature",
}

#: HKCategoryValueSleepAnalysis raw values, should a sample's value arrive as its number.
_SLEEP_CODES = {"0": "in_bed", "1": "asleep", "2": "awake", "3": "core", "4": "deep", "5": "rem"}


def shortcut_stage(value) -> Optional[str]:
    """Shortcuts' sleep value in any of the spellings iOS versions use -- "Core", "Asleep Core",
    "Asleep (Unspecified)", "In Bed", "REM Sleep", the raw code -- mapped to a stored stage."""
    v = str(value or "").strip().lower()
    if not v:
        return None
    if v in _SLEEP_CODES:
        return _STAGES.get(_SLEEP_CODES[v].replace("_", " "), _SLEEP_CODES[v])
    direct = _stage(v)
    if direct:
        return direct
    for key, stage in (("core", "core"), ("deep", "deep"), ("rem", "rem"), ("awake", "awake"),
                       ("in bed", "in_bed"), ("inbed", "in_bed"), ("unspecified", "asleep"),
                       ("asleep", "asleep"), ("light", "core")):
        if key in v:
            return stage
    return None


def ingest_shortcut(conn: sqlite3.Connection, payload: dict) -> dict:
    kind = str(payload.get("kind") or payload.get("type") or "").strip().lower().replace(" ", "_")
    starts = _column(payload, "start", "startDate", "start_date")
    ends = _column(payload, "end", "endDate", "end_date")
    values = _column(payload, "value", "values")
    sources = _column(payload, "source", "sources")
    n = len(starts)
    note = None
    if values and len(values) != n:
        note = f"{n} start dates but {len(values)} values; paired the first {min(n, len(values))}"
        n = min(n, len(values))
    received = _now()
    stored = skipped = 0
    reasons: dict = {}
    by_source: dict = {}

    def skip(why: str, src: str) -> None:
        nonlocal skipped
        skipped += 1
        if len(reasons) < 12 or why in reasons:
            reasons[why] = reasons.get(why, 0) + 1
        by_source.setdefault(src or "?", [0, 0])[1] += 1

    for i in range(n):
        src = str(sources[i] if i < len(sources) else (sources[0] if len(sources) == 1 else ""))
        start = parse_any_ts(str(starts[i]))
        if kind in ("sleep", "sleep_analysis"):
            end = parse_any_ts(str(ends[i])) if i < len(ends) else None
            raw = values[i] if i < len(values) else None
            stage = shortcut_stage(raw)
            if not start:
                skip(f"unreadable start date {str(starts[i])[:40]!r}", src)
                continue
            if not end:
                skip(f"unreadable end date {str(ends[i])[:40] if i < len(ends) else None!r}", src)
                continue
            if not stage:
                skip(f"unknown sleep value {str(raw)[:30]!r}", src)
                continue
            by_source.setdefault(src or "?", [0, 0])[0] += 1
            conn.execute(
                "INSERT INTO health_sleep (start, end, stage, source, received) VALUES (?,?,?,?,?) "
                "ON CONFLICT(start, end, stage, source) DO UPDATE SET received=excluded.received",
                (start, end, stage, src[:80], received))
            stored += 1
        elif kind in _NUMERIC_KINDS:
            metric = _NUMERIC_KINDS[kind]
            m = _NUM.search(str(values[i] if i < len(values) else ""))
            if not start:
                skip(f"unreadable date {str(starts[i])[:40]!r}", src)
                continue
            if not m:
                skip(f"no number in {str(values[i] if i < len(values) else None)[:30]!r}", src)
                continue
            by_source.setdefault(src or "?", [0, 0])[0] += 1
            val = float(m.group())
            if metric == "blood_oxygen_saturation" and val <= 1.0:
                val *= 100.0                # Shortcuts may give 0.97 or "97%": store percent
            conn.execute(
                "INSERT INTO apple_health (ts, metric, value, extra, received) VALUES (?,?,?,?,?) "
                "ON CONFLICT(ts, metric) DO UPDATE SET value=excluded.value, extra=excluded.extra, "
                "received=excluded.received",
                (start, metric, val, json.dumps({"source": src[:80]}), received))
            stored += 1
        else:
            return {"ok": False, "error": "kind must be one of: sleep, heart_rate, hrv, "
                    "respiratory_rate, resting_heart_rate, blood_oxygen, wrist_temperature",
                    "got": kind[:30]}
    conn.commit()
    out: dict = {"ok": stored > 0, "kind": kind, "stored": stored, "skipped": skipped}
    if skipped:
        out["skipped_because"] = reasons
    # per writing app: [stored, skipped] -- shows at a glance whether Bevel's stages came through
    ranked = sorted(by_source.items(), key=lambda kv: (-kv[1][0], -kv[1][1]))
    out["by_source"] = {k[:40]: v for k, v in ranked[:10]}   # apps that stored anything first
    if note:
        out["note"] = note
    if skipped and not stored:
        out["first_unread"] = {"start": str(starts[0])[:60] if starts else None,
                               "value": str(values[0])[:30] if values else None}
    if stored and kind in ("sleep", "sleep_analysis"):
        out["consensus"] = kick_consensus()
    elif stored:
        # heart rate completes the picture for nights with no live feed (the replay stages them
        # from it), and it is normally sent straight after the sleep samples -- so rerun now
        # rather than wait out the sleep upload's rerun window
        out["consensus"] = kick_consensus(force=True)
    return out
