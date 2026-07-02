"""Randomized EFFICACY MICRO-TRIALS: measure the controller's TRUE CAUSAL effect instead of
assuming it helps.

On a small, capped fraction of ELIGIBLE nights, the controller is randomized between:

  * ``active`` — normal closed-loop control (the learned setpoint, in-night thermal steering,
    predictive pre-emption — everything else in this repo).
  * ``sham``   — a conservative, do-no-harm fixed-neutral hold: no deep-bias experimentation, no
    in-night steering, no predictive pre-emption. Still a fully clamped, valid SetpointProfile —
    the 55-110 F device range and the existing slew/variability caps apply exactly as on any
    other night, and smart-wake is completely untouched. This is "less active control", never
    "no safety".

Over many nights this lets ``analyze_trials`` estimate the mean difference in wake_events (the
user's #1 problem: staying asleep) — plus deep%, HRV, and sleep efficiency — between the two arms,
with a confidence interval, instead of just assuming the controller is helping.

Distinct from the pre-existing standing trial in ``sleepctl.eval.efficacy`` (CONTROLLED vs HELD,
block-alternated, always-on once enabled, no eligibility gate): this module is ELIGIBILITY-GATED
(never randomizes a short/recovery/nap night — see ``is_eligible``), FRACTION-capped rather than
block-alternated (a small, configurable share of eligible nights, not every other block), and
AUTO-STOPS sham assignment if it is clearly trending worse. It is intentionally kept in its own
table (``efficacy_trials``, see ``sleepctl.storage.schema``) rather than reusing
``efficacy_nights``: the two systems use different arm vocabularies ('active'/'sham' vs
'controlled'/'held') and ``efficacy_nights.night_date`` is a primary key, so sharing rows between
the two would force one system to silently overwrite the other's assignment on any night both
happened to be enabled.

Safety invariants (do-no-harm):
  * ELIGIBILITY: only NORMAL, full-length nights are ever randomized (see ``is_eligible``). Short
    work nights, recovery nights, and nap/induction sessions ALWAYS run ``active`` — a sham night
    is never sprung on a night that's already constrained.
  * DETERMINISM: the coin flip is a pure hash of the calendar date string (never
    ``random``/``datetime.now()`` inside the decision), so a given date always assigns the same
    arm and the whole schedule is auditable/reproducible from the DB alone.
  * FRACTION CAP: at most ``cfg.sham_fraction`` (default ~20%, hard-capped at 25% by
    ``MAX_SHAM_FRACTION``) of ELIGIBLE nights are ever sham.
  * AUTO-STOP: once enough sham nights exist, if they are trending clearly worse on wake_events
    than active nights, sham assignment is suspended (logged) and every subsequent night runs
    active until the trend is no longer clear.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from typing import List, Optional

ACTIVE = "active"
SHAM = "sham"

# Hard ceiling on the sham fraction regardless of config — a micro-trial must never dominate the
# schedule. cfg.sham_fraction is clamped to this even if someone sets it higher.
MAX_SHAM_FRACTION = 0.25

# Night types (SleepPlan.mode.value, see sleepctl.benchmarks.NightMode) that are eligible for
# randomization. CONSTRAINED (short/work night) and RECOVERY (off-day / debt repayment) are
# deliberately excluded: those nights already need the FULL active policy, not an experiment.
_ELIGIBLE_NIGHT_TYPES = ("normal",)

_METRICS = ("wake_events", "deep_pct", "hrv", "efficiency")
_LOWER_BETTER = {"wake_events"}

_PREEMPT_DISABLED_THRESHOLD = 1.01  # above the max possible score (1.0): the gate never fires


# --------------------------------------------------------------------------- eligibility


def is_eligible(context: dict) -> bool:
    """A night is eligible for randomization only when it is a NORMAL, full-length, in-bed-for-
    the-night session: never a short/work night, a recovery night, or a nap/induction session.

    ``context`` is a plain dict (deliberately not coupled to any one dataclass) with (at least):
      * ``night_type``  -- ``SleepPlan.mode.value`` / ``ContextRecord.night_type``: 'normal' |
        'constrained' | 'recovery' | None. None (not yet planned) is NOT eligible -- randomize
        only once the plan has actually classified the night as normal.
      * ``session_mode`` -- 'night' | 'induce' | 'nap' (daemon session kind). Only 'night'
        sessions are eligible; naps/inductions are excluded even if `night_type` is unset.
    """
    night_type = context.get("night_type")
    session_mode = context.get("session_mode", "night")
    if session_mode != "night":
        return False
    return night_type in _ELIGIBLE_NIGHT_TYPES


# --------------------------------------------------------------------------- deterministic draw


def _seed_fraction(date_str: str, salt: str = "sleepctl-efficacy-micro-trial") -> float:
    """A deterministic pseudo-random draw in [0, 1) from a SHA-256 hash of ``salt:date_str``.

    Pure function of the date string -- no wall-clock, no RNG state -- so a given night's
    assignment is reproducible/auditable purely from the DB, and is stable no matter how many
    times (or in what order) it is recomputed."""
    digest = hashlib.sha256(f"{salt}:{date_str}".encode()).hexdigest()
    # Use the first 15 hex digits (60 bits) -- ample entropy, avoids float precision games with
    # the full 256-bit int.
    n = int(digest[:15], 16)
    return n / float(16 ** 15)


# --------------------------------------------------------------------------- auto-stop guardrail


def _auto_stop_triggered(repo, cfg) -> bool:
    """True when sham nights are trending CLEARLY worse than active nights on wake_events (the
    primary "stay asleep" outcome), past a small minimum sample -- in which case sham assignment
    must be suspended. Conservative by design: requires BOTH a minimum n per arm AND the sham
    mean to exceed the active mean by ``auto_stop_threshold`` extra wake_events/night.

    Returns False (never auto-stop) if there isn't a repo to check history against, or not
    enough data yet -- the guardrail only ever acts on real evidence, never a hunch."""
    if repo is None:
        return False
    min_n = max(1, int(getattr(cfg, "auto_stop_min_n", 6)))
    threshold = float(getattr(cfg, "auto_stop_threshold", 1.0))
    rows = [r for r in repo.efficacy_trial_rows(resolved_only=True) if r.get("wake_events") is not None]
    active = [r["wake_events"] for r in rows if r.get("arm") == ACTIVE]
    sham = [r["wake_events"] for r in rows if r.get("arm") == SHAM]
    if len(active) < min_n or len(sham) < min_n:
        return False
    mean_active = sum(active) / len(active)
    mean_sham = sum(sham) / len(sham)
    return (mean_sham - mean_active) >= threshold


def _log_auto_stop(repo, date_str: str, cfg) -> None:
    """Best-effort structured-event log entry the first time auto-stop suppresses a sham
    assignment for a given night (never allowed to break the control loop)."""
    try:
        repo.log_event(
            "efficacy_trial", "warn", "auto_stop",
            f"efficacy micro-trial auto-stopped sham assignment for {date_str}: sham nights are "
            "trending worse on wake_events; forcing active control until the trend clears.",
            {"night_date": date_str},
        )
    except Exception:
        pass


# --------------------------------------------------------------------------- arm assignment


def assign_arm(date_str: str, context: dict, cfg, repo=None) -> str:
    """Decide tonight's arm: 'active' | 'sham'.

    Deterministic (seeded off ``date_str`` alone), eligibility-gated (``is_eligible``),
    fraction-capped (``cfg.sham_fraction``, hard-clamped to ``MAX_SHAM_FRACTION``), and
    auto-stop-aware (suspends sham assignment -- logging it -- once sham is clearly trending
    worse; see ``_auto_stop_triggered``). Ineligible nights, a disabled trial, or an active
    auto-stop all resolve to 'active' -- the SAFE default in every case.
    """
    if not getattr(cfg, "enabled", True):
        return ACTIVE
    if not is_eligible(context):
        return ACTIVE
    if _auto_stop_triggered(repo, cfg):
        _log_auto_stop(repo, date_str, cfg)
        return ACTIVE

    fraction = max(0.0, min(MAX_SHAM_FRACTION, float(getattr(cfg, "sham_fraction", 0.2))))
    draw = _seed_fraction(date_str)
    return SHAM if draw < fraction else ACTIVE


# --------------------------------------------------------------------------- SHAM-arm application


def sham_profile(base_profile, cfg):
    """A strictly do-no-harm, fixed-neutral SetpointProfile for a SHAM night: the same neutral_f
    tunable every night starts from, all thermal STEERING biases zeroed (no deep-bias cooling, no
    REM-warm offset, no wake-ramp beyond plain neutral) -- but still a fully valid, clamped
    profile the existing thermal controller renders exactly like any other night. Keeps
    ``composite_bed_weight`` (a comfort blend, not an experimental steering behavior) untouched."""
    from dataclasses import replace

    t = cfg.tunables
    return replace(
        base_profile,
        neutral_f=t.neutral_temp_f,
        deep_bias_f=0.0,
        rem_warm_offset_f=0.0,
        wake_ramp_f=t.neutral_temp_f,
    )


def apply_trial_arm(repo, cfg, controller, date_str: str, context: dict, base_profile):
    """Assign + persist tonight's micro-trial arm and apply it to the controller. Returns
    ``(profile_for_tonight, info)`` where ``info`` is a dict describing the assignment (never
    None -- unlike the standing trial, this is on by default so every night is recorded, even
    ineligible ones, with ``eligible: False`` for audit).

    ``cfg`` is the full ``AppConfig`` (``cfg.tunables`` builds the sham profile / restores the
    preempt defaults, ``cfg.efficacy_trial`` -- an ``EfficacyTrialConfig`` -- drives the
    assignment itself; see ``assign_arm``).

    On a SHAM night this disables the same EXPERIMENTAL levers as the standing trial (in-night
    thermal steering + predictive pre-emption), reusing the controller's EXISTING setters --
    never the safety clamps, never smart-wake. On an ACTIVE night both preempt gates are
    explicitly restored to their configured default (undoing any prior SHAM night's override),
    because the detectors are constructed once at controller start-up and would otherwise
    silently carry a disabled threshold forward forever.
    """
    t = cfg.tunables
    trial_cfg = getattr(cfg, "efficacy_trial", cfg)  # accept a bare EfficacyTrialConfig too
    eligible = is_eligible(context)
    arm = assign_arm(date_str, context, trial_cfg, repo=repo)
    seed = _seed_fraction(date_str)

    if repo is not None:
        repo.assign_efficacy_trial_night(date_str, arm, eligible, seed)

    wra = getattr(controller, "wake_risk_assessor", None)
    pd = getattr(controller, "precursor_detector", None)

    if arm == SHAM:
        profile = sham_profile(base_profile, cfg)
        controller.set_steer_policy(actuate=False)  # no experimental deepen/REM-warm nudges
        if wra is not None:
            wra.preempt_threshold = _PREEMPT_DISABLED_THRESHOLD
        if pd is not None:
            pd.preempt_threshold = _PREEMPT_DISABLED_THRESHOLD
        return profile, {"arm": arm, "eligible": eligible, "seed": seed, "applied": True}

    # active: restore the real thresholds (undo any prior SHAM night's override)
    if wra is not None:
        wra.preempt_threshold = getattr(t, "wake_risk_preempt_threshold", 0.5)
    if pd is not None:
        pd.preempt_threshold = getattr(t, "precursor_preempt_threshold", 0.40)
    return base_profile, {"arm": arm, "eligible": eligible, "seed": seed, "applied": False}


# --------------------------------------------------------------------------- outcome recording


def record_trial_outcome(repo, night_date: str, wake_events=None, deep_pct=None, hrv=None,
                         efficiency=None, outcome_score=None) -> None:
    """Persist tonight's measured outcome against its already-assigned arm. No-op if the night
    was never assigned an arm (e.g. the trial wasn't wired in / the daemon restarted mid-night)."""
    if repo.efficacy_trial_night(night_date) is None:
        return
    repo.record_efficacy_trial_outcome(night_date, wake_events=wake_events, deep_pct=deep_pct,
                                       hrv=hrv, efficiency=efficiency,
                                       outcome_score=outcome_score)


def trial_rows(repo, resolved_only: bool = True) -> List[dict]:
    return repo.efficacy_trial_rows(resolved_only=resolved_only)


# --------------------------------------------------------------------------- result type


@dataclass
class EfficacyTrialResult:
    """One resolved micro-trial night: the assignment + its measured outcome. A thin, typed view
    over an ``efficacy_trials`` row (see ``sleepctl.storage.schema``)."""

    night_date: str
    arm: str                      # 'active' | 'sham'
    eligible: bool
    seed: Optional[float] = None
    wake_events: Optional[int] = None
    deep_pct: Optional[float] = None
    hrv: Optional[float] = None
    efficiency: Optional[float] = None
    outcome_score: Optional[float] = None

    @classmethod
    def from_row(cls, row: dict) -> "EfficacyTrialResult":
        return cls(
            night_date=row.get("night_date"), arm=row.get("arm"),
            eligible=bool(row.get("eligible", 1)), seed=row.get("seed"),
            wake_events=row.get("wake_events"), deep_pct=row.get("deep_pct"),
            hrv=row.get("hrv"), efficiency=row.get("efficiency"),
            outcome_score=row.get("outcome_score"),
        )


# --------------------------------------------------------------------------- pure-python stats
#
# Same hand-rolled, dependency-free approach as sleepctl.eval.efficacy / sleepctl.experiments:
# Welch's t-test (unequal variance, unpaired two-sample) for the mean difference + a 95% CI, with
# a normal-approximation two-sided p-value so this stays numpy/scipy-free (sleepctl.ml.linalg is
# the house style for "small, pure-python numerics" this mirrors).


def _mean(xs: List[float]) -> Optional[float]:
    return sum(xs) / len(xs) if xs else None


def _welch(a: List[float], b: List[float]):
    """95% CI + a two-sided p-value for mean(b) - mean(a) (Welch's t, unequal variance).
    Returns (diff, ci_low, ci_high, p) with None entries when there isn't enough data."""
    na, nb = len(a), len(b)
    if na < 2 or nb < 2:
        diff = (_mean(b) - _mean(a)) if (a and b) else None
        return (round(diff, 3) if diff is not None else None, None, None, None)
    ma, mb = sum(a) / na, sum(b) / nb
    va = sum((x - ma) ** 2 for x in a) / (na - 1)
    vb = sum((x - mb) ** 2 for x in b) / (nb - 1)
    se2 = va / na + vb / nb
    diff = mb - ma
    if se2 <= 0:
        return (round(diff, 3), round(diff, 3), round(diff, 3), 1.0 if diff == 0 else 0.0)
    se = se2 ** 0.5
    t = diff / se
    p = max(0.0, min(1.0, math.erfc(abs(t) / math.sqrt(2))))
    ci_low, ci_high = diff - 1.96 * se, diff + 1.96 * se
    return (round(diff, 3), round(ci_low, 3), round(ci_high, 3), round(p, 4))


def analyze_trials(rows: List[dict], min_nights_before_verdict: int = 10) -> dict:
    """Compare ACTIVE vs SHAM nights on wake_events (primary) / deep% / HRV / efficiency
    (secondary), estimating the mean SHAM-minus-ACTIVE difference with a Welch 95% CI + p-value
    per metric. ``rows`` is any iterable of dict-like rows (``efficacy_trials`` rows or
    ``EfficacyTrialResult.__dict__``-shaped dicts) with at least ``arm`` + the metric keys.

    Returns::

        {n_active, n_sham,
         wake_events: {diff, ci_low, ci_high, p, mean_active, mean_sham, n_active, n_sham},
         deep_pct:   {...same shape...},
         hrv:        {...same shape...},
         efficiency: {...same shape...},
         verdict}

    ``diff`` = mean(sham) - mean(active); for wake_events (lower is better) a POSITIVE diff with
    a CI excluding 0 means the controller is genuinely reducing awakenings. With too few nights,
    the verdict says so plainly instead of implying a result.
    """
    by_arm = {ACTIVE: {m: [] for m in _METRICS}, SHAM: {m: [] for m in _METRICS}}
    for r in rows:
        arm = r.get("arm")
        if arm not in by_arm:
            continue
        for m in _METRICS:
            v = r.get(m)
            if v is not None:
                by_arm[arm][m].append(float(v))

    n_active = len(by_arm[ACTIVE]["wake_events"]) or _first_len(by_arm[ACTIVE])
    n_sham = len(by_arm[SHAM]["wake_events"]) or _first_len(by_arm[SHAM])

    metrics_out = {}
    for m in _METRICS:
        a_vals, s_vals = by_arm[ACTIVE][m], by_arm[SHAM][m]
        diff, ci_low, ci_high, p = _welch(a_vals, s_vals)
        metrics_out[m] = {
            "diff": diff, "ci_low": ci_low, "ci_high": ci_high, "p": p,
            "mean_active": round(_mean(a_vals), 3) if a_vals else None,
            "mean_sham": round(_mean(s_vals), 3) if s_vals else None,
            "n_active": len(a_vals), "n_sham": len(s_vals),
            "lower_better": m in _LOWER_BETTER,
        }

    enough = n_active >= min_nights_before_verdict and n_sham >= min_nights_before_verdict
    verdict = _verdict(metrics_out, n_active, n_sham, min_nights_before_verdict, enough)

    return {
        "n_active": n_active, "n_sham": n_sham,
        "min_nights_before_verdict": min_nights_before_verdict,
        "enough_data": enough,
        "wake_events": metrics_out["wake_events"],
        "deep_pct": metrics_out["deep_pct"],
        "hrv": metrics_out["hrv"],
        "efficiency": metrics_out["efficiency"],
        "verdict": verdict,
    }


def _first_len(arm_dict: dict) -> int:
    for vals in arm_dict.values():
        if vals:
            return len(vals)
    return 0


def _verdict(metrics_out: dict, n_active: int, n_sham: int, min_n: int, enough: bool) -> str:
    if not enough:
        need_a = max(0, min_n - n_active)
        need_s = max(0, min_n - n_sham)
        return (f"Not enough data yet for a verdict (active n={n_active}, sham n={n_sham}; need "
                f">={min_n}/arm -- {need_a} more active, {need_s} more sham nights).")
    wake = metrics_out["wake_events"]
    diff, ci_low, ci_high, p = wake["diff"], wake["ci_low"], wake["ci_high"], wake["p"]
    if diff is None or ci_low is None:
        return "Not enough variance in wake_events yet to compare arms."
    ci_excludes_zero = (ci_low > 0) or (ci_high < 0)
    significant = (p is not None and p < 0.05) and ci_excludes_zero
    # diff = sham - active; a POSITIVE diff means sham had MORE wake_events, i.e. active control
    # (fewer wake_events) is genuinely helping.
    if significant and diff > 0:
        pct = (diff / wake["mean_sham"] * 100.0) if wake["mean_sham"] else None
        pct_str = f" ({pct:.0f}% fewer)" if pct is not None else ""
        return (f"The controller reduces awakenings by {diff:.2f}/night{pct_str} vs the sham "
                f"baseline (n_active={n_active}, n_sham={n_sham}, p={p:.3f}, "
                f"95% CI [{ci_low:.2f}, {ci_high:.2f}]).")
    if significant and diff < 0:
        return (f"Surprising: the sham baseline shows FEWER awakenings than active control "
                f"(Δ={diff:.2f}/night, n_active={n_active}, n_sham={n_sham}, p={p:.3f}). Worth "
                "investigating before trusting the controller's steering.")
    p_str = f"{p:.3f}" if p is not None else "n/a"
    return (f"No significant difference in awakenings yet (Δ={diff:.2f}/night, p={p_str}, "
            f"n_active={n_active}, n_sham={n_sham}). Keep collecting nights.")
