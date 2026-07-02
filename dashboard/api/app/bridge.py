"""Control bridge: the API↔daemon contract.

The daemon OWNS the device. The API never calls pyEight; it enqueues a command the daemon
applies on its next tick, and reads the daemon's ``runtime_state`` snapshot for status. This
keeps control race-free and means a UI/API crash can never disrupt the closed loop.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone

VALID_COMMANDS = {
    "start", "pause", "resume", "stop", "safe_default",
    "set_mode", "set_temp", "nudge_temp", "set_wake", "clear_wake",
    # Eight Sleep app parity
    "power_on", "power_off", "away_on", "away_off", "prime",
    # On-demand onset induction + nap sessions
    "induce_sleep", "start_nap", "end_session",
    # On-bed self-test / thermal calibration battery
    "self_test", "self_test_cancel",
    # Interactive in-bed comfort mapping sweep
    "comfort_cal_start", "comfort_cal_rate", "comfort_cal_cancel",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---- API side ----------------------------------------------------------------
def enqueue_command(conn: sqlite3.Connection, ctype: str, payload: dict | None = None) -> int:
    if ctype not in VALID_COMMANDS:
        raise ValueError(f"unknown command {ctype!r}")
    cur = conn.execute(
        "INSERT INTO commands (ts, type, payload, status) VALUES (?,?,?,'pending')",
        (_now(), ctype, json.dumps(payload or {})),
    )
    conn.commit()
    return cur.lastrowid


def read_runtime_state(conn: sqlite3.Connection, stale_seconds: int = 180) -> dict:
    row = conn.execute("SELECT * FROM runtime_state WHERE id = 1").fetchone()
    if row is None:
        return {"daemon_alive": False, "state": "UNKNOWN", "stale": True, "updated": None}
    d = dict(row)
    d["daemon_alive"] = bool(d.get("daemon_alive"))
    d["extra"] = json.loads(d["extra"]) if d.get("extra") else {}
    # freshness check
    stale = True
    if d.get("updated"):
        try:
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(d["updated"])).total_seconds()
            stale = age > stale_seconds
        except Exception:
            stale = True
    d["stale"] = stale
    if stale:
        d["daemon_alive"] = False
    return d


# ---- daemon side -------------------------------------------------------------
def next_pending_command(conn: sqlite3.Connection) -> dict | None:
    row = conn.execute(
        "SELECT * FROM commands WHERE status = 'pending' ORDER BY id ASC LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    d = dict(row)
    d["payload"] = json.loads(d["payload"]) if d["payload"] else {}
    return d


def mark_applied(conn: sqlite3.Connection, command_id: int) -> None:
    conn.execute(
        "UPDATE commands SET status='applied', applied_ts=? WHERE id=?",
        (_now(), command_id),
    )
    conn.commit()


# ---- phone/independent-sensor sample (iPhone accelerometer → BCG) ------------
def write_sensor_sample(conn: sqlite3.Connection, sample: dict) -> None:
    """Persist the latest phone/sensor-derived sample (singleton). Written by the API's
    /bcg/ingest after the BCG processor turns a raw accel batch into HR/HRV/movement; read
    by the daemon's ``BridgeWearableSource`` to fuse sub-minute movement onto the Pod frame."""
    conn.execute(
        """INSERT INTO live_sensor (id, updated, hr, hrv, movement, source)
        VALUES (1,?,?,?,?,?)
        ON CONFLICT(id) DO UPDATE SET
         updated=excluded.updated, hr=excluded.hr, hrv=excluded.hrv,
         movement=excluded.movement, source=excluded.source""",
        (_now(), sample.get("hr"), sample.get("hrv"),
         sample.get("movement"), sample.get("source", "phone")),
    )
    conn.commit()


def write_wake_log(conn: sqlite3.Connection, row: dict) -> None:
    """Record how the user was woken on ``row['date']`` (one row/night; last write wins). Joined
    with the morning grogginess check-in to personalize the wake tuning."""
    conn.execute(
        """INSERT INTO wake_log (date, woke_from_stage, minutes_early, window_min, forced,
            p_wake, wake_thermal_f, created, onset_warm_f, night_type)
            VALUES (?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(date) DO UPDATE SET
         woke_from_stage=excluded.woke_from_stage, minutes_early=excluded.minutes_early,
         window_min=excluded.window_min, forced=excluded.forced, p_wake=excluded.p_wake,
         wake_thermal_f=excluded.wake_thermal_f, created=excluded.created,
         onset_warm_f=excluded.onset_warm_f, night_type=excluded.night_type""",
        (row.get("date"), row.get("woke_from_stage"), row.get("minutes_early"),
         row.get("window_min"), 1 if row.get("forced") else 0, row.get("p_wake"),
         row.get("wake_thermal_f"), _now(), row.get("onset_warm_f"), row.get("night_type")))
    conn.commit()


def read_wake_logs(conn: sqlite3.Connection, limit: int = 30) -> list:
    rows = conn.execute("SELECT * FROM wake_log ORDER BY date DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def read_sensor_sample(conn: sqlite3.Connection) -> dict | None:
    """Latest phone/sensor sample with a computed ``age_seconds``, or None if never written."""
    row = conn.execute("SELECT * FROM live_sensor WHERE id = 1").fetchone()
    if row is None:
        return None
    d = dict(row)
    age = None
    if d.get("updated"):
        try:
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(d["updated"])).total_seconds()
        except Exception:
            age = None
    d["age_seconds"] = age
    return d


def write_self_test(conn: sqlite3.Connection, report: dict | None) -> None:
    """Merge the live self-test report into ``runtime_state.extra['self_test']`` in place,
    leaving the rest of the snapshot untouched so the dashboard's sensor fields don't blank out
    while the battery runs. ``None`` clears it."""
    row = conn.execute("SELECT extra FROM runtime_state WHERE id = 1").fetchone()
    extra = {}
    if row is not None and row["extra"]:
        try:
            extra = json.loads(row["extra"])
        except Exception:
            extra = {}
    extra["self_test"] = report
    conn.execute("UPDATE runtime_state SET extra = ?, updated = ? WHERE id = 1",
                 (json.dumps(extra), _now()))
    conn.commit()


def read_self_test(conn: sqlite3.Connection) -> dict | None:
    row = conn.execute("SELECT extra FROM runtime_state WHERE id = 1").fetchone()
    if row is None or not row["extra"]:
        return None
    try:
        return json.loads(row["extra"]).get("self_test")
    except Exception:
        return None


def write_runtime_state(conn: sqlite3.Connection, snapshot: dict) -> None:
    conn.execute(
        """INSERT INTO runtime_state
        (id, updated, state, objective, mode, target_temp_f, bed_temp_f, room_temp_f,
         stage, confidence, target_level, daemon_alive, extra)
        VALUES (1,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(id) DO UPDATE SET
         updated=excluded.updated, state=excluded.state, objective=excluded.objective,
         mode=excluded.mode, target_temp_f=excluded.target_temp_f,
         bed_temp_f=excluded.bed_temp_f, room_temp_f=excluded.room_temp_f,
         stage=excluded.stage, confidence=excluded.confidence,
         target_level=excluded.target_level, daemon_alive=excluded.daemon_alive,
         extra=excluded.extra""",
        (
            _now(), snapshot.get("state"), snapshot.get("objective"), snapshot.get("mode", "auto"),
            snapshot.get("target_temp_f"), snapshot.get("bed_temp_f"), snapshot.get("room_temp_f"),
            snapshot.get("stage"), snapshot.get("confidence"), snapshot.get("target_level"),
            int(bool(snapshot.get("daemon_alive", True))),
            json.dumps(snapshot.get("extra", {})),
        ),
    )
    conn.commit()


# ---- diagnostics: lightweight liveness heartbeats -----------------------------
# Written to a plain file (NOT the DB) so a SQLite hiccup/lock can't itself make the daemon
# look dead, and so ``diagnostics.py`` can check freshness with a cheap stat() call. The
# ``runtime_state.updated`` DB write above is the richer signal; this is the belt-and-suspenders
# one that's independent of it.
def run_dir() -> str:
    """Resolve the ``.run`` directory next to the SQLite DB (or cwd) — same rule the API's
    ``/diag`` endpoint and ``diagnostics.py`` use, kept in one place so they can't drift."""
    db = os.environ.get("SLEEPCTL_DB", "")
    root = os.path.dirname(db) if db else os.getcwd()
    return os.path.join(root, ".run")


def write_heartbeat(name: str) -> None:
    """Touch ``.run/<name>.heartbeat`` with the current time. Best-effort: a permissions/disk
    issue here must never take down the control loop that calls it every tick."""
    try:
        run = run_dir()
        os.makedirs(run, exist_ok=True)
        with open(os.path.join(run, f"{name}.heartbeat"), "w", encoding="utf-8") as fh:
            fh.write(datetime.now(timezone.utc).isoformat())
    except Exception:
        pass
