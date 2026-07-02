"""Randomized efficacy MICRO-trials (sleepctl.ml.efficacy_trial): eligibility gating,
deterministic + fraction-capped arm assignment, the auto-stop guardrail, do-no-harm SHAM-arm
application, and the pure-python causal-effect analysis."""

import tempfile
from dataclasses import dataclass
from datetime import date, timedelta

import pytest

from sleepctl.config import AppConfig, EfficacyTrialConfig
from sleepctl.ml.efficacy_trial import (
    ACTIVE,
    MAX_SHAM_FRACTION,
    SHAM,
    EfficacyTrialResult,
    analyze_trials,
    apply_trial_arm,
    assign_arm,
    is_eligible,
    record_trial_outcome,
    sham_profile,
)
from sleepctl.storage.repository import Repository


@pytest.fixture
def repo():
    r = Repository(tempfile.mktemp(suffix=".db"))
    yield r
    r.close()


def _dates(n, start=date(2026, 1, 1)):
    return [(start + timedelta(days=i)).isoformat() for i in range(n)]


# --------------------------------------------------------------------------- eligibility


def test_eligible_only_on_normal_night_sessions():
    assert is_eligible({"night_type": "normal", "session_mode": "night"}) is True


@pytest.mark.parametrize("night_type", ["constrained", "recovery", None, "bogus"])
def test_ineligible_night_types(night_type):
    assert is_eligible({"night_type": night_type, "session_mode": "night"}) is False


@pytest.mark.parametrize("session_mode", ["nap", "induce"])
def test_ineligible_non_night_sessions(session_mode):
    # Even a 'normal'-labeled night_type must not be randomized during a nap/induce session.
    assert is_eligible({"night_type": "normal", "session_mode": session_mode}) is False


# --------------------------------------------------------------------------- assign_arm safety


def test_never_sham_on_short_recovery_or_nap_nights():
    """assign_arm must ALWAYS return 'active' on ineligible nights, across many dates and a
    high sham_fraction, so an ineligible night is never randomized no matter the draw."""
    cfg = EfficacyTrialConfig(enabled=True, sham_fraction=MAX_SHAM_FRACTION)
    for night_type in ("constrained", "recovery", None):
        for d in _dates(60):
            arm = assign_arm(d, {"night_type": night_type, "session_mode": "night"}, cfg)
            assert arm == ACTIVE
    for d in _dates(60):
        arm = assign_arm(d, {"night_type": "normal", "session_mode": "nap"}, cfg)
        assert arm == ACTIVE


def test_disabled_trial_always_active():
    cfg = EfficacyTrialConfig(enabled=False, sham_fraction=MAX_SHAM_FRACTION)
    for d in _dates(30):
        assert assign_arm(d, {"night_type": "normal", "session_mode": "night"}, cfg) == ACTIVE


def test_assign_arm_is_deterministic_for_a_fixed_date():
    cfg = EfficacyTrialConfig(enabled=True, sham_fraction=0.2)
    context = {"night_type": "normal", "session_mode": "night"}
    results = {assign_arm("2026-03-14", context, cfg) for _ in range(20)}
    assert len(results) == 1  # same date -> same arm every time, no wall-clock/RNG involved


def test_assign_arm_reproducible_across_fresh_config_instances():
    """A different (but equal-valued) config object must produce the SAME assignment -- the seed
    is purely a hash of the date string, never call order or object identity."""
    context = {"night_type": "normal", "session_mode": "night"}
    a = assign_arm("2026-05-01", context, EfficacyTrialConfig(enabled=True, sham_fraction=0.2))
    b = assign_arm("2026-05-01", context, EfficacyTrialConfig(enabled=True, sham_fraction=0.2))
    assert a == b


# --------------------------------------------------------------------------- fraction cap


def test_fraction_cap_respected_over_many_eligible_nights():
    cfg = EfficacyTrialConfig(enabled=True, sham_fraction=0.2)
    context = {"night_type": "normal", "session_mode": "night"}
    dates = _dates(2000)
    arms = [assign_arm(d, context, cfg) for d in dates]
    frac_sham = arms.count(SHAM) / len(arms)
    # Deterministic hash draw over many dates should land close to the target fraction.
    assert 0.15 <= frac_sham <= 0.25


def test_sham_fraction_hard_capped_even_if_config_requests_more():
    """Even if sham_fraction is configured absurdly high, MAX_SHAM_FRACTION caps it."""
    cfg = EfficacyTrialConfig(enabled=True, sham_fraction=0.9)
    context = {"night_type": "normal", "session_mode": "night"}
    dates = _dates(2000)
    arms = [assign_arm(d, context, cfg) for d in dates]
    frac_sham = arms.count(SHAM) / len(arms)
    assert frac_sham <= MAX_SHAM_FRACTION + 0.03  # small slack for hash-draw noise


# --------------------------------------------------------------------------- auto-stop


def _seed_trial_rows(repo, active_wake, sham_wake, start=date(2026, 6, 1)):
    for i, w in enumerate(active_wake):
        d = (start + timedelta(days=i)).isoformat()
        repo.assign_efficacy_trial_night(d, ACTIVE, True, 0.1)
        repo.record_efficacy_trial_outcome(d, wake_events=w)
    for i, w in enumerate(sham_wake):
        d = (start + timedelta(days=100 + i)).isoformat()
        repo.assign_efficacy_trial_night(d, SHAM, True, 0.1)
        repo.record_efficacy_trial_outcome(d, wake_events=w)


def test_auto_stop_triggers_when_sham_trends_clearly_worse(repo):
    cfg = EfficacyTrialConfig(enabled=True, sham_fraction=MAX_SHAM_FRACTION,
                              auto_stop_min_n=6, auto_stop_threshold=1.0)
    # sham is trending much worse: active averages ~1 wake event, sham averages ~4.
    _seed_trial_rows(repo, active_wake=[1, 1, 2, 1, 1, 0, 1], sham_wake=[4, 5, 4, 5, 4, 5, 4])
    context = {"night_type": "normal", "session_mode": "night"}
    # A brand-new date that would otherwise draw 'sham' under this fraction/date-hash must be
    # forced 'active' once the guardrail has tripped.
    forced_active_count = 0
    for d in _dates(40, start=date(2027, 1, 1)):
        arm = assign_arm(d, context, cfg, repo=repo)
        if arm == SHAM:
            forced_active_count = -1  # a sham slipped through -- guardrail failed
            break
    assert forced_active_count == 0
    # And it must have logged the auto-stop event (best-effort, never raises).
    events = repo.conn.execute(
        "SELECT * FROM events WHERE category='efficacy_trial' AND code='auto_stop'").fetchall()
    assert len(events) >= 1


def test_auto_stop_does_not_trigger_on_thin_data(repo):
    cfg = EfficacyTrialConfig(enabled=True, sham_fraction=MAX_SHAM_FRACTION,
                              auto_stop_min_n=6, auto_stop_threshold=1.0)
    # Only 2 nights per arm -- below auto_stop_min_n -- even though sham looks worse.
    _seed_trial_rows(repo, active_wake=[1, 1], sham_wake=[5, 5])
    context = {"night_type": "normal", "session_mode": "night"}
    arms = {assign_arm(d, context, cfg, repo=repo) for d in _dates(40, start=date(2027, 2, 1))}
    assert SHAM in arms  # guardrail must not fire on too little evidence


def test_auto_stop_does_not_trigger_when_arms_are_similar(repo):
    cfg = EfficacyTrialConfig(enabled=True, sham_fraction=MAX_SHAM_FRACTION,
                              auto_stop_min_n=6, auto_stop_threshold=1.0)
    _seed_trial_rows(repo, active_wake=[2, 3, 2, 3, 2, 3], sham_wake=[2, 3, 3, 2, 3, 2])
    context = {"night_type": "normal", "session_mode": "night"}
    arms = {assign_arm(d, context, cfg, repo=repo) for d in _dates(40, start=date(2027, 3, 1))}
    assert SHAM in arms


# --------------------------------------------------------------------------- SHAM-arm application


def test_sham_profile_stays_within_device_clamp_and_zeroes_steering():
    cfg = AppConfig.default()
    base = cfg.default_setpoints()
    prof = sham_profile(base, cfg)
    assert prof.deep_bias_f == 0.0
    assert prof.rem_warm_offset_f == 0.0
    assert prof.neutral_f == cfg.tunables.neutral_temp_f
    assert prof.wake_ramp_f == cfg.tunables.neutral_temp_f
    # Still inside the Eight Sleep Pod's real 55-110 F device range -- do-no-harm never means
    # "no clamp", it means "the SAME clamp as every other night".
    assert 55.0 <= prof.neutral_f <= 110.0
    assert 55.0 <= prof.wake_ramp_f <= 110.0
    # The magnitude of the neutral-hold move is bounded by the tunables' own variability cap --
    # it never demands a bigger single-night swing than the controller already allows.
    assert abs(prof.neutral_f - base.neutral_f) <= cfg.tunables.variability_cap_f + 1e-6


def test_apply_trial_arm_inactive_forces_active_on_ineligible_night(repo):
    from sleepctl.controller.controller import SleepController
    cfg = AppConfig.default()
    controller = SleepController(cfg)
    base = cfg.default_setpoints()
    context = {"night_type": "constrained", "session_mode": "night"}  # short work night
    prof, info = apply_trial_arm(repo, cfg, controller, "2026-07-01", context, base)
    assert info["arm"] == ACTIVE
    assert info["eligible"] is False
    assert prof is base  # untouched -- the full active policy runs exactly as normal


def test_sham_night_disables_steering_and_preemption_do_no_harm(repo):
    from sleepctl.controller.controller import SleepController
    cfg = AppConfig.default()
    controller = SleepController(cfg)
    cfg.efficacy_trial = EfficacyTrialConfig(enabled=True, sham_fraction=MAX_SHAM_FRACTION)
    context = {"night_type": "normal", "session_mode": "night"}

    # Walk dates until a SHAM night is drawn (deterministic schedule; the fraction cap guarantees
    # one shows up within a modest number of days).
    info = None
    for d in _dates(60):
        base = cfg.default_setpoints()
        prof, info = apply_trial_arm(repo, cfg, controller, d, context, base)
        if info["arm"] == SHAM:
            break
    assert info["arm"] == SHAM and info["eligible"] is True
    assert prof.deep_bias_f == 0.0 and prof.rem_warm_offset_f == 0.0
    assert controller.steer_actuate is False
    assert controller.wake_risk_assessor.preempt_threshold > 1.0
    assert controller.precursor_detector.preempt_threshold > 1.0
    assert 55.0 <= prof.neutral_f <= 110.0  # still a normal, fully clamped profile


def test_active_night_restores_preempt_thresholds_after_a_sham_night(repo):
    from sleepctl.controller.controller import SleepController
    cfg = AppConfig.default()
    controller = SleepController(cfg)
    cfg.efficacy_trial = EfficacyTrialConfig(enabled=True, sham_fraction=MAX_SHAM_FRACTION)
    context = {"night_type": "normal", "session_mode": "night"}

    seen_active_after_sham = False
    prev_arm = None
    for d in _dates(60):
        base = cfg.default_setpoints()
        _, info = apply_trial_arm(repo, cfg, controller, d, context, base)
        if prev_arm == SHAM and info["arm"] == ACTIVE:
            seen_active_after_sham = True
            assert controller.wake_risk_assessor.preempt_threshold <= 1.0
            assert controller.precursor_detector.preempt_threshold <= 1.0
        prev_arm = info["arm"]
    assert seen_active_after_sham


def test_apply_trial_arm_persists_assignment_idempotently(repo):
    from sleepctl.controller.controller import SleepController
    cfg = AppConfig.default()
    controller = SleepController(cfg)
    context = {"night_type": "normal", "session_mode": "night"}
    base = cfg.default_setpoints()
    _, info1 = apply_trial_arm(repo, cfg, controller, "2026-08-01", context, base)
    _, info2 = apply_trial_arm(repo, cfg, controller, "2026-08-01", context, base)
    assert info1["arm"] == info2["arm"]
    row = repo.efficacy_trial_night("2026-08-01")
    assert row is not None and row["arm"] == info1["arm"]


# --------------------------------------------------------------------------- outcome recording


def test_record_outcome_noop_without_assignment(repo):
    record_trial_outcome(repo, "2026-09-01", wake_events=2)
    assert repo.efficacy_trial_night("2026-09-01") is None


def test_record_outcome_persists_against_assigned_arm(repo):
    repo.assign_efficacy_trial_night("2026-09-05", ACTIVE, True, 0.05)
    record_trial_outcome(repo, "2026-09-05", wake_events=1, deep_pct=0.22, hrv=65.0,
                         efficiency=0.91, outcome_score=5.0)
    row = repo.efficacy_trial_night("2026-09-05")
    assert row["resolved"] == 1
    assert row["wake_events"] == 1
    assert row["hrv"] == pytest.approx(65.0)
    assert row["deep_pct"] == pytest.approx(0.22)


# --------------------------------------------------------------------------- EfficacyTrialResult


def test_efficacy_trial_result_from_row():
    row = {"night_date": "2026-01-01", "arm": "sham", "eligible": 1, "seed": 0.05,
          "wake_events": 2, "deep_pct": 0.18, "hrv": 60.0, "efficiency": 0.9,
          "outcome_score": 3.0}
    result = EfficacyTrialResult.from_row(row)
    assert result.arm == "sham" and result.eligible is True and result.wake_events == 2


# --------------------------------------------------------------------------- analyze_trials


def _rows(active_wake, sham_wake, extra=None):
    rows = []
    for w in active_wake:
        r = {"arm": ACTIVE, "wake_events": w, "deep_pct": 0.20, "efficiency": 0.90, "hrv": 60.0}
        if extra:
            r.update(extra)
        rows.append(r)
    for w in sham_wake:
        r = {"arm": SHAM, "wake_events": w, "deep_pct": 0.20, "efficiency": 0.90, "hrv": 60.0}
        if extra:
            r.update(extra)
        rows.append(r)
    return rows


def test_analyze_trials_wide_ci_on_tiny_n():
    rows = _rows(active_wake=[1, 2], sham_wake=[3, 4])
    out = analyze_trials(rows, min_nights_before_verdict=10)
    assert out["enough_data"] is False
    assert "Not enough data" in out["verdict"]
    assert out["n_active"] == 2 and out["n_sham"] == 2


def test_analyze_trials_recovers_injected_active_better_effect():
    """Synthetic data where ACTIVE clearly, consistently beats SHAM on wake_events: the sign of
    the estimated effect must say the controller helps, and the 95% CI must exclude 0."""
    active = [1, 1, 2, 1, 1, 0, 1, 1, 2, 1, 1, 0]
    sham = [4, 5, 4, 5, 4, 5, 4, 5, 4, 5, 4, 5]
    rows = _rows(active, sham)
    out = analyze_trials(rows, min_nights_before_verdict=10)
    assert out["enough_data"] is True
    wake = out["wake_events"]
    assert wake["diff"] > 0  # sham - active > 0 => sham has MORE awakenings => active helps
    assert wake["ci_low"] > 0  # CI excludes 0
    assert wake["p"] is not None and wake["p"] < 0.05
    assert "reduces awakenings" in out["verdict"]


def test_analyze_trials_no_significant_difference():
    active = [2, 3, 2, 3, 2, 3, 2, 3, 2, 3]
    sham = [2, 3, 3, 2, 3, 2, 3, 2, 3, 2]
    rows = _rows(active, sham)
    out = analyze_trials(rows, min_nights_before_verdict=10)
    assert out["enough_data"] is True
    assert "No significant difference" in out["verdict"]
    wake = out["wake_events"]
    assert wake["ci_low"] <= 0 <= wake["ci_high"]  # CI includes 0


def test_analyze_trials_includes_all_secondary_metrics():
    rows = _rows([1, 2, 1, 2, 1, 2, 1, 2, 1, 2], [3, 4, 3, 4, 3, 4, 3, 4, 3, 4])
    out = analyze_trials(rows, min_nights_before_verdict=10)
    for key in ("wake_events", "deep_pct", "hrv", "efficiency"):
        assert key in out
        for stat_key in ("diff", "ci_low", "ci_high", "p"):
            assert stat_key in out[key]


def test_analyze_trials_from_repo_rows_shape(repo):
    """End-to-end: rows straight out of the repository feed analyze_trials without adaptation."""
    _seed_trial_rows(repo, active_wake=[1, 1, 2, 1, 1, 0, 1, 1, 2, 1],
                     sham_wake=[4, 5, 4, 5, 4, 5, 4, 5, 4, 5])
    rows = repo.efficacy_trial_rows(resolved_only=True)
    out = analyze_trials(rows, min_nights_before_verdict=10)
    assert out["n_active"] == 10 and out["n_sham"] == 10
    assert out["wake_events"]["diff"] > 0
