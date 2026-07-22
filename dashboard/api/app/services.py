"""Service helpers: status assembly, analytics, ML surfacing, alerts, data-source health.

All read through the sleepctl ``Repository`` + the dashboard tables, reusing engine logic
(config objective rules, ML recommender, phenotype). Kept in one module for v1 simplicity.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone

from sleepctl.benchmarks import NightMode, perfect_sleep_index
from sleepctl.config import AppConfig
from sleepctl.controller.sleep_plan import plan_night
from sleepctl.ml.confounders import clean_rows
from sleepctl.ml.dataset import build_feature_rows
from sleepctl.ml.model import SetpointModel
from sleepctl.ml.phenotype import correlate_with_outcome
from sleepctl.ml.recommend import recommend_action

from app import bridge
from app.config import settings

CFG = AppConfig.default()


# ------------------------------------------------------------------ sleep plan
def _wake_dt(wake_time: str | None):
    """Resolve an 'HH:MM' wake string to the next datetime it occurs."""
    from datetime import timedelta
    if not wake_time:
        return None
    try:
        hh, mm = (int(x) for x in str(wake_time).split(":"))
        if not (0 <= hh < 24 and 0 <= mm < 60):       # reject impossible clocks (e.g. "25:99")
            return None
        now = datetime.now()
        w = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    except Exception:
        return None
    if w <= now:
        w += timedelta(days=1)
    return w


def current_plan(repo):
    """Tonight's wake-aware plan from the stored wake settings + recent history."""
    rt = bridge.read_runtime_state(repo.conn, settings.runtime_stale_seconds)
    extra = rt.get("extra") or {}
    wake = extra.get("wake") or {}
    wake_dt = _wake_dt(wake.get("wake_time"))
    hint = wake.get("night_type") or "auto"
    window = wake.get("window_min") or 30
    recent = repo.recent_nights(14)
    return plan_night(datetime.now(), wake_dt, recent, hint=hint, base_window_min=window,
                      repo=repo)


def sleep_plan(repo) -> dict:
    plan = current_plan(repo)
    nights = repo.recent_nights(1)
    last_index = None
    if nights:
        # Score against the night's PERSONALIZED ideal (the same targets the plan/controller
        # chase), so the displayed score and the objective are one and the same.
        last_index = perfect_sleep_index(nights[-1], plan.mode, targets=plan.targets)
    d = plan.to_dict()
    d["last_night_index"] = last_index
    return d


def current_mode(repo) -> NightMode:
    return current_plan(repo).mode


def nap_preview(duration_min=None, wake_time=None) -> dict:
    """Preview the nap strategy (power/cycle/trap + advice) for a given length, without
    starting it — drives the Nap card's live explanation."""
    from datetime import timedelta
    from sleepctl.controller.nap import nap_strategy
    now = datetime.now()
    if wake_time:
        try:
            hh, mm = (int(x) for x in str(wake_time).split(":"))
            deadline = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
            if deadline <= now:
                deadline += timedelta(days=1)
            window = max(5, int((deadline - now).total_seconds() // 60))
        except Exception:
            window = 20
    else:
        window = int(duration_min or 20)
    return nap_strategy(window, now_hour=now.hour, cfg=CFG).to_dict()


# ------------------------------------------------------ sleep maintenance
def maintenance_summary(repo) -> dict:
    """The proactive + reactive sleep-maintenance picture: the learned awakening pattern
    used to PREVENT wakeups, plus how recent nights' awakenings were handled."""
    from sleepctl.learning.lead_time import build_lead_time_profile
    from sleepctl.ml.wake_profile import build_wake_profile
    profile = build_wake_profile(repo)
    lead = build_lead_time_profile(repo)  # also resolves pending pre-cool efficacy labels
    try:
        efficacy = repo.precool_efficacy()
    except Exception:
        efficacy = {}

    def _hhmm(m):
        return f"{m // 60:02d}:{m % 60:02d}"

    nights = repo.recent_nights(14)
    recent = [{"date": n.date, "wake_events": n.wake_events, "waso_min": n.waso_min}
              for n in nights[-7:]]
    avg_wakes = (sum((n.wake_events or 0) for n in nights) / len(nights)) if nights else None
    avg_waso = None
    wasos = [n.waso_min for n in nights if n.waso_min is not None]
    if wasos:
        avg_waso = sum(wasos) / len(wasos)
    return {
        "recurring_wake_times": [_hhmm(m) for m in profile.awakening_minutes],
        "personal_warm_threshold_f": profile.warm_temp_threshold_f,
        "avg_wake_events": round(avg_wakes, 1) if avg_wakes is not None else None,
        "avg_waso_min": round(avg_waso, 1) if avg_waso is not None else None,
        "recent": recent,
        "profile_source": profile.source,
        "response_lag_min": lead.response_lag_min,
        "lead_times_min": lead.leads,
        "lead_source": lead.source,
        "precool_efficacy": efficacy,
        "strategy": (
            "Prevent: watches for wake precursors (rising heart rate, restlessness, the bed "
            "running warm, and your recurring wake times) and pre-emptively cools in light "
            "sleep — never disturbing deep sleep. Handle: a detected awakening triggers a "
            "gentle cooling assist to re-settle you fast, then holds steady. Both are tuned "
            "to your own awakening pattern and rewarded for fewer, shorter wakeups."
        ),
    }


# ------------------------------------------------------- wake-up exit survey
def checkin_status(repo) -> dict:
    """Whether a morning check-in is due for the most recent night, + that night's
    objective benchmark score to compare the survey against."""
    nights = repo.recent_nights(1)
    if not nights:
        return {"due": False, "date": None, "last_night": None, "perfect_sleep": None}
    last = nights[-1]
    ctx = repo.get_context(last.date)
    done = ctx is not None and getattr(ctx, "subjective_quality", None) is not None
    mode = current_mode(repo)
    return {
        "due": not done,
        "date": last.date,
        "last_night": _night_brief(last),
        "perfect_sleep": perfect_sleep_index(last, mode),
    }


def submit_checkin(repo, payload: dict) -> dict:
    """Persist the wake-up survey into the night's context (so the ML reward + confounder
    handling use it) and return a felt-vs-measured comparison against the benchmarks."""
    from sleepctl.models import ContextRecord
    from sleepctl.ml.reward import night_outcome_score

    nights = repo.recent_nights(60)
    date = payload.get("date") or (nights[-1].date if nights else
                                   datetime.now().date().isoformat())
    ctx = repo.get_context(date) or ContextRecord(date=date)

    rested = payload.get("rested")
    grog = payload.get("grogginess")
    energy = payload.get("daytime_energy")
    ctx.subjective_quality = rested
    ctx.grogginess = grog
    ctx.daytime_performance = energy
    f = payload.get("factors") or {}
    if "caffeine" in f:
        ctx.caffeine = bool(f["caffeine"])
    if "alcohol" in f:
        ctx.alcohol = bool(f["alcohol"])
    if "late_work" in f:
        ctx.late_night_work = bool(f["late_work"])
    if "illness" in f:
        ctx.illness = bool(f["illness"])
    if "travel" in f:
        ctx.travel = bool(f["travel"])
    if f.get("stress"):
        ctx.stress = 8.0
    repo.save_context(ctx)

    night = next((n for n in nights if n.date == date), None)
    mode = current_mode(repo)
    comparison = {"date": date, "subjective": {
        "rested": rested, "grogginess": grog, "daytime_energy": energy,
        "awakenings_felt": payload.get("awakenings_felt"),
        "onset_feel": payload.get("onset_feel"),
    }}
    if night is not None:
        # Re-score the night with the subjective signal + mode so the ML reward reflects it.
        night.outcome_score = night_outcome_score(
            night, CFG, subjective_quality=rested, grogginess=grog, mode=mode)
        repo.save_night_summary(night)
        psi = perfect_sleep_index(night, mode)
        total = max(1.0, night.total_sleep_min or 0)
        comparison["perfect_sleep"] = psi
        comparison["objective"] = {
            "wake_events": night.wake_events,
            "sleep_efficiency": night.sleep_efficiency,
            "deep_pct": (night.deep_min or 0) / total,
            "rem_pct": (night.rem_min or 0) / total,
            "sleep_onset_latency_min": night.sleep_onset_latency_min,
            "avg_hrv": night.avg_hrv,
        }
        comparison["insights"] = _checkin_insights(payload, night, psi)
    return comparison


def _checkin_insights(payload: dict, night, psi: dict) -> list:
    """Plain-language comparisons of how the user FELT vs what was MEASURED."""
    out = []
    rested = payload.get("rested")
    grog = payload.get("grogginess")
    felt_awak = payload.get("awakenings_felt")
    onset = payload.get("onset_feel")
    score = psi.get("score", 0)

    if felt_awak is not None and night.wake_events is not None:
        if felt_awak > night.wake_events + 1:
            out.append("You remember more awakenings than were detected — light, fragmented "
                       "sleep. We'll bias toward steadier temperatures to protect maintenance.")
        elif felt_awak < night.wake_events:
            out.append("You slept through awakenings the sensors caught — good sleep depth.")
    if grog is not None and grog >= 6 and score >= 70:
        out.append("High grogginess despite a strong night suggests sleep inertia — the smart "
                   "wake will aim to catch you in lighter sleep.")
    if rested is not None and rested <= 4 and score >= 70:
        out.append("Measured sleep was strong but you feel unrested — likely a confounder "
                   "(alcohol, stress, illness). Logged so it won't skew the learning.")
    if rested is not None and rested >= 7 and score < 55:
        out.append("You feel rested even though the metrics were modest — your personal "
                   "benchmark is being learned from how you actually feel.")
    if onset == "slow":
        out.append("Slow to fall asleep — we'll cool a little faster at lights-out to speed "
                   "onset.")
    if not out:
        out.append("Logged. Your subjective rating is now part of the reward the learner "
                   "optimises — personalising the benchmarks to you.")
    return out


# ----------------------------------------------------------------------- status
def build_status(repo) -> dict:
    rt = bridge.read_runtime_state(repo.conn, settings.runtime_stale_seconds)
    nights = repo.recent_nights(1)
    last = nights[-1] if nights else None
    # Read-only: NEVER recompute the ML recommendation here (see ``cached_ml_recommendation``) --
    # build_status is hit every 5s by the SSE loop + by /status, and the real computation is an
    # O(all-history) ridge refit meant to run once per night.
    rec = cached_ml_recommendation(repo)
    extra = rt.get("extra") or {}
    return {
        "state": rt.get("state") or "IDLE",
        "objective": rt.get("objective"),
        "mode": rt.get("mode", "auto"),
        "target_temp_f": rt.get("target_temp_f"),
        "bed_temp_f": rt.get("bed_temp_f"),
        "room_temp_f": rt.get("room_temp_f"),
        "stage": rt.get("stage"),
        "confidence": rt.get("confidence"),
        "power_on": extra.get("power_on", True),
        "away": extra.get("away", False),
        "wake": extra.get("wake"),
        "target_level": rt.get("target_level"),                    # commanded device level
        "device_level": extra.get("device_level"),                 # bed's reported actual level
        "device_target_level": extra.get("device_target_level"),   # level the bed accepted
        "bed_presence": extra.get("bed_presence"),
        "device": extra.get("device"),                             # online/water/priming/alarm
        "session_mode": extra.get("session_mode"),
        "device_error": extra.get("device_error"),
        "live": extra.get("live", False),
        "daemon_alive": rt.get("daemon_alive", False),
        "stale": rt.get("stale", True),
        "updated": rt.get("updated"),
        "recommendation": {"action": rec.get("action"), "reason": rec.get("reason"),
                           "confidence": rec.get("confidence"), "mode": rec.get("mode")},
        "last_night": _last_night_brief(repo, last) if last else None,
        "alerts": _status_alerts(repo),
        "schedule": schedule_brief(repo),
    }


def _status_alerts(repo) -> list[dict]:
    """Alerts for the ``/status``/SSE payload: piggy-backs the health-monitor sync onto
    this already-frequently-polled path (see ``evaluate_and_sync_health_alerts`` for why
    this is "the least-invasive periodic check that works" — no extra process, just a
    few indexed lookups on a request that already happens every few seconds while the
    app is open). Never let a health-monitor hiccup break the core status response."""
    try:
        evaluate_and_sync_health_alerts(repo)
    except Exception:
        pass
    return active_alerts(repo)


def _last_night_brief(repo, last) -> dict:
    """Last-night summary with its mode-appropriate perfect-sleep score attached."""
    d = _night_brief(last)
    try:
        d["perfect_sleep"] = perfect_sleep_index(last, current_mode(repo))
    except Exception:
        d["perfect_sleep"] = None
    return d


def _night_brief(n) -> dict:
    return {
        "date": n.date, "total_sleep_min": n.total_sleep_min, "deep_min": n.deep_min,
        "rem_min": n.rem_min, "wake_events": n.wake_events,
        "sleep_efficiency": n.sleep_efficiency, "avg_hrv": n.avg_hrv,
        "outcome_score": n.outcome_score,
    }


def schedule_brief(repo) -> dict:
    today = datetime.now().date().isoformat()
    ctx = repo.get_context(today)
    return {
        "required_wake_time": ctx.required_wake_time.isoformat()
        if ctx and ctx.required_wake_time else None,
        "sleep_opportunity_min": getattr(ctx, "sleep_opportunity_min", None),
        "is_short_sleep_day": getattr(ctx, "is_short_sleep_day", None),
    }


# -------------------------------------------------------------------------- ml
# ``ml_recommendation`` drives ``recommend_action`` -> ``build_feature_rows`` (per-night queries
# over ALL history) -> ``SetpointModel().fit()`` (a pure-Python ridge regression) -- an
# O(all-history) refit that's meant to run once per night (see ``NightlyUpdater.run`` /
# ``sleepctl/loop/nightly.py``), NOT on every poll. ``build_status`` is hit every 5s by the SSE
# loop and by ``/status``, so it must never call this directly -- see ``cached_ml_recommendation``
# below, which it uses instead. Everything else (``/ml/recommendation``, ``/ml/overview``,
# ``/tonight`` -- all much less frequently polled) still gets a live recommendation here, and
# write-through refreshes the shared cache so ``cached_ml_recommendation`` benefits too.
_ML_REC_CACHE_KEY = "ml_recommendation_cache"


def ml_recommendation(repo, mode: NightMode | None = None) -> dict:
    profile = repo.latest_setpoints() or CFG.default_setpoints()
    if mode is None:
        try:
            mode = current_mode(repo)
        except Exception:
            mode = NightMode.NORMAL
    # The ML optimises for tonight's situation-specific benchmark (work vs off-day).
    chosen = recommend_action(repo, profile, CFG, mode=mode)
    if chosen is None:
        result = {"action": "rule-policy", "reason": "deferring to safe rule policy (insufficient "
                  "data or confidence)", "confidence": 0.0, "source": "fallback",
                  "low_confidence": True, "mode": mode.value}
    else:
        result = {
            "action": chosen.name, "reason": chosen.reason, "confidence": chosen.confidence,
            "predicted": chosen.predicted, "source": "ml", "mode": mode.value,
            "low_confidence": chosen.confidence < CFG.ml.conf_min,
        }
    _write_ml_recommendation_cache(repo, result)
    return result


def _write_ml_recommendation_cache(repo, result: dict) -> None:
    """Persist the freshly-computed recommendation so ``cached_ml_recommendation`` can serve it
    without recomputing. Repo-backed (not just a module-level dict) because the nightly refit
    that MUST refresh this runs in the daemon process, a separate OS process from the API --
    the two only share the SQLite file. Best-effort: caching must never break the caller's
    (already-computed) result."""
    import json as _json
    payload = {"result": result, "computed_at": datetime.now(timezone.utc).isoformat()}
    try:
        repo.conn.execute(
            "INSERT INTO settings_kv (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (_ML_REC_CACHE_KEY, _json.dumps(payload)),
        )
        repo.conn.commit()
    except Exception:
        pass


def cached_ml_recommendation(repo) -> dict:
    """Read-only last-computed ML recommendation -- NEVER triggers ``recommend_action`` /
    ``build_feature_rows`` / ``SetpointModel.fit()``. This is what ``build_status`` uses so the
    O(all-history) ridge refit never runs on the SSE/poll hot path; the tradeoff is the shown
    recommendation is "last computed", not "recomputed every poll" (refreshed once per night by
    the nightly close-out, and opportunistically whenever anything -- ``/ml/recommendation``,
    ``/ml/overview``, ``/tonight`` -- computes a fresh one; see ``ml_recommendation``)."""
    import json as _json
    try:
        row = repo.conn.execute(
            "SELECT value FROM settings_kv WHERE key=?", (_ML_REC_CACHE_KEY,)).fetchone()
    except Exception:
        row = None
    if row:
        try:
            cached = _json.loads(row["value"])
            result = cached.get("result")
            if result:
                return result
        except Exception:
            pass
    # Nothing computed yet (e.g. brand-new DB, no night has closed out) -- a safe default in the
    # same shape ``ml_recommendation`` returns on insufficient data, without doing any of the
    # expensive work.
    try:
        mode = current_mode(repo)
    except Exception:
        mode = NightMode.NORMAL
    return {"action": "rule-policy", "reason": "no recommendation computed yet",
            "confidence": 0.0, "source": "fallback", "low_confidence": True, "mode": mode.value}


def refresh_ml_recommendation_cache(repo, mode: NightMode | None = None) -> dict:
    """Force a fresh computation + cache write. Called (1) once per night right after
    ``NightlyUpdater.run`` closes out (see ``live_daemon._maybe_close_out``) and (2) periodically
    (every few minutes) by the API's background health-watchdog thread -- both OFF the
    request/SSE hot path, so ``build_status`` still never triggers the refit itself."""
    return ml_recommendation(repo, mode=mode)


def ml_overview(repo) -> dict:
    baselines = repo.latest_baselines()
    sp = repo.latest_setpoints()
    rows = build_feature_rows(repo)
    clean = clean_rows(rows)
    confidence = 0.0
    if len(clean) >= 3:
        confidence = SetpointModel(lam=CFG.ml.ridge_lambda).fit(clean).confidence()
    actions = [
        {"date": a.date, "action": a.action_name, "source": a.source,
         "confidence": a.confidence, "reward": a.reward_observed}
        for a in repo.recent_actions(10)
    ]
    return {
        "baselines": baselines.metrics if baselines else {},
        "setpoint": _setpoint_dict(sp) if sp else None,
        "model_confidence": confidence,
        "clean_nights": len(clean),
        "min_nights": CFG.ml.min_nights,
        "recommendation": ml_recommendation(repo),
        "actions": actions,
        "phenotype": [{"feature": f, "r": r, "n": n}
                      for f, r, n in correlate_with_outcome(repo)[:6]],
    }


def _setpoint_dict(sp) -> dict:
    return {"version": sp.version, "source": sp.source, "neutral_f": sp.neutral_f,
            "deep_bias_f": sp.deep_bias_f, "rem_warm_offset_f": sp.rem_warm_offset_f,
            "wake_ramp_f": sp.wake_ramp_f, "composite_bed_weight": sp.composite_bed_weight}


# ------------------------------------------------------------------- analytics
_METRIC_FIELDS = {
    "wake_events": "wake_events", "deep_min": "deep_min", "rem_min": "rem_min",
    "avg_hrv": "avg_hrv", "total_sleep_min": "total_sleep_min",
    "sleep_efficiency": "sleep_efficiency", "outcome_score": "outcome_score",
}


def trends(repo, metric: str, window: int = 30) -> dict:
    field = _METRIC_FIELDS.get(metric)
    if field is None:
        return {"metric": metric, "points": []}
    nights = repo.recent_nights(window)
    points = [{"date": n.date, "value": getattr(n, field)} for n in nights]
    return {"metric": metric, "points": points}


def effectiveness(repo) -> dict:
    """Mean reward by action, from the action ledger (intervention effectiveness)."""
    rows = repo.conn.execute(
        """SELECT action_name, COUNT(*) c, AVG(reward_observed) r
           FROM actions WHERE reward_observed IS NOT NULL GROUP BY action_name"""
    ).fetchall()
    return {"by_action": [{"action": x["action_name"], "n": x["c"],
                           "mean_reward": x["r"]} for x in rows]}


# ---------------------------------------------------------------------- alerts
def active_alerts(repo) -> list[dict]:
    rows = repo.conn.execute(
        "SELECT * FROM alerts WHERE acknowledged=0 ORDER BY id DESC LIMIT 20"
    ).fetchall()
    return [dict(r) for r in rows]


def generate_alerts(repo) -> int:
    """Rule-based alerts from the latest night + runtime; returns count created."""
    created = 0
    nights = repo.recent_nights(1)
    if nights:
        n = nights[-1]
        b = CFG.benchmarks
        if n.wake_events is not None and n.wake_events > b.wake_events_max:
            created += _add_alert(repo, "wake_events", "warning",
                                  f"{n.wake_events} wake events last night (target ≤{b.wake_events_max}).")
        if n.avg_hrv is not None and n.avg_hrv < b.hrv_target_ms * 0.8:
            created += _add_alert(repo, "low_hrv", "warning",
                                  f"HRV {n.avg_hrv:.0f} ms is low vs target {b.hrv_target_ms}.")
        if n.total_sleep_min is not None and n.total_sleep_min < 360:
            created += _add_alert(repo, "short_sleep", "info",
                                  f"Short sleep: {n.total_sleep_min:.0f} min.")
    rt = bridge.read_runtime_state(repo.conn, settings.runtime_stale_seconds)
    if rt.get("stale"):
        created += _add_alert(repo, "stale_data", "critical",
                              "Controller data is stale / daemon not reporting.")
    if rt.get("confidence") is not None and rt["confidence"] < CFG.ml.conf_min:
        created += _add_alert(repo, "low_confidence", "info",
                              "Model confidence low — using safe rule policy.")
    return created


def _add_alert(repo, atype: str, severity: str, message: str) -> int:
    # de-dupe: skip if an identical unacknowledged alert already exists today
    today = datetime.now().date().isoformat()
    exists = repo.conn.execute(
        "SELECT 1 FROM alerts WHERE type=? AND acknowledged=0 AND substr(ts,1,10)=? LIMIT 1",
        (atype, today),
    ).fetchone()
    if exists:
        return 0
    repo.conn.execute(
        "INSERT INTO alerts (ts, type, severity, message, acknowledged) VALUES (?,?,?,?,0)",
        (datetime.now(timezone.utc).isoformat(), atype, severity, message),
    )
    repo.conn.commit()
    return 1


# -------------------------------------------------------------- data-source health
def data_health(repo) -> dict:
    rt = bridge.read_runtime_state(repo.conn, settings.runtime_stale_seconds)
    rows = repo.conn.execute("SELECT * FROM data_sync").fetchall()
    sources = {r["source"]: dict(r) for r in rows}
    extra = rt.get("extra") or {}
    # Phone/independent-sensor freshness, so the user can confirm their iPhone is streaming and
    # being fused (movement is the trustworthy phone signal; HR/HRV are best-effort).
    phone = bridge.read_sensor_sample(repo.conn)
    if phone is not None:
        age = phone.get("age_seconds")
        in_bed = (extra.get("bed_presence") is True)
        fresh = bool(age is not None and age < 90)
        phone = {"updated": phone.get("updated"), "source": phone.get("source"),
                 "age_seconds": round(age, 1) if age is not None else None,
                 "movement": phone.get("movement"), "hr": phone.get("hr"),
                 "hrv": phone.get("hrv"),
                 "streaming": bool(age is not None and age < 120),
                 # actually fused = fresh AND the Pod senses you in bed (presence-gated).
                 "fusing": bool(extra.get("phone_fused")) or (fresh and in_bed),
                 "in_bed": in_bed}
    # Dedicated cardiac sensor (Polar Verity Sense armband) freshness — the authoritative HR/HRV
    # channel merged with the phone's movement. Metadata only; no raw biometric values leak here.
    cardiac = bridge.read_cardiac_sample(repo.conn)
    if cardiac is not None:
        c_age = cardiac.get("age_seconds")
        cardiac = {"updated": cardiac.get("updated"), "source": cardiac.get("source"),
                   "age_seconds": round(c_age, 1) if c_age is not None else None,
                   "hr": cardiac.get("hr"), "hrv": cardiac.get("hrv"),
                   "streaming": bool(c_age is not None and c_age < 120)}
    return {
        "cardiac": cardiac,
        "daemon": {"alive": rt.get("daemon_alive", False), "updated": rt.get("updated"),
                   "stale": rt.get("stale", True),
                   "live": bool(extra.get("live", False)),
                   "dry_run": bool(extra.get("dry_run", False))},
        "sources": sources,
        "phone_sensor": phone,
        "pending_commands": repo.conn.execute(
            "SELECT COUNT(*) c FROM commands WHERE status='pending'").fetchone()["c"],
    }


# ======================================================================================
# High-leverage features: predictive pre-emption, readiness, weather pre-comp, forensics,
# n-of-1 experiments. Each reads engine logic + the daemon runtime_state.
# ======================================================================================

def _hhmm_min(m: int) -> str:
    return f"{int(m) // 60:02d}:{int(m) % 60:02d}"


def preemption_status(repo) -> dict:
    """Live predictive-pre-emption state for the Tonight page."""
    rt = bridge.read_runtime_state(repo.conn, settings.runtime_stale_seconds)
    extra = rt.get("extra") or {}
    pre = extra.get("preemption") or {}
    recurring = []
    efficacy = {}
    try:
        from sleepctl.ml.wake_profile import build_wake_profile
        recurring = [_hhmm_min(m) for m in build_wake_profile(repo).awakening_minutes]
    except Exception:
        pass
    try:
        efficacy = repo.precool_efficacy()
    except Exception:
        pass
    steer = extra.get("steering") or {}
    steer_efficacy = {}
    try:
        steer_efficacy = repo.steer_efficacy()
    except Exception:
        pass
    return {
        "preempting": bool(pre.get("preempting", False)),
        "wake_risk": pre.get("wake_risk"),
        "risk_reasons": pre.get("risk_reasons", []),
        "precursor_score": pre.get("precursor_score"),
        "precursor_reasons": pre.get("precursor_reasons", []),
        "recurring_wake_times": recurring,
        "precool_efficacy": efficacy,
        # In-night architecture steering ("nudge me deeper"): live maneuver + how far off the
        # ideal deep curve we are, plus the learned per-maneuver deepen/wake rates.
        "steering": {
            "active": bool(steer.get("active", False)),       # nudging deeper (acquire)
            "defending": bool(steer.get("defending", False)),  # holding a favorable state
            "maneuver": steer.get("maneuver", "hold"),
            "deep_deficit_min": steer.get("deep_deficit_min"),
            "deep_min_so_far": steer.get("deep_min_so_far"),
            "rem_min_so_far": steer.get("rem_min_so_far"),
            "reason": steer.get("reason"),
            "efficacy": steer_efficacy,
        },
        "stale": rt.get("stale", True),
    }


def morning_readiness_summary(repo) -> dict:
    """Composite morning readiness / clinical-safety score for the Home page."""
    from sleepctl.readiness import morning_readiness
    nights = repo.recent_nights(14)
    if not nights:
        return {"available": False}
    last = nights[-1]
    mode = current_mode(repo)
    hrvs = sorted(n.avg_hrv for n in nights[:-1] if n.avg_hrv is not None)
    baseline = hrvs[len(hrvs) // 2] if hrvs else None
    # Score against the user's revealed-preference personalized weights (evidence prior when thin).
    from sleepctl.learning.perfect_weights import personalized_targets
    tgt = personalized_targets(repo, mode)
    out = morning_readiness(last, nights, mode, baseline_hrv=baseline, targets=tgt).to_dict()
    out["available"] = True
    out["date"] = last.date
    out["mode"] = mode.value
    return out


def wake_catalog(repo) -> dict:
    """Each recent mid-sleep awakening with the converging-signal vector that flagged it — the
    record for honing the personalized wake-trajectory."""
    from sleepctl.controller.sleep_wake import catalog_awakening_signals
    return {"awakenings": catalog_awakening_signals(repo)}


def perfect_weights_view(repo) -> dict:
    """The user's personalized perfect-sleep weights vs the evidence prior, per mode — so the
    objective the controller optimizes toward is visible and explainable."""
    from sleepctl.benchmarks import NightMode, targets_for
    from sleepctl.learning.ideal_architecture import is_personalized, learn_ideal_architecture
    from sleepctl.learning.perfect_weights import learn_perfect_weights
    out = {"active_mode": current_mode(repo).value, "modes": {}}
    for mode in NightMode:
        t = targets_for(mode)
        prior = t.weights
        learned = learn_perfect_weights(repo, mode)
        lvl = learn_ideal_architecture(repo, mode)        # learned from the morning survey
        out["modes"][mode.value] = {
            # the EVIDENCE targets (the literature "what good looks like" for this mode)
            "targets": {
                "deep_pct": [round(t.deep_pct_min, 3), round(t.deep_pct_ideal, 3)],
                "rem_pct": [round(t.rem_pct_min, 3), round(t.rem_pct_ideal, 3)],
                "efficiency_min": round(t.efficiency_min, 3),
                "sol_max_min": t.sol_max_min,
                "waso_max_min": t.waso_max_min,
                "awakenings_max": t.awakenings_max,
                "total_sleep_target_min": t.total_sleep_target_min,
            },
            # YOUR learned ideal architecture (deep/REM levels), from the heavily-weighted morning
            # subjective survey, shrunk to + bounded around the evidence prior.
            "learned_ideal": {
                "deep_pct": [lvl["deep_pct_min"], lvl["deep_pct_ideal"]],
                "rem_pct": [lvl["rem_pct_min"], lvl["rem_pct_ideal"]],
                "is_personalized": is_personalized(lvl, mode),
            },
            "prior": {k: round(v, 4) for k, v in prior.items()},
            "personalized": learned,
            "is_personalized": learned != prior,
            "rationale": t.rationale,
        }
    return out


def weather_forecast(repo) -> dict:
    """Environmental pre-compensation: overnight forecast + feed-forward bias."""
    rt = bridge.read_runtime_state(repo.conn, settings.runtime_stale_seconds)
    pc = (rt.get("extra") or {}).get("precompensation")
    if pc and pc.get("trend") is not None:
        return {"source": "daemon", **pc}
    try:
        from sleepctl.adapters.weather import OpenMeteoWeather
        from sleepctl.precompensation import compute_precompensation
        w = OpenMeteoWeather()
        fc = w.overnight_forecast()
        out = compute_precompensation(fc, CFG)
        out["forecast"] = fc
        out["source"] = "on_demand"
        return out
    except Exception:
        return {"bias_f": 0.0, "pre_cool": False, "trend": None,
                "reason": "forecast unavailable", "source": "error"}


def awakening_forensics_summary(repo, limit: int = 20) -> dict:
    from sleepctl.forensics import awakening_forensics, forensics_summary, suggest_experiment
    profile = None
    try:
        from sleepctl.ml.wake_profile import build_wake_profile
        profile = build_wake_profile(repo)
    except Exception:
        pass
    events = awakening_forensics(repo, limit=limit, profile=profile)
    summary = forensics_summary(events)
    return {"events": events, "summary": summary,
            "suggested_experiment": suggest_experiment(summary)}


def experiments_list(repo) -> dict:
    from sleepctl.experiments import list_experiments
    return {"experiments": list_experiments(repo)}


def experiment_create(repo, spec: dict) -> dict:
    from sleepctl.experiments import create_experiment
    return create_experiment(repo, spec)


def experiment_templates(repo) -> dict:
    """Curated one-tap n-of-1 templates + a power estimate for each (vs the user's own variance)."""
    from sleepctl.experiment_templates import estimate_nights_needed, list_templates
    out = []
    for t in list_templates():
        effect = 1.0 if t["metric"] == "wake_events" else 5.0
        t = dict(t)
        t["power"] = estimate_nights_needed(repo, t["metric"], target_effect=effect)
        out.append(t)
    return {"templates": out}


def experiment_from_template(repo, key: str, period=None, washout=None) -> dict:
    from sleepctl.experiment_templates import create_from_template
    return create_from_template(repo, key, period=period, washout=washout)


def experiment_analyze(repo, exp_id: int) -> dict:
    from sleepctl.experiments import analyze_experiment, get_experiment
    exp = get_experiment(repo, exp_id)
    if exp is None:
        return {"error": "not found"}
    return {"experiment": exp, "analysis": analyze_experiment(repo, exp_id)}


def experiment_stop(repo, exp_id: int) -> dict:
    from sleepctl.experiments import stop_experiment
    out = stop_experiment(repo, exp_id)
    return out or {"error": "not found"}


# ------------------------------------------------------------------ standing efficacy trial
# "Does the controller help?" — the standing CONTROLLED vs HELD (do-no-harm neutral baseline)
# comparison. Opt-in (default OFF); see sleepctl.eval.efficacy for the arm-assignment/analysis
# logic this just exposes over HTTP.

def efficacy_status(repo) -> dict:
    from sleepctl.eval.efficacy import (
        analyze_efficacy, backfill_from_nightly_summaries, get_efficacy_config)
    # Best-effort catch-up: resolve any efficacy_nights row whose night has since produced a
    # nightly_summaries row but wasn't recorded at close-out (e.g. daemon restarted mid-flush,
    # or the simulator daemon, which has no nightly close-out wiring at all).
    try:
        backfill_from_nightly_summaries(repo)
    except Exception:
        pass
    return {"config": get_efficacy_config(repo), "analysis": analyze_efficacy(repo)}


def efficacy_config_view(repo) -> dict:
    from sleepctl.eval.efficacy import get_efficacy_config
    return get_efficacy_config(repo)


def efficacy_config_update(repo, values: dict) -> dict:
    from sleepctl.eval.efficacy import set_efficacy_config
    return set_efficacy_config(repo, values)


# ------------------------------------------------------------------ phone/iPhone BCG ingest
import threading  # noqa: E402

# A rolling BCG processor fed by the phone's accelerometer stream. Module-level so batches
# POSTed every few seconds accumulate into a window long enough for HRV; guarded by a lock
# because FastAPI dispatches sync handlers across threadpool threads. (Single-worker deploy.)
_BCG_LOCK = threading.Lock()
_BCG_STATE: dict = {"proc": None, "fs": None}


def _bcg_processor(fs: float):
    """Lazily build/rebuild the rolling processor; rebuild only if the sample rate changes
    meaningfully (>5%) — the window length is fs-dependent, but small jitter in an auto-detected
    rate shouldn't keep clearing the accumulated buffer."""
    from sleepctl.adapters.bcg import BCGProcessor
    st = _BCG_STATE
    cur = st["fs"]
    if st["proc"] is None or cur is None or abs(fs - cur) / cur > 0.05:
        st["proc"] = BCGProcessor(fs=fs)
        st["fs"] = fs
    return st["proc"]


def _fs_from_times(times_ns: list) -> "float | None":
    """Estimate the sample rate (Hz) from a batch of per-sample UTC-epoch-nanosecond timestamps
    (Sensor Logger's ``time`` field), so the user never has to hand-match it. None if unusable."""
    ts = sorted(float(t) for t in times_ns if t is not None)
    if len(ts) < 2:
        return None
    span_s = (ts[-1] - ts[0]) / 1e9
    if span_s <= 0:
        return None
    fs = (len(ts) - 1) / span_s
    return fs if 1.0 <= fs <= 1000.0 else None


def _get_gym_config(repo):
    from sleepctl.gym_advisor import GymConfig
    row = repo.conn.execute("SELECT value FROM settings_kv WHERE key='gym_config'").fetchone()
    import json as _json
    return GymConfig.from_dict(_json.loads(row["value"]) if row else None)


def gym_config_view(repo) -> dict:
    return {"config": _get_gym_config(repo).to_dict()}


def gym_config_update(repo, values: dict) -> dict:
    import json as _json
    from sleepctl.gym_advisor import GymConfig
    merged = {**_get_gym_config(repo).to_dict(), **(values or {})}
    cfg = GymConfig.from_dict(merged)            # validates/clamps (e.g. lean)
    repo.conn.execute("INSERT INTO settings_kv (key, value) VALUES ('gym_config', ?) "
                      "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                      (_json.dumps(cfg.to_dict()),))
    repo.conn.commit()
    return {"config": cfg.to_dict()}


def _get_shift_config(repo) -> dict:
    """Next-shift hint stored in settings_kv — either typed by hand or auto-synced from the work
    calendar (see ``sync_calendar_to_shift``). ``source`` distinguishes the two so a manual entry
    is never silently overwritten once the calendar is turned off."""
    import json as _json
    row = repo.conn.execute("SELECT value FROM settings_kv WHERE key='shift_config'").fetchone()
    d = _json.loads(row["value"]) if row else {}
    return {"enabled": bool(d.get("enabled", False)),
            "next_shift": d.get("next_shift"),               # ISO datetime of the shift start
            "kind": d.get("kind", "night"),                  # 'night' | 'call' | 'day'
            "source": d.get("source", "manual"),             # 'manual' | 'calendar'
            "shift_end": d.get("shift_end")}                 # ISO datetime of the shift end


def shift_config_view(repo) -> dict:
    return _get_shift_config(repo)


def shift_config_update(repo, values: dict) -> dict:
    import json as _json
    merged = {**_get_shift_config(repo), **(values or {})}
    if merged.get("kind") not in ("night", "call", "day"):
        merged["kind"] = "night"
    if merged.get("source") not in ("manual", "calendar"):
        merged["source"] = "manual"
    # A manual edit (no explicit source="calendar" in this update) takes the config back under
    # the user's control, so the next calendar sync won't silently overwrite it unless the
    # calendar legitimately produces a new next-shift event.
    if "source" not in (values or {}) and any(k in (values or {}) for k in
                                              ("enabled", "next_shift", "kind")):
        merged["source"] = "manual"
    repo.conn.execute("INSERT INTO settings_kv (key, value) VALUES ('shift_config', ?) "
                      "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                      (_json.dumps(merged),))
    repo.conn.commit()
    return merged


def sync_calendar_to_shift(repo) -> dict:
    """Bridge the OAuth-free calendar feed into the shift planner: the resident dedicates ONE
    calendar to work shifts only, so each event IS the shift itself (start->end), not a separate
    "first commitment of the day". When the calendar is ENABLED, take the next upcoming shift
    event, classify it day/night by its start hour, and write it into ``shift_config`` (tagged
    ``source: "calendar"`` so it's clearly distinguishable from a manually-typed hint). When the
    calendar is DISABLED, this is a no-op — any manually-set shift_config (source != "calendar")
    is left completely alone, so turning the calendar off never clobbers a manual override.

    Returns the (possibly updated) shift_config dict."""
    from sleepctl.adapters.calendar import classify_shift, upcoming_events
    cal_cfg = _get_calendar_config(repo)
    if not cal_cfg["enabled"]:
        return _get_shift_config(repo)

    src = _get_ics_source(repo)
    if src is None:
        return _get_shift_config(repo)

    events = src.refresh(force=False)
    upcoming = upcoming_events(events)
    if not upcoming:
        return _get_shift_config(repo)

    nxt = upcoming[0]
    kind = classify_shift(nxt.start)
    return shift_config_update(repo, {
        "enabled": True,
        "next_shift": nxt.start.isoformat(),
        "kind": kind,
        "source": "calendar",
        "shift_end": nxt.end.isoformat() if nxt.end else None,
    })


def calendar_effective_wake(repo, for_night_date=None):
    """The auto-wake target implied by the next calendar shift, or None.

    DAY shift: return ``shift_start - shift_prep_buffer_min`` (time to get up, ready, and out
    the door before the shift starts). NIGHT shift: return None — no morning alarm; the
    daytime-sleep/banking side of the shift planner (``plan_shift_sleep``) handles those. Also
    None when the calendar is disabled/unconfigured or there's no upcoming shift."""
    from datetime import datetime, timedelta
    cal_cfg = _get_calendar_config(repo)
    if not cal_cfg["enabled"]:
        return None
    cfg = _get_shift_config(repo)
    if not cfg.get("enabled") or not cfg.get("next_shift") or cfg.get("source") != "calendar":
        return None
    if cfg.get("kind") == "night":
        return None
    try:
        start = datetime.fromisoformat(cfg["next_shift"])
    except Exception:
        return None
    buffer_min = CFG.tunables.shift_prep_buffer_min
    return start - timedelta(minutes=buffer_min)


def shift_plan_view(repo) -> dict:
    """The strategic cross-shift sleep plan: live debt, tonight's target, banking before a night
    block, prophylactic/recovery/anchor naps, and safety warnings. Computed on demand from the
    user's recent nights + the next shift (auto-synced from the work calendar when connected,
    else the manual next-shift hint)."""
    from datetime import datetime, timedelta

    from sleepctl.shift_manager import Shift, plan_shift_sleep
    sync_calendar_to_shift(repo)
    cfg = _get_shift_config(repo)
    shifts = []
    if cfg["enabled"] and cfg["next_shift"]:
        try:
            start = datetime.fromisoformat(cfg["next_shift"])
            end = None
            if cfg.get("shift_end"):
                try:
                    end = datetime.fromisoformat(cfg["shift_end"])
                except Exception:
                    end = None
            shifts = [Shift(start=start, end=end or start + timedelta(hours=12), kind=cfg["kind"])]
        except Exception:
            shifts = []
    plan = plan_shift_sleep(repo.recent_nights(14), shifts, datetime.now())
    out = plan.to_dict()
    out["shift_enabled"] = cfg["enabled"]
    out["next_shift"] = cfg["next_shift"] if cfg["enabled"] else None
    out["next_shift_kind"] = cfg["kind"]
    out["next_shift_source"] = cfg.get("source", "manual") if cfg["enabled"] else None
    out["shift_end"] = cfg.get("shift_end") if cfg["enabled"] else None
    recommended_wake = None
    if cfg["enabled"] and cfg["kind"] != "night":
        wake = calendar_effective_wake(repo)
        recommended_wake = wake.isoformat() if wake else None
    out["recommended_wake"] = recommended_wake
    return out


def gym_advice(repo) -> dict:
    """Tonight/this-morning's GO-train vs SLEEP-IN call, from the stored gym config + the user's
    own recent sleep (debt, recovery) and typical bedtime. Uses live in-bed onset when the daemon
    has it; otherwise plans from the median recent bedtime."""
    from datetime import datetime, timedelta

    from sleepctl.gym_advisor import gym_decision
    cfg = _get_gym_config(repo)
    now = datetime.now()
    recent = repo.recent_nights(14)
    last_night = recent[0] if recent else None

    # normal wake time: the stored wake setting, else 07:00 tomorrow.
    rt = bridge.read_runtime_state(repo.conn, settings.runtime_stale_seconds)
    wake_hhmm = ((rt.get("extra") or {}).get("wake") or {}).get("wake_time") or "07:00"
    try:
        hh, mm = (int(x) for x in str(wake_hhmm).split(":"))
        if not (0 <= hh < 24 and 0 <= mm < 60):       # reject impossible clocks
            hh, mm = 7, 0
    except Exception:
        hh, mm = 7, 0
    normal_wake = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if normal_wake <= now:
        normal_wake += timedelta(days=1)

    # planned bedtime: median recent bedtime-of-day, projected onto tonight.
    planned_bed = None
    beds = [n.bedtime for n in recent if getattr(n, "bedtime", None)]
    if beds:
        mins = sorted((b.hour * 60 + b.minute) for b in beds)
        med = mins[len(mins) // 2]
        planned_bed = now.replace(hour=med // 60, minute=med % 60, second=0, microsecond=0)
        if planned_bed > normal_wake:
            planned_bed -= timedelta(days=1)

    baseline_hrv = None
    hrvs = sorted(float(n.avg_hrv) for n in recent if getattr(n, "avg_hrv", None))
    if hrvs:
        baseline_hrv = hrvs[len(hrvs) // 2]

    day_demanding = bool((rt.get("extra") or {}).get("shift_plan", {}) and
                         (rt.get("extra") or {}).get("is_short_sleep_day"))

    d = gym_decision(now, normal_wake, recent, cfg=cfg, planned_bedtime=planned_bed,
                     last_night=last_night, baseline_hrv=baseline_hrv,
                     day_demanding=day_demanding)
    out = d.to_dict()
    out["enabled"] = cfg.enabled
    return out


def _get_hue_config(repo) -> dict:
    import json as _json
    row = repo.conn.execute("SELECT value FROM settings_kv WHERE key='hue_config'").fetchone()
    d = _json.loads(row["value"]) if row else {}
    ids = d.get("target_ids")
    if ids is None and d.get("target_id"):     # migrate the old single-target shape
        ids = [d["target_id"]]
    return {"enabled": bool(d.get("enabled", False)), "bridge_ip": d.get("bridge_ip"),
            "token": d.get("token"), "target_ids": ids or [],
            "therapy_ids": d.get("therapy_ids") or [],   # smart-plug light ids (10k-lux lamp)
            "kind": d.get("kind", "lights")}


def hue_config_view(repo) -> dict:
    c = _get_hue_config(repo)
    return {"enabled": c["enabled"], "bridge_ip": c["bridge_ip"], "target_ids": c["target_ids"],
            "therapy_ids": c["therapy_ids"], "kind": c["kind"],
            "paired": bool(c["token"])}     # token never returned to the client


def hue_config_update(repo, values: dict) -> dict:
    import json as _json
    cur = _get_hue_config(repo)
    for k in ("enabled", "bridge_ip", "target_ids", "therapy_ids", "kind", "token"):
        if k in values and values[k] is not None:
            cur[k] = values[k]
    repo.conn.execute("INSERT INTO settings_kv (key, value) VALUES ('hue_config', ?) "
                      "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (_json.dumps(cur),))
    repo.conn.commit()
    return hue_config_view(repo)


def hue_discover() -> dict:
    from sleepctl.adapters import hue
    return {"bridges": hue.discover_bridges()}


def hue_pair(repo, bridge_ip: str | None = None) -> dict:
    """Press-the-link-button pairing. Stores the token (server-side only) on success."""
    from sleepctl.adapters import hue
    if not bridge_ip:
        bridges = hue.discover_bridges()
        bridge_ip = bridges[0]["internalipaddress"] if bridges else None
    if not bridge_ip:
        return {"ok": False, "error": "no Hue bridge found on the network"}
    try:
        token = hue.create_token(bridge_ip)
    except Exception as exc:
        return {"ok": False, "error": str(exc), "bridge_ip": bridge_ip}
    hue_config_update(repo, {"bridge_ip": bridge_ip, "token": token})
    return {"ok": True, "bridge_ip": bridge_ip, "paired": True}


def hue_lights(repo) -> dict:
    from sleepctl.adapters import hue
    c = _get_hue_config(repo)
    if not (c["bridge_ip"] and c["token"]):
        return {"error": "not paired"}
    return hue.list_targets(c["bridge_ip"], c["token"])


def hue_test(repo, level: float = 0.8) -> dict:
    from sleepctl.adapters import hue
    c = _get_hue_config(repo)
    if not (c["bridge_ip"] and c["token"] and c["target_ids"]):
        return {"ok": False, "error": "set a bridge, pair, and pick your light(s) first"}
    ok = hue.apply(c["bridge_ip"], c["token"], c["target_ids"], level, c["kind"])
    return {"ok": ok}


def gym_effective_wake(repo, normal_wake):
    """The deadline the daemon should actually arm given the gym advisor: earlier on a GO call,
    otherwise the user's set wake time. Safe no-op (returns normal_wake) when gym is off."""
    from datetime import datetime, timedelta

    from sleepctl.gym_advisor import gym_decision, wake_target_from_decision
    cfg = _get_gym_config(repo)
    if not cfg.enabled:
        return normal_wake
    recent = repo.recent_nights(14)
    beds = [n.bedtime for n in recent if getattr(n, "bedtime", None)]
    planned = None
    if beds:
        mins = sorted(b.hour * 60 + b.minute for b in beds)
        med = mins[len(mins) // 2]
        planned = normal_wake.replace(hour=med // 60, minute=med % 60, second=0, microsecond=0)
        if planned > normal_wake:
            planned -= timedelta(days=1)
    d = gym_decision(datetime.now(), normal_wake, recent, cfg=cfg, planned_bedtime=planned,
                     last_night=recent[0] if recent else None)
    return wake_target_from_decision(d, normal_wake, cfg.early_offset_min)


def backtest_summary(nights: int = 8, scenario: str = "normal") -> dict:
    """Run the validation backtest on demand (controller vs no-control on the response-aware
    model) so the dashboard can show the closed loop is working + safe before trusting it live."""
    from sleepctl.eval.backtest import backtest
    rep = backtest(nights=nights, scenario=scenario)
    d = rep["delta"]
    rep["improved"] = bool(d["wake_events"] < 0 and d["outcome_score"] > 0)
    return rep


def wake_tuning_view(repo) -> dict:
    """The alarm's learned-to-you settings (window + lift bar + thermal maneuver) from your
    grogginess check-ins."""
    from sleepctl.config import AppConfig
    from sleepctl.learning.thermal_wake import learn_thermal_wake, thermal_wake_records
    from sleepctl.learning.wake_tuning import learn_wake_tuning, wake_tuning_records
    cfg = AppConfig.default()
    out = learn_wake_tuning(wake_tuning_records(repo), base_window=cfg.tunables.wake_window_min).to_dict()
    out["thermal"] = learn_thermal_wake(thermal_wake_records(repo),
                                        base_f=cfg.tunables.wake_ramp_temp_f).to_dict()
    return out


def _hhmm_delta_min(earlier: str, later: str) -> int:
    """Minutes that ``earlier`` precedes ``later`` (both 'HH:MM'), wrapping a day. Best-effort."""
    try:
        eh, em = (int(x) for x in earlier.split(":"))
        lh, lm = (int(x) for x in later.split(":"))
        d = (lh * 60 + lm) - (eh * 60 + em)
        return d if d >= 0 else d + 1440
    except Exception:
        return 0


def wake_readiness(repo, normal: str | None, effective: str | None) -> dict:
    """Post-wake guidance grounded in the sleep-inertia literature, tuned to a resident who may
    do something safety-critical right after waking.

    Inertia is worst waking out of deep sleep and near the core-temperature trough — i.e. when
    you wake well before your habitual time or deep in sleep debt — and usually clears within
    ~30 min (Tassi & Muzet 2000, doi:10.1053/smrv.2000.0098). A 100 mg caffeine dose at wake
    measurably shortens it, kicking in from ~10–20 min (Newman 2013,
    doi:10.2466/29.22.25.PMS.116.1.280-293)."""
    from sleepctl.benchmarks import sleep_debt_min
    debt = 0.0
    try:
        debt = float(sleep_debt_min(repo.recent_nights(14)))
    except Exception:
        pass
    early_min = _hhmm_delta_min(effective, normal) if (normal and effective) else 0

    # Readiness buffer before anything critical: a 15-min floor (typical inertia), widened when
    # waking far before habitual time or in heavy debt (deeper sleep -> stronger inertia).
    buffer_min = 15
    reasons = []
    if early_min >= 45:
        buffer_min = 30
        reasons.append(f"~{early_min} min before your usual wake (near your core-temp low)")
    elif early_min >= 20:
        buffer_min = 25
        reasons.append(f"~{early_min} min earlier than usual")
    if debt >= 240:
        buffer_min = max(buffer_min, 30)
        reasons.append(f"~{round(debt / 60, 1)} h sleep debt (deeper sleep, stronger inertia)")
    note = ("Expect a groggier wake — " + "; ".join(reasons) +
            f". Allow ~{buffer_min} min before anything safety-critical."
            if reasons else
            "Low expected grogginess — a normal-timed, well-rested wake.")

    strong = bool(early_min >= 20 or debt >= 240)
    caffeine = {
        "recommend": True,
        "dose_mg": 100,
        "onset": "~10–20 min",
        "strength": "stronger" if strong else "optional",
        "note": ("100 mg caffeine the moment you're up clears inertia fastest"
                 + (" — worth it on a short/early wake like this." if strong
                    else "; skip it if you're heading back to bed soon.")),
    }
    return {"buffer_min": buffer_min, "minutes_earlier_than_usual": early_min,
            "sleep_debt_min": round(debt), "note": note, "caffeine": caffeine}


def wake_plan(repo) -> dict:
    """The unified smart-alarm plan: the gym-aware effective wake time + the smart-wake window
    and silent escalation ladder, plus the live wake-action if the daemon is mid-wake. This is
    where the gym advisor and the smart alarm meet — GO moves the alarm earlier."""
    from sleepctl.controller.wake_orchestrator import WakeConfig
    cfg = _get_gym_config(repo)
    adv = gym_advice(repo)
    wc = WakeConfig.from_tunables(CFG.tunables)
    normal = adv.get("normal_wake_time")
    effective = (adv.get("early_wake_time") if adv.get("recommend") == "go"
                 and adv.get("early_wake_time") else normal)
    rt = bridge.read_runtime_state(repo.conn, settings.runtime_stale_seconds)
    extra = rt.get("extra") or {}
    live = extra.get("wake_action")
    # Prefer the per-night window the daemon actually chose (context-adaptive); else the default.
    chosen_window = ((extra.get("wake") or {}).get("window_min")) or wc.window_min
    hue = _get_hue_config(repo)
    return {
        "gym_enabled": cfg.enabled,
        "recommend": adv.get("recommend"),
        "normal_wake": normal,
        "effective_wake": effective,
        "moved_earlier": bool(effective and normal and effective != normal),
        "smart_window_min": chosen_window,
        "thermal_dawn_min": wc.thermal_dawn_min,
        "silent_only": wc.silent_only,
        "vibration_ladder": [wc.gentle_vibration, wc.strong_vibration, wc.max_vibration],
        "headline": adv.get("headline") if cfg.enabled else None,
        "live": live,
        # Hue lights ride the same wake logic: a sunrise ramp through the dawn window + a bright
        # therapy lamp that snaps on at the wake moment (keyed off the orchestrator's should_wake).
        "dawn_light": {
            "enabled": bool(hue["enabled"]),
            "sunrise": bool(hue["enabled"] and hue["target_ids"]),
            "therapy": bool(hue["enabled"] and hue["therapy_ids"]),
            "dawn_ramp_min": wc.thermal_dawn_min,
            "post_wake_hold_min": wc.post_wake_light_min,   # bright dose held this long past wake
        },
        # Opt-in post-wake cool snap: surfaced as an available option but NOT yet active (the
        # cooling maneuver isn't wired). `active` stays False until implemented even if enabled.
        "cold_snap": {
            "available": True,
            "enabled": bool(wc.cold_snap_enabled),
            "active": False,
            "note": "Planned: a brief cool drop right after wake to shake off sleepiness "
                    "(suits a hot sleeper). Not wired up yet — toggle is a placeholder.",
        },
        # Post-wake readiness: inertia buffer + caffeine timing, grounded in the literature.
        "readiness": wake_readiness(repo, normal, effective),
        "learned": wake_tuning_view(repo),    # personalized window + lift bar from your grogginess
    }


def learning_phases(repo) -> dict:
    """The unified picture of what the controller has learned, per sleep PHASE
    (onset / maintenance / wake), so the user can watch all three converge over months.

    Each phase reports its learned value, whether it's personalized yet, the nights of data, and a
    plain-language rationale — and the thermal learners are reported per night-MODE (normal vs short
    vs recovery) since the optimum differs by constraint."""
    from sleepctl.config import AppConfig
    from sleepctl.learning.onset_tuning import learn_onset, onset_records
    from sleepctl.learning.settle import learn_settle_nudge
    from sleepctl.learning.thermal_wake import learn_thermal_wake, thermal_wake_records
    from sleepctl.learning.wake_tuning import learn_wake_tuning, wake_tuning_records
    cfg = AppConfig.default()
    t = cfg.tunables
    modes = ["normal", "constrained", "recovery"]

    onset_recs = onset_records(repo)
    wake_recs = wake_tuning_records(repo)
    thermal_recs = thermal_wake_records(repo)

    def per_mode(learn_fn, recs, **kw):
        out = {"pooled": learn_fn(recs, **kw).to_dict()}
        for m in modes:
            out[m] = learn_fn(recs, mode=m, **kw).to_dict()
        return out

    settle = learn_settle_nudge(repo, cfg)
    try:
        eff = repo.precool_efficacy() or {}
        precool_n = sum(int(v.get("n", 0) or 0) for v in eff.values())
    except Exception:
        precool_n = 0

    # Architecture-steering learners (in-night): the deepen + lighten causal response policies, and
    # the personalized awakening-precursor trajectory that drives earlier pre-emption.
    from sleepctl.learning.deepening import (
        deepening_records, learn_deepening, lightening_records, learn_lightening)
    from sleepctl.learning.wake_causation import awakening_precursor_profile, wake_causation_audit
    deepen_pol = {"pooled": learn_deepening(deepening_records(repo)).to_dict()}
    for m in modes:
        deepen_pol[m] = learn_deepening(deepening_records(repo), mode=m).to_dict()
    lighten_pol = learn_lightening(lightening_records(repo)).to_dict()
    precursor = awakening_precursor_profile(repo)
    wake_audit = wake_causation_audit(repo)

    return {
        "onset": {
            "label": "Going to sleep",
            "knob": "induction warmth",
            "per_mode": per_mode(learn_onset, onset_recs, base_f=t.onset_warm_nudge_f),
            "n": len(onset_recs),
        },
        "maintenance": {
            "label": "Staying asleep",
            "knob": "settle nudge + pre-cool",
            "settle_nudge_f": round(settle, 2),
            "settle_direction": ("cooler" if settle < 0 else "warmer" if settle > 0 else "neutral"),
            "precool_events": precool_n,
            "is_personalized": bool(precool_n >= 6),
            # the learned trajectory that PREDICTS your awakenings (drives earlier pre-emption)
            "awakening_precursors": precursor,
            # the failure-mode audit: which adjustments wake you, base-rate-controlled
            "wake_causation_audit": wake_audit,
        },
        "architecture": {
            "label": "Right depth (in-night steering)",
            "knob": "deepen / lighten thermal nudge",
            # does cool-to-deepen actually move YOUR architecture, learned via the n-of-1 control?
            "deepening": deepen_pol,
            "lightening": lighten_pol,
        },
        "wake": {
            "label": "Waking up",
            "knob": "window + lift bar + wake ramp",
            "window_per_mode": per_mode(learn_wake_tuning, wake_recs,
                                        base_window=t.wake_window_min),
            "thermal_per_mode": per_mode(learn_thermal_wake, thermal_recs,
                                         base_f=t.wake_ramp_temp_f),
            "n": len(wake_recs),
        },
    }


def bcg_should_record(repo) -> dict:
    """Whether the phone should be recording right now, driven by the Pod's bed presence (so an
    optional iOS Shortcuts automation can poll this and start/stop Sensor Logger on bed-in/out).

    record=True while the Pod senses you in bed (or presence is unknown but the daemon is live
    and powered — fail-open so we never miss data); False once you've left the bed."""
    rt = bridge.read_runtime_state(repo.conn, settings.runtime_stale_seconds)
    extra = rt.get("extra") or {}
    presence = extra.get("bed_presence")
    powered = bool(extra.get("power_on", True)) and not rt.get("stale", True)
    if presence is True:
        record = True
    elif presence is False:
        record = False
    else:  # unknown presence: record while the daemon is live + powered
        record = powered
    return {"record": bool(record), "presence": presence,
            "daemon_alive": rt.get("daemon_alive", False), "stale": rt.get("stale", True)}


# A batch is normally ~1s of phone accel at 50Hz (~50 samples); this is a generous ceiling
# (~400s at 50Hz) that still rejects a malicious/misbehaving client trying to hand us an
# unbounded array (memory blowup / worker stall). Mirrored by the ``Field(max_length=...)``
# caps on ``BCGBody`` in ``app/main.py`` -- kept here too since ``ingest_bcg`` can in principle
# be called with a raw dict that never passed through that model.
BCG_MAX_SAMPLES = 20_000


def _finite_or_none(x):
    """``x`` if it's a real (non-NaN/Inf) number, else ``None`` -- so a corrupt/adversarial
    non-finite value can never reach a persisted column (sensor_samples / live_sensor)."""
    try:
        return x if x is not None and math.isfinite(x) else None
    except TypeError:
        return None


def _rmssd(rr_ms: list) -> "float | None":
    """RMSSD (ms) — root-mean-square of successive RR-interval differences, the standard
    short-window HRV metric. Needs ≥2 usable intervals; returns None otherwise / if non-finite."""
    clean = [float(x) for x in rr_ms
             if isinstance(x, (int, float)) and math.isfinite(x) and 250.0 <= float(x) <= 2000.0]
    if len(clean) < 2:
        return None
    diffs = [clean[i + 1] - clean[i] for i in range(len(clean) - 1)]
    val = math.sqrt(sum(d * d for d in diffs) / len(diffs))
    return val if math.isfinite(val) else None


def ingest_hr(repo, payload: dict) -> dict:
    """Ingest a cardiac batch from a DEDICATED BLE HR sensor — e.g. a Polar Verity Sense armband
    forwarded by ``scripts/verity_forwarder.py``: an instantaneous ``hr`` (bpm) plus optional
    beat-to-beat ``rr`` intervals (milliseconds). Computes HRV (RMSSD) from the RR intervals and
    publishes to the bridge's cardiac channel (``live_cardiac``), which ``read_fused_sensor``
    treats as the AUTHORITATIVE HR/HRV source and MERGES with — never clobbering — the phone
    accelerometer's movement. Also appends to the ``sensor_samples`` history (source-tagged) for
    later model training. Zero device risk: an independent sensor; the Pod is never touched.

    Accepted body (query ``?token=`` + ``?source=`` optional, header-less friendly):
      {"hr": 58, "rr": [1010, 1032, 998, ...], "source": "verity"}
    ``hr`` and ``rr`` are both optional individually but at least one must yield a value; with RR
    but no HR, HR is derived from the mean RR interval."""
    source = payload.get("source") or "verity"
    rr = payload.get("rr") or []
    if len(rr) > BCG_MAX_SAMPLES:
        return {"ok": False, "error": f"rr batch too large ({len(rr)} > {BCG_MAX_SAMPLES})",
                "ingested": 0}

    hr = _finite_or_none(payload.get("hr"))
    hrv = _finite_or_none(_rmssd(rr))
    # RR present but no explicit HR → derive HR from the mean interval (60000 ms / mean_rr_ms).
    if hr is None and rr:
        clean = [float(x) for x in rr
                 if isinstance(x, (int, float)) and math.isfinite(x) and 250.0 <= float(x) <= 2000.0]
        if clean:
            hr = _finite_or_none(60000.0 / (sum(clean) / len(clean)))
    # sanity-clamp HR to a physiological band so a corrupt reading can't poison the fusion
    if hr is not None and not (25.0 <= hr <= 240.0):
        hr = None

    if hr is None and hrv is None:
        return {"ok": False, "error": "no usable hr/rr in batch", "ingested": 0}

    bridge.write_cardiac_sample(repo.conn, {"hr": hr, "hrv": hrv, "source": source})
    # Accumulate into the same overnight time-series as the phone samples (source-tagged so the
    # two channels stay distinguishable for model training). Best-effort; never fails the ingest.
    bridge.append_sensor_sample(repo.conn, {
        "hr": hr, "hrv": hrv, "movement": None, "source": source,
        "fs": None, "n_samples": len(rr),
    })
    return {"ok": True, "hr": hr, "hrv": hrv, "rr_count": len(rr), "source": source}


def ingest_bcg(repo, payload: dict) -> dict:
    """Ingest a raw accelerometer batch from the phone (kept in bed), derive sub-minute
    movement (+ best-effort HR/HRV from the ballistocardiogram), and publish it to the bridge
    so the daemon fuses it onto the Pod frame. Zero device risk — the phone never touches the Pod.

    Accepted batch shapes (all in native units; ``fs`` = sample rate in Hz):
      {"fs": 50, "ax": [...], "ay": [...], "az": [...]}   3-axis accel (collapsed to magnitude)
      {"fs": 50, "mag": [...]}                            pre-computed 1-D magnitude / single axis
      {"fs": 50, "payload": [{"x":..,"y":..,"z":..}, ...]} list of samples (Sensor Logger style)
    Movement is the trustworthy phone signal; HR/HRV are returned only when the BCG is clean
    enough, and are advisory (the Pod cloud HR stays the cardiac source of record).

    Rejects oversized batches outright (see ``BCG_MAX_SAMPLES``); silently drops individual
    non-finite (NaN/Inf) values rather than failing the whole batch, since one bad accel sample
    shouldn't discard an otherwise-good second of data -- but a non-finite value must never
    reach ``proc.ingest``/the fusion path or get persisted."""
    from sleepctl.adapters.bcg import accel_magnitude

    for key in ("ax", "ay", "az", "mag", "payload"):
        n = len(payload.get(key) or [])
        if n > BCG_MAX_SAMPLES:
            return {"ok": False, "error": f"{key} batch too large ({n} > {BCG_MAX_SAMPLES})",
                    "ingested": 0}

    explicit_fs = payload.get("fs")
    source = payload.get("source") or "phone"

    samples: list = []
    times: list = []
    if isinstance(payload.get("mag"), list):
        for v in payload["mag"]:
            try:
                samples.append(float(v))
            except (TypeError, ValueError):
                continue
    elif isinstance(payload.get("ax"), list):
        samples = accel_magnitude(payload.get("ax") or [], payload.get("ay") or [],
                                  payload.get("az") or [])
    elif isinstance(payload.get("payload"), list):
        # Sensor Logger streams every enabled sensor in one list, each tagged by "name"; keep
        # only accelerometer-family entries (gyro/magnetometer also carry x/y/z and would
        # corrupt the magnitude). Entries with no "name" (our simple format) are all accepted.
        ax, ay, az = [], [], []
        for s in payload["payload"]:
            if not isinstance(s, dict):
                continue
            name = str(s.get("name", "")).lower()
            if name and "celerati" not in name and "celerometer" not in name:
                continue  # not accelerometer / userAcceleration
            vals = s.get("values", s)
            try:
                ax.append(float(vals["x"])); ay.append(float(vals["y"])); az.append(float(vals["z"]))
            except (KeyError, TypeError, ValueError):
                continue
            if s.get("time") is not None:
                times.append(s["time"])
        samples = accel_magnitude(ax, ay, az)

    # Drop non-finite (NaN/Inf) samples -- e.g. from a NaN accel reading or an overflowed
    # magnitude -- so they never reach the BCG processor or get persisted downstream.
    samples = [s for s in samples if math.isfinite(s)]

    if not samples:
        return {"ok": False, "error": "no usable samples", "ingested": 0}

    # Sample rate: explicit ?fs= wins; else auto-detect from the per-sample timestamps Sensor
    # Logger sends (so the user never has to match it); else a sane 50 Hz default.
    fs = float(explicit_fs) if explicit_fs else (_fs_from_times(times) or 50.0)
    if not math.isfinite(fs) or fs <= 0:
        fs = 50.0

    with _BCG_LOCK:
        proc = _bcg_processor(fs)
        proc.ingest(samples)
        v = proc.vitals()
        buffered = len(proc._buf)

    if v is not None:
        # Movement always published; HR/HRV only when the waveform yielded them. Coerced through
        # _finite_or_none so a NaN/Inf can never land in sensor_samples/live_sensor even if it
        # somehow survived the input-side filtering above (e.g. a processor edge case).
        hr = _finite_or_none(v.get("hr"))
        hrv = _finite_or_none(v.get("hrv"))
        movement = _finite_or_none(v.get("movement"))
        bridge.write_sensor_sample(repo.conn, {
            "hr": hr, "hrv": hrv, "movement": movement, "source": source,
        })
        # Also append to the time-series history (singleton above is for the daemon's real-time
        # fusion; this accumulates overnight so there's a dataset for later model training).
        bridge.append_sensor_sample(repo.conn, {
            "hr": hr, "hrv": hrv, "movement": movement, "source": source,
            "fs": round(fs, 1), "n_samples": len(samples),
        })
        v = {**v, "hr": hr, "hrv": hrv, "movement": movement}

    return {"ok": True, "ingested": len(samples), "buffered": buffered,
            "fs": round(fs, 1), "fs_source": "explicit" if explicit_fs else (
                "detected" if times else "default"),
            "source": source, "vitals": v}


# ================================================================================
# Interpretability surface: "why did the controller just do that?" / "what's it learned?"
# Entirely read-only — joins the decision/intervention ledgers for a human-readable
# timeline, and surfaces the currently-active learned parameters + their source/confidence.
# ================================================================================

def insights_decisions(repo, limit: int = 50) -> dict:
    """Recent controller decisions as a human-readable "why it did that" timeline: each entry
    joins the per-tick decision log (state/intent/target/reason/confidence) with the nearest
    intervention (an actual commanded level CHANGE) so the reader can see both what the
    controller was thinking and whether it actually moved the bed."""
    decisions = repo.recent_decisions(limit)
    interventions = repo.recent_interventions(limit * 2)
    # Index interventions by timestamp for an exact-match device-level/magnitude lookup; decisions
    # log every tick, interventions only log on a level change, so most ticks won't have a match —
    # that's expected (it means "held" rather than "moved").
    by_ts: dict[str, list] = {}
    for iv in interventions:
        key = iv.timestamp.isoformat() if iv.timestamp else None
        if key:
            by_ts.setdefault(key, []).append(iv)

    out = []
    for d in decisions:
        ts = d.get("ts")
        matched = by_ts.get(ts, [])
        moved = bool(matched)
        out.append({
            "ts": ts,
            "night_date": d.get("night_date"),
            "state": d.get("state"),
            "objective": d.get("objective"),
            "intent": d.get("thermal_intent"),
            "action": d.get("action"),
            "target_temp_f": d.get("target_temp_f"),
            "target_level": d.get("target_level"),
            "confidence": d.get("confidence"),
            "reason": d.get("reason"),
            "moved": moved,
            "magnitude_f": matched[0].magnitude_f if matched else None,
        })
    return {"decisions": out, "n": len(out)}


def insights_parameters(repo) -> dict:
    """A table of the currently-learned control parameters: value + source/confidence + a plain
    "what it does" note, pulled from the setpoint profile, the thermal/comfort/resting-baseline
    singletons, and a couple of easily-available learner summaries. Read-only — this mirrors
    the knobs the controller actually reads, so "what's it learned" matches "why did it do that"."""
    rows: list[dict] = []

    sp = repo.latest_setpoints()
    if sp is not None:
        rows.append({
            "name": "neutral_f", "value": sp.neutral_f, "source": sp.source or "default",
            "confidence": None, "version": sp.version,
            "what": "Baseline bed temperature the controller treats as thermally neutral.",
        })
        rows.append({
            "name": "deep_bias_f", "value": sp.deep_bias_f, "source": sp.source or "default",
            "confidence": None, "version": sp.version,
            "what": "How much cooler to run during deep-sleep-seeking windows.",
        })
        rows.append({
            "name": "rem_warm_offset_f", "value": sp.rem_warm_offset_f,
            "source": sp.source or "default", "confidence": None, "version": sp.version,
            "what": "Warm offset applied to protect/encourage REM sleep.",
        })
        rows.append({
            "name": "wake_ramp_f", "value": sp.wake_ramp_f, "source": sp.source or "default",
            "confidence": None, "version": sp.version,
            "what": "Warmth added during the wake ramp to help you surface gently.",
        })
        rows.append({
            "name": "composite_bed_weight", "value": sp.composite_bed_weight,
            "source": sp.source or "default", "confidence": None, "version": sp.version,
            "what": "How much weight the ML setpoint model gives the bed (vs room) temperature.",
        })
    else:
        d = CFG.default_setpoints()
        rows.append({
            "name": "neutral_f", "value": d.neutral_f, "source": "default", "confidence": None,
            "version": None, "what": "Baseline bed temperature (no learned setpoint yet).",
        })

    cal = repo.get_thermal_calibration()
    if cal:
        rows.append({
            "name": "cool_f_per_min", "value": cal.get("cool_f_per_min"),
            "source": cal.get("source") or "self_test", "confidence": None, "version": None,
            "what": "Measured cooling rate — how fast the bed actually cools (feeds pre-cool timing).",
        })
        rows.append({
            "name": "heat_f_per_min", "value": cal.get("heat_f_per_min"),
            "source": cal.get("source") or "self_test", "confidence": None, "version": None,
            "what": "Measured heating rate — how fast the bed actually warms (feeds wake warm-up timing).",
        })

    comfort = repo.get_comfort_profile()
    if comfort:
        rows.append({
            "name": "comfort_neutral_f", "value": comfort.get("neutral_f"),
            "source": comfort.get("source") or "comfort_cal", "confidence": None, "version": None,
            "what": "The temperature YOU rated \"just right\" on this mattress from the comfort sweep.",
        })
        if comfort.get("cool_edge_f") is not None and comfort.get("warm_edge_f") is not None:
            rows.append({
                "name": "comfort_band_f", "value": [comfort.get("cool_edge_f"), comfort.get("warm_edge_f")],
                "source": comfort.get("source") or "comfort_cal", "confidence": None, "version": None,
                "what": "Coolest/warmest temperatures you still rated comfortable.",
            })

    baseline = repo.get_resting_baseline()
    if baseline:
        rows.append({
            "name": "resting_hr_hrv", "value": [baseline.get("hr"), baseline.get("hrv")],
            "source": baseline.get("source") or "self_test", "confidence": None, "version": None,
            "what": "Your quiet-awake-in-bed HR/HRV baseline — anchors arousal/wake-risk detection.",
        })

    # A couple of easily-available learner summaries: settle-nudge (maintenance) + model confidence.
    try:
        from sleepctl.learning.settle import learn_settle_nudge
        settle = learn_settle_nudge(repo, CFG)
        rows.append({
            "name": "settle_nudge_f", "value": round(settle, 2), "source": "learned",
            "confidence": None, "version": None,
            "what": "Learned nudge applied after an awakening to help you re-settle (cooler/warmer).",
        })
    except Exception:
        pass

    try:
        rows_ml = build_feature_rows(repo)
        clean = clean_rows(rows_ml)
        if len(clean) >= 3:
            conf = SetpointModel(lam=CFG.ml.ridge_lambda).fit(clean).confidence()
            rows.append({
                "name": "model_confidence", "value": round(conf, 3), "source": "ml",
                "confidence": conf, "version": None,
                "what": "The setpoint model's confidence, from nights of clean revealed-preference data.",
            })
    except Exception:
        pass

    return {"parameters": rows, "n": len(rows)}
# ------------------------------------------------------------- meta-learning ledger
def learning_ledger_view(repo) -> dict:
    """The cross-learner confidence ledger: what EVERY learner currently reports (value,
    maturity, confidence, source), plus an advisory (never enforced) check for learners quietly
    pulling the same phase's temperature in opposite directions. Pure read-model — never
    retrains anything and never changes controller behavior."""
    from sleepctl.learning.coordinator import build_ledger_report
    return build_ledger_report(repo, CFG).to_dict()
# ============================================================================
# Goal #2: detect controller/bed failures and push them to the phone so a silent
# multi-hour outage becomes a 2-minute fix. ``health_monitor.evaluate_health`` is the
# pure decision layer (see that module for the full rationale); everything below wires
# it to the existing ``alerts`` table/endpoint and the Web Push sender.
# ============================================================================
from app import health_monitor, push_sender  # noqa: E402


# Health-monitor alert types are namespaced with "health_" so they never collide with
# the pre-existing per-day-deduped alert types above (e.g. "stale_data") — they use a
# different lifecycle (raise-on-appear, auto-clear-on-resolve) rather than one-per-day.
_HEALTH_ALERT_PREFIX = "health_"


def _health_alert_type(code: str) -> str:
    return f"{_HEALTH_ALERT_PREFIX}{code}"


def active_health_alert_codes(repo) -> set[str]:
    """Codes (bare, without the ``health_`` prefix) with a currently-open alert row —
    used both to avoid re-pushing every poll and to know what to auto-clear."""
    rows = repo.conn.execute(
        "SELECT type FROM alerts WHERE acknowledged=0 AND type LIKE ?",
        (f"{_HEALTH_ALERT_PREFIX}%",),
    ).fetchall()
    return {r["type"][len(_HEALTH_ALERT_PREFIX):] for r in rows}


def _raise_health_alert(repo, issue: dict) -> bool:
    """Insert an open alert row for ``issue`` if one isn't already open. Returns True if
    a NEW row was inserted (i.e. this is a newly-appearing issue, not a repeat)."""
    atype = _health_alert_type(issue["code"])
    exists = repo.conn.execute(
        "SELECT 1 FROM alerts WHERE type=? AND acknowledged=0 LIMIT 1", (atype,)
    ).fetchone()
    if exists:
        return False
    repo.conn.execute(
        "INSERT INTO alerts (ts, type, severity, message, acknowledged) VALUES (?,?,?,?,0)",
        (datetime.now(timezone.utc).isoformat(), atype, issue["severity"], issue["message"]),
    )
    repo.conn.commit()
    return True


def _clear_health_alert(repo, code: str) -> None:
    """Auto-acknowledge the open alert for a health-monitor code whose condition has
    resolved — this is what makes the alert list reflect CURRENT state rather than
    accumulating stale entries forever."""
    repo.conn.execute(
        "UPDATE alerts SET acknowledged=1 WHERE type=? AND acknowledged=0",
        (_health_alert_type(code),),
    )
    repo.conn.commit()


def evaluate_and_sync_health_alerts(repo, recent_errors: list[str] | None = None) -> dict:
    """Run the health evaluator against the live runtime_state, raise alerts for newly
    appearing issues, clear alerts for issues that resolved, and push newly-appearing
    CRITICAL issues to any subscribed phones. Returns a small summary dict (useful for
    tests/diagnostics); safe to call on every ``/status``/``/alerts`` request — it's a
    handful of indexed SQLite lookups, not a background job, so there's no extra process
    to keep alive (see the module docstring in health_monitor.py for why that's enough).

    ``recent_errors``, when not given explicitly by the caller, is read from the live
    daemon's own persisted signal: ``runtime_state.extra["recent_errors"]`` (a rolling
    window of tick-error reprs the daemon writes every tick — see
    ``live_daemon.LiveDashboardDaemon._recent_errors``/``_snapshot``). That's what wires up
    the previously-dead "3 consecutive errors -> critical push" path: a sustained daytime
    Eight Sleep cloud outage now actually crosses ``health_monitor``'s repeated-error
    threshold and pushes, instead of every real caller passing ``None`` forever."""
    rt = bridge.read_runtime_state(repo.conn, settings.runtime_stale_seconds)
    if recent_errors is None:
        extra = rt.get("extra") or {}
        if isinstance(extra, dict):
            recent_errors = extra.get("recent_errors")
    issues = health_monitor.evaluate_health(rt, recent_errors=recent_errors,
                                            stale_seconds=settings.runtime_stale_seconds)
    current_codes = {i["code"] for i in issues}
    previously_active = active_health_alert_codes(repo)

    newly_raised = []
    for issue in issues:
        if _raise_health_alert(repo, issue):
            newly_raised.append(issue)

    for code in previously_active - current_codes:
        _clear_health_alert(repo, code)

    pushed = 0
    to_push = push_sender.select_new_critical(newly_raised, previously_active)
    if to_push:
        subs = list_push_subscriptions(repo)
        for issue in to_push:
            result = push_sender.deliver(issue, subs)
            if result.ok:
                pushed += 1
            if result.stale_removed:
                # best-effort cleanup of dead subscriptions surfaced by the transport
                pass

    return {"issues": issues, "newly_raised": [i["code"] for i in newly_raised],
            "cleared": sorted(previously_active - current_codes), "pushed": pushed}


# ---------------------------------------------------------------------- push subscriptions
def add_push_subscription(repo, endpoint: str, p256dh: str, auth: str) -> dict:
    repo.conn.execute(
        """INSERT INTO push_subscriptions (endpoint, p256dh, auth, created) VALUES (?,?,?,?)
        ON CONFLICT(endpoint) DO UPDATE SET p256dh=excluded.p256dh, auth=excluded.auth""",
        (endpoint, p256dh, auth, datetime.now(timezone.utc).isoformat()),
    )
    repo.conn.commit()
    return {"ok": True}


def remove_push_subscription(repo, endpoint: str) -> dict:
    repo.conn.execute("DELETE FROM push_subscriptions WHERE endpoint=?", (endpoint,))
    repo.conn.commit()
    return {"ok": True}


def list_push_subscriptions(repo) -> list[dict]:
    rows = repo.conn.execute("SELECT endpoint, p256dh, auth FROM push_subscriptions").fetchall()
    return [dict(r) for r in rows]


def vapid_public_key() -> dict:
    return {"public_key": settings.vapid_public_key or None,
            "configured": push_sender.vapid_configured()}
# ------------------------------------------------------------------ circadian phase (#10)
def circadian_view(repo) -> dict:
    """The circadian phase estimate + wake-maintenance zone, from recent sleep history.

    Thin wrapper over ``sleepctl.controller.circadian.estimate_circadian`` — the pure,
    unit-tested model — so the dashboard can surface it directly.
    """
    from sleepctl.controller.circadian import estimate_circadian
    est = estimate_circadian(repo)
    return est.to_dict()


# ------------------------------------------------------------------ OAuth-free calendar (#10)
def _get_calendar_config(repo) -> dict:
    """ICS calendar ingest config (secret read-only ICS URL), stored in settings_kv.

    The URL is user data, not a secret we generate — never hardcoded, never logged. Kept
    server-side in the same settings_kv table the shift/gym/hue configs already use.
    """
    import json as _json
    row = repo.conn.execute(
        "SELECT value FROM settings_kv WHERE key='calendar_config'").fetchone()
    d = _json.loads(row["value"]) if row else {}
    return {"enabled": bool(d.get("enabled", False)), "ics_url": d.get("ics_url")}


def calendar_config_view(repo) -> dict:
    cfg = _get_calendar_config(repo)
    # Never echo the raw URL back in full once set — enough to confirm it's configured
    # without re-displaying the secret path on every poll.
    url = cfg.get("ics_url")
    masked = None
    if url:
        masked = url if len(url) <= 24 else f"{url[:16]}...{url[-8:]}"
    return {"enabled": cfg["enabled"], "configured": bool(url), "ics_url_masked": masked}


def calendar_config_update(repo, values: dict) -> dict:
    import json as _json
    merged = {**_get_calendar_config(repo), **(values or {})}
    repo.conn.execute(
        "INSERT INTO settings_kv (key, value) VALUES ('calendar_config', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (_json.dumps(merged),))
    repo.conn.commit()
    return calendar_config_view(repo)


def seed_calendar_from_env(repo) -> bool:
    """Connect the work-shift calendar from a ``CALENDAR_ICS_URL`` env var (put in deploy/.env) so
    it can be configured WITHOUT the dashboard UI. Runs once at startup: seeds the config only if
    the env var is set AND no URL is configured yet, so a later change/disconnect made in the UI is
    never clobbered on the next boot. The URL is user config (kept in settings_kv, masked on
    read-back, never logged) — do NOT hardcode it in the repo; it belongs in the gitignored
    deploy/.env. Returns True if it seeded."""
    import os
    url = (os.environ.get("CALENDAR_ICS_URL") or "").strip()
    if not url:
        return False
    if _get_calendar_config(repo).get("ics_url"):
        return False  # already configured (e.g. via the UI) — respect it
    calendar_config_update(repo, {"enabled": True, "ics_url": url})
    return True


_ICS_SOURCE_CACHE: dict = {}


def _get_ics_source(repo):
    """Build (or reuse) an ``IcsCalendarSource`` for the configured URL. None if unset."""
    from sleepctl.adapters.calendar import IcsCalendarSource
    cfg = _get_calendar_config(repo)
    url = cfg.get("ics_url")
    if not cfg["enabled"] or not url:
        return None
    cached = _ICS_SOURCE_CACHE.get(repo.path)
    if cached is not None and cached.ics_url == url:
        return cached
    src = IcsCalendarSource(url)
    _ICS_SOURCE_CACHE[repo.path] = src
    return src


def calendar_refresh(repo) -> dict:
    """Force a re-fetch of the configured ICS feed; returns upcoming events + any fetch error.

    Also syncs the freshly-fetched feed into ``shift_config`` (see ``sync_calendar_to_shift``)
    so the shift plan reflects the real next shift immediately after a refresh, not just on the
    next ``/shift/plan`` poll."""
    from sleepctl.adapters.calendar import next_wake_time_from_events, upcoming_events
    src = _get_ics_source(repo)
    if src is None:
        return {"ok": False, "configured": False, "events": [], "next_wake_time": None}
    events = src.refresh(force=True)
    upcoming = upcoming_events(events, within_days=14)
    nxt = next_wake_time_from_events(events)
    try:
        sync_calendar_to_shift(repo)
    except Exception:
        pass
    return {
        "ok": src._last_error is None,
        "configured": True,
        "error": src._last_error,
        "events": [e.to_dict() for e in upcoming],
        "next_wake_time": nxt.isoformat() if nxt else None,
    }


def calendar_events_view(repo) -> dict:
    """Upcoming parsed events from the last (cached) fetch, without forcing a network hit."""
    from sleepctl.adapters.calendar import next_wake_time_from_events, upcoming_events
    src = _get_ics_source(repo)
    if src is None:
        return {"ok": False, "configured": False, "events": [], "next_wake_time": None}
    events = src.refresh(force=False)
    upcoming = upcoming_events(events, within_days=14)
    nxt = next_wake_time_from_events(events)
    return {
        "ok": src._last_error is None,
        "configured": True,
        "error": src._last_error,
        "events": [e.to_dict() for e in upcoming],
        "next_wake_time": nxt.isoformat() if nxt else None,
    }


# ======================================================================================
# Safety/quality surfacing: data-quality gate (Feature #6) + decision guardrail (Feature #8).
# Both read the daemon's ``runtime_state.extra`` the same way ``preemption_status`` above
# reads "preemption"/"steering" -- additive, so until a daemon is updated to publish these
# keys the endpoints just report the neutral "unavailable" defaults below (no daemon changes
# were made for this feature; the daemon side is a small future follow-up: publish
# ``controller.data_quality_summary()`` / ``controller.guardrail_summary()`` into ``extra``).
# ======================================================================================
def data_quality_status(repo) -> dict:
    """Live data-quality-gate state for the dashboard: current trust score, top reason, and
    whether it's currently forcing a conservative HOLD."""
    rt = bridge.read_runtime_state(repo.conn, settings.runtime_stale_seconds)
    extra = rt.get("extra") or {}
    dq = extra.get("data_quality") or {}
    return {
        "score": dq.get("score"),
        "reasons": dq.get("reasons", []),
        "top_reason": dq.get("top_reason"),
        "gating": bool(dq.get("gating", False)),
        "stale": rt.get("stale", True),
    }


def guardrail_status(repo) -> dict:
    """Live decision-guardrail state for the dashboard: any current findings and whether a
    CRITICAL one is forcing a safe hold this tick."""
    rt = bridge.read_runtime_state(repo.conn, settings.runtime_stale_seconds)
    extra = rt.get("extra") or {}
    gr = extra.get("guardrail") or {}
    return {
        "triggered": bool(gr.get("triggered", False)),
        "critical": bool(gr.get("critical", False)),
        "findings": gr.get("findings", []),
        "stale": rt.get("stale", True),
    }


# ======================================================================================
# Feature #6: daily health + last-night push ("morning report"). Reuses the SAME diagnostics
# battery /diag uses (``app.diagnostics.run_diagnostics``) so the phone and the maintainer's
# /diag never disagree, plus the SAME last-night summary /status already shows. Delivery reuses
# the existing Web Push infra (``push_sender`` + ``push_subscriptions`` above) -- nothing new to
# set up on the client side, the same "Enable alerts" subscription covers this too.
# ======================================================================================
_MORNING_REPORT_LAST_SENT_KEY = "morning_report_last_sent_date"   # settings_kv: "YYYY-MM-DD"
_MORNING_REPORT_LAST_CRITICAL_KEY = "morning_report_last_critical_push"  # settings_kv: ISO ts
_MORNING_REPORT_CRITICAL_COOLDOWN_S = 3600  # an out-of-band DOWN push fires at most once/hour


def build_morning_report(repo) -> dict:
    """{health_verdict, headline, body} for the daily push + the ``GET /diag/morning-report``
    dashboard view. ``health_verdict``/the underlying checks come straight from
    ``run_diagnostics`` (the exact same battery ``/diag`` renders as text); the sleep summary
    comes from the same night-brief ``/status`` uses. Never raises -- diagnostics/night lookups
    are individually guarded so a bad night row or a diagnostics hiccup degrades the wording
    instead of breaking the push."""
    from app.diagnostics import run_diagnostics

    try:
        report = run_diagnostics(repo, run_dir=bridge.run_dir())
    except Exception:
        report = {"verdict": "UNKNOWN", "headline": "diagnostics unavailable", "primary_remedy": None}
    verdict = report.get("verdict", "UNKNOWN")

    night = None
    try:
        nights = repo.recent_nights(1)
        if nights:
            night = _night_brief(nights[-1])
    except Exception:
        night = None

    if night:
        parts = []
        if night.get("total_sleep_min") is not None:
            parts.append(f"{night['total_sleep_min'] / 60.0:.1f}h sleep")
        if night.get("wake_events") is not None:
            parts.append(f"{night['wake_events']} wake-ups")
        if night.get("sleep_efficiency") is not None:
            parts.append(f"{night['sleep_efficiency']:.0f}% efficiency")
        sleep_summary = ", ".join(parts) if parts else "no sleep metrics logged"
    else:
        sleep_summary = "no sleep data yet"

    if verdict == "HEALTHY":
        headline = f"All systems nominal. Last night: {sleep_summary}."
    else:
        headline = f"{verdict}: {report.get('headline', 'see /diag')}"

    body_lines = [f"System: {verdict}", f"Last night: {sleep_summary}"]
    if verdict != "HEALTHY" and report.get("primary_remedy"):
        body_lines.append(f"Fix: {report['primary_remedy']}")

    return {
        "health_verdict": verdict,
        "headline": headline,
        "body": "\n".join(body_lines),
        "night": night,
        "generated_at": report.get("generated_at"),
    }


def _kv_get_json(repo, key: str):
    import json as _json
    row = repo.conn.execute("SELECT value FROM settings_kv WHERE key=?", (key,)).fetchone()
    if not row:
        return None
    try:
        return _json.loads(row["value"])
    except Exception:
        return None


def _kv_set_json(repo, key: str, value) -> None:
    import json as _json
    repo.conn.execute(
        "INSERT INTO settings_kv (key, value) VALUES (?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, _json.dumps(value)))
    repo.conn.commit()


def maybe_send_morning_report(repo, force: bool = False) -> dict:
    """Send the morning-report push, self-throttled so it's safe to call this often (e.g. from
    a Scheduled Task hitting ``POST /diag/morning-report/send`` every 15-30 min, not just once
    at dawn): at most ONE routine push per calendar day, PLUS at most one immediate push per
    hour whenever the live health verdict is DOWN (so a real outage doesn't have to wait for
    morning) -- covers "allow an immediate push...rate-limited to once per hour" without a
    second code path. ``force=True`` (an explicit maintainer call) bypasses the throttle, e.g.
    for testing that push delivery itself works.

    This IS the "once-per-day send" scheduling hook: call it from any external timer that hits
    the token-gated endpoint; no daemon/background-thread change was needed because the
    self-throttle makes frequent polling safe and correct."""
    report = build_morning_report(repo)
    verdict = report["health_verdict"]
    now = datetime.now(timezone.utc)
    today = now.date().isoformat()
    is_critical = verdict == "DOWN"

    if force:
        should_send, reason = True, "forced"
    elif is_critical:
        last_critical = _kv_get_json(repo, _MORNING_REPORT_LAST_CRITICAL_KEY)
        age = _MORNING_REPORT_CRITICAL_COOLDOWN_S + 1
        if last_critical:
            try:
                age = (now - datetime.fromisoformat(last_critical)).total_seconds()
            except Exception:
                pass
        should_send, reason = age >= _MORNING_REPORT_CRITICAL_COOLDOWN_S, "critical_now"
    else:
        last_sent = _kv_get_json(repo, _MORNING_REPORT_LAST_SENT_KEY)
        should_send, reason = last_sent != today, "daily"

    if not should_send:
        return {"sent": False, "reason": "throttled", "trigger": reason, "report": report}

    subs = list_push_subscriptions(repo)
    title = "SleepCtl: system needs attention" if is_critical else "SleepCtl morning report"
    result = push_sender.deliver_custom(title=title, body=report["body"], subscriptions=subs,
                                        tag="sleepctl-morning-report")

    if is_critical:
        _kv_set_json(repo, _MORNING_REPORT_LAST_CRITICAL_KEY, now.isoformat())
    else:
        _kv_set_json(repo, _MORNING_REPORT_LAST_SENT_KEY, today)

    return {
        "sent": result.ok, "reason": result.reason or "sent", "trigger": reason,
        "push": {"sent": result.sent, "failed": result.failed},
        "report": report,
    }


# ============================================================================
# Nighttime failure push: a device gone offline, an empty water reservoir, a wedged
# command queue, or the daemon's own data going stale while someone is actually in bed
# are exactly the failures a resident must not discover by lying there uncomfortable at
# 3am. Fired from the DAEMON TICK (both live_daemon.py and run_daemon.py) rather than
# piggy-backed on an API request like the health-monitor alerts above -- nobody has the
# web app open to generate a request at 3am, so a request-driven-only check would
# silently never fire overnight. Delivery/throttle mirror the morning report above:
# ``push_sender.deliver_custom`` + a settings_kv last-sent timestamp per condition,
# auto-cleared the moment the condition resolves so a recurrence re-alerts instead of
# waiting out the rest of the hour.
# ============================================================================
_STUCK_COMMAND_THRESHOLD_S = 600     # oldest pending command older than this -> queue looks wedged
_STALE_AT_NIGHT_THRESHOLD_S = 300    # runtime_state older than this WHILE someone's in bed -> quiet loop
_NIGHT_FAILURE_RATE_LIMIT_S = 3600   # at most one push per condition per hour
_NIGHT_WINDOW_START_HOUR = 21        # fallback "probably asleep" window (21:00-09:00) for when
_NIGHT_WINDOW_END_HOUR = 9           # bed presence isn't (yet) known, e.g. right at lights-out
# Water-loop/capacity + frozen-telemetry device-health conditions (see
# ``_detect_thermal_failure_conditions`` below) are pushed under a WIDER gate than the
# original four -- a stuck prime or an air-bound loop is a device-health problem, not just a
# 3am comfort issue, so they alert whenever the bed is actively being driven, not only at
# night/with confirmed presence (see ``check_and_alert_failures``).
_THERMAL_FAILURE_CODES = {"stuck_prime", "reduced_capacity", "low_water", "frozen_telemetry"}
_NIGHT_FAILURE_CODES = ({"device_offline", "reservoir_empty", "stuck_commands",
                        "data_stale_at_night"} | _THERMAL_FAILURE_CODES)
_NIGHT_FAILURE_LAST_SENT_PREFIX = "night_failure_last_sent__"  # + code -> settings_kv: ISO ts
_THERMAL_HISTORY_HOURS = 1           # state_history window fed to the thermal detectors
_THERMAL_HISTORY_LIMIT = 200


def _in_night_window(now: datetime) -> bool:
    """True during the configured night hours (default 21:00-09:00, wrapping past midnight) --
    the fallback "probably asleep" signal for when bed presence isn't available (e.g. right at
    lights-out, before the Pod has detected anyone lying down). A standalone, monkeypatchable
    seam so tests can force day/night context deterministically instead of depending on the
    wall clock at whatever moment the suite happens to run."""
    h = now.hour
    if _NIGHT_WINDOW_START_HOUR <= _NIGHT_WINDOW_END_HOUR:
        return _NIGHT_WINDOW_START_HOUR <= h < _NIGHT_WINDOW_END_HOUR
    return h >= _NIGHT_WINDOW_START_HOUR or h < _NIGHT_WINDOW_END_HOUR


def _oldest_pending_command_age_s(repo, now: datetime) -> "float | None":
    row = repo.conn.execute(
        "SELECT ts FROM commands WHERE status='pending' ORDER BY id ASC LIMIT 1"
    ).fetchone()
    if not row or not row["ts"]:
        return None
    try:
        ts = datetime.fromisoformat(row["ts"])
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return (now - ts).total_seconds()
    except Exception:
        return None


def _detect_failure_conditions(rt: dict, repo, now: datetime) -> list[dict]:
    """Detection of the four nighttime-failure conditions from ``runtime_state`` (+ one indexed
    read of the pending-commands queue) -- independent of whether it's currently "night" or
    live; that gating belongs to the caller (``check_and_alert_failures``) so this stays simple
    to unit test against a hand-built runtime_state, the same way
    ``health_monitor.evaluate_health`` does. Returns ``{code, severity, title, body}`` dicts."""
    extra = rt.get("extra") or {}
    device = extra.get("device") or {}
    if not isinstance(device, dict):
        device = {}
    out: list[dict] = []

    if device.get("online") is False or extra.get("device_error"):
        detail = extra.get("device_error") or "the bed/hub is reporting offline"
        out.append({
            "code": "device_offline", "severity": "critical",
            "title": "Bed alert: device offline",
            "body": f"The controller can't reach the bed ({detail}). It won't heat or cool "
                    "until this clears.",
        })

    if device.get("has_water") is False:
        out.append({
            "code": "reservoir_empty", "severity": "critical",
            "title": "Bed alert: water reservoir empty",
            "body": "The water reservoir is empty — the bed can't heat/cool. Refill and prime.",
        })

    stuck_age = _oldest_pending_command_age_s(repo, now)
    if stuck_age is not None and stuck_age >= _STUCK_COMMAND_THRESHOLD_S:
        out.append({
            "code": "stuck_commands", "severity": "critical",
            "title": "Bed alert: commands not applying",
            "body": f"A command has been queued for {int(stuck_age // 60)} min without being "
                    "applied — the controller may be wedged.",
        })

    bed_presence = bool(extra.get("bed_presence"))
    data_age = None
    if rt.get("updated"):
        try:
            ts = datetime.fromisoformat(rt["updated"])
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            data_age = (now - ts).total_seconds()
        except Exception:
            data_age = None
    if bed_presence and data_age is not None and data_age >= _STALE_AT_NIGHT_THRESHOLD_S:
        out.append({
            "code": "data_stale_at_night", "severity": "critical",
            "title": "Bed alert: controller gone quiet",
            "body": f"You're in bed but the controller hasn't reported in {int(data_age // 60)} "
                    "min — the loop may have stalled.",
        })

    return out


def _detect_thermal_failure_conditions(rt: dict, repo, now: datetime) -> list[dict]:
    """Water-loop/capacity + frozen-telemetry device-health conditions, from the pure
    ``sleepctl.diagnostics_thermal`` engine fed by ``state_history`` (already-recorded trend
    data -- no new sampling needed, see ``Repository.record_state_snapshot``). Independent of
    gating, mirroring ``_detect_failure_conditions`` above: never raises, degrades to an empty
    list on any error so a bad history row can never break the daemon tick that calls this."""
    extra = rt.get("extra") or {}
    device = extra.get("device") or {}
    if not isinstance(device, dict):
        device = {}
    out: list[dict] = []
    try:
        from sleepctl.diagnostics_thermal import analyze_thermal_capacity, detect_frozen_telemetry
        history = repo.state_history(hours=_THERMAL_HISTORY_HOURS, limit=_THERMAL_HISTORY_LIMIT)
        capacity = analyze_thermal_capacity(device, history, now.isoformat())
        status = capacity.get("status")
        if status == "stuck_prime":
            out.append({
                "code": "stuck_prime", "severity": "critical",
                "title": "Bed alert: prime stuck",
                "body": capacity.get("remedy") or capacity.get("reason") or
                        "Priming has been running far longer than it should.",
            })
        elif status == "reduced_capacity":
            out.append({
                "code": "reduced_capacity", "severity": "critical",
                "title": "Bed alert: reduced thermal capacity",
                "body": capacity.get("remedy") or capacity.get("reason") or
                        "The bed isn't responding to strong thermal commands.",
            })
        elif status == "low_water":
            out.append({
                "code": "low_water", "severity": "warning",
                "title": "Bed alert: low water",
                "body": capacity.get("remedy") or capacity.get("reason") or
                        "The reservoir is reporting low.",
            })

        frozen = detect_frozen_telemetry(history)
        if frozen.get("status") == "frozen_telemetry":
            out.append({
                "code": "frozen_telemetry", "severity": "critical",
                "title": "Bed alert: telemetry frozen",
                "body": frozen.get("remedy") or frozen.get("reason") or
                        "bed_temp_f/device_level haven't changed in a while despite active control.",
            })
    except Exception:
        pass
    return out


def check_and_alert_failures(repo) -> list[dict]:
    """The nighttime-failure detector + pusher. Meant to be called on every daemon tick (both
    daemons) so a silent failure gets a phone push even with no browser tab open to drive the
    request-based health monitor above -- and it's cheap/idempotent enough to also back
    ``GET /alerts/active`` for the web app's banner.

    Gated on it actually mattering, LIVE mode (not the simulator/dry-run) always required, plus
    one of two applicability gates depending on the condition:
      * the original four (``device_offline``, ``reservoir_empty``, ``stuck_commands``,
        ``data_stale_at_night``) need "night context" (bed presence, or within the configured
        night window -- see ``_in_night_window``) -- they're comfort/safety issues that matter
        most while someone's actually trying to sleep.
      * the water-loop/capacity + frozen-telemetry conditions (``_THERMAL_FAILURE_CODES``) are
        DEVICE-HEALTH problems, not just a 3am comfort issue -- a stuck prime or an air-bound
        loop matters whenever the bed is actively being driven, so they also fire while the
        controller is powered on and not in away mode, even mid-day with nobody (yet) detected
        in bed (bed-presence sensing is itself unreliable -- see eightsleep_cloud.py).
    Outside its gate a condition is still logged via ``repo.log_event`` for forensics, but
    NOTHING is pushed for it.

    Each condition pushes at most once per hour (persisted via the same settings_kv
    last-sent-timestamp pattern the morning report throttle uses) and that throttle is cleared
    the instant the condition is no longer active, so a recurrence re-alerts immediately instead
    of waiting out the rest of the hour."""
    rt = bridge.read_runtime_state(repo.conn, settings.runtime_stale_seconds)
    extra = rt.get("extra") or {}
    now = datetime.now(timezone.utc)

    conditions = _detect_failure_conditions(rt, repo, now)
    conditions += _detect_thermal_failure_conditions(rt, repo, now)
    active_codes = {c["code"] for c in conditions}

    # Auto-clear any per-condition throttle whose condition is no longer active, so the NEXT
    # time it appears it re-alerts right away rather than waiting out the rest of the hour.
    for code in _NIGHT_FAILURE_CODES - active_codes:
        key = _NIGHT_FAILURE_LAST_SENT_PREFIX + code
        if _kv_get_json(repo, key):
            _kv_set_json(repo, key, None)

    live = bool(extra.get("live", False))
    dry_run = bool(extra.get("dry_run", False))
    bed_presence = bool(extra.get("bed_presence"))
    night_context = bed_presence or _in_night_window(now)
    device_active = bool(extra.get("power_on", True)) and not bool(extra.get("away", False))
    comfort_matters = live and not dry_run and night_context
    device_health_matters = live and not dry_run and (night_context or device_active)

    if not (comfort_matters or device_health_matters):
        for cond in conditions:
            try:
                repo.log_event("alert", "info", cond["code"],
                               f"(not paging: outside live/night gate) {cond['body']}", cond)
            except Exception:
                pass
        return []

    subs = None
    result: list[dict] = []
    for cond in conditions:
        code_matters = (device_health_matters if cond["code"] in _THERMAL_FAILURE_CODES
                        else comfort_matters)
        if not code_matters:
            try:
                repo.log_event("alert", "info", cond["code"],
                               f"(not paging: outside live/night gate) {cond['body']}", cond)
            except Exception:
                pass
            continue
        result.append(cond)
        key = _NIGHT_FAILURE_LAST_SENT_PREFIX + cond["code"]
        last_sent = _kv_get_json(repo, key)
        should_push = True
        if last_sent:
            try:
                age = (now - datetime.fromisoformat(last_sent)).total_seconds()
                should_push = age >= _NIGHT_FAILURE_RATE_LIMIT_S
            except Exception:
                should_push = True
        try:
            repo.log_event("alert", "warn" if should_push else "info", cond["code"],
                           cond["body"], cond)
        except Exception:
            pass
        if should_push:
            if subs is None:
                subs = list_push_subscriptions(repo)
            push_sender.deliver_custom(title=cond["title"], body=cond["body"],
                                       subscriptions=subs, tag=f"sleepctl-night-{cond['code']}")
            _kv_set_json(repo, key, now.isoformat())

    return result


def send_test_night_alert(repo) -> dict:
    """Send a single TEST nighttime-failure push end-to-end (bypassing the live/night gate and
    the per-condition throttle) so delivery can be verified without waiting for a real failure
    or faking runtime_state -- same delivery path (``push_sender.deliver_custom``) real
    conditions use. Backs ``POST /diag/action/test-alert``."""
    subs = list_push_subscriptions(repo)
    result = push_sender.deliver_custom(
        title="SleepCtl test alert",
        body="Test of the nighttime failure push — if this arrived, delivery works.",
        subscriptions=subs, tag="sleepctl-night-test")
    try:
        repo.log_event("alert", "info", "test_alert", "test nighttime failure push sent",
                       {"sent": result.sent, "failed": result.failed, "ok": result.ok})
    except Exception:
        pass
    return {"ok": result.ok, "sent": result.sent}
