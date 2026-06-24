"""sleepctl dashboard API — FastAPI app wiring all routes + SSE.

The API reuses the sleepctl engine for reads and the control bridge for writes (it never
calls the device directly). Single module for v1 clarity.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app import bridge, services
from app.config import settings
from app.db import get_repo
from app.security import (
    AuthDep,
    authenticate,
    create_token,
    current_user,
    ensure_bootstrap_user,
)

app = FastAPI(title="sleepctl dashboard", version="1.0.0")
app.add_middleware(
    CORSMiddleware, allow_origins=settings.cors_origins, allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"],
)


@app.on_event("startup")
def _startup() -> None:
    ensure_bootstrap_user()


def repo_dep():
    repo = get_repo()
    try:
        yield repo
    finally:
        repo.close()


# ------------------------------------------------------------------ models
class LoginBody(BaseModel):
    username: str
    password: str


class TempBody(BaseModel):
    target_f: float


class NudgeBody(BaseModel):
    delta_f: float  # +/- adjustment for quick realtime control


class ModeBody(BaseModel):
    mode: str  # auto | manual | view


class WakeBody(BaseModel):
    wake_time: str  # HH:MM
    window_min: int | None = None
    vibration_power: int | None = None   # 0=off, 20 low, 50 med, 100 high
    thermal_level: int | None = None     # thermal nudge intensity for wake ramp
    night_type: str | None = None        # work | recovery | normal | auto


class NoteBody(BaseModel):
    date: str
    text: str


class SettingsBody(BaseModel):
    values: dict


# ------------------------------------------------------------------ health/auth
@app.get("/health")
def health():
    return {"ok": True, "ts": datetime.now(timezone.utc).isoformat()}


@app.post("/auth/login")
def login(body: LoginBody, response: Response):
    if not authenticate(body.username, body.password):
        raise HTTPException(401, "invalid credentials")
    token = create_token(body.username)
    response.set_cookie("session", token, httponly=True, samesite="lax",
                        max_age=settings.jwt_ttl_hours * 3600)
    return {"token": token, "user": body.username}


@app.post("/auth/logout")
def logout(response: Response):
    response.delete_cookie("session")
    return {"ok": True}


@app.get("/auth/me")
def me(user: str = AuthDep):
    return {"user": user}


# ------------------------------------------------------------------ status + SSE
@app.get("/status")
def status(repo=Depends(repo_dep), user: str = AuthDep):
    return services.build_status(repo)


@app.get("/stream/status")
async def stream_status(request: Request, token: str | None = None):
    # SSE auth: EventSource can't set headers, so accept the same-origin session cookie
    # or an explicit ?token (from the login response).
    from app.security import decode_token
    decode_token(token or request.cookies.get("session") or "")  # raises 401 if invalid

    async def gen():
        while True:
            repo = get_repo()
            try:
                payload = services.build_status(repo)
            finally:
                repo.close()
            yield f"data: {json.dumps(payload, default=str)}\n\n"
            await asyncio.sleep(5)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ------------------------------------------------------------------ tonight / control
@app.get("/tonight")
def tonight(repo=Depends(repo_dep), user: str = AuthDep):
    rt = bridge.read_runtime_state(repo.conn, settings.runtime_stale_seconds)
    extra = rt.get("extra") or {}
    return {
        "mode": rt.get("mode", "auto"),
        "state": rt.get("state"),
        "target_temp_f": rt.get("target_temp_f"),
        "power_on": extra.get("power_on", True),
        "away": extra.get("away", False),
        "wake": extra.get("wake"),
        "session_mode": extra.get("session_mode", "night"),
        "nap": extra.get("nap"),
        "nap_deadline": extra.get("nap_deadline"),
        "schedule": services.schedule_brief(repo),
        "recommendation": services.ml_recommendation(repo),
        "setpoint": services.ml_overview(repo)["setpoint"],
    }


def _enqueue(repo, ctype, payload=None):
    cid = bridge.enqueue_command(repo.conn, ctype, payload)
    return {"queued": ctype, "command_id": cid}


@app.post("/control/{action}")
def control(action: str, repo=Depends(repo_dep), user: str = AuthDep):
    # Maps the dashboard's control buttons to daemon commands. Includes the
    # Eight Sleep app's controls: power on/off the side, away mode, prime.
    mapping = {"start": "start", "pause": "pause", "resume": "resume",
               "stop": "stop", "safe-default": "safe_default",
               "power-on": "power_on", "power-off": "power_off",
               "away-on": "away_on", "away-off": "away_off", "prime": "prime"}
    if action not in mapping:
        raise HTTPException(404, "unknown control action")
    return _enqueue(repo, mapping[action])


def _log_manual_temp(repo, target_f: float) -> None:
    # logged as a MANUAL override so the ML's revealed-preference learner picks it up.
    from sleepctl.models import ActionRecord
    from sleepctl.loop.cycle import ControlCycle
    night = ControlCycle.night_date(datetime.now())
    repo.log_action(ActionRecord(date=night, action_name="manual_override",
                                 params={"target_f": target_f}, source="manual",
                                 applied=True))


@app.post("/tonight/temp")
def set_temp(body: TempBody, repo=Depends(repo_dep), user: str = AuthDep):
    _log_manual_temp(repo, body.target_f)
    return _enqueue(repo, "set_temp", {"target_f": body.target_f})


@app.post("/tonight/temp/nudge")
def nudge_temp(body: NudgeBody, repo=Depends(repo_dep), user: str = AuthDep):
    """Realtime +/- adjustment (the app's fine-tune buttons). The daemon applies
    it against the current target on its next (sub-second) command poll."""
    return _enqueue(repo, "nudge_temp", {"delta_f": body.delta_f})


@app.post("/tonight/mode")
def set_mode(body: ModeBody, repo=Depends(repo_dep), user: str = AuthDep):
    if body.mode not in ("auto", "manual", "view"):
        raise HTTPException(400, "mode must be auto|manual|view")
    return _enqueue(repo, "set_mode", {"mode": body.mode})


@app.post("/tonight/wake")
def set_wake(body: WakeBody, repo=Depends(repo_dep), user: str = AuthDep):
    return _enqueue(repo, "set_wake", {"wake_time": body.wake_time,
                                       "window_min": body.window_min,
                                       "vibration_power": body.vibration_power,
                                       "thermal_level": body.thermal_level,
                                       "night_type": body.night_type})


@app.delete("/tonight/wake")
def clear_wake(repo=Depends(repo_dep), user: str = AuthDep):
    return _enqueue(repo, "clear_wake")


@app.get("/tonight/plan")
def tonight_plan(repo=Depends(repo_dep), user: str = AuthDep):
    """Tonight's wake-aware, benchmark-driven sleep plan (mode, opportunity, cycles,
    sleep debt, smart-wake window, thermal strategy, literature targets)."""
    return services.sleep_plan(repo)


class NapBody(BaseModel):
    duration_min: int | None = None   # e.g. 20 or 90
    wake_time: str | None = None      # HH:MM (alternative to duration)


@app.post("/tonight/induce")
def induce_sleep(repo=Depends(repo_dep), user: str = AuthDep):
    """'Make me tired': run the onset-induction (warm->cool) cascade now."""
    return _enqueue(repo, "induce_sleep")


@app.post("/tonight/nap")
def start_nap(body: NapBody, repo=Depends(repo_dep), user: str = AuthDep):
    """Start a nap: fall asleep fast, optimise the nap, wake by the deadline."""
    if not body.duration_min and not body.wake_time:
        raise HTTPException(400, "provide duration_min or wake_time")
    return _enqueue(repo, "start_nap", {"duration_min": body.duration_min,
                                        "wake_time": body.wake_time})


@app.post("/tonight/nap/preview")
def nap_preview(body: NapBody, repo=Depends(repo_dep), user: str = AuthDep):
    """Preview the strategy for a nap length without starting it (for the UI)."""
    return services.nap_preview(body.duration_min, body.wake_time)


@app.post("/tonight/session/end")
def end_session(repo=Depends(repo_dep), user: str = AuthDep):
    """End an active induce/nap session and return to normal night control."""
    return _enqueue(repo, "end_session")


@app.get("/maintenance")
def maintenance(repo=Depends(repo_dep), user: str = AuthDep):
    """Sleep-maintenance summary: learned awakening pattern (prevention) + how recent
    awakenings were handled."""
    return services.maintenance_summary(repo)


# ------------------------------------------------------------------ data + notes
@app.get("/nights")
def nights(limit: int = 30, repo=Depends(repo_dep), user: str = AuthDep):
    return [services._night_brief(n) for n in repo.recent_nights(limit)]


@app.get("/nights/{date}")
def night(date: str, repo=Depends(repo_dep), user: str = AuthDep):
    for n in repo.recent_nights(400):
        if n.date == date:
            d = services._night_brief(n)
            d["context"] = _ctx_dict(repo.get_context(date))
            return d
    raise HTTPException(404, "night not found")


@app.get("/nights/{date}/samples")
def samples(date: str, repo=Depends(repo_dep), user: str = AuthDep):
    return [
        {"ts": s.timestamp.isoformat() if s.timestamp else None, "stage": s.stage.value,
         "heart_rate": s.heart_rate, "hrv": s.hrv, "bed_temp_f": s.bed_temp_f,
         "room_temp_f": s.room_temp_f}
        for s in repo.samples_for_night(date)
    ]


@app.get("/interventions")
def interventions(limit: int = 50, repo=Depends(repo_dep), user: str = AuthDep):
    return [{"ts": i.timestamp.isoformat() if i.timestamp else None,
             "state": i.state.value, "action": i.action.value,
             "magnitude_f": i.magnitude_f, "reason": i.reason}
            for i in repo.recent_interventions(limit)]


# --------------------------------------------------------- wake-up exit survey
class CheckInBody(BaseModel):
    date: str | None = None
    rested: float | None = None          # 0-10 how rested you feel
    grogginess: float | None = None      # 0-10 sleep inertia / fogginess
    daytime_energy: float | None = None  # 0-10 expected daytime performance
    awakenings_felt: int | None = None   # how many wakes you remember
    onset_feel: str | None = None        # quick | normal | slow
    factors: dict | None = None          # caffeine/alcohol/late_work/illness/travel/stress


@app.get("/checkin/status")
def checkin_status(repo=Depends(repo_dep), user: str = AuthDep):
    return services.checkin_status(repo)


@app.post("/checkin")
def submit_checkin(body: CheckInBody, repo=Depends(repo_dep), user: str = AuthDep):
    return services.submit_checkin(repo, body.model_dump())


def _ctx_dict(ctx):
    if ctx is None:
        return None
    return {k: (v.isoformat() if isinstance(v, datetime) else v)
            for k, v in vars(ctx).items()}


@app.get("/notes")
def get_notes(date: str | None = None, repo=Depends(repo_dep), user: str = AuthDep):
    if date:
        rows = repo.conn.execute("SELECT * FROM notes WHERE date=? ORDER BY id DESC",
                                 (date,)).fetchall()
    else:
        rows = repo.conn.execute("SELECT * FROM notes ORDER BY id DESC LIMIT 50").fetchall()
    return [dict(r) for r in rows]


@app.post("/notes")
def add_note(body: NoteBody, repo=Depends(repo_dep), user: str = AuthDep):
    repo.conn.execute("INSERT INTO notes (date, text, created) VALUES (?,?,?)",
                      (body.date, body.text, datetime.now(timezone.utc).isoformat()))
    repo.conn.commit()
    return {"ok": True}


# ------------------------------------------------------------------ learning
@app.get("/ml/overview")
def ml_overview(repo=Depends(repo_dep), user: str = AuthDep):
    return services.ml_overview(repo)


@app.get("/ml/recommendation")
def ml_rec(repo=Depends(repo_dep), user: str = AuthDep):
    return services.ml_recommendation(repo)


# ------------------------------------------------------------------ analytics
@app.get("/analytics/trends")
def analytics_trends(metric: str = "wake_events", window: int = 30,
                     repo=Depends(repo_dep), user: str = AuthDep):
    return services.trends(repo, metric, window)


@app.get("/analytics/effectiveness")
def analytics_eff(repo=Depends(repo_dep), user: str = AuthDep):
    return services.effectiveness(repo)


# ------------------------------------------------------------------ settings
@app.get("/settings")
def get_settings(repo=Depends(repo_dep), user: str = AuthDep):
    rows = repo.conn.execute("SELECT key, value FROM settings_kv").fetchall()
    stored = {r["key"]: json.loads(r["value"]) for r in rows}
    return {"stored": stored, "defaults": _config_defaults()}


@app.put("/settings")
def put_settings(body: SettingsBody, repo=Depends(repo_dep), user: str = AuthDep):
    for k, v in body.values.items():
        old = repo.conn.execute("SELECT value FROM settings_kv WHERE key=?", (k,)).fetchone()
        repo.conn.execute("INSERT INTO settings_kv (key, value) VALUES (?,?) "
                          "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                          (k, json.dumps(v)))
        repo.conn.execute("INSERT INTO settings_changes (ts, key, old_value, new_value) "
                          "VALUES (?,?,?,?)",
                          (datetime.now(timezone.utc).isoformat(), k,
                           old["value"] if old else None, json.dumps(v)))
    repo.conn.commit()
    return {"ok": True}


def _config_defaults() -> dict:
    from sleepctl.config import AppConfig
    t = AppConfig.default().tunables
    b = AppConfig.default().benchmarks
    return {
        "neutral_temp_f": t.neutral_temp_f, "deep_bias_temp_f": t.deep_bias_temp_f,
        "wake_ramp_temp_f": t.wake_ramp_temp_f, "wake_window_min": t.wake_window_min,
        "wake_vibration_power": t.wake_vibration_power,
        "max_step_f": t.max_step_f, "hrv_target_ms": b.hrv_target_ms,
        "wake_events_max": b.wake_events_max,
    }


# ------------------------------------------------------------------ admin / alerts
@app.get("/admin/health")
def admin_health(repo=Depends(repo_dep), user: str = AuthDep):
    return services.data_health(repo)


@app.get("/admin/logs")
def admin_logs(limit: int = 50, repo=Depends(repo_dep), user: str = AuthDep):
    rows = repo.conn.execute(
        "SELECT ts, state, thermal_intent, target_temp_f, action, reason, confidence "
        "FROM decisions ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


@app.get("/alerts")
def get_alerts(repo=Depends(repo_dep), user: str = AuthDep):
    services.generate_alerts(repo)
    return services.active_alerts(repo)


@app.post("/alerts/{alert_id}/ack")
def ack_alert(alert_id: int, repo=Depends(repo_dep), user: str = AuthDep):
    repo.conn.execute("UPDATE alerts SET acknowledged=1 WHERE id=?", (alert_id,))
    repo.conn.commit()
    return {"ok": True}
