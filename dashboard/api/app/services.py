"""Service helpers: status assembly, analytics, ML surfacing, alerts, data-source health.

All read through the sleepctl ``Repository`` + the dashboard tables, reusing engine logic
(config objective rules, ML recommender, phenotype). Kept in one module for v1 simplicity.
"""

from __future__ import annotations

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
        hh, mm = (int(x) for x in wake_time.split(":"))
    except Exception:
        return None
    now = datetime.now()
    w = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
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
    return plan_night(datetime.now(), wake_dt, recent, hint=hint, base_window_min=window)


def sleep_plan(repo) -> dict:
    plan = current_plan(repo)
    nights = repo.recent_nights(1)
    last_index = None
    if nights:
        last_index = perfect_sleep_index(nights[-1], plan.mode)
    d = plan.to_dict()
    d["last_night_index"] = last_index
    return d


def current_mode(repo) -> NightMode:
    return current_plan(repo).mode


# ------------------------------------------------------ sleep maintenance
def maintenance_summary(repo) -> dict:
    """The proactive + reactive sleep-maintenance picture: the learned awakening pattern
    used to PREVENT wakeups, plus how recent nights' awakenings were handled."""
    from sleepctl.learning.lead_time import build_lead_time_profile
    from sleepctl.ml.wake_profile import build_wake_profile
    profile = build_wake_profile(repo)
    lead = build_lead_time_profile(repo)

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
    rec = ml_recommendation(repo)
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
        "daemon_alive": rt.get("daemon_alive", False),
        "stale": rt.get("stale", True),
        "updated": rt.get("updated"),
        "recommendation": {"action": rec.get("action"), "reason": rec.get("reason"),
                           "confidence": rec.get("confidence"), "mode": rec.get("mode")},
        "last_night": _last_night_brief(repo, last) if last else None,
        "alerts": active_alerts(repo),
        "schedule": schedule_brief(repo),
    }


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
        return {"action": "rule-policy", "reason": "deferring to safe rule policy (insufficient "
                "data or confidence)", "confidence": 0.0, "source": "fallback",
                "low_confidence": True, "mode": mode.value}
    return {
        "action": chosen.name, "reason": chosen.reason, "confidence": chosen.confidence,
        "predicted": chosen.predicted, "source": "ml", "mode": mode.value,
        "low_confidence": chosen.confidence < CFG.ml.conf_min,
    }


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
    return {
        "daemon": {"alive": rt.get("daemon_alive", False), "updated": rt.get("updated"),
                   "stale": rt.get("stale", True)},
        "sources": sources,
        "pending_commands": repo.conn.execute(
            "SELECT COUNT(*) c FROM commands WHERE status='pending'").fetchone()["c"],
    }
