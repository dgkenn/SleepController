"""n-of-1 THERMAL DOSE-RESPONSE trial: what maintenance-temperature offset minimizes THIS
user's awakenings?

We ship population-default thermal targets (see ``Tunables.hot_sleeper_cool_bias_f`` /
``maintenance_settle_nudge_f``): a hot sleeper is assumed to want a COOL bias. But the user's
primary complaint is STAYING asleep, not falling asleep, and there is a specific, published,
counterintuitive result that argues the opposite for that exact problem: Raymann, Swaab & Van
Someren 2008 (Brain, DOI 10.1093/brain/awm315) found that a +0.4 C skin-temperature rise
SUPPRESSED nocturnal wakefulness and deepened sleep -- mild WARMING, not cooling, reduced
awakenings. We don't know which direction (or magnitude) works for THIS person, so instead of
guessing we run a randomized trial and let his own data answer it.

On a capped, block-balanced fraction of ELIGIBLE nights, the MAINTENANCE-phase neutral setpoint
is shifted by an offset drawn from a small ladder (default ``[-1.5, -0.75, 0.0, +0.4, +0.8]``
degrees F, 0.0 = the current policy / control arm). ``analyze_dose_response`` then compares
each arm's wake_events (primary), deep minutes / sleep efficiency / HRV / subjective morning
rating (secondary) against control, with a confidence interval and an honest verdict about
whether there's enough data to trust it.

Architecture deliberately mirrors ``sleepctl.ml.efficacy_trial`` (read that module's docstring
first -- this one assumes familiarity with it) and reuses its safety posture wholesale:

  * DETERMINISM: every assignment decision is a pure function of the calendar date string (+ a
    night-type block key + config) -- never ``random``/``datetime.now()`` -- so the whole
    schedule is reproducible/auditable from the DB alone. See ``_seed_fraction`` /
    ``_deterministic_permutation``.
  * ELIGIBILITY: only NORMAL, full-length, in-bed nights are ever randomized (``is_eligible``,
    byte-for-byte the same gate as ``efficacy_trial``). Short/work/recovery nights and
    nap/induction sessions ALWAYS get the control offset (0.0) -- a night that's already
    constrained never also gets a thermal experiment sprung on it.
  * FRACTION CAP: at most ``cfg.experimental_fraction`` (hard-capped at
    ``MAX_EXPERIMENTAL_FRACTION``) of ELIGIBLE nights ever run a NON-control offset.
  * AUTO-STOP: once an individual arm has enough resolved nights, if it is trending clearly
    worse than control on wake_events, THAT arm (only that arm) is suspended -- logged -- and
    every subsequent draw of it resolves to control until the trend clears.
  * ``enabled`` defaults to **False**. Unlike the efficacy micro-trial (which only toggles
    active-vs-sham CONTROL, not the temperature itself), this changes what temperature the bed
    actually runs at overnight -- it must be explicitly opted into.

Kept in its own table (``thermal_trials``, see ``sleepctl.storage.schema``) for the same reason
``efficacy_trial`` gives for not reusing ``efficacy_nights``: different arm vocabularies (a
continuous offset ladder vs a fixed active/sham pair) sharing a ``night_date``-keyed table would
let one randomization scheme silently clobber another's assignment.

Blocking / balance (new relative to ``efficacy_trial``, because this user is a shift worker and
night type is a huge confounder on wake_events): arms are assigned via PERMUTED-BLOCK
randomization, stratified by a night-type "block key" from the context. Concretely, for a given
block key the calendar is sliced into consecutive-day blocks sized to a "pool" built from the
ladder (each non-control offset once, plus enough repeated control slots to hit the target
``experimental_fraction`` -- see ``_expanded_pool``); each block is then independently and
deterministically shuffled (``_deterministic_permutation``, seeded off the block key + block
index -- never ``random``) and each calendar day in the block takes the next pool slot in shuffle
order. This guarantees EXACT arm balance within every full block for a given stratum (each arm
appears exactly as many times as its pool count, no more, no less) rather than only approximate
balance over a long random sequence -- important for an n-of-1 trial where "long run" may never
really arrive. Only ELIGIBLE nights actually apply their block-assigned offset; ineligible nights
still consume a pool slot (so the balance guarantee is about calendar-day slots, not realized
randomized nights) but always render as control regardless of what the block assigned.

Safety: the offset is applied by shifting ONLY ``SetpointProfile.neutral_f`` -- the same anchor
``ThermalController.target_for`` already reads for every maintenance-anchored intent (NEUTRAL,
WIND_DOWN, INDUCTION_COOL, REM_NEUTRAL, SETTLE_COOL) via ``neutral = p.neutral_f + bias`` -- via a
plain ``dataclasses.replace``, exactly like ``efficacy_trial.sham_profile``. Deep-bias and
wake-ramp targets are separate absolute anchors on the profile and are deliberately left alone:
the trial isolates the "what should the bed run at while trying to STAY asleep" question, not
deep-sleep or wake-ramp behavior. Nothing here touches slew limiting, the variability cap, the
55-110 F device clamp, or smart-wake -- those all still run downstream in ``ThermalController``
exactly as on any other night, and this module never sets an absolute target or device level
directly.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, replace
from datetime import date as _date
from typing import Dict, List, Optional

# Default maintenance-offset ladder (degrees F, relative to the learned neutral). 0.0 is the
# control arm (today's real policy). Overridable via ``ThermalTrialConfig.offset_ladder_f``.
DEFAULT_OFFSET_LADDER_F: List[float] = [-1.5, -0.75, 0.0, 0.4, 0.8]

# Hard ceiling on the experimental fraction regardless of config -- a dose-response ladder has
# several non-control arms (unlike efficacy_trial's single sham arm), so the cap is looser than
# MAX_SHAM_FRACTION (0.25), but the trial must still not dominate the schedule.
MAX_EXPERIMENTAL_FRACTION = 0.6

# Night types (SleepPlan.mode.value / ContextRecord.night_type) eligible for randomization --
# identical gate to sleepctl.ml.efficacy_trial: only a full-length NORMAL night is ever
# randomized; short/work/recovery nights need the plain, unexperimented default.
_ELIGIBLE_NIGHT_TYPES = ("normal",)

_SECONDARY_METRICS = ("deep_min", "sleep_efficiency", "hrv", "subjective_rating")


# --------------------------------------------------------------------------- eligibility


def is_eligible(context: dict) -> bool:
    """Identical eligibility gate to ``sleepctl.ml.efficacy_trial.is_eligible``: only a NORMAL,
    full-length, in-bed-for-the-night session may be randomized. See that module for the
    rationale -- kept as a literal duplicate (not a shared import) so each trial module stays
    independently readable/auditable and a change to one trial's eligibility can never silently
    change the other's."""
    night_type = context.get("night_type")
    session_mode = context.get("session_mode", "night")
    if session_mode != "night":
        return False
    return night_type in _ELIGIBLE_NIGHT_TYPES


def block_key(context: dict) -> str:
    """The stratification key used to balance arms WITHIN night-type strata (see module
    docstring): today's ``night_type`` if present, else ``"unknown"``.

    Currently ``is_eligible`` only ever randomizes 'normal' nights, so in practice this is
    almost always ``"normal"`` -- but keeping the stratification generic (a) makes the balance
    guarantee independently testable without threading eligibility through the test, and (b)
    means the mechanism is ready to extend, should a future arm ladder ever get randomized
    across more than one night type.
    """
    return str(context.get("night_type") or "unknown")


# --------------------------------------------------------------------------- deterministic draw


def _seed_fraction(key: str, salt: str = "sleepctl-thermal-dose-trial") -> float:
    """A deterministic pseudo-random draw in [0, 1) from a SHA-256 hash of ``salt:key``. Pure
    function of ``key`` -- no wall-clock, no RNG state -- mirrors
    ``efficacy_trial._seed_fraction`` exactly."""
    digest = hashlib.sha256(f"{salt}:{key}".encode()).hexdigest()
    n = int(digest[:15], 16)
    return n / float(16 ** 15)


def _deterministic_permutation(n: int, key: str) -> List[int]:
    """A deterministic Fisher-Yates shuffle of ``range(n)``, seeded off ``key`` alone (via
    ``_seed_fraction`` per swap position -- never ``random``). Pure function of ``(n, key)``:
    calling it twice with the same arguments always returns the same permutation, so the whole
    block-randomization schedule is reproducible from the date + block key alone."""
    idxs = list(range(n))
    for i in range(n - 1, 0, -1):
        f = _seed_fraction(f"{key}:{i}", salt="sleepctl-thermal-dose-trial-block")
        j = int(f * (i + 1))
        if j > i:  # guard the (astronomically rare) float-precision edge at f -> 1.0
            j = i
        idxs[i], idxs[j] = idxs[j], idxs[i]
    return idxs


# --------------------------------------------------------------------------- ladder / clamping


def _clamp_offset(offset_f: float, cfg) -> float:
    """Clamp a single offset to the configured comfort band (default +/-2.0 F). This is a
    SEPARATE, tighter clamp than the device's 55-110 F range -- it bounds how far the trial may
    push the setpoint away from neutral at all, before the usual downstream safety clamps
    (slew/variability/device range) even see the result."""
    band = abs(float(getattr(cfg, "comfort_band_f", 2.0)))
    return round(max(-band, min(band, float(offset_f))), 4)


def _clamped_ladder(cfg) -> List[float]:
    """The configured offset ladder, comfort-band-clamped and de-duplicated (two configured
    offsets can collapse to the same clamped value -- e.g. a comfort band tighter than the
    ladder's spread -- and de-duping keeps them as ONE arm rather than silently double-weighting
    it). The control offset is always included even if a caller's ladder omits it: a
    dose-response trial without a baseline arm can't answer anything."""
    raw = list(getattr(cfg, "offset_ladder_f", None) or DEFAULT_OFFSET_LADDER_F)
    control = _clamp_offset(getattr(cfg, "control_offset_f", 0.0), cfg)
    clamped = [_clamp_offset(o, cfg) for o in raw]
    if control not in clamped:
        clamped.append(control)
    out: List[float] = []
    for o in clamped:
        if o not in out:
            out.append(o)
    return out


def _format_arm(offset_f: float) -> str:
    """Canonical arm label for storage/analysis: a signed, fixed-precision string (e.g.
    '+0.40', '-1.50', '+0.00' for control) so the same offset always serializes identically."""
    return f"{offset_f:+.2f}"


def _parse_arm(arm_label) -> float:
    """Inverse of ``_format_arm`` -- tolerant of already-numeric input and bad rows (returns
    0.0 rather than raising, so a single malformed row can't crash ``analyze_dose_response``)."""
    try:
        return float(arm_label)
    except (TypeError, ValueError):
        return 0.0


# --------------------------------------------------------------------------- block randomization


def _expanded_pool(cfg) -> List[float]:
    """The block-randomization pool for one full block: each non-control offset exactly once,
    plus enough repeated control slots to make the pool's non-control share approximate
    ``cfg.experimental_fraction`` (clamped to ``MAX_EXPERIMENTAL_FRACTION``). Every scheduling
    block is one shuffled pass through this pool (see ``_block_offset``) -- so within any full
    block: (a) each non-control arm appears exactly once (perfect balance), and (b) the long-run
    non-control share tracks the configured fraction cap.
    """
    ladder = _clamped_ladder(cfg)
    control = _clamp_offset(getattr(cfg, "control_offset_f", 0.0), cfg)
    non_control = [o for o in ladder if not math.isclose(o, control, abs_tol=1e-6)]
    if not non_control:
        return [control]
    fraction = max(0.0, min(MAX_EXPERIMENTAL_FRACTION,
                            float(getattr(cfg, "experimental_fraction", 0.5))))
    if fraction <= 0.0:
        return [control]
    n_exp = len(non_control)
    if fraction >= 1.0:
        k_control = 0
    else:
        k_control = max(1, round(n_exp * (1.0 - fraction) / fraction))
    return non_control + [control] * k_control


def _block_offset(date_str: str, key: str, cfg) -> float:
    """The offset the permuted-block schedule assigns to ``date_str`` within stratum ``key`` --
    a pure function of ``(date_str, key, cfg)`` alone. Slices the calendar into consecutive-day
    blocks sized to ``_expanded_pool``, deterministically shuffles each block
    (``_deterministic_permutation``, seeded off the block's own key+index), and returns the pool
    slot ``date_str`` lands on. Does NOT consult eligibility or the repo -- callers (``assign_arm``)
    layer eligibility/auto-stop on top; this function alone is what the "balanced within a block"
    guarantee is about, and is directly testable in isolation.
    """
    pool = _expanded_pool(cfg)
    n = len(pool)
    if n <= 1:
        return pool[0] if pool else _clamp_offset(getattr(cfg, "control_offset_f", 0.0), cfg)
    ordinal = _date.fromisoformat(date_str).toordinal()
    block_index, position = divmod(ordinal, n)
    perm = _deterministic_permutation(n, f"{key}:{block_index}")
    return pool[perm[position]]


# --------------------------------------------------------------------------- auto-stop guardrail


def _auto_stopped_arms(repo, cfg) -> set:
    """The set of formatted arm labels currently trending CLEARLY worse than control on
    wake_events, past a minimum per-arm sample -- these must resolve to control until the trend
    clears. Mirrors ``efficacy_trial._auto_stop_triggered`` but per-arm (this trial has several
    experimental arms, not one sham arm, so only the specific offset that's underperforming is
    suspended -- the others keep running). Conservative by design: requires BOTH control and the
    candidate arm to have >= ``cfg.auto_stop_min_n`` resolved nights, and the arm's mean
    wake_events to exceed control's by >= ``cfg.auto_stop_threshold``.

    Returns an empty set (never auto-stop) if there's no repo to check history against, or not
    enough data yet -- the guardrail only ever acts on real evidence.
    """
    if repo is None:
        return set()
    min_n = max(1, int(getattr(cfg, "auto_stop_min_n", 6)))
    threshold = float(getattr(cfg, "auto_stop_threshold", 1.0))
    rows = [r for r in repo.thermal_trial_rows(resolved_only=True) if r.get("wake_events") is not None]
    control_label = _format_arm(_clamp_offset(getattr(cfg, "control_offset_f", 0.0), cfg))

    by_arm: Dict[str, List[float]] = {}
    for r in rows:
        by_arm.setdefault(r["arm"], []).append(float(r["wake_events"]))

    control_vals = by_arm.get(control_label, [])
    if len(control_vals) < min_n:
        return set()
    mean_control = sum(control_vals) / len(control_vals)

    bad = set()
    for arm, vals in by_arm.items():
        if arm == control_label or len(vals) < min_n:
            continue
        mean_arm = sum(vals) / len(vals)
        if (mean_arm - mean_control) >= threshold:
            bad.add(arm)
    return bad


def _log_auto_stop(repo, date_str: str, arm_label: str, cfg) -> None:
    """Best-effort structured-event log entry the first time auto-stop suppresses an arm
    assignment for a given night (never allowed to break the control loop)."""
    try:
        repo.log_event(
            "thermal_trial", "warn", "auto_stop",
            f"thermal dose-response trial auto-stopped arm {arm_label} for {date_str}: this "
            "offset is trending clearly worse on wake_events than control; forcing control "
            "until the trend clears.",
            {"night_date": date_str, "arm": arm_label},
        )
    except Exception:
        pass


# --------------------------------------------------------------------------- arm assignment


def assign_arm(date_str: str, context: dict, cfg, repo=None) -> float:
    """Decide tonight's maintenance offset (degrees F).

    Deterministic (a pure function of ``date_str`` + the context's block key + ``cfg`` -- never
    wall-clock/RNG), eligibility-gated (``is_eligible``), block-balanced within the night-type
    stratum (``_block_offset``), and auto-stop-aware (``_auto_stopped_arms``). A disabled trial,
    an ineligible night, or a currently-auto-stopped arm all resolve to the (comfort-clamped)
    control offset -- the SAFE default in every case.
    """
    control = _clamp_offset(getattr(cfg, "control_offset_f", 0.0), cfg)
    if not getattr(cfg, "enabled", False):
        return control
    if not is_eligible(context):
        return control

    key = block_key(context)
    offset = _block_offset(date_str, key, cfg)
    arm_label = _format_arm(offset)
    control_label = _format_arm(control)
    if arm_label != control_label and arm_label in _auto_stopped_arms(repo, cfg):
        _log_auto_stop(repo, date_str, arm_label, cfg)
        return control
    return offset


# --------------------------------------------------------------------------- profile application


def dose_response_profile(base_profile, offset_f: float, cfg):
    """The maintenance-phase ``SetpointProfile`` for tonight's assigned offset: the learned
    neutral shifted by the (comfort-band-clamped) offset, everything else on the profile left
    untouched. A plain ``dataclasses.replace`` -- exactly like ``efficacy_trial.sham_profile`` --
    so it flows through every existing safety layer unchanged downstream in
    ``ThermalController``: ``target_for`` still runs the result through ``clamp_fahrenheit``
    (the 55-110 F device range), ``resolve()`` still slew-limits and variability-caps the move,
    and ``to_level`` still clamps to ``level_min``/``level_max``. This function never computes or
    returns an absolute target or device level itself -- only a modified, still-fully-learnable
    ``SetpointProfile``.

    Only ``neutral_f`` moves. ``deep_bias_f`` and ``wake_ramp_f`` are separate absolute anchors
    the profile already carries (used by DEEP_BIAS_COOL / WAKE_RAMP, neither of which reads
    ``neutral_f`` at all -- see ``ThermalController.target_for``), so the trial doesn't touch
    them: it is answering "what should the bed run at while trying to STAY asleep", not
    perturbing deep-sleep or wake-ramp behavior it isn't testing.
    """
    clamped = _clamp_offset(offset_f, cfg)
    return replace(base_profile, neutral_f=base_profile.neutral_f + clamped)


def apply_trial_arm(repo, cfg, date_str: str, context: dict, base_profile):
    """Assign + persist tonight's dose-response arm and apply it to ``base_profile``. Returns
    ``(profile_for_tonight, info)`` where ``info`` is a dict describing the assignment (never
    None -- every night is recorded, including ineligible ones, with ``eligible: False`` for
    audit).

    ``cfg`` is the full ``AppConfig`` (``cfg.thermal_trial`` -- a ``ThermalTrialConfig`` --
    drives the assignment; see ``assign_arm``). This function never touches steering,
    pre-emption, or smart-wake -- only the setpoint's ``neutral_f`` (via
    ``dose_response_profile``); everything else about how the controller behaves tonight is
    untouched.
    """
    trial_cfg = getattr(cfg, "thermal_trial", cfg)  # accept a bare ThermalTrialConfig too
    eligible = is_eligible(context)
    key = block_key(context)
    offset = assign_arm(date_str, context, trial_cfg, repo=repo)
    arm_label = _format_arm(offset)
    seed = _seed_fraction(f"{key}:{date_str}")  # audit-trail draw only, not itself the decision

    if repo is not None:
        repo.assign_thermal_trial_night(date_str, arm_label, offset, eligible,
                                        block_key=key, seed=seed)

    profile = dose_response_profile(base_profile, offset, trial_cfg)
    return profile, {"arm": arm_label, "offset_f": offset, "eligible": eligible, "block_key": key}


# --------------------------------------------------------------------------- outcome recording


def record_trial_outcome(repo, night_date: str, wake_events=None, deep_min=None,
                         sleep_efficiency=None, hrv=None, subjective_rating=None) -> None:
    """Persist tonight's measured outcome against its already-assigned arm. No-op if the night
    was never assigned an arm (e.g. the trial wasn't wired in / the daemon restarted mid-night)."""
    if repo.thermal_trial_night(night_date) is None:
        return
    repo.record_thermal_trial_outcome(night_date, wake_events=wake_events, deep_min=deep_min,
                                      sleep_efficiency=sleep_efficiency, hrv=hrv,
                                      subjective_rating=subjective_rating)


def trial_rows(repo, resolved_only: bool = True) -> List[dict]:
    return repo.thermal_trial_rows(resolved_only=resolved_only)


# --------------------------------------------------------------------------- result type


@dataclass
class ThermalTrialResult:
    """One resolved dose-response trial night: the assignment + its measured outcome. A thin,
    typed view over a ``thermal_trials`` row (see ``sleepctl.storage.schema``)."""

    night_date: str
    arm: str                       # formatted offset, e.g. '+0.40' ('+0.00' = control)
    offset_f: float
    eligible: bool
    block_key: Optional[str] = None
    seed: Optional[float] = None
    wake_events: Optional[int] = None
    deep_min: Optional[float] = None
    sleep_efficiency: Optional[float] = None
    hrv: Optional[float] = None
    subjective_rating: Optional[float] = None

    @classmethod
    def from_row(cls, row: dict) -> "ThermalTrialResult":
        return cls(
            night_date=row.get("night_date"), arm=row.get("arm"),
            offset_f=row.get("offset_f", _parse_arm(row.get("arm"))),
            eligible=bool(row.get("eligible", 1)), block_key=row.get("block_key"),
            seed=row.get("seed"), wake_events=row.get("wake_events"),
            deep_min=row.get("deep_min"), sleep_efficiency=row.get("sleep_efficiency"),
            hrv=row.get("hrv"), subjective_rating=row.get("subjective_rating"),
        )


# --------------------------------------------------------------------------- pure-python stats
#
# Same hand-rolled, dependency-free Welch's t-test approach as efficacy_trial.py / sleepctl.
# eval.efficacy / sleepctl.experiments -- see those for the house-style rationale.


def _mean(xs: List[float]) -> Optional[float]:
    return sum(xs) / len(xs) if xs else None


def _stderr(xs: List[float]) -> Optional[float]:
    n = len(xs)
    if n < 2:
        return None
    m = sum(xs) / n
    var = sum((x - m) ** 2 for x in xs) / (n - 1)
    return (var / n) ** 0.5


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


# --------------------------------------------------------------------------- trend readout


def _trend_readout(arms_out: dict, arms_present: List[str]) -> dict:
    """A plain monotonic-trend readout across the offset ladder: as the offset moves from
    coolest to warmest, do mean wake_events fall monotonically (warmer is better), rise
    monotonically (cooler is better), or show no consistent direction? Purely descriptive --
    the CI-backed pairwise comparisons in ``analyze_dose_response`` carry the actual statistical
    claim; this is a cheap sanity readout of whether the arms line up the way a real dose
    response would (a genuine dose-response relationship should be monotonic; a scattered,
    non-monotonic pattern is a hint the "signal" seen so far may just be noise)."""
    points = [(arms_out[a]["offset_f"], arms_out[a]["mean_wake_events"])
              for a in arms_present if arms_out[a]["mean_wake_events"] is not None]
    points.sort(key=lambda p: p[0])
    if len(points) < 2:
        return {"direction": "insufficient_data",
                "note": "Need at least 2 arms with data to read a trend.", "points": []}

    diffs = [points[i + 1][1] - points[i][1] for i in range(len(points) - 1)]
    eps = 1e-9
    if all(d <= eps for d in diffs) and any(d < -eps for d in diffs):
        direction = "warmer_is_better"
        note = "Mean wake_events fall monotonically as the offset warms across tested arms."
    elif all(d >= -eps for d in diffs) and any(d > eps for d in diffs):
        direction = "cooler_is_better"
        note = "Mean wake_events rise monotonically as the offset warms across tested arms."
    else:
        direction = "no_clear_trend"
        note = "No consistent monotonic relationship between offset and wake_events yet."
    return {
        "direction": direction, "note": note,
        "points": [{"offset_f": o, "mean_wake_events": m} for o, m in points],
    }


# --------------------------------------------------------------------------- analysis


def analyze_dose_response(rows: List[dict], cfg=None, min_nights_per_arm: Optional[int] = None) -> dict:
    """Per-arm summary of the maintenance-offset ladder, pairwise comparisons against the
    control (0.0) arm, and a monotonic-trend readout -- the shape a genuine n-of-1
    dose-response readout needs: is there a dose relationship at all, and if so, does any tested
    offset actually beat what the controller runs today?

    ``rows`` is any iterable of dict-like ``thermal_trials`` rows (or
    ``ThermalTrialResult.__dict__``-shaped dicts) with at least ``arm`` + ``wake_events`` (+ the
    secondary metric keys when available). ``min_nights_per_arm`` overrides
    ``cfg.min_nights_before_verdict`` (defaults to 8 with no cfg) -- the floor below which this
    function refuses to name a winner: a difference measured from a handful of nights is noise,
    not an answer, and the returned ``confident`` flag plus ``verdict`` say so explicitly instead
    of implying a result.

    Returns::

        {control_arm, min_nights_per_arm, confident,
         arms: {arm_label: {offset_f, n, mean_wake_events, se_wake_events,
                             mean_deep_min, mean_sleep_efficiency, mean_hrv,
                             mean_subjective_rating}, ...},
         comparisons: {arm_label: {offset_f, diff_vs_control, ci_low, ci_high, p,
                                    n, n_control}, ...},
         trend: {direction, note, points},
         verdict}

    ``diff_vs_control`` = mean(arm) - mean(control) for wake_events; NEGATIVE means that arm had
    FEWER wake events than control (i.e. that offset looks better for staying asleep), POSITIVE
    means worse.
    """
    control = _clamp_offset(getattr(cfg, "control_offset_f", 0.0), cfg) if cfg is not None else 0.0
    control_label = _format_arm(control)
    min_n = min_nights_per_arm
    if min_n is None:
        min_n = int(getattr(cfg, "min_nights_before_verdict", 8)) if cfg is not None else 8

    by_arm: Dict[str, Dict[str, List[float]]] = {}
    for r in rows:
        arm = r.get("arm")
        if not arm:
            continue
        bucket = by_arm.setdefault(arm, {})
        wv = r.get("wake_events")
        if wv is not None:
            bucket.setdefault("wake_events", []).append(float(wv))
        for m in _SECONDARY_METRICS:
            v = r.get(m)
            if v is not None:
                bucket.setdefault(m, []).append(float(v))

    arms_present = sorted(by_arm.keys(), key=_parse_arm)
    arms_out: Dict[str, dict] = {}
    for arm in arms_present:
        wv = by_arm[arm].get("wake_events", [])
        se = _stderr(wv)
        entry = {
            "offset_f": _parse_arm(arm),
            "n": len(wv),
            "mean_wake_events": round(_mean(wv), 3) if wv else None,
            "se_wake_events": round(se, 3) if se is not None else None,
        }
        for m in _SECONDARY_METRICS:
            vals = by_arm[arm].get(m, [])
            entry[f"mean_{m}"] = round(_mean(vals), 3) if vals else None
        arms_out[arm] = entry

    control_vals = by_arm.get(control_label, {}).get("wake_events", [])
    comparisons: Dict[str, dict] = {}
    for arm in arms_present:
        if arm == control_label:
            continue
        vals = by_arm[arm].get("wake_events", [])
        diff, ci_low, ci_high, p = _welch(control_vals, vals)
        comparisons[arm] = {
            "offset_f": _parse_arm(arm),
            "diff_vs_control": diff, "ci_low": ci_low, "ci_high": ci_high, "p": p,
            "n": len(vals), "n_control": len(control_vals),
        }

    trend = _trend_readout(arms_out, arms_present)

    underpowered = [a for a in arms_present if arms_out[a]["n"] and arms_out[a]["n"] < min_n]
    have_control = len(control_vals) >= min_n
    have_experimental = any(a != control_label and arms_out[a]["n"] >= min_n for a in arms_present)
    confident = bool(have_control and have_experimental and not underpowered)

    verdict = _verdict(arms_out, comparisons, control_label, min_n, confident)

    return {
        "control_arm": control_label,
        "min_nights_per_arm": min_n,
        "confident": confident,
        "arms": arms_out,
        "comparisons": comparisons,
        "trend": trend,
        "verdict": verdict,
    }


def _verdict(arms_out: dict, comparisons: dict, control_label: str, min_n: int,
            confident: bool) -> str:
    if not confident:
        short = [f"{a} (n={arms_out[a]['n']})" for a in arms_out if arms_out[a]["n"] < min_n]
        missing_control = control_label not in arms_out or arms_out[control_label]["n"] < min_n
        detail = ", ".join(short) if short else ("control" if missing_control else "an arm")
        return (f"Not enough data yet for a dose-response verdict (need >= {min_n} nights/arm; "
                f"short: {detail}).")

    best_arm, best_diff = None, None
    for arm, comp in comparisons.items():
        if arms_out[arm]["n"] < min_n:
            continue
        diff = comp["diff_vs_control"]
        if diff is None:
            continue
        if best_diff is None or diff < best_diff:
            best_diff, best_arm = diff, arm

    if best_arm is None:
        return "No experimental arm has enough data yet to compare against control."

    comp = comparisons[best_arm]
    ci_low, ci_high, p = comp["ci_low"], comp["ci_high"], comp["p"]
    ci_excludes_zero = ci_low is not None and ((ci_low > 0) or (ci_high < 0))
    significant = ci_excludes_zero and (p is not None and p < 0.05)
    offset = arms_out[best_arm]["offset_f"]

    if significant and best_diff < 0:
        return (f"Offset {offset:+.2f} F reduces awakenings by {abs(best_diff):.2f}/night vs "
                f"control (n={arms_out[best_arm]['n']}, n_control={arms_out[control_label]['n']}, "
                f"p={p:.3f}, 95% CI [{ci_low:.2f}, {ci_high:.2f}]).")
    if significant and best_diff > 0:
        return (f"Every tested offset performs at or worse than control on wake_events; the "
                f"current policy ({control_label} F) still looks best (closest challenger "
                f"{offset:+.2f} F, delta={best_diff:.2f}/night, p={p:.3f}).")
    p_str = f"{p:.3f}" if p is not None else "n/a"
    return (f"No offset yet shows a statistically significant difference from control on "
            f"wake_events (closest: {offset:+.2f} F, delta={best_diff:.2f}/night, p={p_str}). "
            "Keep collecting nights.")
