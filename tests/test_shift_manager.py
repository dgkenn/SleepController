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


def test_banking_before_a_night_block_days_out():
    # A night shift ~2 days away (past the immediate prophylactic-nap window) -> bank sleep now:
    # extend tonight toward ~9.5 h and surface the Rupp banking prescription.
    shifts = [Shift(start=NOW + timedelta(hours=48), end=NOW + timedelta(hours=60), kind="night")]
    plan = plan_shift_sleep(_nights(3, 460), shifts, NOW)
    assert plan.banking is not None and "extend" in plan.banking.lower()
    assert plan.tonight_target_min >= 570          # raised toward the banking goal
    assert "Bank" in plan.strategy


def test_no_banking_inside_prophylactic_window():
    # Within 16 h of the night shift it's a nap, not a banking-night recommendation.
    shifts = [Shift(start=NOW + timedelta(hours=7), end=NOW + timedelta(hours=19), kind="night")]
    plan = plan_shift_sleep(_nights(3, 460), shifts, NOW)
    assert plan.banking is None
    assert any(n.type == "prophylactic" for n in plan.naps)


def test_no_banking_for_a_day_shift():
    shifts = [Shift(start=NOW + timedelta(hours=48), end=NOW + timedelta(hours=60), kind="day")]
    plan = plan_shift_sleep(_nights(3, 460), shifts, NOW)
    assert plan.banking is None
