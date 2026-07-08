"""Typed persistence over the SQLite schema.

Writes serialize enums (``.value``), datetimes (ISO), and dicts (JSON); reads
reconstruct the dataclasses, tolerating NULLs. The learning loop reads back rolling
windows via ``recent_nights`` / ``recent_interventions`` / ``latest_baselines``.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from typing import Optional

from sleepctl.models import (
    ActionRecord,
    Baselines,
    ContextRecord,
    ControllerState,
    CorrectionAction,
    Decision,
    Intervention,
    NightObjective,
    NightSummary,
    SensorFrame,
    SetpointProfile,
    SleepStage,
    ThermalIntent,
)
from sleepctl.storage import schema


# --------------------------------------------------------------- (de)serialization


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if dt is not None else None


def _dt(value) -> Optional[datetime]:
    if value is None or value == "":
        return None
    return datetime.fromisoformat(value)


def _b2i(value: Optional[bool]) -> Optional[int]:
    return None if value is None else int(value)


def _i2b(value) -> Optional[bool]:
    return None if value is None else bool(value)


def _jdump(value) -> str:
    return json.dumps(value or {})


def _jload(value) -> dict:
    if not value:
        return {}
    return json.loads(value)


class Repository:
    """Read/write access to the sleepctl dataset."""

    def __init__(self, path: str = "sleepctl.db", check_same_thread: bool = True,
                 ensure_schema: bool = True) -> None:
        self.path = path
        if ensure_schema:
            self.conn: sqlite3.Connection = schema.connect(
                path, check_same_thread=check_same_thread)
        else:
            # Skip re-running the schema DDL/migrations -- for callers that already guaranteed
            # the schema exists once elsewhere and just need a plain connection (see
            # dashboard/api/app/db.py's per-request ``get_repo()``).
            self.conn = schema.connect_light(path, check_same_thread=check_same_thread)

    # -- lifecycle ---------------------------------------------------------------
    def __enter__(self) -> "Repository":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def commit(self) -> None:
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # -- writes ------------------------------------------------------------------
    def log_sample(
        self,
        frame: SensorFrame,
        controller_state: str,
        wake_event: bool,
        night_date: str,
    ) -> None:
        self.conn.execute(
            """INSERT INTO raw_samples
            (ts, night_date, stage, stage_confidence, heart_rate, hrv,
             respiratory_rate, movement, presence, bed_temp_f, room_temp_f,
             commanded_level, controller_state, wake_event, data_age_seconds)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                _iso(frame.timestamp),
                night_date,
                frame.stage.value if frame.stage else None,
                frame.stage_confidence,
                frame.heart_rate,
                frame.hrv,
                frame.respiratory_rate,
                frame.movement,
                _b2i(frame.presence),
                frame.bed_temp_f,
                frame.room_temp_f,
                frame.commanded_level,
                controller_state,
                int(bool(wake_event)),
                frame.data_age_seconds,
            ),
        )
        self.conn.commit()

    def log_decision(self, decision: Decision, night_date: str) -> None:
        self.conn.execute(
            """INSERT INTO decisions
            (ts, night_date, state, objective, thermal_intent, target_temp_f,
             target_level, action, reason, confidence, log_payload)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                _iso(decision.timestamp),
                night_date,
                decision.state.value,
                decision.objective.value,
                decision.thermal_intent.value,
                decision.target_temp_f,
                decision.target_level,
                decision.action.value,
                decision.reason,
                decision.confidence,
                _jdump(decision.log_payload),
            ),
        )
        self.conn.commit()

    def log_intervention(self, iv: Intervention, night_date: str) -> None:
        self.conn.execute(
            """INSERT INTO interventions
            (ts, night_date, controller_state, action, magnitude_f, reason,
             held, reverted, outcome_delta)
            VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                _iso(iv.timestamp),
                night_date,
                iv.state.value,
                iv.action.value,
                iv.magnitude_f,
                iv.reason,
                _b2i(iv.held),
                _b2i(iv.reverted),
                iv.outcome_delta,
            ),
        )
        self.conn.commit()

    # ---- structured event log ("what happened and when" as one query) --------
    def log_event(self, category: str, severity: str, code: str, message: str,
                  data: Optional[dict] = None) -> None:
        """Append a structured event row. Defensive: swallows any error rather than raising,
        so a broken event log can never break the control loop that calls it."""
        try:
            self.conn.execute(
                "INSERT INTO events (ts, category, severity, code, message, data) "
                "VALUES (?,?,?,?,?,?)",
                (_iso(datetime.now()), category, severity, code, message, _jdump(data)),
            )
            self.conn.commit()
        except Exception:
            pass

    def recent_events(self, limit: int = 200, category: Optional[str] = None,
                      severity: Optional[str] = None, since_iso: Optional[str] = None) -> list[dict]:
        """Newest-first structured events, optionally filtered by category / severity / a minimum
        ISO timestamp. Defensive: returns [] on any error rather than raising."""
        try:
            q = "SELECT * FROM events WHERE 1=1"
            args: list = []
            if category:
                q += " AND category = ?"
                args.append(category)
            if severity:
                q += " AND severity = ?"
                args.append(severity)
            if since_iso:
                q += " AND ts >= ?"
                args.append(since_iso)
            q += " ORDER BY id DESC LIMIT ?"
            args.append(int(limit))
            rows = self.conn.execute(q, args).fetchall()
            out = []
            for r in rows:
                d = dict(r)
                d["data"] = _jload(d.get("data"))
                out.append(d)
            return out
        except Exception:
            return []

    def prune_events(self, keep_days: int = 14, max_rows: int = 20000) -> int:
        """Delete events older than ``keep_days`` and, if still over ``max_rows``, the oldest
        excess rows. Defensive: returns 0 on any error rather than raising. Returns rows deleted."""
        try:
            cutoff = _iso(datetime.now() - timedelta(days=keep_days))
            cur = self.conn.execute("DELETE FROM events WHERE ts < ?", (cutoff,))
            deleted = cur.rowcount or 0
            total = self.conn.execute("SELECT COUNT(*) c FROM events").fetchone()["c"]
            if total > max_rows:
                excess = total - max_rows
                ids = [r["id"] for r in self.conn.execute(
                    "SELECT id FROM events ORDER BY id ASC LIMIT ?", (excess,)).fetchall()]
                if ids:
                    qmarks = ",".join("?" * len(ids))
                    cur2 = self.conn.execute(f"DELETE FROM events WHERE id IN ({qmarks})", ids)
                    deleted += cur2.rowcount or 0
            self.conn.commit()
            return deleted
        except Exception:
            return 0

    def _prune_ts_table(self, table: str, keep_days: int, max_rows: Optional[int] = None) -> int:
        """Shared prune primitive for append-only, ``ts``-indexed high-write tables (mirrors
        ``prune_events`` above): delete rows older than ``keep_days`` and, if ``max_rows`` is
        given and still exceeded, the oldest excess rows. Defensive: returns 0 on any error
        rather than raising. Returns rows deleted. ``table`` is always one of this module's own
        fixed table names, never user input."""
        try:
            cutoff = _iso(datetime.now() - timedelta(days=keep_days))
            cur = self.conn.execute(f"DELETE FROM {table} WHERE ts < ?", (cutoff,))
            deleted = cur.rowcount or 0
            if max_rows is not None:
                total = self.conn.execute(f"SELECT COUNT(*) c FROM {table}").fetchone()["c"]
                if total > max_rows:
                    excess = total - max_rows
                    ids = [r["id"] for r in self.conn.execute(
                        f"SELECT id FROM {table} ORDER BY id ASC LIMIT ?", (excess,)).fetchall()]
                    if ids:
                        qmarks = ",".join("?" * len(ids))
                        cur2 = self.conn.execute(
                            f"DELETE FROM {table} WHERE id IN ({qmarks})", ids)
                        deleted += cur2.rowcount or 0
            self.conn.commit()
            return deleted
        except Exception:
            return 0

    # raw_samples/decisions/interventions/thermal_samples are high-write tables (roughly one row
    # per control tick) that, unlike events/state_history/sensor_samples, were never pruned --
    # left to grow unbounded for the life of the DB. Pruned once/night at the nightly close-out
    # seam (see LiveDashboardDaemon._maybe_close_out), NEVER on the per-tick hot path.
    def prune_raw_samples(self, keep_days: int = 45) -> int:
        return self._prune_ts_table("raw_samples", keep_days)

    def prune_decisions(self, keep_days: int = 45) -> int:
        return self._prune_ts_table("decisions", keep_days)

    def prune_interventions(self, keep_days: int = 45) -> int:
        return self._prune_ts_table("interventions", keep_days)

    def prune_thermal_samples(self, keep_days: int = 45) -> int:
        return self._prune_ts_table("thermal_samples", keep_days)

    def save_night_summary(self, ns: NightSummary) -> None:
        self.conn.execute(
            """INSERT INTO nightly_summaries
            (date, bedtime, wake_time, total_sleep_min, sleep_onset_latency_min,
             deep_min, rem_min, light_min, wake_events, waso_min, sleep_efficiency,
             avg_hr, avg_hrv, avg_respiratory_rate, temp_profile_summary,
             intervention_summary, setpoint_version, outcome_score)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(date) DO UPDATE SET
             bedtime=excluded.bedtime, wake_time=excluded.wake_time,
             total_sleep_min=excluded.total_sleep_min,
             sleep_onset_latency_min=excluded.sleep_onset_latency_min,
             deep_min=excluded.deep_min, rem_min=excluded.rem_min,
             light_min=excluded.light_min, wake_events=excluded.wake_events,
             waso_min=excluded.waso_min, sleep_efficiency=excluded.sleep_efficiency,
             avg_hr=excluded.avg_hr, avg_hrv=excluded.avg_hrv,
             avg_respiratory_rate=excluded.avg_respiratory_rate,
             temp_profile_summary=excluded.temp_profile_summary,
             intervention_summary=excluded.intervention_summary,
             setpoint_version=excluded.setpoint_version,
             outcome_score=excluded.outcome_score""",
            (
                ns.date,
                _iso(ns.bedtime),
                _iso(ns.wake_time),
                ns.total_sleep_min,
                ns.sleep_onset_latency_min,
                ns.deep_min,
                ns.rem_min,
                ns.light_min,
                ns.wake_events,
                ns.waso_min,
                ns.sleep_efficiency,
                ns.avg_hr,
                ns.avg_hrv,
                ns.avg_respiratory_rate,
                _jdump(ns.temp_profile_summary),
                _jdump(ns.intervention_summary),
                ns.setpoint_version,
                ns.outcome_score,
            ),
        )
        self.conn.commit()

    def save_context(self, ctx: ContextRecord) -> None:
        self.conn.execute(
            """INSERT INTO context
            (date, required_wake_time, work_start_time, first_commitment, outdoor_temp_f,
             sleep_opportunity_min, is_short_sleep_day, schedule_variable, steps,
             workout_timing, workout_intensity, resting_hr_trend, hr_recovery,
             strain, caffeine, alcohol, screen_time_min, stress, travel, illness,
             late_night_work, routine_complete, subjective_quality, grogginess,
             daytime_performance)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(date) DO UPDATE SET
             required_wake_time=excluded.required_wake_time,
             work_start_time=excluded.work_start_time,
             first_commitment=excluded.first_commitment,
             outdoor_temp_f=excluded.outdoor_temp_f,
             sleep_opportunity_min=excluded.sleep_opportunity_min,
             is_short_sleep_day=excluded.is_short_sleep_day,
             schedule_variable=excluded.schedule_variable, steps=excluded.steps,
             workout_timing=excluded.workout_timing,
             workout_intensity=excluded.workout_intensity,
             resting_hr_trend=excluded.resting_hr_trend,
             hr_recovery=excluded.hr_recovery, strain=excluded.strain,
             caffeine=excluded.caffeine, alcohol=excluded.alcohol,
             screen_time_min=excluded.screen_time_min, stress=excluded.stress,
             travel=excluded.travel, illness=excluded.illness,
             late_night_work=excluded.late_night_work,
             routine_complete=excluded.routine_complete,
             subjective_quality=excluded.subjective_quality,
             grogginess=excluded.grogginess,
             daytime_performance=excluded.daytime_performance""",
            (
                ctx.date,
                _iso(ctx.required_wake_time),
                _iso(ctx.work_start_time),
                _iso(ctx.first_commitment),
                ctx.outdoor_temp_f,
                ctx.sleep_opportunity_min,
                _b2i(ctx.is_short_sleep_day),
                _b2i(ctx.schedule_variable),
                ctx.steps,
                _iso(ctx.workout_timing),
                ctx.workout_intensity,
                ctx.resting_hr_trend,
                ctx.hr_recovery,
                ctx.strain,
                _b2i(ctx.caffeine),
                _b2i(ctx.alcohol),
                ctx.screen_time_min,
                ctx.stress,
                _b2i(ctx.travel),
                _b2i(ctx.illness),
                _b2i(ctx.late_night_work),
                _b2i(ctx.routine_complete),
                ctx.subjective_quality,
                ctx.grogginess,
                ctx.daytime_performance,
            ),
        )
        self.conn.commit()

    def save_baselines(self, baselines: Baselines) -> None:
        self.conn.execute(
            "INSERT INTO baselines (ts, metrics) VALUES (?,?)",
            (_iso(baselines.updated or datetime.now()), _jdump(baselines.metrics)),
        )
        self.conn.commit()

    def save_setpoints(self, profile: "SetpointProfile") -> None:
        """Persist a versioned snapshot of the learnable setpoint profile."""
        payload = {
            "neutral_f": profile.neutral_f,
            "deep_bias_f": profile.deep_bias_f,
            "rem_warm_offset_f": profile.rem_warm_offset_f,
            "wake_ramp_f": profile.wake_ramp_f,
            "composite_bed_weight": profile.composite_bed_weight,
        }
        self.conn.execute(
            """INSERT INTO setpoints (version, ts, source, profile) VALUES (?,?,?,?)
            ON CONFLICT(version) DO UPDATE SET
             ts=excluded.ts, source=excluded.source, profile=excluded.profile""",
            (profile.version, _iso(profile.updated or datetime.now()), profile.source,
             _jdump(payload)),
        )
        self.conn.commit()

    # ---- measured thermal-response calibration (from the in-bed self-test) ----
    def save_thermal_calibration(self, cal: dict) -> None:
        """Persist the measured cool/heat rates (singleton). ``cal`` carries
        cool_levels_per_min / heat_levels_per_min (+ optional °F/min) and a source tag."""
        self.conn.execute(
            """INSERT INTO thermal_calibration
            (id, ts, cool_levels_per_min, heat_levels_per_min, cool_f_per_min,
             heat_f_per_min, cool_lag_min, heat_lag_min, warmback_levels_per_min,
             warmback_lag_min, source) VALUES (1,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET
             ts=excluded.ts, cool_levels_per_min=excluded.cool_levels_per_min,
             heat_levels_per_min=excluded.heat_levels_per_min,
             cool_f_per_min=excluded.cool_f_per_min, heat_f_per_min=excluded.heat_f_per_min,
             cool_lag_min=excluded.cool_lag_min, heat_lag_min=excluded.heat_lag_min,
             warmback_levels_per_min=excluded.warmback_levels_per_min,
             warmback_lag_min=excluded.warmback_lag_min, source=excluded.source""",
            (_iso(datetime.now()), cal.get("cool_levels_per_min"),
             cal.get("heat_levels_per_min"), cal.get("cool_f_per_min"),
             cal.get("heat_f_per_min"), cal.get("cool_lag_min"), cal.get("heat_lag_min"),
             cal.get("warmback_levels_per_min"), cal.get("warmback_lag_min"),
             cal.get("source", "self_test")),
        )
        self.conn.commit()

    def get_thermal_calibration(self) -> Optional[dict]:
        """The latest measured thermal-response calibration, or None if never measured."""
        try:
            row = self.conn.execute(
                "SELECT * FROM thermal_calibration WHERE id = 1").fetchone()
        except sqlite3.Error:
            return None
        return dict(row) if row is not None else None

    # ---- personal comfort mapping (from the in-bed comfort sweep) -------------
    def save_comfort_profile(self, prof: dict) -> None:
        self.conn.execute(
            """INSERT INTO comfort_profile (id, ts, neutral_f, cool_edge_f, warm_edge_f,
             ratings, source) VALUES (1,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET
             ts=excluded.ts, neutral_f=excluded.neutral_f, cool_edge_f=excluded.cool_edge_f,
             warm_edge_f=excluded.warm_edge_f, ratings=excluded.ratings, source=excluded.source""",
            (_iso(datetime.now()), prof.get("neutral_f"), prof.get("cool_edge_f"),
             prof.get("warm_edge_f"), _jdump(prof.get("ratings")), prof.get("source", "comfort_cal")),
        )
        self.conn.commit()

    def get_comfort_profile(self) -> Optional[dict]:
        try:
            row = self.conn.execute("SELECT * FROM comfort_profile WHERE id = 1").fetchone()
        except sqlite3.Error:
            return None
        if row is None:
            return None
        d = dict(row)
        d["ratings"] = _jload(d["ratings"]) if d.get("ratings") else None
        return d

    # ---- resting-physiology baseline (quiet-and-awake in bed) -----------------
    def save_resting_baseline(self, base: dict) -> None:
        self.conn.execute(
            """INSERT INTO resting_baseline (id, ts, hr, hrv, rr, movement, n_samples, source)
             VALUES (1,?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET
             ts=excluded.ts, hr=excluded.hr, hrv=excluded.hrv, rr=excluded.rr,
             movement=excluded.movement, n_samples=excluded.n_samples, source=excluded.source""",
            (_iso(datetime.now()), base.get("hr"), base.get("hrv"), base.get("rr"),
             base.get("movement"), base.get("n_samples"), base.get("source", "self_test")),
        )
        self.conn.commit()

    def get_resting_baseline(self) -> Optional[dict]:
        try:
            row = self.conn.execute("SELECT * FROM resting_baseline WHERE id = 1").fetchone()
        except sqlite3.Error:
            return None
        return dict(row) if row is not None else None

    def log_action(self, action: ActionRecord) -> int:
        """Append a learning action to the ledger; returns its row id."""
        cur = self.conn.execute(
            """INSERT INTO actions
            (night_date, action_name, params, predicted, confidence, reward_observed,
             applied, source, creates_version)
            VALUES (?,?,?,?,?,?,?,?,?)""",
            (action.date, action.action_name, _jdump(action.params),
             _jdump(action.predicted), action.confidence, action.reward_observed,
             int(bool(action.applied)), action.source, action.creates_version),
        )
        self.conn.commit()
        return cur.lastrowid

    # ---- anticipatory pre-cool efficacy ledger -------------------------------
    def log_precool_event(self, night_date, ts, window_type: str,
                          lead_used_min: float, eta_min: float) -> int:
        cur = self.conn.execute(
            "INSERT INTO precool_events (night_date, ts, window_type, lead_used_min, "
            "eta_min, prevented, resolved) VALUES (?,?,?,?,?,NULL,0)",
            (night_date, _iso(ts) if not isinstance(ts, str) else ts,
             window_type, float(lead_used_min), float(eta_min)),
        )
        self.conn.commit()
        return cur.lastrowid

    def resolve_precool_events(self, tail_buffer_min: float = 8.0) -> int:
        """Label unresolved pre-cool events whose window has passed: prevented unless a
        wake-event was logged inside [ts, ts + eta + buffer]. Returns rows resolved."""
        rows = self.conn.execute(
            "SELECT id, ts, eta_min FROM precool_events WHERE resolved = 0"
        ).fetchall()
        resolved = 0
        for r in rows:
            t0 = _dt(r["ts"])
            if t0 is None:
                continue
            end = t0 + timedelta(minutes=float(r["eta_min"]) + tail_buffer_min)
            if datetime.now() < end:
                continue  # window hasn't fully passed yet
            hit = self.conn.execute(
                "SELECT COUNT(*) c FROM raw_samples WHERE wake_event = 1 AND ts >= ? AND ts <= ?",
                (_iso(t0), _iso(end)),
            ).fetchone()["c"]
            self.conn.execute(
                "UPDATE precool_events SET prevented = ?, resolved = 1 WHERE id = ?",
                (0 if hit else 1, r["id"]),
            )
            resolved += 1
        if resolved:
            self.conn.commit()
        return resolved

    # ---- in-night architecture-steering ledger -------------------------------
    def log_steer_event(self, night_date, ts, maneuver: str, stage_before,
                        deep_deficit_min: float, frac_of_night: float,
                        horizon_min: float, applied: int = 1) -> int:
        cur = self.conn.execute(
            "INSERT INTO steer_events (night_date, ts, maneuver, stage_before, "
            "deep_deficit_min, frac_of_night, horizon_min, applied, deepened, caused_wake, "
            "resolved) VALUES (?,?,?,?,?,?,?,?,NULL,NULL,0)",
            (night_date, _iso(ts) if not isinstance(ts, str) else ts, maneuver,
             stage_before, float(deep_deficit_min), float(frac_of_night), float(horizon_min),
             1 if applied else 0),
        )
        self.conn.commit()
        return cur.lastrowid

    def resolve_steer_events(self) -> int:
        """Label unresolved steer events whose response horizon has passed: ``deepened`` if any DEEP
        sample occurred in (ts, ts+horizon]; ``succeeded`` if the maneuver's TARGET stage did (deep
        for 'deepen', REM for 'rem_warm'); ``caused_wake`` if any wake-event did. Returns rows
        resolved. The supervised signal for the deepening / lightening response learners."""
        rows = self.conn.execute(
            "SELECT id, ts, horizon_min, maneuver FROM steer_events WHERE resolved = 0"
        ).fetchall()
        resolved = 0
        for r in rows:
            t0 = _dt(r["ts"])
            if t0 is None:
                continue
            end = t0 + timedelta(minutes=float(r["horizon_min"]))
            if datetime.now() < end:
                continue  # horizon hasn't fully passed yet
            target_stage = "rem" if r["maneuver"] == "rem_warm" else "deep"
            deepened = self.conn.execute(
                "SELECT COUNT(*) c FROM raw_samples WHERE stage = 'deep' AND ts > ? AND ts <= ?",
                (_iso(t0), _iso(end)),
            ).fetchone()["c"]
            succeeded = deepened if target_stage == "deep" else self.conn.execute(
                "SELECT COUNT(*) c FROM raw_samples WHERE stage = ? AND ts > ? AND ts <= ?",
                (target_stage, _iso(t0), _iso(end)),
            ).fetchone()["c"]
            woke = self.conn.execute(
                "SELECT COUNT(*) c FROM raw_samples WHERE wake_event = 1 AND ts > ? AND ts <= ?",
                (_iso(t0), _iso(end)),
            ).fetchone()["c"]
            self.conn.execute(
                "UPDATE steer_events SET deepened = ?, succeeded = ?, caused_wake = ?, "
                "resolved = 1 WHERE id = ?",
                (1 if deepened else 0, 1 if succeeded else 0, 1 if woke else 0, r["id"]),
            )
            resolved += 1
        if resolved:
            self.conn.commit()
        return resolved

    def steer_efficacy(self) -> dict:
        """Per-maneuver outcome from resolved steer events, split into the ACTUATED arm and the
        SHADOW/CONTROL arm: {maneuver: {act: {...}, control: {...}}}. Comparing the two answers the
        causal question — does cool-to-deepen actually move YOUR architecture vs leaving it alone,
        and without waking you."""
        rows = self.conn.execute(
            "SELECT maneuver, applied, COUNT(*) n, SUM(deepened) deepened, SUM(caused_wake) woke "
            "FROM steer_events WHERE resolved = 1 GROUP BY maneuver, applied"
        ).fetchall()
        out: dict = {}
        for r in rows:
            n = r["n"] or 0
            deepened = r["deepened"] or 0
            woke = r["woke"] or 0
            arm = "act" if (r["applied"] in (1, None)) else "control"
            out.setdefault(r["maneuver"], {})[arm] = {
                "n": n, "deepened": deepened, "woke": woke,
                "deepen_rate": round(deepened / n, 3) if n else None,
                "wake_rate": round(woke / n, 3) if n else None,
            }
        return out

    def maneuver_records(self, maneuver: str = "deepen", nights: int = 60) -> list:
        """Resolved steer events for ``maneuver`` as learner rows: {applied, deepened, succeeded,
        caused_wake, night_type}. ``succeeded`` is reaching the maneuver's target stage (deep for
        deepen, REM for rem_warm). Joins each night's mode from context for per-mode learning."""
        rows = self.conn.execute(
            "SELECT night_date, applied, deepened, succeeded, caused_wake FROM steer_events "
            "WHERE resolved = 1 AND maneuver = ? ORDER BY id DESC LIMIT ?",
            (maneuver, int(nights) * 40),     # several events per night
        ).fetchall()
        out = []
        for r in rows:
            ctx = self.get_context(r["night_date"]) if hasattr(self, "get_context") else None
            succeeded = r["succeeded"]
            if succeeded is None:                       # back-compat: deepen rows pre-`succeeded`
                succeeded = r["deepened"]
            out.append({
                "applied": 1 if r["applied"] in (1, None) else 0,
                "deepened": int(r["deepened"] or 0),
                "succeeded": int(succeeded or 0),
                "caused_wake": int(r["caused_wake"] or 0),
                "night_type": (getattr(ctx, "night_type", None) or "normal") if ctx else "normal",
            })
        return out

    def deepening_records(self, nights: int = 60) -> list:
        """Resolved 'deepen' steer events as learner rows (see ``maneuver_records``)."""
        return self.maneuver_records("deepen", nights)

    def precool_efficacy(self) -> dict:
        """Per-window prevention rate from resolved events: {window: {n, prevented, rate,
        mean_lead}}."""
        rows = self.conn.execute(
            "SELECT window_type, COUNT(*) n, SUM(prevented) prevented, AVG(lead_used_min) lead "
            "FROM precool_events WHERE resolved = 1 GROUP BY window_type"
        ).fetchall()
        out = {}
        for r in rows:
            n = r["n"] or 0
            prevented = r["prevented"] or 0
            out[r["window_type"]] = {
                "n": n, "prevented": prevented,
                "rate": round(prevented / n, 3) if n else None,
                "mean_lead": round(r["lead"], 1) if r["lead"] is not None else None,
            }
        return out

    # ---- standing "does the controller help?" efficacy trial -----------------
    def assign_efficacy_night(self, night_date: str, arm: str) -> None:
        """Persist tonight's efficacy-trial arm assignment ('controlled'|'held'). Idempotent:
        a night's arm must not change once assigned, so this never overwrites an existing row."""
        self.conn.execute(
            "INSERT INTO efficacy_nights (night_date, arm, resolved) VALUES (?,?,0) "
            "ON CONFLICT(night_date) DO NOTHING",
            (night_date, arm),
        )
        self.conn.commit()

    def record_efficacy_outcome(self, night_date: str, wake_events=None, deep_pct=None,
                                efficiency=None, outcome_score=None) -> None:
        """Record tonight's measured outcome against its already-assigned arm (no-op if the
        night was never assigned one)."""
        self.conn.execute(
            "UPDATE efficacy_nights SET wake_events=?, deep_pct=?, efficiency=?, "
            "outcome_score=?, resolved=1 WHERE night_date=?",
            (wake_events, deep_pct, efficiency, outcome_score, night_date),
        )
        self.conn.commit()

    def efficacy_rows(self, resolved_only: bool = False) -> list:
        q = "SELECT * FROM efficacy_nights"
        if resolved_only:
            q += " WHERE resolved=1"
        q += " ORDER BY night_date ASC"
        return [dict(r) for r in self.conn.execute(q).fetchall()]

    def efficacy_night(self, night_date: str) -> Optional[dict]:
        row = self.conn.execute(
            "SELECT * FROM efficacy_nights WHERE night_date=?", (night_date,)
        ).fetchone()
        return dict(row) if row else None

    # ---- randomized efficacy MICRO-trials (sleepctl.ml.efficacy_trial) -------
    def assign_efficacy_trial_night(self, night_date: str, arm: str, eligible: bool,
                                    seed: Optional[float] = None) -> None:
        """Persist tonight's micro-trial arm assignment ('active'|'sham') + whether the night
        was eligible for randomization + the deterministic draw used. Idempotent: a night's
        assignment must not change once made, so this never overwrites an existing row."""
        self.conn.execute(
            "INSERT INTO efficacy_trials (night_date, arm, eligible, seed, resolved) "
            "VALUES (?,?,?,?,0) ON CONFLICT(night_date) DO NOTHING",
            (night_date, arm, int(bool(eligible)), seed),
        )
        self.conn.commit()

    def record_efficacy_trial_outcome(self, night_date: str, wake_events=None, deep_pct=None,
                                      hrv=None, efficiency=None, outcome_score=None) -> None:
        """Record tonight's measured outcome against its already-assigned micro-trial arm
        (no-op if the night was never assigned one)."""
        self.conn.execute(
            "UPDATE efficacy_trials SET wake_events=?, deep_pct=?, hrv=?, efficiency=?, "
            "outcome_score=?, resolved=1 WHERE night_date=?",
            (wake_events, deep_pct, hrv, efficiency, outcome_score, night_date),
        )
        self.conn.commit()

    def efficacy_trial_rows(self, resolved_only: bool = False) -> list:
        q = "SELECT * FROM efficacy_trials"
        if resolved_only:
            q += " WHERE resolved=1"
        q += " ORDER BY night_date ASC"
        return [dict(r) for r in self.conn.execute(q).fetchall()]

    def efficacy_trial_night(self, night_date: str) -> Optional[dict]:
        row = self.conn.execute(
            "SELECT * FROM efficacy_trials WHERE night_date=?", (night_date,)
        ).fetchone()
        return dict(row) if row else None

    def backfill_action_rewards(self) -> None:
        """Set each action's reward = mean outcome_score of the nights its version produced.

        Averaging over all nights that ran on a version naturally captures an action's
        multi-night (delayed) effect.
        """
        self.conn.execute(
            """UPDATE actions SET reward_observed = (
                SELECT AVG(n.outcome_score) FROM nightly_summaries n
                WHERE n.setpoint_version = actions.creates_version
                  AND n.outcome_score IS NOT NULL
            ) WHERE creates_version IS NOT NULL"""
        )
        self.conn.commit()

    def recent_actions(self, n: int) -> list[ActionRecord]:
        rows = self.conn.execute(
            "SELECT * FROM actions ORDER BY id DESC LIMIT ?", (n,)
        ).fetchall()
        out = [self._row_to_action(r) for r in rows]
        out.reverse()
        return out

    @staticmethod
    def _row_to_action(r: sqlite3.Row) -> ActionRecord:
        return ActionRecord(
            date=r["night_date"],
            action_name=r["action_name"],
            params=_jload(r["params"]),
            predicted=_jload(r["predicted"]),
            confidence=r["confidence"] or 0.0,
            reward_observed=r["reward_observed"],
            applied=bool(r["applied"]),
            source=r["source"],
            creates_version=r["creates_version"],
        )

    def latest_setpoints(self) -> "Optional[SetpointProfile]":
        row = self.conn.execute(
            "SELECT * FROM setpoints ORDER BY version DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        p = _jload(row["profile"])
        return SetpointProfile(
            neutral_f=p["neutral_f"],
            deep_bias_f=p["deep_bias_f"],
            rem_warm_offset_f=p["rem_warm_offset_f"],
            wake_ramp_f=p["wake_ramp_f"],
            composite_bed_weight=p["composite_bed_weight"],
            version=row["version"],
            source=row["source"],
            updated=_dt(row["ts"]),
        )

    # -- reads -------------------------------------------------------------------
    def recent_nights(self, n: int) -> list[NightSummary]:
        rows = self.conn.execute(
            "SELECT * FROM nightly_summaries ORDER BY date DESC LIMIT ?", (n,)
        ).fetchall()
        nights = [self._row_to_night(r) for r in rows]
        nights.reverse()  # oldest-first
        return nights

    def all_nights(self) -> list[NightSummary]:
        """Every night summary, oldest-first (for ML dataset building)."""
        rows = self.conn.execute(
            "SELECT * FROM nightly_summaries ORDER BY date ASC"
        ).fetchall()
        return [self._row_to_night(r) for r in rows]

    def setpoints_by_version(self) -> dict[int, SetpointProfile]:
        """All stored setpoint versions keyed by version (for joining to nights)."""
        rows = self.conn.execute("SELECT * FROM setpoints").fetchall()
        out: dict[int, SetpointProfile] = {}
        for r in rows:
            p = _jload(r["profile"])
            out[r["version"]] = SetpointProfile(
                neutral_f=p["neutral_f"],
                deep_bias_f=p["deep_bias_f"],
                rem_warm_offset_f=p["rem_warm_offset_f"],
                wake_ramp_f=p["wake_ramp_f"],
                composite_bed_weight=p["composite_bed_weight"],
                version=r["version"],
                source=r["source"],
                updated=_dt(r["ts"]),
            )
        return out

    def recent_interventions(self, n: int) -> list[Intervention]:
        rows = self.conn.execute(
            "SELECT * FROM interventions ORDER BY id DESC LIMIT ?", (n,)
        ).fetchall()
        ivs = [self._row_to_intervention(r) for r in rows]
        ivs.reverse()
        return ivs

    def recent_decisions(self, n: int) -> list[dict]:
        """Raw recent decision-log rows (newest last), for the interpretability surface. Each
        row carries the per-tick controller call: state/objective/thermal_intent/target_temp_f/
        target_level/action/reason/confidence — everything needed to explain "why" without
        re-deriving it from the controller."""
        rows = self.conn.execute(
            "SELECT * FROM decisions ORDER BY id DESC LIMIT ?", (n,)
        ).fetchall()
        out = [dict(r) for r in rows]
        out.reverse()
        return out

    def latest_baselines(self) -> Optional[Baselines]:
        row = self.conn.execute(
            "SELECT * FROM baselines ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        return Baselines(metrics=_jload(row["metrics"]), updated=_dt(row["ts"]))

    def get_context(self, date: str) -> Optional[ContextRecord]:
        row = self.conn.execute(
            "SELECT * FROM context WHERE date = ?", (date,)
        ).fetchone()
        if row is None:
            return None
        return self._row_to_context(row)

    def samples_for_night(self, night_date: str) -> list[SensorFrame]:
        rows = self.conn.execute(
            "SELECT * FROM raw_samples WHERE night_date = ? ORDER BY id ASC",
            (night_date,),
        ).fetchall()
        return [self._row_to_frame(r) for r in rows]

    # -- row mappers -------------------------------------------------------------
    @staticmethod
    def _row_to_frame(r: sqlite3.Row) -> SensorFrame:
        return SensorFrame(
            timestamp=_dt(r["ts"]),
            stage=SleepStage(r["stage"]) if r["stage"] else SleepStage.UNKNOWN,
            stage_confidence=r["stage_confidence"],
            heart_rate=r["heart_rate"],
            hrv=r["hrv"],
            respiratory_rate=r["respiratory_rate"],
            movement=r["movement"],
            presence=_i2b(r["presence"]),
            bed_temp_f=r["bed_temp_f"],
            room_temp_f=r["room_temp_f"],
            commanded_level=r["commanded_level"],
            data_age_seconds=r["data_age_seconds"],
        )

    @staticmethod
    def _row_to_night(r: sqlite3.Row) -> NightSummary:
        return NightSummary(
            date=r["date"],
            bedtime=_dt(r["bedtime"]),
            wake_time=_dt(r["wake_time"]),
            total_sleep_min=r["total_sleep_min"],
            sleep_onset_latency_min=r["sleep_onset_latency_min"],
            deep_min=r["deep_min"],
            rem_min=r["rem_min"],
            light_min=r["light_min"],
            wake_events=r["wake_events"],
            waso_min=r["waso_min"],
            sleep_efficiency=r["sleep_efficiency"],
            avg_hr=r["avg_hr"],
            avg_hrv=r["avg_hrv"],
            avg_respiratory_rate=r["avg_respiratory_rate"],
            temp_profile_summary=_jload(r["temp_profile_summary"]),
            intervention_summary=_jload(r["intervention_summary"]),
            setpoint_version=r["setpoint_version"],
            outcome_score=r["outcome_score"],
        )

    @staticmethod
    def _row_to_intervention(r: sqlite3.Row) -> Intervention:
        return Intervention(
            timestamp=_dt(r["ts"]),
            state=ControllerState(r["controller_state"])
            if r["controller_state"]
            else ControllerState.IDLE,
            action=CorrectionAction(r["action"]),
            magnitude_f=r["magnitude_f"],
            reason=r["reason"],
            held=_i2b(r["held"]),
            reverted=_i2b(r["reverted"]),
            outcome_delta=r["outcome_delta"],
        )

    @staticmethod
    def _row_to_context(r: sqlite3.Row) -> ContextRecord:
        return ContextRecord(
            date=r["date"],
            required_wake_time=_dt(r["required_wake_time"]),
            work_start_time=_dt(r["work_start_time"]),
            first_commitment=_dt(r["first_commitment"]),
            outdoor_temp_f=r["outdoor_temp_f"],
            sleep_opportunity_min=r["sleep_opportunity_min"],
            is_short_sleep_day=_i2b(r["is_short_sleep_day"]),
            schedule_variable=_i2b(r["schedule_variable"]),
            steps=r["steps"],
            workout_timing=_dt(r["workout_timing"]),
            workout_intensity=r["workout_intensity"],
            resting_hr_trend=r["resting_hr_trend"],
            hr_recovery=r["hr_recovery"],
            strain=r["strain"],
            caffeine=_i2b(r["caffeine"]),
            alcohol=_i2b(r["alcohol"]),
            screen_time_min=r["screen_time_min"],
            stress=r["stress"],
            travel=_i2b(r["travel"]),
            illness=_i2b(r["illness"]),
            late_night_work=_i2b(r["late_night_work"]),
            routine_complete=_i2b(r["routine_complete"]),
            subjective_quality=r["subjective_quality"],
            grogginess=r["grogginess"],
            daytime_performance=r["daytime_performance"],
        )

    # ---- runtime-state history: append-only trend of runtime_state snapshots (48h+ window) --
    def record_state_snapshot(self, snapshot: dict) -> None:
        """Append one runtime_state-shaped snapshot to ``state_history`` (see ``bridge.
        write_runtime_state`` for the same field set). Defensive: swallows any error rather
        than raising, so a broken history write can never break the control loop that calls
        it. Also prunes rows older than ~7 days on every write so the table stays bounded
        regardless of how often callers throttle their writes."""
        try:
            self.conn.execute(
                """INSERT INTO state_history
                (ts, state, mode, target_temp_f, bed_temp_f, room_temp_f, stage, confidence,
                 target_level, daemon_alive, extra)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    snapshot.get("ts") or _iso(datetime.now()),
                    snapshot.get("state"),
                    snapshot.get("mode"),
                    snapshot.get("target_temp_f"),
                    snapshot.get("bed_temp_f"),
                    snapshot.get("room_temp_f"),
                    snapshot.get("stage"),
                    snapshot.get("confidence"),
                    snapshot.get("target_level"),
                    _b2i(bool(snapshot.get("daemon_alive", True))),
                    _jdump(snapshot.get("extra")),
                ),
            )
            self.conn.execute(
                "DELETE FROM state_history WHERE ts < ?",
                (_iso(datetime.now() - timedelta(days=7)),),
            )
            self.conn.commit()
        except Exception:
            pass

    def state_history(self, hours: int = 48, limit: int = 2000) -> list[dict]:
        """Newest-first ``state_history`` rows from the last ``hours``, JSON-decoding ``extra``.
        Defensive: returns [] on any error rather than raising."""
        try:
            cutoff = _iso(datetime.now() - timedelta(hours=hours))
            rows = self.conn.execute(
                "SELECT * FROM state_history WHERE ts >= ? ORDER BY id DESC LIMIT ?",
                (cutoff, int(limit)),
            ).fetchall()
            out = []
            for r in rows:
                d = dict(r)
                d["extra"] = _jload(d.get("extra"))
                out.append(d)
            return out
        except Exception:
            return []
