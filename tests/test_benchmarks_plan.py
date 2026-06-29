"""Tests for literature-backed benchmarks, the wake-aware plan, and mode-aware reward."""

from datetime import datetime

from sleepctl.benchmarks import (
    NightMode,
    chronic_shortfall,
    perfect_sleep_index,
    sleep_debt_min,
    targets_for,
)
from sleepctl.config import AppConfig
from sleepctl.controller.sleep_plan import bedtime_guidance, decide_mode, plan_night
from sleepctl.ml.reward import reward_from_outcomes
from sleepctl.models import NightObjective, NightSummary


def _night(**kw):
    base = dict(date="2026-06-22", total_sleep_min=420, deep_min=84, rem_min=92,
                wake_events=1, sleep_efficiency=0.92, avg_hrv=62)
    base.update(kw)
    return NightSummary(**base)


def test_chronic_shortfall_flags_sustained_short_sleep():
    short = [NightSummary(date=f"2026-06-{10+i:02d}", total_sleep_min=330) for i in range(7)]
    c = chronic_shortfall(short, need_min=480)
    assert c["is_chronic"] is True and c["avg_tst_min"] == 330
    assert c["mean_shortfall_min"] == 150 and c["short_nights_frac"] == 1.0
    # A few good nights -> not chronic.
    ok = [NightSummary(date=f"2026-06-{10+i:02d}", total_sleep_min=470) for i in range(7)]
    assert chronic_shortfall(ok, need_min=480)["is_chronic"] is False
    assert chronic_shortfall([], need_min=480)["is_chronic"] is False   # no data -> safe


def test_bedtime_guidance_inverts_the_wake_time_and_finds_the_shortfall():
    # 04:30 wake, habitual 23:00 bedtime, 12-min onset, 8 h need -> structurally ~2.7 h short.
    nights = [NightSummary(date=f"2026-06-{10+i:02d}", total_sleep_min=330,
                           bedtime=datetime(2026, 6, 10 + i, 23, 0),
                           sleep_onset_latency_min=12) for i in range(7)]
    g = bedtime_guidance(datetime(2026, 6, 29, 4, 30), nights, need_min=480)
    assert g.recommended_lights_out == "20:30"          # 04:30 minus 8 h
    assert g.habitual_bedtime == "23:00"
    assert g.structural_shortfall_min and g.structural_shortfall_min > 120
    assert g.go_earlier_min and g.go_earlier_min > 120
    assert g.is_chronic_short is True
    assert bedtime_guidance(None, nights) is None       # no wake time -> no guidance


def test_perfect_sleep_index_bounds_and_modes():
    n = _night()
    for m in NightMode:
        r = perfect_sleep_index(n, m)
        assert 0.0 <= r["score"] <= 100.0
        assert r["mode"] == m.value


def test_constrained_rewards_quality_over_duration():
    # A short but efficient, deep-rich night should score HIGHER under the constrained
    # objective (duration de-weighted) than under the normal objective.
    short = _night(total_sleep_min=320, deep_min=78, rem_min=52, sleep_efficiency=0.94)
    normal_score = perfect_sleep_index(short, NightMode.NORMAL)["score"]
    constrained_score = perfect_sleep_index(short, NightMode.CONSTRAINED)["score"]
    assert constrained_score > normal_score


def test_recovery_rewards_more_total_sleep():
    # Under recovery, a longer night must score >= a short one (duration up-weighted).
    long_night = _night(total_sleep_min=540, deep_min=110, rem_min=140)
    short_night = _night(total_sleep_min=330, deep_min=70, rem_min=70)
    long_s = perfect_sleep_index(long_night, NightMode.RECOVERY)["score"]
    short_s = perfect_sleep_index(short_night, NightMode.RECOVERY)["score"]
    assert long_s > short_s


def test_sleep_debt_accumulates():
    nights = [_night(total_sleep_min=360) for _ in range(10)]  # 2h short each
    debt = sleep_debt_min(nights, need_min=480)
    assert debt > 0
    # all-good nights -> ~no debt
    assert sleep_debt_min([_night(total_sleep_min=490) for _ in range(10)]) == 0.0


def test_decide_mode_from_schedule_and_debt():
    now = datetime(2026, 6, 23, 23, 0)
    # short opportunity + alarm -> constrained
    assert decide_mode(330, datetime(2026, 6, 24, 4, 30), 0.0) == NightMode.CONSTRAINED
    # no alarm + high debt -> recovery
    assert decide_mode(None, None, 200.0) == NightMode.RECOVERY
    # no alarm + low debt -> normal
    assert decide_mode(None, None, 10.0) == NightMode.NORMAL
    # explicit hint wins
    assert decide_mode(600, datetime(2026, 6, 24, 9, 0), 0.0, hint="recovery") \
        == NightMode.RECOVERY


def test_plan_maps_to_objective_and_extends_recovery():
    now = datetime(2026, 6, 23, 23, 0)
    debt_hist = [_night(total_sleep_min=360) for _ in range(10)]
    # constrained work night
    p = plan_night(now, datetime(2026, 6, 24, 4, 30), debt_hist, hint="work")
    assert p.mode == NightMode.CONSTRAINED
    assert p.objective == NightObjective.DAMAGE_CONTROL
    assert p.smart_wake_window_min <= 20
    assert p.deep_bias_delta_f < 0  # cooler to protect deep
    # recovery off day extends the duration target beyond need
    p2 = plan_night(now, None, debt_hist, hint="recovery")
    assert p2.mode == NightMode.RECOVERY
    assert p2.objective == NightObjective.RECOVERY
    assert p2.targets.total_sleep_target_min > 480
    assert p2.rem_warm_delta_f > 0  # warm bias supports REM rebound


def test_reward_mode_changes_objective():
    cfg = AppConfig.default()
    # outcomes: short but high quality
    out = {"wake_events": 1, "deep_pct": 0.22, "rem_pct": 0.16,
           "sleep_efficiency": 0.94, "total_sleep_min": 320, "avg_hrv": 60}
    out_long = {**out, "total_sleep_min": 540}
    # Recovery up-weights duration: a longer night scores higher.
    recovery_short = reward_from_outcomes(out, cfg, mode=NightMode.RECOVERY)
    recovery_long = reward_from_outcomes(out_long, cfg, mode=NightMode.RECOVERY)
    assert recovery_long > recovery_short
    # Constrained ignores raw duration entirely (weight 0): same reward regardless of TST.
    constrained_short = reward_from_outcomes(out, cfg, mode=NightMode.CONSTRAINED)
    constrained_long = reward_from_outcomes(out_long, cfg, mode=NightMode.CONSTRAINED)
    assert constrained_short == constrained_long
    # Normal sits between: duration matters, but less than under recovery.
    d_recovery = recovery_long - recovery_short
    normal_short = reward_from_outcomes(out, cfg, mode=NightMode.NORMAL)
    normal_long = reward_from_outcomes(out_long, cfg, mode=NightMode.NORMAL)
    assert d_recovery > (normal_long - normal_short) > 0
