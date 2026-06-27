"""Nightly intelligence report — turn the controller from a black box into a collaborator.

Synthesizes the pieces the system already computes (readiness, awakening forensics, the
intervention ledger, anticipatory-pre-emption efficacy, the learned setpoint + ML actions)
into one explainable morning report: what happened, WHAT THE CONTROLLER DID AND WHY, what it
learned, how confident it is, and one or two concrete things to try. Built for a quantitative
user who will only trust (and tune) a closed-loop system whose reasoning is visible.

Pure read-side: takes a Repository, returns a structured dict (+ a human-readable narrative).
Every section degrades gracefully when data is thin, so it works from night one.
"""

from __future__ import annotations

from typing import Optional

from sleepctl.benchmarks import NightMode


def _safe(fn, default=None):
    try:
        return fn()
    except Exception:
        return default


def _night_metrics(n) -> dict:
    if n is None:
        return {}
    g = lambda a: getattr(n, a, None)
    return {
        "date": g("date"),
        "total_sleep_min": g("total_sleep_min"),
        "wake_events": g("wake_events"),
        "waso_min": g("waso_min"),
        "deep_pct": g("deep_pct"),
        "rem_pct": g("rem_pct"),
        "sleep_efficiency": g("sleep_efficiency"),
        "avg_hrv": g("avg_hrv"),
        "outcome_score": g("outcome_score"),
    }


def _explain_interventions(repo, n: int = 20) -> dict:
    """The 'what I did and WHY' layer, summarized from the intervention ledger."""
    items = _safe(lambda: repo.recent_interventions(n), []) or []
    by_reason: dict = {}
    recent = []
    held = reverted = 0
    for iv in items:
        reason = getattr(iv, "reason", "") or "unspecified"
        by_reason[reason] = by_reason.get(reason, 0) + 1
        if getattr(iv, "held", None):
            held += 1
        if getattr(iv, "reverted", None):
            reverted += 1
        recent.append({
            "when": _safe(lambda iv=iv: iv.timestamp.isoformat()),
            "action": _safe(lambda iv=iv: iv.action.value, str(getattr(iv, "action", ""))),
            "magnitude_f": getattr(iv, "magnitude_f", None),
            "reason": reason,
            "held": getattr(iv, "held", None),
            "reverted": getattr(iv, "reverted", None),
            "outcome_delta": getattr(iv, "outcome_delta", None),
        })
    ranked = sorted(by_reason.items(), key=lambda kv: kv[1], reverse=True)
    return {
        "n_actions": len(items),
        "held": held,
        "reverted": reverted,
        "top_reasons": [{"reason": r, "count": c} for r, c in ranked[:5]],
        "recent": recent[:6],
    }


def _what_i_learned(repo) -> dict:
    sp = _safe(lambda: repo.latest_setpoints())
    learned = {"setpoint": None, "recent_actions": [], "baselines": None}
    if sp is not None:
        learned["setpoint"] = {
            "version": getattr(sp, "version", None),
            "source": getattr(sp, "source", None),
            "neutral_f": getattr(sp, "neutral_f", None),
            "deep_bias_f": getattr(sp, "deep_bias_f", None),
            "rem_warm_offset_f": getattr(sp, "rem_warm_offset_f", None),
            "wake_ramp_f": getattr(sp, "wake_ramp_f", None),
        }
    actions = _safe(lambda: repo.recent_actions(5), []) or []
    learned["recent_actions"] = [{
        "name": _safe(lambda a=a: a.action_name, getattr(a, "action_name", None)),
        "applied": getattr(a, "applied", None),
        "confidence": getattr(a, "confidence", None),
        "reward": getattr(a, "reward_observed", None),
    } for a in actions]
    b = _safe(lambda: repo.latest_baselines())
    if b is not None:
        learned["baselines"] = {
            "hrv": getattr(b, "hrv_median", None) or getattr(b, "avg_hrv", None),
        }
    return learned


def build_night_report(repo, cfg=None, mode: Optional[NightMode] = None) -> dict:
    """Assemble the explainable nightly report from everything the system already tracks."""
    nights = _safe(lambda: repo.recent_nights(14), []) or []
    last = nights[-1] if nights else None
    mode = mode or NightMode.NORMAL

    # 1) readiness (clinical-safety + recovery), with flags
    readiness = {}
    if last is not None:
        from sleepctl.readiness import morning_readiness
        baseline_hrv = None
        b = _safe(lambda: repo.latest_baselines())
        if b is not None:
            baseline_hrv = getattr(b, "hrv_median", None) or getattr(b, "avg_hrv", None)
        readiness = _safe(
            lambda: morning_readiness(last, nights, mode, baseline_hrv=baseline_hrv).to_dict(),
            {}) or {}

    # 2) what happened — awakening forensics (top causes) + suggestion
    forensics, suggestion = {}, None
    try:
        from sleepctl.forensics import awakening_forensics, forensics_summary, suggest_experiment
        events = awakening_forensics(repo, limit=20)
        forensics = forensics_summary(events)
        suggestion = suggest_experiment(forensics)
    except Exception:
        pass

    # 3) what the controller DID and WHY (explainability)
    did = _explain_interventions(repo)

    # 4) anticipatory pre-emption efficacy (did predicting + acting actually prevent wakes?)
    preemption = _safe(lambda: repo.precool_efficacy(), {}) or {}

    # 5) what it learned + confidence
    learned = _what_i_learned(repo)

    # 6) suggestions: forensics-driven + readiness flags
    suggestions = []
    if suggestion:
        suggestions.append(suggestion)
    for fl in (readiness.get("flags") or []):
        if fl.get("severity") in ("high", "warning"):
            suggestions.append({"reason": fl.get("message"), "source": "readiness"})

    metrics = _night_metrics(last)
    headline = _headline(metrics, readiness, forensics)
    report = {
        "date": metrics.get("date"),
        "headline": headline,
        "have_data": last is not None,
        "last_night": metrics,
        "readiness": readiness,
        "what_happened": forensics,
        "what_i_did": did,
        "preemption": preemption,
        "what_i_learned": learned,
        "suggestions": suggestions[:3],
    }
    report["narrative"] = _narrative(report)
    return report


def _headline(metrics, readiness, forensics) -> str:
    if not metrics:
        return "Not enough data yet — the report fills in after your first tracked night."
    band = (readiness or {}).get("band", "—")
    we = metrics.get("wake_events")
    we_txt = f"{we} awakening{'s' if (we or 0) != 1 else ''}" if we is not None else "—"
    return f"Readiness {band}; {we_txt} last night."


def _narrative(r: dict) -> str:
    if not r.get("have_data"):
        return r["headline"]
    L = [r["headline"]]
    m = r["last_night"]
    if m.get("total_sleep_min") is not None:
        L.append(f"Slept {round((m['total_sleep_min'] or 0)/60, 1)}h, "
                 f"deep {m.get('deep_pct') or '—'}%, efficiency {m.get('sleep_efficiency') or '—'}%.")
    tf = (r["what_happened"] or {}).get("top_factors") or []
    if tf:
        L.append("Most likely awakening cause(s): "
                 + ", ".join(f"{x['factor']} (×{x['count']})" for x in tf[:3]) + ".")
    did = r["what_i_did"]
    if did.get("n_actions"):
        reasons = ", ".join(f"{x['reason']} ×{x['count']}" for x in did["top_reasons"][:3])
        L.append(f"I made {did['n_actions']} thermal adjustment(s) "
                 f"({did['held']} held, {did['reverted']} reverted). Mainly: {reasons}.")
    pe = r["preemption"] or {}
    if pe.get("n_events"):
        L.append(f"Anticipatory pre-emption fired {pe.get('n_events')}× "
                 f"(prevented {pe.get('prevented', '—')}).")
    sp = (r["what_i_learned"] or {}).get("setpoint")
    if sp:
        L.append(f"Current setpoint v{sp.get('version')} ({sp.get('source')}): "
                 f"neutral {sp.get('neutral_f')}°F, deep bias {sp.get('deep_bias_f')}°F.")
    if r["suggestions"]:
        L.append("Suggested next: " + r["suggestions"][0].get("reason", ""))
    return " ".join(s for s in L if s)
