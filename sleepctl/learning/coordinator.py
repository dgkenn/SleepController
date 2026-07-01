"""Meta-learning confidence ledger + a conservative contradiction check.

With many independent learners (onset, settle/maintenance, wake ramp/tuning, lead time,
architecture steering, thermal calibration, comfort profile, resting baseline, setpoints,
baselines...) there is no single place that says "what has the system actually learned, from
how much data, how confident are we" — and nothing that would catch two learners quietly
pulling the SAME knob in opposite directions on the same night.

This module is a pure READ-MODEL: it re-reads each learner's *current* output (never retrains
anything, never touches the controller) and assembles a uniform ledger of entries, plus an
advisory (never enforced) contradiction check across them.

Each entry is a plain dict:
    {
        "name": str,            # learner/knob identifier, e.g. "onset.warm_nudge"
        "phase": str,           # "onset" | "maintenance" | "wake" | "thermal"
        "value": float | None,  # the current learned value
        "unit": str,            # "f" (degrees F, absolute or offset), "min", "ratio", "count"
        "source": str,          # "preset" | "learned" | "measured"
        "maturity": int,        # nights / samples backing the value
        "confidence": float,    # 0..1 heuristic from maturity + effect size
        "note": str,            # short plain-language rationale
    }

Every gatherer is wrapped in try/except so one learner's failure (missing table, empty
history, import error) can never sink the whole ledger — it just contributes nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Optional

# ----------------------------------------------------------------------------------- helpers


def _confidence_from_maturity(n: int, full_at: int, effect: float = 1.0) -> float:
    """A conservative 0..1 confidence heuristic: grows with sample size toward ``full_at``,
    scaled down when the learned effect is negligible (nothing has really moved off the
    preset, so there's nothing to be confident ABOUT yet beyond "the default holds")."""
    if n <= 0:
        return 0.0
    maturity = max(0.0, min(1.0, n / float(full_at)))
    effect_scale = max(0.15, min(1.0, abs(effect)))  # never fully zero -- data still counts
    return round(maturity * (0.4 + 0.6 * effect_scale), 3)


def _entry(name: str, phase: str, value: Optional[float], unit: str, source: str,
           maturity: int, confidence: float, note: str) -> dict:
    return {
        "name": name, "phase": phase, "value": value, "unit": unit, "source": source,
        "maturity": int(maturity), "confidence": round(max(0.0, min(1.0, confidence)), 3),
        "note": note,
    }


def _safe(fn: Callable[[], Optional[dict]]) -> Optional[dict]:
    """Run one gatherer; on ANY failure (missing data, import error, empty history) swallow it
    and contribute nothing rather than sinking the whole ledger."""
    try:
        return fn()
    except Exception:
        return None


# ------------------------------------------------------------------------------- gatherers


def _gather_onset(repo, cfg) -> Optional[dict]:
    from sleepctl.learning.onset_tuning import learn_onset, onset_records
    recs = onset_records(repo)
    m = learn_onset(recs, base_f=cfg.tunables.onset_warm_nudge_f)
    effect = abs(m.onset_warm_f - cfg.tunables.onset_warm_nudge_f) / max(
        1e-6, cfg.tunables.onset_warm_comfort_cap_f)
    return _entry(
        "onset.warm_nudge", "onset", m.onset_warm_f, "f",
        "learned" if m.is_personalized else "preset", m.n,
        _confidence_from_maturity(m.n, full_at=24, effect=effect), m.rationale,
    )


def _gather_settle(repo, cfg) -> Optional[dict]:
    from sleepctl.learning.settle import learn_settle_nudge
    val = learn_settle_nudge(repo, cfg)
    eff = repo.precool_efficacy() or {}
    n = sum(int(v.get("n", 0) or 0) for v in eff.values())
    base = cfg.tunables.maintenance_settle_nudge_f
    effect = abs(val - base) / max(1e-6, cfg.tunables.maintenance_settle_cap_f)
    source = "preset" if n < 6 else "learned"
    note = (f"settle nudge {val:+.2f} F from {n} pre-cool events"
            if n else "no pre-cool events logged yet — using the evidence-default nudge")
    return _entry("maintenance.settle_nudge", "maintenance", val, "f", source, n,
                  _confidence_from_maturity(n, full_at=20, effect=effect), note)


def _gather_lead_time(repo) -> Optional[dict]:
    from sleepctl.learning.lead_time import build_lead_time_profile
    prof = build_lead_time_profile(repo)
    eff = repo.precool_efficacy() or {}
    n = sum(int(v.get("n", 0) or 0) for v in eff.values())
    note = f"response lag ~{prof.response_lag_min:.1f} min ({prof.source})"
    return _entry("maintenance.lead_time", "maintenance", prof.response_lag_min, "min",
                  prof.source if prof.source != "preset" else "preset", n,
                  _confidence_from_maturity(n, full_at=12), note)


def _gather_wake_ramp(repo, cfg) -> Optional[dict]:
    from sleepctl.learning.wake_ramp import learn_wake_ramp
    base = cfg.tunables.wake_ramp_temp_f
    val = learn_wake_ramp(repo, cfg, current_f=base)
    n = 0
    try:
        sp_by_v = repo.setpoints_by_version()
        n = sum(1 for night in repo.recent_nights(21)
                if getattr(repo.get_context(night.date), "grogginess", None) is not None
                and getattr(sp_by_v.get(getattr(night, "setpoint_version", None)),
                            "wake_ramp_f", None) is not None)
    except Exception:
        n = 0
    effect = abs(val - base) / 8.0  # WAKE_RAMP_BOUNDS span ~16F, half-span normalizer
    source = "learned" if abs(val - base) >= 0.5 else "preset"
    note = (f"wake ramp adjusted to {val:.1f} F from grogginess check-ins"
            if source == "learned" else "no clear grogginess-vs-ramp signal yet")
    return _entry("wake.ramp_temp", "wake", val, "f", source, n,
                  _confidence_from_maturity(n, full_at=12, effect=effect), note)


def _gather_wake_tuning(repo, cfg) -> Optional[dict]:
    from sleepctl.learning.wake_tuning import learn_wake_tuning, wake_tuning_records
    recs = wake_tuning_records(repo)
    t = learn_wake_tuning(recs, base_window=cfg.tunables.wake_window_min)
    effect = abs(t.window_min - cfg.tunables.wake_window_min) / 10.0
    return _entry("wake.window_min", "wake", float(t.window_min), "min",
                  "learned" if t.is_personalized else "preset", t.n,
                  _confidence_from_maturity(t.n, full_at=20, effect=effect), t.rationale)


def _gather_thermal_wake(repo, cfg) -> Optional[dict]:
    from sleepctl.learning.thermal_wake import learn_thermal_wake, thermal_wake_records
    recs = thermal_wake_records(repo)
    m = learn_thermal_wake(recs, base_f=cfg.tunables.wake_ramp_temp_f)
    effect = abs(m.wake_f - cfg.tunables.wake_ramp_temp_f) / 8.0
    return _entry("wake.thermal_wake", "wake", m.wake_f, "f",
                  "learned" if m.is_personalized else "preset", m.n,
                  _confidence_from_maturity(m.n, full_at=20, effect=effect), m.rationale)


def _gather_deepening(repo) -> Optional[dict]:
    from sleepctl.learning.deepening import deepening_records, learn_deepening
    recs = deepening_records(repo)
    pol = learn_deepening(recs)
    n = pol.n_act + pol.n_ctrl
    note = pol.rationale
    return _entry("architecture.deepening_enabled", "maintenance",
                  1.0 if pol.enabled else 0.0, "ratio",
                  "learned" if pol.is_personalized else "preset", n,
                  pol.confidence, note)


def _gather_setpoints(repo) -> Optional[dict]:
    sp = repo.latest_setpoints()
    if sp is None:
        return None
    n = len(repo.recent_nights(60))
    source = "learned" if sp.source not in (None, "default", "preset") else "preset"
    note = f"setpoints v{sp.version} ({sp.source})"
    return _entry("setpoints.neutral", "maintenance", sp.neutral_f, "f", source, n,
                  _confidence_from_maturity(n, full_at=30), note)


def _gather_thermal_calibration(repo) -> Optional[dict]:
    cal = repo.get_thermal_calibration()
    if not cal:
        return None
    n = 1  # a singleton self-test measurement, not per-night; treat as a single high-trust sample
    note = f"measured cool rate {cal.get('cool_f_per_min')} F/min ({cal.get('source')})"
    return _entry("thermal.cool_rate", "thermal", cal.get("cool_f_per_min"), "f/min",
                  "measured", n, 0.7 if cal.get("cool_f_per_min") is not None else 0.0, note)


def _gather_comfort_profile(repo) -> Optional[dict]:
    prof = repo.get_comfort_profile()
    if not prof:
        return None
    note = f"comfort-sweep neutral {prof.get('neutral_f')} F ({prof.get('source')})"
    return _entry("thermal.comfort_neutral", "thermal", prof.get("neutral_f"), "f",
                  "measured", 1, 0.7 if prof.get("neutral_f") is not None else 0.0, note)


def _gather_resting_baseline(repo) -> Optional[dict]:
    base = repo.get_resting_baseline()
    if not base:
        return None
    n = int(base.get("n_samples") or 0)
    note = f"resting HR {base.get('hr')} bpm from {n} samples ({base.get('source')})"
    return _entry("thermal.resting_hr", "thermal", base.get("hr"), "bpm", "measured", n,
                  _confidence_from_maturity(n, full_at=200), note)


def _gather_baselines(repo) -> Optional[dict]:
    b = repo.latest_baselines()
    if b is None:
        return None
    n = int(b.metrics.get("total_sleep_min_14d_n", 0) or 0)
    val = b.metrics.get("wake_events_7d_median")
    note = "rolling 7/14-day robust baselines (median + MAD)"
    return _entry("baselines.wake_events_median", "maintenance", val, "count", "measured", n,
                  _confidence_from_maturity(n, full_at=14), note)


# The full list of gatherers, each (name-for-debugging, fn(repo, cfg) -> Optional[dict]).
_GATHERERS: list[tuple[str, Callable[[Any, Any], Optional[dict]]]] = [
    ("onset", lambda repo, cfg: _gather_onset(repo, cfg)),
    ("settle", lambda repo, cfg: _gather_settle(repo, cfg)),
    ("lead_time", lambda repo, cfg: _gather_lead_time(repo)),
    ("wake_ramp", lambda repo, cfg: _gather_wake_ramp(repo, cfg)),
    ("wake_tuning", lambda repo, cfg: _gather_wake_tuning(repo, cfg)),
    ("thermal_wake", lambda repo, cfg: _gather_thermal_wake(repo, cfg)),
    ("deepening", lambda repo, cfg: _gather_deepening(repo)),
    ("setpoints", lambda repo, cfg: _gather_setpoints(repo)),
    ("thermal_calibration", lambda repo, cfg: _gather_thermal_calibration(repo)),
    ("comfort_profile", lambda repo, cfg: _gather_comfort_profile(repo)),
    ("resting_baseline", lambda repo, cfg: _gather_resting_baseline(repo)),
    ("baselines", lambda repo, cfg: _gather_baselines(repo)),
]


def learning_ledger(repo, cfg=None) -> list[dict]:
    """Gather every learner's CURRENT state into a uniform list of ledger entries.

    Read-model only: never retrains, never mutates anything. Resilient — any gatherer that
    raises (missing table, no history, stale schema) simply contributes no entry."""
    if cfg is None:
        from sleepctl.config import AppConfig
        cfg = AppConfig.default()
    entries: list[dict] = []
    for _name, fn in _GATHERERS:
        result = _safe(lambda fn=fn: fn(repo, cfg))
        if result is not None:
            entries.append(result)
    return entries


# --------------------------------------------------------------------------- contradictions

# Entries whose "value" is a temperature (absolute °F or a signed °F offset/nudge) that pushes
# the SAME phase's effective temperature warmer/cooler. Only these are compared for
# contradictions — comparing a temperature nudge against e.g. a window_min or a ratio is
# meaningless.
_TEMP_UNITS = {"f", "f/min"}

# Small threshold: two independent learners both nudging within this combined spread aren't
# worth flagging (noise); only flag when they'd pull genuinely opposite directions by more than
# this many combined degrees.
_CONTRADICTION_THRESHOLD_F = 1.5


def detect_contradictions(entries: list[dict], threshold_f: float = _CONTRADICTION_THRESHOLD_F
                          ) -> list[dict]:
    """Advisory-only: flag pairs of entries in the SAME phase that are both confidently
    "learned" (not still on preset) and whose signed temperature values disagree in direction
    by more than ``threshold_f`` combined. Never mutates anything — purely a warning surfaced
    to the user; the controller and learners are untouched.

    Returns a list of {"phase", "a", "b", "combined_spread_f", "message"} dicts.
    """
    warnings: list[dict] = []
    by_phase: dict[str, list[dict]] = {}
    for e in entries:
        if e.get("unit") not in _TEMP_UNITS or e.get("value") is None:
            continue
        if e.get("source") != "learned":
            continue  # only compare things that have actually moved off the evidence default
        by_phase.setdefault(e["phase"], []).append(e)

    for phase, group in by_phase.items():
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                a, b = group[i], group[j]
                va, vb = a["value"], b["value"]
                # opposite signs (or one pulls warm, one pulls cool) and a meaningful combined
                # magnitude -> advisory contradiction.
                if va == 0 or vb == 0:
                    continue
                same_direction = (va > 0) == (vb > 0)
                if same_direction:
                    continue
                combined = abs(va) + abs(vb)
                if combined <= threshold_f:
                    continue
                warnings.append({
                    "phase": phase,
                    "a": a["name"],
                    "b": b["name"],
                    "combined_spread_f": round(combined, 2),
                    "message": (
                        f"In '{phase}': {a['name']} pushes "
                        f"{'warmer' if va > 0 else 'cooler'} ({va:+.2f} F) while "
                        f"{b['name']} pushes {'warmer' if vb > 0 else 'cooler'} "
                        f"({vb:+.2f} F) — {combined:.1f} F combined spread. Advisory only; "
                        "no automatic override applied."
                    ),
                })
    return warnings


@dataclass
class LedgerReport:
    entries: list[dict]
    contradictions: list[dict]

    def to_dict(self) -> dict:
        return {"entries": self.entries, "contradictions": self.contradictions}


def build_ledger_report(repo, cfg=None) -> LedgerReport:
    """Convenience: the ledger + its contradiction check together."""
    entries = learning_ledger(repo, cfg)
    return LedgerReport(entries=entries, contradictions=detect_contradictions(entries))
