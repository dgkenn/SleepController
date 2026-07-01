"""Standing "does the controller help?" efficacy trial: assignment balance/washout, HELD-arm
application (do-no-harm), outcome recording, and the CONTROLLED-vs-HELD analysis math."""

import tempfile
from datetime import date, timedelta

import pytest

from sleepctl.config import AppConfig
from sleepctl.eval.efficacy import (
    analyze_efficacy,
    apply_efficacy_arm,
    assign_tonight_arm,
    backfill_from_nightly_summaries,
    get_efficacy_config,
    neutral_setpoint,
    record_efficacy_outcome,
    set_efficacy_config,
)
from sleepctl.models import NightSummary
from sleepctl.storage.repository import Repository


@pytest.fixture
def repo():
    r = Repository(tempfile.mktemp(suffix=".db"))
    yield r
    r.close()


def _dates(n, start=date(2026, 1, 1)):
    return [(start + timedelta(days=i)).isoformat() for i in range(n)]


# --------------------------------------------------------------------------- config


def test_config_defaults_off(repo):
    cfg = get_efficacy_config(repo)
    assert cfg["enabled"] is False
    assert cfg["block_nights"] >= 1


def test_config_update_persists(repo):
    out = set_efficacy_config(repo, {"enabled": True, "block_nights": 4})
    assert out["enabled"] is True and out["block_nights"] == 4
    assert get_efficacy_config(repo) == {"enabled": True, "block_nights": 4}


# --------------------------------------------------------------------------- assignment / washout


def test_assignment_inactive_when_disabled(repo):
    assert assign_tonight_arm(repo, night_date="2026-01-01") is None


def test_assignment_idempotent_per_date(repo):
    set_efficacy_config(repo, {"enabled": True})
    a1 = assign_tonight_arm(repo, night_date="2026-01-01")
    a2 = assign_tonight_arm(repo, night_date="2026-01-01")
    assert a1 == a2
    assert a1 in ("controlled", "held")


def test_assignment_holds_blocks_before_flipping(repo):
    """With block_nights=3, the arm must not change more often than every 3 nights."""
    set_efficacy_config(repo, {"enabled": True, "block_nights": 3})
    dates = _dates(30)
    arms = [assign_tonight_arm(repo, night_date=d) for d in dates]
    # Find run-lengths of consecutive identical arms; every run except possibly a truncated
    # first/last run must be >= 3 (washout / min-hold).
    runs = []
    cur, cur_len = arms[0], 1
    for a in arms[1:]:
        if a == cur:
            cur_len += 1
        else:
            runs.append(cur_len)
            cur, cur_len = a, 1
    runs.append(cur_len)
    # interior runs (not the very first, which could be a partial block if history is empty)
    for n in runs:
        assert n >= 3, f"washout violated: run length {n} < block_nights=3"


def test_assignment_is_balanced_over_many_nights(repo):
    """Over a long run, both arms should appear roughly equally (no permanent bias)."""
    set_efficacy_config(repo, {"enabled": True, "block_nights": 2})
    dates = _dates(120)
    arms = [assign_tonight_arm(repo, night_date=d) for d in dates]
    n_controlled = arms.count("controlled")
    n_held = arms.count("held")
    assert n_controlled + n_held == len(dates)
    # balanced within a generous tolerance (this is a block schedule, not a coin flip per night)
    assert abs(n_controlled - n_held) <= 0.34 * len(dates)


def test_assignment_persisted_and_readable(repo):
    set_efficacy_config(repo, {"enabled": True})
    arm = assign_tonight_arm(repo, night_date="2026-02-01")
    row = repo.efficacy_night("2026-02-01")
    assert row is not None and row["arm"] == arm and row["resolved"] == 0


# --------------------------------------------------------------------------- HELD-arm application


def test_neutral_setpoint_zeroes_steering_biases():
    cfg = AppConfig.default()
    base = cfg.default_setpoints()
    neutral = neutral_setpoint(base, cfg)
    assert neutral.deep_bias_f == 0.0
    assert neutral.rem_warm_offset_f == 0.0
    assert neutral.neutral_f == cfg.tunables.neutral_temp_f
    assert neutral.wake_ramp_f == cfg.tunables.neutral_temp_f


def test_apply_efficacy_arm_inactive_when_disabled(repo):
    from sleepctl.controller.controller import SleepController
    cfg = AppConfig.default()
    controller = SleepController(cfg)
    base = cfg.default_setpoints()
    prof, info = apply_efficacy_arm(repo, cfg, controller, "2026-01-01", base)
    assert prof is base and info is None


def test_held_night_disables_steering_and_preemption_do_no_harm(repo):
    from sleepctl.controller.controller import SleepController
    cfg = AppConfig.default()
    controller = SleepController(cfg)
    set_efficacy_config(repo, {"enabled": True, "block_nights": 1})

    # Walk dates until we observe a HELD night (deterministic schedule; a handful of days is
    # enough since block_nights=1 flips every night after the first).
    prof = None
    for d in _dates(10):
        base = cfg.default_setpoints()
        prof, info = apply_efficacy_arm(repo, cfg, controller, d, base)
        if info["arm"] == "held":
            break
    assert info["arm"] == "held" and info["applied"] is True
    # neutral, zero-bias profile applied
    assert prof.deep_bias_f == 0.0 and prof.rem_warm_offset_f == 0.0
    # experimental steering disabled via the EXISTING setter (not touching controller.py)
    assert controller.steer_actuate is False
    # predictive pre-emption gates neutralized (score is capped at 1.0, so > 1.0 never fires)
    assert controller.wake_risk_assessor.preempt_threshold > 1.0
    assert controller.precursor_detector.preempt_threshold > 1.0
    # STILL clamped + smart-wake untouched: the profile is a normal, valid SetpointProfile and
    # the wake orchestrator / state machine are completely unaffected by this call.
    assert 55.0 <= prof.neutral_f <= 110.0


def test_controlled_night_restores_preempt_thresholds_after_a_held_night(repo):
    """A HELD night's disabled preempt thresholds must not leak into the next CONTROLLED night."""
    from sleepctl.controller.controller import SleepController
    cfg = AppConfig.default()
    controller = SleepController(cfg)
    set_efficacy_config(repo, {"enabled": True, "block_nights": 1})

    seen_controlled_after_held = False
    prev_arm = None
    for d in _dates(12):
        base = cfg.default_setpoints()
        _, info = apply_efficacy_arm(repo, cfg, controller, d, base)
        if prev_arm == "held" and info["arm"] == "controlled":
            seen_controlled_after_held = True
            assert controller.wake_risk_assessor.preempt_threshold <= 1.0
            assert controller.precursor_detector.preempt_threshold <= 1.0
        prev_arm = info["arm"]
    assert seen_controlled_after_held


# --------------------------------------------------------------------------- outcome recording


def test_record_outcome_noop_without_assignment(repo):
    record_efficacy_outcome(repo, "2026-03-01", wake_events=2)
    assert repo.efficacy_night("2026-03-01") is None


def test_record_outcome_persists_against_assigned_arm(repo):
    set_efficacy_config(repo, {"enabled": True})
    assign_tonight_arm(repo, night_date="2026-03-01")
    record_efficacy_outcome(repo, "2026-03-01", wake_events=1, deep_pct=0.22,
                            efficiency=0.91, outcome_score=5.0)
    row = repo.efficacy_night("2026-03-01")
    assert row["resolved"] == 1
    assert row["wake_events"] == 1
    assert row["deep_pct"] == pytest.approx(0.22)
    assert row["efficiency"] == pytest.approx(0.91)


def test_backfill_from_nightly_summaries(repo):
    set_efficacy_config(repo, {"enabled": True})
    assign_tonight_arm(repo, night_date="2026-03-05")
    ns = NightSummary(date="2026-03-05", wake_events=3, deep_min=90.0,
                      total_sleep_min=420.0, sleep_efficiency=0.88, outcome_score=2.5)
    repo.save_night_summary(ns)
    resolved = backfill_from_nightly_summaries(repo)
    assert resolved == 1
    row = repo.efficacy_night("2026-03-05")
    assert row["resolved"] == 1
    assert row["wake_events"] == 3
    assert row["deep_pct"] == pytest.approx(90.0 / 420.0)
    assert row["efficiency"] == pytest.approx(0.88)


def test_backfill_leaves_unresolvable_nights_alone(repo):
    set_efficacy_config(repo, {"enabled": True})
    assign_tonight_arm(repo, night_date="2026-03-06")  # no matching nightly_summaries row
    resolved = backfill_from_nightly_summaries(repo)
    assert resolved == 0
    assert repo.efficacy_night("2026-03-06")["resolved"] == 0


# --------------------------------------------------------------------------- analysis


def _seed_outcomes(repo, controlled_wake, held_wake):
    """Directly seed resolved efficacy_nights rows for controlled/held wake_events lists."""
    d = date(2026, 4, 1)
    for i, w in enumerate(controlled_wake):
        dt = (d + timedelta(days=i)).isoformat()
        repo.assign_efficacy_night(dt, "controlled")
        repo.record_efficacy_outcome(dt, wake_events=w, deep_pct=0.20, efficiency=0.90)
    for i, w in enumerate(held_wake):
        dt = (d + timedelta(days=100 + i)).isoformat()
        repo.assign_efficacy_night(dt, "held")
        repo.record_efficacy_outcome(dt, wake_events=w, deep_pct=0.20, efficiency=0.90)


def test_analysis_not_enough_data(repo):
    _seed_outcomes(repo, controlled_wake=[1, 2], held_wake=[3, 4])
    out = analyze_efficacy(repo, min_n_per_arm=5)
    assert out["enough_data"] is False
    assert "Not enough data" in out["verdict"]
    assert out["n_controlled"] == 2 and out["n_held"] == 2


def test_analysis_detects_controller_reduces_wake_events(repo):
    # Controlled nights: consistently fewer awakenings than held nights.
    controlled = [1, 1, 2, 1, 1, 0, 1, 1, 2, 1]
    held = [4, 5, 4, 5, 4, 5, 4, 5, 4, 5]
    _seed_outcomes(repo, controlled, held)
    out = analyze_efficacy(repo, min_n_per_arm=5)
    assert out["enough_data"] is True
    wake = out["metrics"]["wake_events"]
    assert wake["diff_held_minus_controlled"] > 0   # held has MORE awakenings
    assert wake["p_value"] is not None and wake["p_value"] < 0.05
    assert "reduces awakenings" in out["verdict"]


def test_analysis_no_significant_difference(repo):
    # Near-identical distributions -> no significant difference.
    controlled = [2, 3, 2, 3, 2, 3, 2, 3]
    held = [2, 3, 3, 2, 3, 2, 3, 2]
    _seed_outcomes(repo, controlled, held)
    out = analyze_efficacy(repo, min_n_per_arm=5)
    assert out["enough_data"] is True
    assert "No significant difference" in out["verdict"]


def test_analysis_metrics_include_all_three(repo):
    _seed_outcomes(repo, [1, 2, 1, 2, 1, 2], [3, 4, 3, 4, 3, 4])
    out = analyze_efficacy(repo, min_n_per_arm=5)
    assert set(out["metrics"].keys()) == {"wake_events", "deep_pct", "efficiency"}
    for m in out["metrics"].values():
        assert "controlled" in m and "held" in m and "ci" in m and "p_value" in m
