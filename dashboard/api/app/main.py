"""sleepctl dashboard API — FastAPI app wiring all routes + SSE.

The API reuses the sleepctl engine for reads and the control bridge for writes (it never
calls the device directly). Single module for v1 clarity.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import time
from datetime import datetime, timezone

from fastapi import Body, Depends, FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse
from pydantic import BaseModel

from app import bridge, services
from app.config import settings
from app.db import get_repo
from app.security import (
    AuthDep,
    authenticate,
    create_token,
    current_user,
    decode_token,
    ensure_bootstrap_user,
    _token_from_request,
)

app = FastAPI(title="sleepctl dashboard", version="1.0.0")
app.add_middleware(
    CORSMiddleware, allow_origins=settings.cors_origins, allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"],
)


@app.on_event("startup")
def _startup() -> None:
    ensure_bootstrap_user()
    # Optional: connect the work-shift calendar from CALENDAR_ICS_URL (deploy/.env) without the UI.
    try:
        repo = get_repo()
        try:
            if services.seed_calendar_from_env(repo):
                print("calendar: seeded ICS feed from CALENDAR_ICS_URL", flush=True)
        finally:
            repo.close()
    except Exception as exc:
        print(f"calendar env-seed skipped: {exc}", flush=True)
    _start_health_watchdog()


def repo_dep():
    repo = get_repo()
    try:
        yield repo
    finally:
        repo.close()


def _tail(path: str, n: int) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return "".join(fh.readlines()[-n:]).rstrip() or "(empty)"
    except FileNotFoundError:
        return "(file not found)"
    except Exception as exc:  # never let diag crash
        return f"(could not read: {exc})"


def _run_dir() -> str:
    """Locate the .run log directory (next to the SQLite DB / repo root)."""
    db = os.environ.get("SLEEPCTL_DB", "")
    root = os.path.dirname(db) if db else os.getcwd()
    return os.path.join(root, ".run")


@app.get("/diag")
def diag(token: str = "", format: str = "", repo=Depends(repo_dep)):
    """Read-only remote diagnostics: a structured DIAGNOSIS (what's wrong + the fix) followed
    by device/live status + the daemon log tails, as plain text — or the full structured dict
    as JSON with ``?format=json`` (lossless; the plaintext is derived from the same dict, so
    reach for JSON when you need to parse/grep it precisely instead of the summarized text).

    Gated by a secret ``DIAG_TOKEN`` (env). Returns 404 when the token is missing/wrong or the
    feature is disabled, so it's invisible to scanners. Contains NO credentials — only status +
    logs — but it IS reachable over the public Funnel URL, so keep the token strong. This exists so
    the maintainer can read the daemon state remotely without shelling into the host."""
    expected = os.environ.get("DIAG_TOKEN")
    if not expected or not token or not secrets.compare_digest(token, expected):
        raise HTTPException(404, "not found")

    from app.diagnostics import render_diagnosis_text, run_diagnostics
    run = _run_dir()
    report = run_diagnostics(repo, run_dir=run)

    if format == "json":
        return JSONResponse(report)

    rt = bridge.read_runtime_state(repo.conn, settings.runtime_stale_seconds)
    extra = rt.get("extra") or {}
    device = extra.get("device") or {}
    status_lines = [
        "=== STATUS ===",
        f"updated={rt.get('updated')}  stale={rt.get('stale')}  daemon_alive={rt.get('daemon_alive')}",
        f"live={extra.get('live')}  dry_run={extra.get('dry_run')}  mode={rt.get('mode')}  "
        f"state={rt.get('state')}",
        f"power_on={extra.get('power_on')}  away={extra.get('away')}  bed_presence={extra.get('bed_presence')}",
        f"target_temp_f={rt.get('target_temp_f')}  bed_temp_f={rt.get('bed_temp_f')}  "
        f"room_temp_f={rt.get('room_temp_f')}  stage={rt.get('stage')}",
        f"target_level={rt.get('target_level')}  device_level={extra.get('device_level')}  "
        f"device_target_level={extra.get('device_target_level')}",
        f"device={json.dumps(device)}",
        f"thermal_health={json.dumps(extra.get('thermal_health'))}",
        f"device_error={extra.get('device_error')}",
    ]
    out = render_diagnosis_text(report)
    out += "\n\n" + "\n".join(status_lines)
    out += "\n\n=== daemon-crash.log (last 40) ===\n" + _tail(os.path.join(run, "daemon-crash.log"), 40)
    out += "\n\n=== daemon.log (last 60) ===\n" + _tail(os.path.join(run, "daemon.log"), 60)
    out += "\n\n=== daemon.err (last 40) ===\n" + _tail(os.path.join(run, "daemon.err"), 40)
    out += "\n\n=== watchdog.log (last 20) ===\n" + _tail(os.path.join(run, "watchdog.log"), 20)
    return PlainTextResponse(out)


# ------------------------------------------------------------------ models
class LoginBody(BaseModel):
    username: str
    password: str
    remember: bool = True   # "keep me logged in" — persistent, sliding session


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


def _issue_session(response: Response, username: str, remember: bool) -> str:
    """Set the session cookie. Remember → a long-lived PERSISTENT cookie; otherwise a
    SESSION cookie (no max_age) that the browser drops when it's closed."""
    ttl_hours = settings.jwt_remember_hours if remember else settings.jwt_session_hours
    token = create_token(username, ttl_hours=ttl_hours, remember=remember)
    kwargs = dict(httponly=True, samesite="lax")
    if remember:
        kwargs["max_age"] = ttl_hours * 3600      # persist across browser restarts
    response.set_cookie("session", token, **kwargs)
    return token


@app.post("/auth/login")
def login(body: LoginBody, response: Response):
    if not authenticate(body.username, body.password):
        raise HTTPException(401, "invalid credentials")
    token = _issue_session(response, body.username, body.remember)
    return {"token": token, "user": body.username, "remember": body.remember}


@app.post("/auth/logout")
def logout(response: Response):
    response.delete_cookie("session")
    return {"ok": True}


@app.get("/auth/me")
def me(request: Request, response: Response, user: str = AuthDep):
    # Sliding renewal: whenever the app checks who's logged in, re-issue a fresh long-lived
    # cookie IF the user opted to stay signed in — so an actively-used session never expires.
    try:
        claims = decode_token(_token_from_request(request) or "")
        if claims.get("rmb"):
            _issue_session(response, user, remember=True)
    except Exception:
        pass
    return {"user": user}


# ------------------------------------------------------------------ status + SSE
@app.get("/status")
def status(repo=Depends(repo_dep), user: str = AuthDep):
    return services.build_status(repo)


@app.get("/report/nightly")
def report_nightly(repo=Depends(repo_dep), user: str = AuthDep):
    """Explainable nightly intelligence report (what happened / what I did + why / learned)."""
    from sleepctl.night_report import build_night_report
    return build_night_report(repo)


@app.get("/perfect-weights")
def perfect_weights(repo=Depends(repo_dep), user: str = AuthDep):
    """The user's personalized perfect-sleep weights vs the evidence prior (per mode)."""
    return services.perfect_weights_view(repo)


@app.get("/wake/catalog")
def wake_catalog(repo=Depends(repo_dep), user: str = AuthDep):
    """Recent mid-sleep awakenings with the converging-signal vector that flagged each."""
    return services.wake_catalog(repo)


class GymConfigBody(BaseModel):
    enabled: bool | None = None
    early_offset_min: int | None = None
    sufficient_sleep_h: float | None = None
    min_safe_sleep_h: float | None = None
    opportunity_value: float | None = None
    lean: str | None = None
    gym_days: list[int] | None = None


@app.get("/gym/advice")
def gym_advice(repo=Depends(repo_dep), user: str = AuthDep):
    """GO-train vs SLEEP-IN call for this morning, from your config + recent sleep."""
    return services.gym_advice(repo)


@app.get("/wake/plan")
def wake_plan(repo=Depends(repo_dep), user: str = AuthDep):
    """Unified smart-alarm plan: gym-aware effective wake time + smart window + silent ladder."""
    return services.wake_plan(repo)


@app.get("/wake/tuning")
def wake_tuning(repo=Depends(repo_dep), user: str = AuthDep):
    """The alarm's learned-to-you window + lift bar, from your morning grogginess check-ins."""
    return services.wake_tuning_view(repo)


@app.get("/learning/phases")
def learning_phases(repo=Depends(repo_dep), user: str = AuthDep):
    """What's been learned across all three sleep phases (onset / maintenance / wake), per mode."""
    return services.learning_phases(repo)


class ShiftConfigBody(BaseModel):
    enabled: bool | None = None
    next_shift: str | None = None   # ISO datetime of the next shift start (null to clear)
    kind: str | None = None         # 'night' | 'call' | 'day'
    source: str | None = None       # 'manual' | 'calendar' (calendar-synced entries are tagged)
    shift_end: str | None = None    # ISO datetime of the shift end (from a calendar event)


@app.get("/shift/plan")
def shift_plan(repo=Depends(repo_dep), user: str = AuthDep):
    """Strategic cross-shift sleep plan: debt, tonight's target, banking, naps, anchor, warnings."""
    return services.shift_plan_view(repo)


@app.get("/shift/config")
def shift_config(repo=Depends(repo_dep), user: str = AuthDep):
    return services.shift_config_view(repo)


@app.put("/shift/config")
def shift_config_update(body: ShiftConfigBody, repo=Depends(repo_dep), user: str = AuthDep):
    # exclude_unset so an explicit null next_shift (clear the shift) is honored.
    return services.shift_config_update(repo, body.model_dump(exclude_unset=True))


class HueConfigBody(BaseModel):
    enabled: bool | None = None
    bridge_ip: str | None = None
    target_ids: list[str] | None = None
    therapy_ids: list[str] | None = None
    kind: str | None = None


class HuePairBody(BaseModel):
    bridge_ip: str | None = None


@app.get("/wake/light/config")
def hue_config(repo=Depends(repo_dep), user: str = AuthDep):
    return services.hue_config_view(repo)


@app.put("/wake/light/config")
def hue_config_update(body: HueConfigBody, repo=Depends(repo_dep), user: str = AuthDep):
    return services.hue_config_update(repo, body.model_dump(exclude_none=True))


@app.get("/wake/light/discover")
def hue_discover(user: str = AuthDep):
    return services.hue_discover()


@app.post("/wake/light/pair")
def hue_pair(body: HuePairBody, repo=Depends(repo_dep), user: str = AuthDep):
    """Press the Hue bridge link button first, then call this to create + store a token."""
    return services.hue_pair(repo, body.bridge_ip)


@app.get("/wake/light/lights")
def hue_lights(repo=Depends(repo_dep), user: str = AuthDep):
    return services.hue_lights(repo)


@app.post("/wake/light/test")
def hue_test(repo=Depends(repo_dep), user: str = AuthDep):
    return services.hue_test(repo)


@app.get("/gym/config")
def gym_config(repo=Depends(repo_dep), user: str = AuthDep):
    return services.gym_config_view(repo)


@app.put("/gym/config")
def gym_config_update(body: GymConfigBody, repo=Depends(repo_dep), user: str = AuthDep):
    return services.gym_config_update(repo, body.model_dump(exclude_none=True))


class BCGBody(BaseModel):
    fs: float | None = None
    ax: list[float] | None = None
    ay: list[float] | None = None
    az: list[float] | None = None
    mag: list[float] | None = None
    payload: list[dict] | None = None
    source: str | None = None


def _bcg_auth(request: Request, token: str | None) -> None:
    """Phone-friendly auth: accept the dashboard token as a ?token= query param (same trick as
    the SSE stream) or the usual header/cookie. When ``BCG_INGEST_OPEN`` is set, auth is dropped
    on the phone endpoints only — for a header-less device on a trusted LAN."""
    if settings.bcg_ingest_open:
        return
    from app.security import _token_from_request, decode_token
    decode_token(token or _token_from_request(request) or "")  # raises 401 if invalid


@app.get("/bcg/should-record")
def bcg_should_record(request: Request, token: str | None = None, repo=Depends(repo_dep)):
    """Bed-presence-driven record flag for an optional iOS Shortcuts automation that starts/stops
    the phone recording on bed-in/out. {"record": true|false, "presence": ...}."""
    _bcg_auth(request, token)
    return services.bcg_should_record(repo)


@app.post("/bcg/ingest")
def bcg_ingest(body: BCGBody, request: Request, token: str | None = None,
               fs: float | None = None, source: str | None = None, repo=Depends(repo_dep)):
    """Ingest a raw accelerometer batch from the phone (e.g. an iPhone in bed) → sub-minute
    movement (+ best-effort HR/HRV) published to the daemon. ``fs``/``source``/``token`` come
    from the query string so Sensor Logger's header-less HTTP push works:
    ``POST /bcg/ingest?token=<JWT>&fs=50`` with the app's native JSON body."""
    _bcg_auth(request, token)
    payload = body.model_dump(exclude_none=True)
    if fs is not None:
        payload["fs"] = fs
    if source is not None:
        payload["source"] = source
    return services.ingest_bcg(repo, payload)


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
        "device": extra.get("device"),
        "thermal_health": extra.get("thermal_health"),
        "stale": rt.get("stale", True),
        "daemon_alive": rt.get("daemon_alive", False),
        "schedule": services.schedule_brief(repo),
        "recommendation": services.ml_recommendation(repo),
        "setpoint": services.ml_overview(repo)["setpoint"],
    }


def _enqueue(repo, ctype, payload=None):
    cid = bridge.enqueue_command(repo.conn, ctype, payload)
    return {"queued": ctype, "command_id": cid}


@app.post("/control/self-test")
def self_test_start(body: dict | None = Body(default=None), repo=Depends(repo_dep),
                    user: str = AuthDep):
    """Kick off the on-bed self-test / thermal-calibration battery (daemon runs it, pausing
    control). Body: {"mode": "full"|"gentle"|"sensing"} (default full)."""
    mode = (body or {}).get("mode", "full")
    if mode not in ("full", "gentle", "sensing"):
        mode = "full"
    return _enqueue(repo, "self_test", {"mode": mode})


@app.post("/control/self-test/cancel")
def self_test_cancel(repo=Depends(repo_dep), user: str = AuthDep):
    return _enqueue(repo, "self_test_cancel")


@app.get("/control/self-test")
def self_test_status(repo=Depends(repo_dep), user: str = AuthDep):
    """Live self-test report (progress + PASS/FAIL per check + measured calibration)."""
    return {"self_test": bridge.read_self_test(repo.conn),
            "calibration": repo.get_thermal_calibration()}


@app.post("/control/comfort-cal")
def comfort_cal_start(body: dict | None = Body(default=None), repo=Depends(repo_dep),
                      user: str = AuthDep):
    """Start the interactive in-bed comfort sweep. Optional body {"steps_f": [..]}."""
    payload = {}
    if body and body.get("steps_f"):
        payload["steps_f"] = body["steps_f"]
    return _enqueue(repo, "comfort_cal_start", payload)


@app.post("/control/comfort-cal/rate")
def comfort_cal_rate(body: dict = Body(...), repo=Depends(repo_dep), user: str = AuthDep):
    """Rate the current comfort step: -2 too cold .. 0 just right .. +2 too warm."""
    rating = (body or {}).get("rating")
    if rating is None:
        raise HTTPException(400, "rating required (-2..2)")
    return _enqueue(repo, "comfort_cal_rate", {"rating": int(rating)})


@app.post("/control/comfort-cal/cancel")
def comfort_cal_cancel(repo=Depends(repo_dep), user: str = AuthDep):
    return _enqueue(repo, "comfort_cal_cancel")


@app.get("/control/comfort-cal")
def comfort_cal_status(repo=Depends(repo_dep), user: str = AuthDep):
    """Live comfort-sweep state + the saved comfort profile."""
    rt = bridge.read_runtime_state(repo.conn)
    return {"comfort_cal": (rt.get("extra") or {}).get("comfort_cal"),
            "profile": repo.get_comfort_profile()}


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


@app.post("/admin/backtest")
def admin_backtest(user: str = AuthDep):
    """On-demand validation: does the closed loop beat no-control (and stay safe) on the
    response-aware model? Reassurance before trusting it overnight."""
    return services.backtest_summary()


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


# ---- High-leverage features: pre-emption, readiness, weather, forensics, n-of-1 ----
@app.get("/predictive/preemption")
def predictive_preemption(repo=Depends(repo_dep), user: str = AuthDep):
    return services.preemption_status(repo)


@app.get("/morning/readiness")
def morning_readiness(repo=Depends(repo_dep), user: str = AuthDep):
    return services.morning_readiness_summary(repo)


@app.get("/weather/forecast")
def weather_forecast(repo=Depends(repo_dep), user: str = AuthDep):
    return services.weather_forecast(repo)


@app.get("/forensics/awakenings")
def forensics_awakenings(limit: int = 20, repo=Depends(repo_dep), user: str = AuthDep):
    return services.awakening_forensics_summary(repo, limit)


class ExperimentBody(BaseModel):
    name: str = "experiment"
    hypothesis: str = ""
    variable: str = ""
    metric: str = "wake_events"
    min_nights_per_arm: int = 3
    washout_nights: int = 1
    arm_a: dict = {"label": "control", "params": {}}
    arm_b: dict = {"label": "treatment", "params": {}}


@app.get("/experiments")
def experiments_list(repo=Depends(repo_dep), user: str = AuthDep):
    return services.experiments_list(repo)


@app.post("/experiments")
def experiment_create(body: ExperimentBody, repo=Depends(repo_dep), user: str = AuthDep):
    return services.experiment_create(repo, body.model_dump())


@app.get("/experiments/templates")
def experiment_templates(repo=Depends(repo_dep), user: str = AuthDep):
    """Curated one-tap n-of-1 templates, each with an a-priori power estimate."""
    return services.experiment_templates(repo)


@app.post("/experiments/from-template/{key}")
def experiment_from_template(key: str, repo=Depends(repo_dep), user: str = AuthDep):
    return services.experiment_from_template(repo, key)


@app.get("/experiments/{exp_id}/analyze")
def experiment_analyze(exp_id: int, repo=Depends(repo_dep), user: str = AuthDep):
    return services.experiment_analyze(repo, exp_id)


@app.post("/experiments/{exp_id}/stop")
def experiment_stop(exp_id: int, repo=Depends(repo_dep), user: str = AuthDep):
    return services.experiment_stop(repo, exp_id)


# ------------------------------------------------------------------ interpretability
@app.get("/insights/decisions")
def insights_decisions(limit: int = 50, repo=Depends(repo_dep), user: str = AuthDep):
    """Recent controller decisions as a human-readable "why it did that" timeline: state,
    intent/action, target temp, reason, and whether it actually moved the bed."""
    return services.insights_decisions(repo, limit)


@app.get("/insights/parameters")
def insights_parameters(repo=Depends(repo_dep), user: str = AuthDep):
    """What's currently learned: the active setpoint profile, measured thermal/comfort/resting
    baselines, and a couple of learner summaries — each with its value, source, and what it does."""
    return services.insights_parameters(repo)
# ---- Meta-learning: what the system has learned, across every learner, + contradiction check
@app.get("/learning/ledger")
def learning_ledger(repo=Depends(repo_dep), user: str = AuthDep):
    return services.learning_ledger_view(repo)
# ======================================================================================
# Goal #2: silent-outage detection -> Web Push. The health evaluator itself already runs
# on every /status + SSE tick (see services._status_alerts). The background watchdog
# below is the belt-and-suspenders half: it keeps evaluating on a fixed interval even
# when NO client/browser tab is open at all (the actual "6-hour silent outage" scenario
# — nobody has the dashboard open to trigger a request). It's a single daemon thread,
# not a new process, so there's nothing extra to deploy/supervise.
# ------------------------------------------------------------------------------------
import threading  # noqa: E402

_HEALTH_WATCHDOG_INTERVAL_S = 60
_health_watchdog_started = False


def _health_watchdog_loop() -> None:
    import time
    while True:
        try:
            repo = get_repo()
            try:
                services.evaluate_and_sync_health_alerts(repo)
            finally:
                repo.close()
        except Exception:
            pass  # never let the watchdog thread die on a transient DB/import hiccup
        time.sleep(_HEALTH_WATCHDOG_INTERVAL_S)


def _start_health_watchdog() -> None:
    global _health_watchdog_started
    if _health_watchdog_started:
        return
    _health_watchdog_started = True
    t = threading.Thread(target=_health_watchdog_loop, name="health-watchdog", daemon=True)
    t.start()


# ------------------------------------------------------------------------------ push
class PushSubscribeBody(BaseModel):
    endpoint: str
    keys: dict  # {"p256dh": ..., "auth": ...}


@app.get("/push/vapid-public-key")
def push_vapid_public_key():
    """Public info only — no auth required so the "Enable alerts" button can fetch the
    key before the user necessarily has a session (mirrors how service workers fetch
    manifest.json unauthenticated)."""
    return services.vapid_public_key()


@app.post("/push/subscribe")
def push_subscribe(body: PushSubscribeBody, repo=Depends(repo_dep), user: str = AuthDep):
    keys = body.keys or {}
    return services.add_push_subscription(repo, body.endpoint, keys.get("p256dh", ""),
                                          keys.get("auth", ""))


@app.post("/push/unsubscribe")
def push_unsubscribe(body: dict = Body(...), repo=Depends(repo_dep), user: str = AuthDep):
    return services.remove_push_subscription(repo, body.get("endpoint", ""))
# ---- Circadian phase model + OAuth-free calendar ingest (#10) ----
@app.get("/circadian")
def circadian(repo=Depends(repo_dep), user: str = AuthDep):
    """Circadian phase estimate (habitual sleep window + midpoint, recent phase shift) and the
    derived wake-maintenance zone — grounded in the user's own recent sleep history."""
    return services.circadian_view(repo)


class CalendarConfigBody(BaseModel):
    enabled: bool | None = None
    ics_url: str | None = None   # secret read-only ICS URL (user data — never hardcoded/logged)


@app.get("/calendar/config")
def calendar_config(repo=Depends(repo_dep), user: str = AuthDep):
    """Whether an ICS feed is configured (URL is masked, never echoed in full)."""
    return services.calendar_config_view(repo)


@app.put("/calendar/config")
def calendar_config_update(body: CalendarConfigBody, repo=Depends(repo_dep), user: str = AuthDep):
    return services.calendar_config_update(repo, body.model_dump(exclude_unset=True))


@app.get("/calendar/events")
def calendar_events(repo=Depends(repo_dep), user: str = AuthDep):
    """Upcoming events parsed from the last cached ICS fetch (no network hit)."""
    return services.calendar_events_view(repo)


@app.post("/calendar/refresh")
def calendar_refresh(repo=Depends(repo_dep), user: str = AuthDep):
    """Force a re-fetch of the configured ICS feed now."""
    return services.calendar_refresh(repo)
# ---- Standing efficacy trial: "does the controller help?" (opt-in, default OFF) ----
class EfficacyConfigBody(BaseModel):
    enabled: bool | None = None
    block_nights: int | None = None


@app.get("/efficacy")
def efficacy_status(repo=Depends(repo_dep), user: str = AuthDep):
    """Standing-trial status: current config + the CONTROLLED-vs-HELD analysis so far."""
    return services.efficacy_status(repo)


@app.get("/efficacy/config")
def efficacy_config(repo=Depends(repo_dep), user: str = AuthDep):
    return services.efficacy_config_view(repo)


@app.put("/efficacy/config")
def efficacy_config_update(body: EfficacyConfigBody, repo=Depends(repo_dep), user: str = AuthDep):
    return services.efficacy_config_update(repo, body.model_dump(exclude_none=True))
# ---- Safety/quality: data-quality gate + decision guardrail ----
@app.get("/safety/data-quality")
def safety_data_quality(repo=Depends(repo_dep), user: str = AuthDep):
    """Live data-quality-gate state: trust score, top reason, and whether it's forcing a HOLD."""
    return services.data_quality_status(repo)


@app.get("/safety/guardrail")
def safety_guardrail(repo=Depends(repo_dep), user: str = AuthDep):
    """Live decision-guardrail state: current findings and whether a CRITICAL one is forcing
    a safe hold."""
    return services.guardrail_status(repo)


# ---- structured event log: "what happened and when" as one query, not a log grep ----
@app.get("/diag/events")
def diag_events(token: str = "", limit: int = 200, category: str = "", severity: str = "",
                since: str = "", repo=Depends(repo_dep)):
    """Remote structured-incident-timeline pull: the daemons' events table, filterable by
    category / severity / a minimum ISO ``since`` timestamp.

    SAME token gating as ``/diag`` (secret ``DIAG_TOKEN`` env, constant-time compare, 404 when
    missing/wrong/disabled — invisible to scanners). Complements ``/diag``'s log tails with a
    structured, queryable event timeline instead of unstructured text."""
    expected = os.environ.get("DIAG_TOKEN")
    if not expected or not token or not secrets.compare_digest(token, expected):
        raise HTTPException(404, "not found")

    return repo.recent_events(
        limit=limit,
        category=category or None,
        severity=severity or None,
        since_iso=since or None,
    )


# ---------------------------------------------------------------- remote deep-dive (token-gated)
# Two "give me the exact data, not a summary" tools for the maintainer, gated identically to
# /diag (secret DIAG_TOKEN, 404 on missing/wrong token so it's invisible to scanners). /diag's
# DIAGNOSIS block is a curated, aggregated verdict; these exist for when that's not enough and
# the maintainer needs the raw material themselves — an exact log slice, or a live device
# round-trip that bypasses whatever runtime_state currently says.
def _diag_gate(token: str) -> None:
    expected = os.environ.get("DIAG_TOKEN")
    if not expected or not token or not secrets.compare_digest(token, expected):
        raise HTTPException(404, "not found")


# file -> real filename in .run/ (see scripts/windows-{dashboard,watchdog}.ps1 for what writes
# each one). Whitelisted on purpose -- no arbitrary path is ever accepted, so there's no
# traversal surface even though this is a public (token-gated) endpoint.
_DIAG_LOG_FILES = {
    "daemon": "daemon.log",
    "daemon-err": "daemon.err",
    "daemon-crash": "daemon-crash.log",
    "watchdog": "watchdog.log",
    "api": "api.log",
    "api-err": "api.err",
    "web": "web.log",
    "web-build": "web-build.log",
}
_DIAG_LOGS_MAX_LINES = 1000
_DIAG_LOGS_MAX_BYTES = 200 * 1024  # ~200KB response cap


def _tail_lines_raw(path: str, n: int) -> list[str] | None:
    """Like ``_tail`` but returns the raw list of lines (not a joined/summarized string), and
    None (not a placeholder string) when the file doesn't exist -- callers decide how to render
    "not found" for their format."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.readlines()[-n:]
    except FileNotFoundError:
        return None
    except Exception:
        return None


@app.get("/diag/logs")
def diag_logs(token: str = "", file: str = "daemon", lines: int = 100, grep: str = ""):
    """Raw, filtered log tail -- the deliberately UN-summarized counterpart to /diag: exactly
    the bytes from the file, never paraphrased. Gated exactly like /diag (404 on missing/wrong
    ``DIAG_TOKEN``).

    Params:
      - ``file``: one of daemon | daemon-err | daemon-crash | watchdog | api | api-err | web |
        web-build (whitelisted -> mapped to the real filename in .run/; no arbitrary paths).
      - ``lines``: how many lines to read from the end of the file before filtering (default
        100, capped at 1000).
      - ``grep``: optional case-insensitive filter applied to that tail window. Tried as a
        Python regex (``re.IGNORECASE``) first; if it doesn't compile, falls back to a plain
        case-insensitive substring match so a literal string like "[WARN]" always works.

    Returns the matching lines verbatim as ``text/plain`` (never JSON-wrapped, never
    truncated-per-line) -- capped at ~200KB total so a huge/greedy request can't blow up the
    response. "(file not found)" / "(no matching lines)" placeholders make an empty result
    unambiguous."""
    _diag_gate(token)
    if file not in _DIAG_LOG_FILES:
        raise HTTPException(
            400, f"file must be one of: {', '.join(sorted(_DIAG_LOG_FILES))}"
        )
    n = max(1, min(int(lines), _DIAG_LOGS_MAX_LINES))
    path = os.path.join(_run_dir(), _DIAG_LOG_FILES[file])
    raw = _tail_lines_raw(path, n)
    if raw is None:
        return PlainTextResponse("(file not found)")

    if grep:
        try:
            matcher = re.compile(grep, re.IGNORECASE).search
        except re.error:
            needle = grep.lower()
            matcher = lambda ln, _needle=needle: _needle in ln.lower()  # noqa: E731
        raw = [ln for ln in raw if matcher(ln)]

    if not raw:
        return PlainTextResponse("(no matching lines)")

    out = "".join(raw)
    encoded = out.encode("utf-8", errors="replace")
    if len(encoded) > _DIAG_LOGS_MAX_BYTES:
        out = "(truncated to the last 200KB)\n" + encoded[-_DIAG_LOGS_MAX_BYTES:].decode(
            "utf-8", errors="ignore"
        )
    return PlainTextResponse(out)


def _diag_probe_result(ok: bool, error: str | None = None, latency_ms: float | None = None,
                       device: dict | None = None, frame: dict | None = None,
                       note: str | None = None) -> dict:
    return {"ok": ok, "error": error, "latency_ms": latency_ms, "device": device,
            "frame": frame, "note": note}


async def _run_diag_probe() -> dict:
    """The actual probe coroutine: connect -> timed update -> read -> close. Isolated from the
    sync endpoint so it can be driven by asyncio.wait_for with a hard timeout."""
    from sleepctl.adapters.credentials import load_credentials

    creds = load_credentials()
    if not creds.is_complete():
        return _diag_probe_result(
            False, error="no Eight Sleep credentials configured "
            "(EIGHTSLEEP_EMAIL/EIGHTSLEEP_PASSWORD not set and no credentials.json)",
            note="never attempted a connection")

    try:
        from sleepctl.adapters.eightsleep_cloud import EightSleepClient
    except Exception as exc:
        return _diag_probe_result(False, error=f"pyEight import failed: {exc}")

    client = EightSleepClient(creds.email, creds.password, creds.timezone, creds.side,
                              creds.client_id, creds.client_secret)
    try:
        await client.connect()
        t0 = time.monotonic()
        await client.update()
        latency_ms = round((time.monotonic() - t0) * 1000.0, 1)

        frame = client.read_frame()
        device = client.device_status()
        return _diag_probe_result(
            True, latency_ms=latency_ms,
            device={"online": device.get("online"), "has_water": device.get("has_water"),
                    "priming": device.get("priming"), "needs_priming": device.get("needs_priming")},
            frame={
                "heart_rate": frame.heart_rate, "hrv": frame.hrv,
                "respiratory_rate": frame.respiratory_rate,
                "stage": frame.stage.value if frame.stage is not None else None,
                "bed_temp_f": frame.bed_temp_f, "presence": frame.presence,
                "device_level": frame.device_level, "target_level": frame.target_level,
                "data_age_seconds": frame.data_age_seconds,
            },
            note="read-only: opened a brief separate cloud session distinct from the daemon's; "
                 "sent no device command",
        )
    except Exception as exc:
        return _diag_probe_result(False, error=f"{type(exc).__name__}: {exc}")
    finally:
        try:
            await client.close()
        except Exception:
            pass  # never let a close-time error mask (or crash past) the probe's real result


_DIAG_PROBE_TIMEOUT_S = 20.0


@app.get("/diag/probe")
def diag_probe(token: str = ""):
    """A fresh, READ-ONLY Eight Sleep cloud round-trip -- bypasses the daemon's (possibly
    stale) ``runtime_state`` entirely, so it answers "is the cloud/device actually responding
    right now?" independent of whatever the daemon last published. It opens its own brief,
    separate cloud session (distinct from the daemon's persistent one) -- fine, since it's
    read-only: connect() -> timed update() -> read_frame()/device_status() -> close().

    NEVER sends a device command (no set_heating_level / turn_on / prime / anything that
    writes) -- purely observational. Gated exactly like /diag (404 on missing/wrong
    ``DIAG_TOKEN``). Defensive by construction: the whole round-trip runs under a hard
    ``asyncio.wait_for`` timeout so a cloud hang can't wedge the request, ``close()`` always
    runs (in a ``finally``), and every failure mode (missing creds, pyEight not installed, a
    cloud/auth error, a timeout) returns ``{"ok": false, "error": ...}`` -- this endpoint is
    designed to never 500.

    Returns JSON: ``{ok, latency_ms, error, device: {online, has_water, priming,
    needs_priming}, frame: {heart_rate, hrv, respiratory_rate, stage, bed_temp_f, presence,
    device_level, target_level, data_age_seconds}, note}`` -- frame/device fields are None
    when the underlying pyEight property wasn't available, so absence is visible rather than
    silently dropped."""
    _diag_gate(token)
    try:
        result = asyncio.run(asyncio.wait_for(_run_diag_probe(), timeout=_DIAG_PROBE_TIMEOUT_S))
    except asyncio.TimeoutError:
        result = _diag_probe_result(
            False, error=f"probe timed out after {_DIAG_PROBE_TIMEOUT_S:.0f}s")
    except Exception as exc:
        result = _diag_probe_result(False, error=f"probe failed: {type(exc).__name__}: {exc}")
    return JSONResponse(result)


# ---- diagnostics: web-facing summary ----
# Auth-gated (dashboard login cookie via AuthDep) counterparts to /diag + /diag/events, meant
# for the logged-in owner's web app (persistent health badge + /diagnostics page) -- NOT the
# DIAG_TOKEN gate, which the browser doesn't have. Kept at this end-of-file seam so it merges
# cleanly alongside the other diag work happening in parallel.
@app.get("/diagnostics")
def diagnostics_summary(repo=Depends(repo_dep), user: str = AuthDep):
    """The same self-diagnosis battery ``/diag?format=json`` returns, but gated by the normal
    dashboard session cookie instead of ``DIAG_TOKEN`` -- what the web app's health badge and
    ``/diagnostics`` page poll. Never 500s: any failure in the diagnostics engine itself is
    caught and reported as a DOWN verdict with the error as a single check, rather than
    raising past this endpoint."""
    try:
        from app.diagnostics import run_diagnostics
        report = run_diagnostics(repo, run_dir=_run_dir())
        return JSONResponse(report)
    except Exception as exc:  # the diagnostics engine is itself defensive; this is a last resort
        return JSONResponse({
            "verdict": "DOWN",
            "headline": f"diagnostics engine crashed: {exc}",
            "primary_remedy": "check the API's own logs; the diagnostics battery failed to run",
            "checks": [{
                "id": "diagnostics_engine", "title": "Diagnostics engine", "status": "fail",
                "detail": f"{type(exc).__name__}: {exc}", "remedy": None,
            }],
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "version": {"sha": None, "branch": None},
        })


@app.get("/diagnostics/events")
def diagnostics_events(limit: int = 200, category: str = "", severity: str = "", since: str = "",
                       repo=Depends(repo_dep), user: str = AuthDep):
    """Auth-gated counterpart to ``/diag/events`` (session cookie, not ``DIAG_TOKEN``) for the
    web diagnostics page's recent-events list. ``repo.recent_events`` already never raises
    (returns ``[]`` on error); this wrapper just adds a belt-and-suspenders try/except so a
    bad query param can never turn into a 500."""
    try:
        return repo.recent_events(
            limit=limit, category=category or None, severity=severity or None,
            since_iso=since or None,
        )
    except Exception:
        return []


# ==============================================================================================
# ---- diagnostics: one-click repair / remote recovery / morning report ----
# Everything below is gated exactly like /diag / /diag/logs / /diag/probe (secret DIAG_TOKEN,
# constant-time compare via ``_diag_gate``, 404 on missing/wrong token -- invisible to
# scanners), with one extra rule for the remote-ACTION endpoints: every parameter is checked
# against a hardcoded allowlist (400 on anything else) and every action is logged to the
# structured events table (``category="remote_action"``) for an audit trail. No arbitrary
# command execution, no path parameters, no shell -- only fixed flag-file writes and fixed safe
# command enqueues (``sleepctl.repair.SAFE_REPAIR_COMMANDS`` / ``bridge.VALID_COMMANDS``).
# ==============================================================================================
@app.post("/diag/repair")
def diag_repair(token: str = "", repo=Depends(repo_dep)):
    """One-click repair: runs the small, safe, idempotent self-healing battery from
    ``sleepctl.repair`` (the SAME logic the standalone ``sleepctl repair`` CLI uses) --
    (a) clears a stuck pending-commands queue, (b) re-enqueues a ``prime``/``safe_default`` if
    the device looks stuck, (c) requests a daemon restart via the ``.run/restart.request``
    protocol if its heartbeat is stale, (d) clears a stale ``.run/watchdog.alert`` only when
    nothing currently looks like it's storming. Returns a JSON report with one
    ``{action, done, detail}`` entry per sub-action. Every sub-action only ever enqueues a
    command from ``sleepctl.repair.SAFE_REPAIR_COMMANDS`` (a subset of ``bridge.VALID_COMMANDS``)
    or writes the two well-known ``.run`` flag files the watchdog already consumes -- never an
    arbitrary/unsafe device command. Safe to call repeatedly (each action is independently
    idempotent; see ``sleepctl/repair.py``)."""
    _diag_gate(token)
    from sleepctl.repair import run_repair

    report = run_repair(repo.conn, _run_dir())
    try:
        done = [a["action"] for a in report["actions"] if a["done"]]
        repo.log_event("repair", "info", "diag_repair", "one-click repair run",
                       {"actions_done": done})
    except Exception:
        pass  # the events log is best-effort; must never break the repair response
    return report


# Hardcoded allowlists -- the ONLY values these remote-action endpoints will ever accept.
_RESTART_TARGETS = {"daemon", "api", "web", "all"}


@app.post("/diag/action/restart")
def diag_action_restart(token: str = "", target: str = "", repo=Depends(repo_dep)):
    """Token-gated remote recovery: request a component restart WITHOUT RDP/SSH. Writes
    ``.run/restart.request`` = ``target`` -- the ONLY mechanism this API uses to restart
    anything; it never kills a process itself. The already-deployed
    ``scripts/windows-watchdog.ps1`` polls for this file each supervise tick, force-stops the
    named component's process(es), deletes the flag, then lets its normal (storm-aware)
    supervise loop bring it back up.

    ``target`` MUST be one of daemon|api|web|all (hardcoded allowlist) -- anything else is a
    400, not a passthrough. Gated identically to ``/diag`` (404 on missing/wrong token)."""
    _diag_gate(token)
    if target not in _RESTART_TARGETS:
        raise HTTPException(400, f"target must be one of: {', '.join(sorted(_RESTART_TARGETS))}")

    run = _run_dir()
    try:
        os.makedirs(run, exist_ok=True)
        with open(os.path.join(run, "restart.request"), "w", encoding="utf-8") as fh:
            fh.write(target)
    except Exception as exc:
        raise HTTPException(500, f"could not write restart.request: {exc}") from exc

    try:
        repo.log_event("remote_action", "warn", "restart_request",
                       f"remote restart requested: target={target}", {"target": target})
    except Exception:
        pass
    return {"requested": target}


@app.post("/diag/action/reconnect")
def diag_action_reconnect(token: str = "", repo=Depends(repo_dep)):
    """Token-gated remote recovery: enqueue a benign ``safe_default`` re-init so a wedged Eight
    Sleep cloud session gets re-established on the daemon's next control tick -- no restart, no
    interruption of the daemon process itself, just the same command an idle/paused bed already
    accepts. De-duped against an already-pending ``safe_default`` so repeated calls (e.g. an
    impatient maintainer double-tapping the button) don't pile up the queue. Gated identically
    to ``/diag``."""
    _diag_gate(token)
    existing = repo.conn.execute(
        "SELECT 1 FROM commands WHERE type='safe_default' AND status='pending' LIMIT 1"
    ).fetchone()
    if existing:
        command_id = None
        detail = "a 'safe_default' command is already pending; not duplicating"
    else:
        command_id = bridge.enqueue_command(repo.conn, "safe_default")
        detail = "enqueued 'safe_default'"

    try:
        repo.log_event("remote_action", "info", "reconnect_request", detail,
                       {"command_id": command_id})
    except Exception:
        pass
    return {"reconnect_requested": True, "command_id": command_id, "detail": detail}


# ---- morning report (Feature #6): daily health + last-night push --------------------------
@app.get("/diag/morning-report")
def diag_morning_report_view(repo=Depends(repo_dep), user: str = AuthDep):
    """Read-only view of today's morning report (health verdict + last-night summary) for the
    dashboard UI -- auth-gated like every other dashboard read (session cookie/JWT), NOT the
    DIAG_TOKEN (this is normal in-app content, not a maintainer-only remote tool). Building it
    is just re-running the same pure ``services.build_morning_report``; it does not send a push
    or touch the once-per-day throttle."""
    return services.build_morning_report(repo)


@app.post("/diag/morning-report/send")
def diag_morning_report_send(token: str = "", force: bool = False, repo=Depends(repo_dep)):
    """Send the daily morning-report push. Token-gated (like the other remote-action endpoints)
    so a Windows Scheduled Task / cron on the box (or the watchdog) can hit it --
    ``POST /diag/morning-report/send?token=...`` -- without any dashboard session. This IS the
    "once-per-day send" scheduling hook chosen for this feature: it is self-throttling
    (``services.maybe_send_morning_report``), so it's safe -- and RECOMMENDED -- to schedule it
    to run every 15-30 minutes rather than trying to hit exactly one precise time each morning;
    the throttle collapses that into at most one routine push per calendar day, PLUS at most one
    immediate push per hour whenever the live health verdict is DOWN (so a real outage doesn't
    have to wait for morning). ``force=true`` bypasses the throttle for manual testing that
    push delivery itself works (still requires the token)."""
    _diag_gate(token)
    result = services.maybe_send_morning_report(repo, force=force)
    try:
        repo.log_event("morning_report", "info", "morning_report_send",
                       f"sent={result['sent']} reason={result['reason']} trigger={result['trigger']}",
                       {"sent": result["sent"], "verdict": result["report"]["health_verdict"]})
    except Exception:
        pass
    return result
