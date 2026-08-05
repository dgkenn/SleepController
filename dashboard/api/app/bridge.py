"""Control bridge: the API↔daemon contract.

The daemon OWNS the device. The API never calls pyEight; it enqueues a command the daemon
applies on its next tick, and reads the daemon's ``runtime_state`` snapshot for status. This
keeps control race-free and means a UI/API crash can never disrupt the closed loop.
"""

from __future__ import annotations

import json
import math
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
    logging failure here must never break /bcg/ingest for the daemon's real-time fusion path.

    ``hr_frozen`` / ``not_worn`` / ``quality_reason`` are the Verity Sense data-quality flags from
    ``services.assess_cardiac_quality`` (see its docstring for the documented Polar behaviours
    they guard against). All three are optional so callers that don't compute quality (the phone
    BCG path, existing callers, direct test inserts) are unaffected -- they persist as NULL."""
    global _last_sensor_prune_monotonic
    try:
        hr_frozen = sample.get("hr_frozen")
        not_worn = sample.get("not_worn")
        conn.execute(
            """INSERT INTO sensor_samples
                (ts, hr, hrv, movement, source, fs, n_samples, hr_frozen, not_worn, quality_reason)
                VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (_now(), sample.get("hr"), sample.get("hrv"), sample.get("movement"),
             sample.get("source", "phone"), sample.get("fs"), sample.get("n_samples"),
             None if hr_frozen is None else int(bool(hr_frozen)),
             None if not_worn is None else int(bool(not_worn)),
             sample.get("quality_reason")),
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


# Raw RR intervals are the irreplaceable input for a PERSONAL model (every HRV metric derives
# from them and they cannot be reconstructed later), so they are kept far longer than the derived
# sensor_samples window. The SQLite file is covered by the encrypted off-box backup, so this is
# durably preserved; local retention just bounds the live table.
_RR_RETENTION_DAYS = 400
_RR_PRUNE_INTERVAL_S = 3600.0
_last_rr_prune_monotonic = 0.0


def append_rr_intervals(conn: sqlite3.Connection, rr_ms: list, source: str = "verity") -> None:
    """Persist one batch of RAW beat-to-beat RR intervals (milliseconds).

    ``append_sensor_sample`` stores only a single derived HRV scalar (RMSSD) per batch; this keeps
    the underlying series so any HRV metric -- SDNN, pNN50, Poincare SD1/SD2, LF/HF -- can be
    computed later, including for training a model personalized to this user. Best-effort: a
    logging failure must never break /hr/ingest's real-time fusion path."""
    global _last_rr_prune_monotonic
    if not rr_ms:
        return
    try:
        vals = [round(float(x), 1) for x in rr_ms
                if isinstance(x, (int, float)) and 200.0 <= float(x) <= 3000.0]
        if not vals:
            return
        conn.execute(
            "INSERT INTO rr_intervals (ts, rr_ms, n, source) VALUES (?,?,?,?)",
            (_now(), json.dumps(vals), len(vals), source),
        )
        now_mono = time.monotonic()
        if now_mono - _last_rr_prune_monotonic >= _RR_PRUNE_INTERVAL_S:
            cutoff = (datetime.now(timezone.utc)
                      - timedelta(days=_RR_RETENTION_DAYS)).isoformat()
            conn.execute("DELETE FROM rr_intervals WHERE ts < ?", (cutoff,))
            _last_rr_prune_monotonic = now_mono
        conn.commit()
    except Exception:
        pass  # never disrupt the ingest path over a telemetry write


def recent_rr_intervals(conn: sqlite3.Connection, minutes: float = 45.0,
                        max_rows: int = 5000) -> list:
    """Flattened recent RR series as ``[(epoch_seconds, rr_ms), ...]`` (batch timestamp carried on
    each interval in the batch). For HRV features at inference and for personal-model training."""
    out: list = []
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=float(minutes))).isoformat()
        rows = conn.execute(
            "SELECT ts, rr_ms FROM rr_intervals WHERE ts >= ? ORDER BY ts ASC LIMIT ?",
            (cutoff, int(max_rows)),
        ).fetchall()
        for r in rows:
            try:
                t = datetime.fromisoformat(r["ts"]).timestamp()
                for v in json.loads(r["rr_ms"]):
                    out.append((t, float(v)))
            except Exception:
                continue
    except Exception:
        return []
    return out


_ACTIGRAPHY_RETENTION_DAYS = 400
_last_actigraphy_prune_monotonic = 0.0


def append_actigraphy(conn: sqlite3.Connection, counts: dict, source: str = "verity") -> None:
    """Persist one batch of actigraphy counts from the wearable's own accelerometer.

    Fields mirror ``scripts/reduce_motion_activity.py`` (pim/zcm/mad/std/pmax/n) so live counts are
    unit-comparable with the training set. Kept for the same long window as the RR intervals -- this
    is personal training data. Best-effort; never breaks the ingest path."""
    global _last_actigraphy_prune_monotonic
    if not counts:
        return
    try:
        def _num(key):
            v = counts.get(key)
            try:
                v = float(v)
            except (TypeError, ValueError):
                return None
            return v if math.isfinite(v) else None

        pim, mad, std, pmax = _num("pim"), _num("mad"), _num("std"), _num("pmax")
        if pim is None and mad is None and std is None:
            return  # nothing usable
        zcm = counts.get("zcm")
        zcm = int(zcm) if isinstance(zcm, (int, float)) and math.isfinite(float(zcm)) else None
        n = counts.get("n")
        n = int(n) if isinstance(n, (int, float)) and math.isfinite(float(n)) else None
        conn.execute(
            """INSERT INTO actigraphy (ts, pim, zcm, mad, std, pmax, n, fs, source,
                                       resp_brpm, resp_conc)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (_now(), pim, zcm, mad, std, pmax, n, _num("fs"), source,
             _num("resp_brpm"), _num("resp_conc")),
        )
        now_mono = time.monotonic()
        if now_mono - _last_actigraphy_prune_monotonic >= _RR_PRUNE_INTERVAL_S:
            cutoff = (datetime.now(timezone.utc)
                      - timedelta(days=_ACTIGRAPHY_RETENTION_DAYS)).isoformat()
            conn.execute("DELETE FROM actigraphy WHERE ts < ?", (cutoff,))
            _last_actigraphy_prune_monotonic = now_mono
        conn.commit()
    except Exception:
        pass


def recent_actigraphy(conn: sqlite3.Connection, minutes: float = 45.0,
                      max_rows: int = 5000) -> list:
    """Recent actigraphy batches as ``[(epoch_seconds, pim), ...]`` for the stager's activity
    features. PIM is the primary movement-energy count; the other columns stay available in the
    table for later modelling."""
    out: list = []
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=float(minutes))).isoformat()
        rows = conn.execute(
            "SELECT ts, pim FROM actigraphy WHERE ts >= ? AND pim IS NOT NULL"
            " ORDER BY ts ASC LIMIT ?", (cutoff, int(max_rows)),
        ).fetchall()
        for r in rows:
            try:
                out.append((datetime.fromisoformat(r["ts"]).timestamp(), float(r["pim"])))
            except Exception:
                continue
    except Exception:
        return []
    return out


def recent_cardiac_history(conn: sqlite3.Connection, source: str, lookback_s: float,
                           max_rows: int = 500) -> list:
    """Recent (ts, hr, pim) history for THIS source, OLDEST -> NEWEST, as a list of
    ``{"ts": epoch_seconds, "hr": bpm|None, "pim": actigraphy PIM|None}`` dicts. Feeds
    ``services.assess_cardiac_quality``'s frozen-HR / not-worn checks, which need to see how long
    a value has persisted, not just the latest one.

    ``hr`` comes from ``sensor_samples``, ``pim`` from the separate ``actigraphy`` table -- the two
    are written by separate INSERTs within the same ``/hr/ingest`` call (see ``services.ingest_hr``)
    so they're joined here by NEAREST timestamp (2s tolerance) rather than assumed row-aligned,
    since a caller that omits ``acc`` on some batches would otherwise desync a naive zip.
    Best-effort: returns ``[]`` on any failure so a lookup hiccup degrades the quality check to
    'insufficient history -> no flags' rather than breaking the real-time ingest path."""
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=float(lookback_s))).isoformat()
        hr_rows = conn.execute(
            "SELECT ts, hr FROM sensor_samples WHERE source = ? AND ts >= ? ORDER BY ts ASC LIMIT ?",
            (source, cutoff, int(max_rows)),
        ).fetchall()
        acc_rows = conn.execute(
            "SELECT ts, pim FROM actigraphy WHERE source = ? AND ts >= ? ORDER BY ts ASC LIMIT ?",
            (source, cutoff, int(max_rows)),
        ).fetchall()
        acc_pts = []
        for r in acc_rows:
            try:
                acc_pts.append((datetime.fromisoformat(r["ts"]).timestamp(), r["pim"]))
            except Exception:
                continue
        out = []
        for r in hr_rows:
            try:
                t = datetime.fromisoformat(r["ts"]).timestamp()
            except Exception:
                continue
            pim = None
            best_dt = 2.0  # seconds; the paired acc row of the same ingest call is ~instantaneous
            for at, ap in acc_pts:
                dt = abs(at - t)
                if dt < best_dt:
                    best_dt = dt
                    pim = ap
            out.append({"ts": t, "hr": r["hr"], "pim": pim})
        return out
    except Exception:
        return []


def sensor_history_series(conn: sqlite3.Connection, minutes: float = 45.0,
                          max_rows: int = 4000) -> dict:
    """DENSE trailing HR + movement series for the wearable sleep-stager, as
    ``{"hr": [(epoch_seconds, bpm), ...], "activity": [(epoch_seconds, movement), ...]}``.

    The daemon's per-tick frame carries only ~1 sample/minute, but ``sensor_samples`` accumulates
    every ingest (a Polar Verity Sense writes ~1 HR sample every 2 s). Short-timescale HR
    variability is a major staging signal, so the stager scores far better on this dense series
    than on the 1/min frame buffer. Best-effort: any failure returns empty series so the caller
    silently falls back to the frame buffer.

    ``activity`` prefers the wearable's OWN actigraphy counts (Polar PMD ACC -> PIM), which are
    unit-comparable with the model's training data. It falls back to the iPhone's 0..1 movement
    index only when no wearable actigraphy is present -- that index is a different unit, so it is
    usable only via the model's scale-free (percentile / robust-z within the night) features.
    ``activity_units`` reports which one is in play so the caller can pick the right feature path.

    Samples flagged ``hr_frozen`` or ``not_worn`` (see ``services.assess_cardiac_quality``) are
    EXCLUDED from the returned ``hr`` series -- a frozen HR has near-zero variability, which is
    itself a strong SLEEP signal to the stager, so leaving it in would let movement (i.e. likely
    wakefulness) masquerade as deep sleep. ``activity`` is left untouched: actigraphy stays valid
    while the device is worn/moving even when the paired HR reading is bad. ``excluded`` reports
    how many HR samples were dropped this way, so the guard is observable rather than silent.
    """
    out = {"hr": [], "activity": [], "activity_units": None, "excluded": 0}
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=float(minutes))).isoformat()
        rows = conn.execute(
            "SELECT ts, hr, movement, hr_frozen, not_worn FROM sensor_samples"
            " WHERE ts >= ? ORDER BY ts ASC LIMIT ?",
            (cutoff, int(max_rows)),
        ).fetchall()
        excluded = 0
        for r in rows:
            ts = r["ts"]
            if not ts:
                continue
            try:
                t = datetime.fromisoformat(ts).timestamp()
            except Exception:
                continue
            if r["hr"] is not None:
                if r["hr_frozen"] or r["not_worn"]:
                    excluded += 1
                else:
                    out["hr"].append((t, float(r["hr"])))
            if r["movement"] is not None:
                out["activity"].append((t, float(r["movement"])))
        out["excluded"] = excluded
        # Prefer the wearable's own actigraphy counts when present: they are in the SAME units as
        # the model's training data, whereas the phone index is unitless and only usable through
        # scale-free features.
        acti = recent_actigraphy(conn, minutes=minutes, max_rows=max_rows)
        if acti:
            out["activity"] = acti
            out["activity_units"] = "counts"
        elif out["activity"]:
            out["activity_units"] = "phone_index"
    except Exception:
        return {"hr": [], "activity": [], "activity_units": None, "excluded": 0}
    return out


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
    """Latest phone/sensor sample with a computed ``age_seconds``, or None if never written.

    Tolerates the table being ABSENT, not just empty: an engine-only database (one opened without
    the dashboard DDL layered on -- which is what the CLI preflight and any bare tooling get)
    otherwise turns "the sensor isn't streaming" into an OperationalError that surfaces to the
    user as "check crashed". Same outcome either way -- no sample -- so say that plainly.
    """
    try:
        row = conn.execute("SELECT * FROM live_sensor WHERE id = 1").fetchone()
    except sqlite3.Error:
        return None
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
        """INSERT INTO live_cardiac (id, updated, hr, hrv, source, respiratory_rate)
        VALUES (1,?,?,?,?,?)
        ON CONFLICT(id) DO UPDATE SET
         updated=excluded.updated, hr=excluded.hr, hrv=excluded.hrv, source=excluded.source,
         respiratory_rate=excluded.respiratory_rate""",
        (_now(), sample.get("hr"), sample.get("hrv"), sample.get("source", "verity"),
         sample.get("respiratory_rate")),
    )
    conn.commit()


def read_cardiac_sample(conn: sqlite3.Connection) -> dict | None:
    """Latest dedicated-cardiac-sensor sample with a computed ``age_seconds``, or None.

    Tolerates the table being ABSENT, not just empty: an engine-only database (one opened without
    the dashboard DDL layered on -- which is what the CLI preflight and any bare tooling get)
    otherwise turns "the sensor isn't streaming" into an OperationalError that surfaces to the
    user as "check crashed". Same outcome either way -- no sample -- so say that plainly.
    """
    try:
        row = conn.execute("SELECT * FROM live_cardiac WHERE id = 1").fetchone()
    except sqlite3.Error:
        return None
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


def read_actigraphy_sample(conn: sqlite3.Connection) -> dict | None:
    """Latest actigraphy batch (the wearable's OWN accelerometer, via Polar PMD) with a computed
    ``age_seconds``, or None. Counterpart to ``read_cardiac_sample`` for the motion channel.
    Carries zcm/n/fs alongside pim so a live-status UI can show the real streaming cadence
    (samples per batch at the device's own accelerometer rate), not just the actigraphy scalar."""
    try:
        row = conn.execute(
            "SELECT ts, pim, zcm, n, fs, source FROM actigraphy WHERE pim IS NOT NULL"
            " ORDER BY ts DESC LIMIT 1").fetchone()
    except Exception:
        return None
    if row is None:
        return None
    age = None
    try:
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(row["ts"])).total_seconds()
    except Exception:
        age = None
    return {"pim": row["pim"], "zcm": row["zcm"], "n": row["n"], "fs": row["fs"],
            "source": row["source"], "age_seconds": age}


# ---- actigraphy counts -> the controller's movement index --------------------------------
# The two motion channels are in DIFFERENT UNITS. The iPhone reports a unitless 0..1 movement
# index; the Verity's accelerometer reduces to PIM counts (same definition as the training-set
# reduction, deliberately kept in native units so live data stays unit-comparable with it). Every
# movement threshold in the controller -- onset stillness 0.15, data-quality 0.2, wake-risk 0.3,
# arousal 0.4, onset-unreliable 0.45 -- is calibrated against the 0..1 index, so PIM counts cannot
# be passed through raw. Map them onto the index using the two PIM anchors the quality guards
# already define, so a single semantic scale governs both:
#
#     PIM <= STILLNESS_PIM_FLOOR (1.0)      "essentially motionless"  -> 0.06, safely under the
#                                                                        0.15 onset-stillness line
#     PIM >= MOVEMENT_PIM_THRESHOLD (5.0)   "clearly moving"          -> 0.30, exactly the
#                                                                        wake-risk movement line
#
# and continue at that slope above the upper anchor, saturating at 1.0 (~PIM 17). That puts the
# arousal threshold (0.4) at PIM ~6.7 -- meaningfully more motion than "clearly moving" -- which
# is the ordering these thresholds assume.
STILLNESS_PIM_FLOOR = 1.0
MOVEMENT_PIM_THRESHOLD = 5.0
_STILL_INDEX = 0.06
_MOVING_INDEX = 0.30


def actigraphy_movement_index(pim: float | None) -> float | None:
    """Convert a PIM actigraphy count to the controller's unitless 0..1 movement index."""
    try:
        p = float(pim)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(p) or p < 0:
        return None
    slope = (_MOVING_INDEX - _STILL_INDEX) / (MOVEMENT_PIM_THRESHOLD - STILLNESS_PIM_FLOOR)
    if p <= STILLNESS_PIM_FLOOR:
        # Below the "motionless" anchor, ramp linearly to 0 so a dead-still sleeper reads ~0
        # rather than a constant floor.
        return round(_STILL_INDEX * (p / STILLNESS_PIM_FLOOR), 4)
    return round(min(1.0, _STILL_INDEX + slope * (p - STILLNESS_PIM_FLOOR)), 4)


def read_fused_sensor(conn: sqlite3.Connection, cardiac_max_age_s: float = 30.0,
                      movement_max_age_s: float = 30.0,
                      phone_hr_max_age_s: float = 30.0) -> dict | None:
    """MERGE the independent fast-sensor channels into one per-field snapshot for the daemon:

      * **movement** — from the iPhone accelerometer (``live_sensor``) when it is fresh; its 0..1
        index is what every movement threshold in the controller was calibrated against, so it
        keeps priority. With no phone (or a stale one) we fall back to the WEARABLE's own
        accelerometer (``actigraphy``, Polar PMD ACC), converted onto that same index by
        ``actigraphy_movement_index``. Without this fallback a Verity-only night loses the motion
        channel entirely, and motion feeds onset confirmation, arousal scoring, awakening
        detection and wake risk — i.e. exactly the machinery that protects sleep MAINTENANCE.
      * **hr / hrv** — from the dedicated cardiac sensor (``live_cardiac``, e.g. Polar Verity
        Sense) when it is fresh; that optical/ECG HR + RR-interval HRV is AUTHORITATIVE and wins
        over the phone's best-effort ballistocardiogram HR. If the cardiac sensor is absent or
        stale, we fall back to the phone's best-effort HR/HRV so a lone iPhone still contributes.

    Each field is gated by ITS OWN freshness (a disconnected Verity doesn't strand a live phone,
    and vice-versa). Returns per-field values + ages, or None if nothing fresh is available.
    ``hr_source``/``movement_source`` record which channel actually supplied each, for the UI."""
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

    # movement: phone first (native 0..1 index), else the wearable's own accelerometer
    mv, mv_age = _fresh(phone, "movement", movement_max_age_s)
    mv_source = "phone" if mv is not None else None
    if mv is None:
        act = read_actigraphy_sample(conn)
        pim, pim_age = _fresh(act, "pim", movement_max_age_s)
        if pim is not None:
            idx = actigraphy_movement_index(pim)
            if idx is not None:
                mv, mv_age = idx, pim_age
                mv_source = (act.get("source") or "verity")
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
    # RSA-derived respiration rides the cardiac channel's freshness: it is computed from the
    # same RR window as HRV, so if the cardiac sample is stale the respiration is too.
    resp, _resp_age = _fresh(card, "respiratory_rate", cardiac_max_age_s)
    return {
        "hr": hr, "hrv": hrv, "movement": mv, "respiratory_rate": resp,
        "hr_age_seconds": hr_age, "hrv_age_seconds": hrv_age, "movement_age_seconds": mv_age,
        "hr_source": hr_source, "movement_source": mv_source,
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
