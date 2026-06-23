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

    def __init__(self, path: str = "sleepctl.db") -> None:
        self.path = path
        self.conn: sqlite3.Connection = schema.connect(path)

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
