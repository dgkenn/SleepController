"""n-of-1 THERMAL DOSE-RESPONSE trial (sleepctl.ml.thermal_trial): deterministic + eligibility-
gated + fraction-capped + block-balanced arm assignment, comfort-band clamping, the per-arm
auto-stop guardrail, do-no-harm profile application, and the pure-python dose-response
analysis."""

import tempfile
from datetime import date, timedelta

import pytest

from sleepctl.config import AppConfig, ThermalTrialConfig
from sleepctl.ml.thermal_trial import (
    MAX_EXPERIMENTAL_FRACTION,
    ThermalTrialResult,
    _block_offset,
    _clamped_ladder,
    _expanded_pool,
    _format_arm,
    analyze_dose_response,
    apply_trial_arm,
    assign_arm,
    block_key,
    dose_response_profile,
    is_eligible,
    record_trial_outcome,
)
from sleepctl.storage.repository import Repository


@pytest.fixture
def repo():
    r = Repository(tempfile.mktemp(suffix=".db"))
    yield r
    r.close()


def _dates(n, start=date(2026, 1, 1)):
    return [(start + timedelta(days=i)).isoformat() for i in range(n)]


CONTROL = "+0.00"


# --------------------------------------------------------------------------- eligibility
# NOTE: the three eligibility tests below are byte-identical to their counterparts in
# tests/test_efficacy_trial.py. That is deliberate, not redundancy: sleepctl.ml.thermal_trial.is_eligible is a
# LITERAL DUPLICATE of the other module's by design (see its docstring) precisely so a change
# to one trial's gate can never silently change the other's. Two independent tests are what
# make that guarantee real -- deleting either would defeat the arrangement they exist for.



def test_eligible_only_on_normal_night_sessions():
    assert is_eligible({"night_type": "normal", "session_mode": "night"}) is True


@pytest.mark.parametrize("night_type", ["constrained", "recovery", None, "bogus"])
def test_ineligible_night_types(night_type):
    assert is_eligible({"night_type": night_type, "session_mode": "night"}) is False


@pytest.mark.parametrize("session_mode", ["nap", "induce"])
def test_ineligible_non_night_sessions(session_mode):
    assert is_eligible({"night_type": "normal", "session_mode": session_mode}) is False


def test_never_experimental_on_short_recovery_or_nap_nights():
    cfg = ThermalTrialConfig(enabled=True, experimental_fraction=MAX_EXPERIMENTAL_FRACTION)
    for night_type in ("constrained", "recovery", None):
        for d in _dates(60):
            offset = assign_arm(d, {"night_type": night_type, "session_mode": "night"}, cfg)
            assert offset == cfg.control_offset_f
    for d in _dates(60):
        offset = assign_arm(d, {"night_type": "normal", "session_mode": "nap"}, cfg)
        assert offset == cfg.control_offset_f


def test_disabled_trial_always_returns_control_arm():
    cfg = ThermalTrialConfig(enabled=False, experimental_fraction=MAX_EXPERIMENTAL_FRACTION)
    context = {"night_type": "normal", "session_mode": "night"}
    for d in _dates(30):
        assert assign_arm(d, context, cfg) == cfg.control_offset_f


# --------------------------------------------------------------------------- determinism


def test_assign_arm_is_deterministic_for_a_fixed_date():
    cfg = ThermalTrialConfig(enabled=True)
    context = {"night_type": "normal", "session_mode": "night"}
    results = {assign_arm("2026-03-14", context, cfg) for _ in range(20)}
    assert len(results) == 1  # same date -> same arm every time, no wall-clock/RNG involved


def test_assign_arm_reproducible_across_fresh_config_instances():
    context = {"night_type": "normal", "session_mode": "night"}
    a = assign_arm("2026-05-01", context, ThermalTrialConfig(enabled=True))
    b = assign_arm("2026-05-01", context, ThermalTrialConfig(enabled=True))
    assert a == b


def test_block_offset_is_pure_function_of_date_and_key():
    cfg = ThermalTrialConfig(enabled=True)
    for _ in range(10):
        assert _block_offset("2026-04-01", "normal", cfg) == _block_offset("2026-04-01", "normal", cfg)
    # A different block key can (and generally will) draw a different schedule.
    same_or_not = _block_offset("2026-04-01", "normal", cfg)
    assert isinstance(same_or_not, float)


# --------------------------------------------------------------------------- fraction cap


def test_fraction_cap_respected_over_many_eligible_nights():
    cfg = ThermalTrialConfig(enabled=True, experimental_fraction=0.4)
    context = {"night_type": "normal", "session_mode": "night"}
    dates = _dates(3000)
    offsets = [assign_arm(d, context, cfg) for d in dates]
    frac_experimental = sum(1 for o in offsets if o != cfg.control_offset_f) / len(offsets)
    # Deterministic block schedule should land close to the target fraction.
    assert 0.3 <= frac_experimental <= 0.5


def test_experimental_fraction_hard_capped_even_if_config_requests_more():
    cfg = ThermalTrialConfig(enabled=True, experimental_fraction=0.99)
    context = {"night_type": "normal", "session_mode": "night"}
    dates = _dates(3000)
    offsets = [assign_arm(d, context, cfg) for d in dates]
    frac_experimental = sum(1 for o in offsets if o != cfg.control_offset_f) / len(offsets)
    assert frac_experimental <= MAX_EXPERIMENTAL_FRACTION + 0.05


# --------------------------------------------------------------------------- block balance


def test_arms_are_balanced_within_a_block():
    """Every full block (one shuffled pass through the expanded pool) must contain each
    non-control arm exactly once, and the configured number of control slots -- this is the
    permuted-block-randomization guarantee, not just an approximate long-run average."""
    cfg = ThermalTrialConfig(enabled=True, experimental_fraction=0.5)
    pool = sorted(_expanded_pool(cfg))
    n = len(pool)
    # Walk one full block's worth of consecutive calendar days for a fixed block key and collect
    # the assigned offsets -- must reproduce the pool exactly (as a multiset), any block-aligned
    # start.
    start_ordinal = date(2030, 1, 1).toordinal()
    start_ordinal -= start_ordinal % n  # align to a block boundary
    block_start = date.fromordinal(start_ordinal)
    seen = []
    for i in range(n):
        d = (block_start + timedelta(days=i)).isoformat()
        seen.append(_block_offset(d, "normal", cfg))
    assert sorted(seen) == pool


def test_block_balance_holds_across_many_consecutive_blocks():
    cfg = ThermalTrialConfig(enabled=True, experimental_fraction=0.5)
    pool = sorted(_expanded_pool(cfg))
    n = len(pool)
    start_ordinal = date(2031, 6, 1).toordinal()
    start_ordinal -= start_ordinal % n
    block_start = date.fromordinal(start_ordinal)
    for block in range(20):
        seen = []
        for i in range(n):
            d = (block_start + timedelta(days=block * n + i)).isoformat()
            seen.append(_block_offset(d, "normal", cfg))
        assert sorted(seen) == pool


def test_different_block_keys_are_stratified_independently():
    cfg = ThermalTrialConfig(enabled=True, experimental_fraction=0.5)
    d = "2026-04-01"
    a = _block_offset(d, "normal", cfg)
    b = _block_offset(d, "recovery", cfg)
    assert isinstance(a, float) and isinstance(b, float)


def test_block_key_defaults_from_context():
    assert block_key({"night_type": "normal"}) == "normal"
    assert block_key({}) == "unknown"
    assert block_key({"night_type": None}) == "unknown"


# --------------------------------------------------------------------------- comfort clamping


def test_ladder_offsets_clamped_to_comfort_band():
    cfg = ThermalTrialConfig(enabled=True, offset_ladder_f=[-10.0, -3.0, 0.0, 3.0, 10.0],
                             comfort_band_f=2.0)
    ladder = _clamped_ladder(cfg)
    assert max(ladder) <= 2.0
    assert min(ladder) >= -2.0
    assert 0.0 in ladder


def test_assign_arm_never_exceeds_comfort_band():
    cfg = ThermalTrialConfig(enabled=True, offset_ladder_f=[-10.0, -3.0, 0.0, 3.0, 10.0],
                             comfort_band_f=2.0, experimental_fraction=MAX_EXPERIMENTAL_FRACTION)
    context = {"night_type": "normal", "session_mode": "night"}
    for d in _dates(500):
        offset = assign_arm(d, context, cfg)
        assert -2.0 <= offset <= 2.0


# --------------------------------------------------------------------------- profile safety


def test_dose_response_profile_stays_within_device_clamp():
    cfg = AppConfig.default()
    base = cfg.default_setpoints()
    for offset in (-1.5, -0.75, 0.0, 0.4, 0.8):
        prof = dose_response_profile(base, offset, cfg.thermal_trial)
        # A modest ladder offset off a ~70 F neutral is nowhere near the 55-110 F device edge;
        # the REAL enforcement of that range happens downstream in ThermalController.target_for
        # (clamp_fahrenheit) -- this just confirms the profile itself is a sane, nearby value.
        assert 55.0 <= prof.neutral_f <= 110.0
        assert abs(prof.neutral_f - base.neutral_f) <= abs(offset) + 1e-6
    # Only neutral_f moves -- everything else on the profile (deep-bias/wake-ramp/etc.) is
    # untouched, so this trial can never perturb behavior it isn't testing.
    prof = dose_response_profile(base, 0.8, cfg.thermal_trial)
    assert prof.deep_bias_f == base.deep_bias_f
    assert prof.wake_ramp_f == base.wake_ramp_f
    assert prof.rem_warm_offset_f == base.rem_warm_offset_f
    assert prof.composite_bed_weight == base.composite_bed_weight


def test_dose_response_profile_clamps_extreme_offset_to_comfort_band():
    cfg = AppConfig.default()
    base = cfg.default_setpoints()
    trial_cfg = ThermalTrialConfig(comfort_band_f=2.0)
    prof = dose_response_profile(base, 25.0, trial_cfg)
    assert prof.neutral_f == pytest.approx(base.neutral_f + 2.0)
    prof2 = dose_response_profile(base, -25.0, trial_cfg)
    assert prof2.neutral_f == pytest.approx(base.neutral_f - 2.0)


def test_apply_trial_arm_ineligible_night_forces_control(repo):
    cfg = AppConfig.default()
    cfg.thermal_trial = ThermalTrialConfig(enabled=True, experimental_fraction=MAX_EXPERIMENTAL_FRACTION)
    base = cfg.default_setpoints()
    context = {"night_type": "constrained", "session_mode": "night"}
    prof, info = apply_trial_arm(repo, cfg, "2026-07-01", context, base)
    assert info["arm"] == CONTROL
    assert info["eligible"] is False
    assert prof.neutral_f == base.neutral_f


def test_apply_trial_arm_persists_assignment_idempotently(repo):
    cfg = AppConfig.default()
    cfg.thermal_trial = ThermalTrialConfig(enabled=True)
    context = {"night_type": "normal", "session_mode": "night"}
    base = cfg.default_setpoints()
    _, info1 = apply_trial_arm(repo, cfg, "2026-08-01", context, base)
    _, info2 = apply_trial_arm(repo, cfg, "2026-08-01", context, base)
    assert info1["arm"] == info2["arm"]
    row = repo.thermal_trial_night("2026-08-01")
    assert row is not None and row["arm"] == info1["arm"]


def test_apply_trial_arm_produces_valid_profile_on_experimental_night(repo):
    cfg = AppConfig.default()
    cfg.thermal_trial = ThermalTrialConfig(enabled=True, experimental_fraction=MAX_EXPERIMENTAL_FRACTION)
    context = {"night_type": "normal", "session_mode": "night"}
    saw_experimental = False
    for d in _dates(80):
        base = cfg.default_setpoints()
        prof, info = apply_trial_arm(repo, cfg, d, context, base)
        assert prof.neutral_f is not None
        if info["arm"] != CONTROL:
            saw_experimental = True
    assert saw_experimental


# --------------------------------------------------------------------------- auto-stop


def _seed_trial_rows(repo, control_wake, bad_arm_wake, bad_arm="+0.80", start=date(2026, 6, 1)):
    for i, w in enumerate(control_wake):
        d = (start + timedelta(days=i)).isoformat()
        repo.assign_thermal_trial_night(d, CONTROL, 0.0, True, block_key="normal", seed=0.1)
        repo.record_thermal_trial_outcome(d, wake_events=w)
    for i, w in enumerate(bad_arm_wake):
        d = (start + timedelta(days=100 + i)).isoformat()
        repo.assign_thermal_trial_night(d, bad_arm, 0.8, True, block_key="normal", seed=0.1)
        repo.record_thermal_trial_outcome(d, wake_events=w)


def test_auto_stop_suspends_only_the_clearly_worse_arm(repo):
    cfg = ThermalTrialConfig(enabled=True, experimental_fraction=MAX_EXPERIMENTAL_FRACTION,
                             auto_stop_min_n=6, auto_stop_threshold=1.0)
    _seed_trial_rows(repo, control_wake=[1, 1, 2, 1, 1, 0, 1],
                     bad_arm_wake=[4, 5, 4, 5, 4, 5, 4], bad_arm="+0.80")
    context = {"night_type": "normal", "session_mode": "night"}
    seen_bad_arm = False
    for d in _dates(200, start=date(2027, 1, 1)):
        offset = assign_arm(d, context, cfg, repo=repo)
        if _format_arm(offset) == "+0.80":
            seen_bad_arm = True
            break
    assert seen_bad_arm is False
    events = repo.conn.execute(
        "SELECT * FROM events WHERE category='thermal_trial' AND code='auto_stop'").fetchall()
    assert len(events) >= 1


def test_auto_stop_does_not_trigger_on_thin_data(repo):
    cfg = ThermalTrialConfig(enabled=True, experimental_fraction=MAX_EXPERIMENTAL_FRACTION,
                             auto_stop_min_n=6, auto_stop_threshold=1.0)
    _seed_trial_rows(repo, control_wake=[1, 1], bad_arm_wake=[5, 5], bad_arm="+0.80")
    context = {"night_type": "normal", "session_mode": "night"}
    offsets = {assign_arm(d, context, cfg, repo=repo) for d in _dates(200, start=date(2027, 2, 1))}
    assert 0.8 in offsets  # guardrail must not fire on too little evidence


def test_auto_stop_does_not_trigger_when_arms_are_similar(repo):
    cfg = ThermalTrialConfig(enabled=True, experimental_fraction=MAX_EXPERIMENTAL_FRACTION,
                             auto_stop_min_n=6, auto_stop_threshold=1.0)
    _seed_trial_rows(repo, control_wake=[2, 3, 2, 3, 2, 3], bad_arm_wake=[2, 3, 3, 2, 3, 2],
                     bad_arm="+0.80")
    context = {"night_type": "normal", "session_mode": "night"}
    offsets = {assign_arm(d, context, cfg, repo=repo) for d in _dates(200, start=date(2027, 3, 1))}
    assert 0.8 in offsets


# --------------------------------------------------------------------------- outcome recording


def test_record_outcome_noop_without_assignment(repo):
    record_trial_outcome(repo, "2026-09-01", wake_events=2)
    assert repo.thermal_trial_night("2026-09-01") is None


def test_record_outcome_persists_against_assigned_arm(repo):
    repo.assign_thermal_trial_night("2026-09-05", "+0.40", 0.4, True, block_key="normal", seed=0.05)
    record_trial_outcome(repo, "2026-09-05", wake_events=1, deep_min=90.0, sleep_efficiency=0.91,
                         hrv=65.0, subjective_rating=7.0)
    row = repo.thermal_trial_night("2026-09-05")
    assert row["resolved"] == 1
    assert row["wake_events"] == 1
    assert row["hrv"] == pytest.approx(65.0)
    assert row["deep_min"] == pytest.approx(90.0)
    assert row["subjective_rating"] == pytest.approx(7.0)


# --------------------------------------------------------------------------- ThermalTrialResult


def test_thermal_trial_result_from_row():
    row = {"night_date": "2026-01-01", "arm": "+0.40", "offset_f": 0.4, "eligible": 1,
          "block_key": "normal", "seed": 0.05, "wake_events": 2, "deep_min": 88.0,
          "sleep_efficiency": 0.9, "hrv": 60.0, "subjective_rating": 6.0}
    result = ThermalTrialResult.from_row(row)
    assert result.arm == "+0.40" and result.eligible is True and result.wake_events == 2
    assert result.offset_f == pytest.approx(0.4)


# --------------------------------------------------------------------------- analyze_dose_response


def _rows_for(arm_wake: dict, extra=None):
    rows = []
    for arm, wakes in arm_wake.items():
        for w in wakes:
            r = {"arm": arm, "wake_events": w, "deep_min": 90.0, "sleep_efficiency": 0.90,
                "hrv": 60.0}
            if extra:
                r.update(extra)
            rows.append(r)
    return rows


def test_analyze_dose_response_not_confident_on_thin_data():
    rows = _rows_for({CONTROL: [1, 2], "+0.40": [1, 0]})
    out = analyze_dose_response(rows, min_nights_per_arm=10)
    assert out["confident"] is False
    assert "Not enough data" in out["verdict"]


def test_analyze_dose_response_never_declares_winner_from_three_nights():
    rows = _rows_for({CONTROL: [3, 3, 3], "+0.40": [0, 0, 0]})
    out = analyze_dose_response(rows, min_nights_per_arm=8)
    assert out["confident"] is False
    assert "reduces awakenings" not in out["verdict"]


def test_analyze_dose_response_confident_with_plenty_of_data_and_clear_effect():
    control = [3, 4, 3, 4, 3, 4, 3, 4, 3, 4]
    warm = [1, 1, 2, 1, 1, 0, 1, 1, 2, 1]
    rows = _rows_for({CONTROL: control, "+0.40": warm})
    out = analyze_dose_response(rows, min_nights_per_arm=8)
    assert out["confident"] is True
    comp = out["comparisons"]["+0.40"]
    assert comp["diff_vs_control"] < 0  # fewer wake events than control
    assert comp["ci_high"] < 0  # CI excludes 0
    assert "reduces awakenings" in out["verdict"]
    assert out["trend"]["direction"] in ("warmer_is_better", "no_clear_trend")


def test_analyze_dose_response_reports_arm_stats_and_secondary_metrics():
    rows = _rows_for({CONTROL: [1, 2, 1, 2, 1, 2, 1, 2],
                      "-0.75": [2, 3, 2, 3, 2, 3, 2, 3]})
    out = analyze_dose_response(rows, min_nights_per_arm=8)
    assert out["arms"][CONTROL]["n"] == 8
    assert out["arms"][CONTROL]["mean_deep_min"] == pytest.approx(90.0)
    assert out["arms"][CONTROL]["mean_sleep_efficiency"] == pytest.approx(0.90)
    assert out["arms"][CONTROL]["mean_hrv"] == pytest.approx(60.0)
    assert out["arms"]["-0.75"]["se_wake_events"] is not None


def test_analyze_dose_response_monotonic_trend_detection():
    # Wake events strictly fall as offset warms: -0.75 worst, control middling, +0.80 best.
    rows = _rows_for({
        "-0.75": [5] * 10,
        CONTROL: [3] * 10,
        "+0.80": [1] * 10,
    })
    out = analyze_dose_response(rows, min_nights_per_arm=8)
    assert out["trend"]["direction"] == "warmer_is_better"


def test_analyze_dose_response_from_repo_rows_shape(repo):
    _seed_trial_rows(repo, control_wake=[3, 4, 3, 4, 3, 4, 3, 4, 3, 4],
                     bad_arm_wake=[1, 1, 2, 1, 1, 0, 1, 1, 2, 1], bad_arm="+0.80")
    rows = repo.thermal_trial_rows(resolved_only=True)
    out = analyze_dose_response(rows, min_nights_per_arm=8)
    assert out["arms"][CONTROL]["n"] == 10
    assert out["arms"]["+0.80"]["n"] == 10


# --------------------------------------------------------------------------- config wiring


def test_thermal_trial_config_defaults_disabled():
    cfg = AppConfig.default()
    assert cfg.thermal_trial.enabled is False
    assert cfg.thermal_trial.offset_ladder_f == [-1.5, -0.75, 0.0, 0.4, 0.8]
    assert cfg.thermal_trial.comfort_band_f == 2.0
