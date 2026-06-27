"""Shift-aware sleep-debt & circadian manager."""

from datetime import datetime, timedelta

from sleepctl.models import NightSummary
from sleepctl.shift_manager import Shift, plan_shift_sleep

NOW = datetime(2026, 6, 27, 12, 0)


def _nights(n, tst_min):
    return [NightSummary(date=f"2026-06-{10+i:02d}", total_sleep_min=tst_min) for i in range(n)]


def test_prophylactic_nap_before_a_night_shift():
    shifts = [Shift(start=NOW + timedelta(hours=7), end=NOW + timedelta(hours=19), kind="night")]
    plan = plan_shift_sleep(_nights(3, 460), shifts, NOW)
    types = {n.type for n in plan.naps}
    assert "prophylactic" in types
    proph = next(n for n in plan.naps if n.type == "prophylactic")
    assert proph.duration_min == 90  # plenty of lead time -> full cycle
    assert "Pre-load" in plan.strategy


def test_post_call_triggers_recovery_and_driving_warning():
    shifts = [Shift(start=NOW - timedelta(hours=14), end=NOW - timedelta(hours=2), kind="call")]
    plan = plan_shift_sleep(_nights(3, 460), shifts, NOW)
    assert any(n.type == "recovery" for n in plan.naps)
    assert any("drowsy-driving" in w or "driving" in w for w in plan.warnings)
    assert "Recovery" in plan.strategy


def test_high_debt_is_flagged_severe():
    plan = plan_shift_sleep(_nights(6, 240), [], NOW)  # 4h nights -> heavy debt
    assert plan.debt_band == "severe"
    assert any("sleep debt" in w for w in plan.warnings)
    # repay-oriented target extends past base need
    assert plan.tonight_target_min > 480


def test_variable_schedule_recommends_anchor_sleep():
    shifts = [
        Shift(start=NOW + timedelta(days=1, hours=8), end=NOW + timedelta(days=1, hours=18), kind="day"),
        Shift(start=NOW + timedelta(days=2, hours=19), end=NOW + timedelta(days=3, hours=7), kind="night"),
    ]
    plan = plan_shift_sleep(_nights(3, 460), shifts, NOW)
    assert plan.anchor_window is not None
    assert any(n.type == "anchor" for n in plan.naps)


def test_quiet_schedule_just_maintains():
    plan = plan_shift_sleep(_nights(3, 470), [], NOW)
    assert plan.debt_band in ("none", "mild")
    assert plan.naps == [] or all(n.type == "anchor" for n in plan.naps)
    assert "Maintain" in plan.strategy or "Repay" in plan.strategy
