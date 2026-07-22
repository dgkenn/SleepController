"""Control bridge: the API↔daemon contract.

The daemon OWNS the device. The API never calls pyEight; it enqueues a command the daemon
applies on its next tick, and reads the daemon's ``runtime_state`` snapshot for status. This
keeps control race-free and means a UI/API crash can never disrupt the closed loop.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone

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


# Rolling retention window for sensor_samples (append-only history, see db.py). The same SQLite
# DB is already covered by the off-box encrypted backup, so this data is durably saved off-box
# automatically -- local retention here is just about keeping the live table bounded, not the
# only copy of the data.
_SENSOR_SAMPLES_RETENTION_DAYS = 60

# /bcg/ingest fires ~1/sec while the phone is streaming; running the retention DELETE on every
# single call was a full table scan of sensor_samples per ingest for no benefit (the window only
# meaningfully changes over hours, not seconds). Gate it to at most once/hour -- the INSERT above
# still runs every call, so accumulation is unaffected. Module-level (not per-connection) since
# it's a single-process API server; a monotonic clock avoids any wall-clock-jump weirdness.
_SENSOR_PRUNE_INTERVAL_S = 3600.0
_last_sensor_prune_monotonic = 0.0


def append_sensor_sample(conn: sqlite3.Connection, sample: dict) -> None:
    """Append one phone/sensor-derived sample (never overwrites) so overnight data ACCUMULATES
    into a time-series dataset for later model training / nightly learning, unlike the
    ``live_sensor`` singleton above which only ever holds the latest reading. Best-effort: a
    logging failure here must never break /bcg/ingest for the daemon's real-time fusion path."""
    global _last_sensor_prune_monotonic
    try:
        conn.execute(
            """INSERT INTO sensor_samples (ts, hr, hrv, movement, source, fs, n_samples)
                VALUES (?,?,?,?,?,?,?)""",
            (_now(), sample.get("hr"), sample.get("hrv"), sample.get("movement"),
             sample.get("source", "phone"), sample.get("fs"), sample.get("n_samples")),
        )
        now_mono = time.monotonic()
        if now_mono - _last_sensor_prune_monotonic >= _SENSOR_PRUNE_INTERVAL_S:
            cutoff = (datetime.now(timezone.utc)
                     - timedelta(days=_SENSOR_SAMPLES_RETENTION_DAYS)).isoformat()
            conn.execute("DELETE FROM sensor_samples WHERE ts < ?", (cutoff,))
            _last_sensor_prune_monotonic = now_mono
        conn.commit()
    except Exception:
        pass  # never disrupt /bcg/ingest's real-time fusion path over a telemetry write


def recent_sensor_samples(conn: sqlite3.Connection, limit: int = 500, since: str | None = None) -> list:
    """Most-recent phone/sensor samples (ts DESC) as dicts, for export/inspection/model training.
    ``since`` (ISO timestamp), if given, restricts to rows at or after it."""
    if since:
        rows = conn.execute(
            "SELECT * FROM sensor_samples WHERE ts >= ? ORDER BY ts DESC LIMIT ?", (since, limit)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM sensor_samples ORDER BY ts DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def write_wake_log(conn: sqlite3.Connection, row: dict) -> None:
    """Record how the user was woken on ``row['date']`` (one row/night; last write wins). Joined
    with the morning grogginess check-in to personalize the wake tuning."""
    conn.execute(
        """INSERT INTO wake_log (date, woke_from_stage, minutes_early, window_min, forced,
            p_wake, wake_thermal_f, created, onset_warm_f, night_type,
            onset_cold_settle_f, warm_pulse_on)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(date) DO UPDATE SET
         woke_from_stage=excluded.woke_from_stage, minutes_early=excluded.minutes_early,
         window_min=excluded.window_min, forced=excluded.forced, p_wake=excluded.p_wake,
         wake_thermal_f=excluded.wake_thermal_f, created=excluded.created,
         onset_warm_f=excluded.onset_warm_f, night_type=excluded.night_type,
         onset_cold_settle_f=excluded.onset_cold_settle_f,
         warm_pulse_on=excluded.warm_pulse_on""",
        (row.get("date"), row.get("woke_from_stage"), row.get("minutes_early"),
         row.get("window_min"), 1 if row.get("forced") else 0, row.get("p_wake"),
         row.get("wake_thermal_f"), _now(), row.get("onset_warm_f"), row.get("night_type"),
         row.get("onset_cold_settle_f"),
         None if row.get("warm_pulse_on") is None else (1 if row.get("warm_pulse_on") else 0)))
    conn.commit()


def record_thermal_sample(conn: sqlite3.Connection, row: dict) -> None:
    """Append one thermal-response sample (bed actively heating/cooling toward a target). Feeds
    later fine-tuning of the controller's lead-time / pre-compensation model. Best-effort: a
    logging failure here must NEVER raise into the control loop that calls it every tick."""
    try:
        conn.execute(
            """INSERT INTO thermal_samples
                (ts, device_level, target_level, delta_level, direction,
                 bed_temp_f, room_temp_f, state, session_mode)
                VALUES (?,?,?,?,?,?,?,?,?)""",
            (row.get("ts"), row.get("device_level"), row.get("target_level"),
             row.get("delta_level"), row.get("direction"), row.get("bed_temp_f"),
             row.get("room_temp_f"), row.get("state"), row.get("session_mode")),
        )
        conn.commit()
    except Exception:
        pass  # never disrupt the control loop over a telemetry write


def recent_thermal_samples(conn: sqlite3.Connection, limit: int = 500) -> list:
    """Most-recent thermal samples (ts DESC) as dicts, for export/inspection/fine-tuning."""
    rows = conn.execute(
        "SELECT * FROM thermal_samples ORDER BY ts DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(r) for r in rows]


def prune_thermal_samples(conn: sqlite3.Connection, keep_days: int = 45) -> int:
    """Delete thermal_samples rows older than ``keep_days``. Mirrors ``Repository.prune_events``/
    ``prune_raw_samples`` etc, but lives here (not on ``Repository``) because ``thermal_samples``
    is a dashboard-layer table (see ``db.py``'s ``_DASHBOARD_DDL``), not part of the sleepctl
    engine schema. Called once/night at the nightly close-out seam (see
    ``LiveDashboardDaemon._maybe_close_out``), NEVER on the per-tick hot path. Defensive: returns
    0 on any error rather than raising."""
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=keep_days)).isoformat()
        cur = conn.execute("DELETE FROM thermal_samples WHERE ts < ?", (cutoff,))
        deleted = cur.rowcount or 0
        conn.commit()
        return deleted
    except Exception:
        return 0


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


# ---- dedicated cardiac sensor (BLE HR strap / armband, e.g. Polar Verity Sense) --------------
def write_cardiac_sample(conn: sqlite3.Connection, sample: dict) -> None:
    """Persist the latest dedicated-cardiac-sensor sample (singleton, ``live_cardiac``). Written
    by /hr/ingest after a BLE HR batch (HR + RR-interval-derived HRV). Deliberately a SEPARATE
    row from ``live_sensor`` (the phone/accelerometer channel) so the Verity's authoritative
    HR/HRV and the phone's movement can be merged per-field without either clobbering the other
    (see ``read_fused_sensor``)."""
    conn.execute(
        """INSERT INTO live_cardiac (id, updated, hr, hrv, source)
        VALUES (1,?,?,?,?)
        ON CONFLICT(id) DO UPDATE SET
         updated=excluded.updated, hr=excluded.hr, hrv=excluded.hrv, source=excluded.source""",
        (_now(), sample.get("hr"), sample.get("hrv"), sample.get("source", "verity")),
    )
    conn.commit()


def read_cardiac_sample(conn: sqlite3.Connection) -> dict | None:
    """Latest dedicated-cardiac-sensor sample with a computed ``age_seconds``, or None."""
    row = conn.execute("SELECT * FROM live_cardiac WHERE id = 1").fetchone()
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


def read_fused_sensor(conn: sqlite3.Connection, cardiac_max_age_s: float = 30.0,
                      movement_max_age_s: float = 30.0,
                      phone_hr_max_age_s: float = 30.0) -> dict | None:
    """MERGE the two independent fast-sensor channels into one per-field snapshot for the daemon:

      * **movement** — from the iPhone accelerometer (``live_sensor``); the phone is the only
        source of the sub-second motion signal.
      * **hr / hrv** — from the dedicated cardiac sensor (``live_cardiac``, e.g. Polar Verity
        Sense) when it is fresh; that optical/ECG HR + RR-interval HRV is AUTHORITATIVE and wins
        over the phone's best-effort ballistocardiogram HR. If the cardiac sensor is absent or
        stale, we fall back to the phone's best-effort HR/HRV so a lone iPhone still contributes.

    Each field is gated by ITS OWN freshness (a disconnected Verity doesn't strand a live phone,
    and vice-versa). Returns per-field values + ages, or None if nothing fresh is available.
    ``hr_source`` records which channel actually supplied HR ("verity"/"phone"), for the UI."""
    phone = read_sensor_sample(conn)
    card = read_cardiac_sample(conn)

    def _fresh(d, key, max_age):
        if not d:
            return (None, None)
        v = d.get(key)
        a = d.get("age_seconds")
        if v is None or a is None or a > max_age:
            return (None, None)
        return (v, a)

    # movement: phone only
    mv, mv_age = _fresh(phone, "movement", movement_max_age_s)
    # HR: dedicated cardiac sensor first (authoritative), else phone best-effort BCG
    hr, hr_age = _fresh(card, "hr", cardiac_max_age_s)
    hr_source = (card.get("source") or "verity") if card and hr is not None else None
    if hr is None:
        hr, hr_age = _fresh(phone, "hr", phone_hr_max_age_s)
        hr_source = (phone.get("source") or "phone") if phone and hr is not None else None
    # HRV: same priority
    hrv, hrv_age = _fresh(card, "hrv", cardiac_max_age_s)
    if hrv is None:
        hrv, hrv_age = _fresh(phone, "hrv", phone_hr_max_age_s)

    if hr is None and hrv is None and mv is None:
        return None
    return {
        "hr": hr, "hrv": hrv, "movement": mv,
        "hr_age_seconds": hr_age, "hrv_age_seconds": hrv_age, "movement_age_seconds": mv_age,
        "hr_source": hr_source,
    }


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
