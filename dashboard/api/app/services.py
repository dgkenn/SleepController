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
    return {
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
    return {
        "preempting": bool(pre.get("preempting", False)),
        "wake_risk": pre.get("wake_risk"),
        "risk_reasons": pre.get("risk_reasons", []),
        "precursor_score": pre.get("precursor_score"),
        "precursor_reasons": pre.get("precursor_reasons", []),
        "recurring_wake_times": recurring,
        "precool_efficacy": efficacy,
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
    """Manual next-shift hint (until a calendar feed lands), stored in settings_kv."""
    import json as _json
    row = repo.conn.execute("SELECT value FROM settings_kv WHERE key='shift_config'").fetchone()
    d = _json.loads(row["value"]) if row else {}
    return {"enabled": bool(d.get("enabled", False)),
            "next_shift": d.get("next_shift"),               # ISO datetime of the shift start
            "kind": d.get("kind", "night")}                  # 'night' | 'call' | 'day'


def shift_config_view(repo) -> dict:
    return _get_shift_config(repo)


def shift_config_update(repo, values: dict) -> dict:
    import json as _json
    merged = {**_get_shift_config(repo), **(values or {})}
    if merged.get("kind") not in ("night", "call", "day"):
        merged["kind"] = "night"
    repo.conn.execute("INSERT INTO settings_kv (key, value) VALUES ('shift_config', ?) "
                      "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                      (_json.dumps(merged),))
    repo.conn.commit()
    return merged


def shift_plan_view(repo) -> dict:
    """The strategic cross-shift sleep plan: live debt, tonight's target, banking before a night
    block, prophylactic/recovery/anchor naps, and safety warnings. Computed on demand from the
    user's recent nights + the manual next-shift hint."""
    from datetime import datetime, timedelta

    from sleepctl.shift_manager import Shift, plan_shift_sleep
    cfg = _get_shift_config(repo)
    shifts = []
    if cfg["enabled"] and cfg["next_shift"]:
        try:
            start = datetime.fromisoformat(cfg["next_shift"])
            shifts = [Shift(start=start, end=start + timedelta(hours=12), kind=cfg["kind"])]
        except Exception:
            shifts = []
    plan = plan_shift_sleep(repo.recent_nights(14), shifts, datetime.now())
    out = plan.to_dict()
    out["shift_enabled"] = cfg["enabled"]
    out["next_shift"] = cfg["next_shift"] if cfg["enabled"] else None
    out["next_shift_kind"] = cfg["kind"]
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


def ingest_bcg(repo, payload: dict) -> dict:
    """Ingest a raw accelerometer batch from the phone (kept in bed), derive sub-minute
    movement (+ best-effort HR/HRV from the ballistocardiogram), and publish it to the bridge
    so the daemon fuses it onto the Pod frame. Zero device risk — the phone never touches the Pod.

    Accepted batch shapes (all in native units; ``fs`` = sample rate in Hz):
      {"fs": 50, "ax": [...], "ay": [...], "az": [...]}   3-axis accel (collapsed to magnitude)
      {"fs": 50, "mag": [...]}                            pre-computed 1-D magnitude / single axis
      {"fs": 50, "payload": [{"x":..,"y":..,"z":..}, ...]} list of samples (Sensor Logger style)
    Movement is the trustworthy phone signal; HR/HRV are returned only when the BCG is clean
    enough, and are advisory (the Pod cloud HR stays the cardiac source of record)."""
    from sleepctl.adapters.bcg import accel_magnitude

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

    if not samples:
        return {"ok": False, "error": "no usable samples", "ingested": 0}

    # Sample rate: explicit ?fs= wins; else auto-detect from the per-sample timestamps Sensor
    # Logger sends (so the user never has to match it); else a sane 50 Hz default.
    fs = float(explicit_fs) if explicit_fs else (_fs_from_times(times) or 50.0)

    with _BCG_LOCK:
        proc = _bcg_processor(fs)
        proc.ingest(samples)
        v = proc.vitals()
        buffered = len(proc._buf)

    if v is not None:
        # Movement always published; HR/HRV only when the waveform yielded them.
        bridge.write_sensor_sample(repo.conn, {
            "hr": v.get("hr"), "hrv": v.get("hrv"),
            "movement": v.get("movement"), "source": source,
        })

    return {"ok": True, "ingested": len(samples), "buffered": buffered,
            "fs": round(fs, 1), "fs_source": "explicit" if explicit_fs else (
                "detected" if times else "default"),
            "source": source, "vitals": v}
