"""The user's standing complaint: "I keep getting woken up bc my bed is so cold" (2026-09-21).

Pooled over every recorded night, the awakening rate per maintenance tick runs 5.7% at 68.0 F
and 1.8% at 69.0 F. The table stops there: 2,965 ticks at 69.0 F and 109 above it. That is not
because warmer was tried and failed -- it is because the comfort clamp, which applies in
MAINTENANCE and WAKE_RECOVERY, bounded every target at the sweep's warm edge of 69.5 F. The
setpoint learner asked for 74.4 F every one of those nights and was overridden; on 2026-09-19
the user woke cold and set the bed to 80 F by hand.
"""
from datetime import datetime, timedelta

from sleepctl.config import AppConfig
from sleepctl.controller.controller import SleepController
from sleepctl.models import (ContextRecord, ControllerState, SensorFrame, SleepStage,
                             ThermalIntent)

BAND = {"cool_edge_f": 67.0, "warm_edge_f": 69.5, "neutral_f": 69.0,
        "source": "evidence_corrected_2026-08-28"}


def _ctl(cfg=None):
    cfg = cfg or AppConfig()
    c = SleepController(cfg)
    c.thermal.set_measured_neutral(69.0)
    c.comfort_profile = dict(BAND)
    return c, cfg


def _frame(ts, stage=SleepStage.LIGHT, hr=62.0, movement=0.02):
    return SensorFrame(timestamp=ts, stage=stage, stage_confidence=0.7, heart_rate=hr,
                       movement=movement, respiratory_rate=14.0, data_age_seconds=5.0)


def test_the_night_floor_is_the_best_temperature_ever_measured_on_this_user():
    cfg = AppConfig()
    assert cfg.tunables.maintenance_floor_f == 69.0
    assert cfg.tunables.settle_cooling_allowed is False


def test_a_warmer_maintenance_target_is_reachable_at_all():
    """THE bug behind the complaint: the clamp made every target above 69.5 F unreachable, so
    the warm side could never be tried, and "no evidence up there" then justified not going."""
    c, cfg = _ctl()
    assert cfg.tunables.comfort_clamp_warm_allowance_f >= 2.0
    t0 = datetime(2026, 9, 21, 2, 0)
    c.restore_session_state("maintenance", t0 - timedelta(hours=3), {"deep_min": 20.0})
    c._last_target_f = 69.0
    c._last_settle_at = t0 - timedelta(minutes=90)   # a released settle: the tick asks for NEUTRAL
    # exactly how the n-of-1 trial applies a warm arm: it is the one caller allowed to move
    # the measured neutral on purpose (dose_response_profile -> set_setpoints)
    from dataclasses import replace
    c.set_setpoints(replace(c.thermal.profile, neutral_f=71.0), keep_measured_neutral=False)
    recent = [_frame(t0 - timedelta(minutes=i)) for i in range(10, 0, -1)]
    d = c.decide(_frame(t0), ContextRecord(date="2026-09-20"), recent, t0)
    assert d.target_temp_f > 69.5, f"still pinned at the old warm edge: {d.target_temp_f}"
    assert d.target_temp_f <= 69.0 + cfg.tunables.comfort_clamp_warm_allowance_f + 1e-9


def test_the_old_clamp_would_have_pinned_that_same_arm_at_the_sweeps_warm_edge():
    """The counterfactual, so the fix cannot silently regress: with no warm allowance, the
    trial's warm arm resolves to 69.5 F and the experiment can never answer its question."""
    from dataclasses import replace
    cfg = AppConfig()
    cfg.tunables.comfort_clamp_warm_allowance_f = 0.0
    c, _ = _ctl(cfg)
    t0 = datetime(2026, 9, 21, 2, 0)
    c.restore_session_state("maintenance", t0 - timedelta(hours=3), {"deep_min": 20.0})
    c._last_target_f = 69.0
    c._last_settle_at = t0 - timedelta(minutes=90)
    c.set_setpoints(replace(c.thermal.profile, neutral_f=71.0), keep_measured_neutral=False)
    recent = [_frame(t0 - timedelta(minutes=i)) for i in range(10, 0, -1)]
    d = c.decide(_frame(t0), ContextRecord(date="2026-09-20"), recent, t0)
    assert d.target_temp_f <= 69.5 + 1e-9
    assert "clamped to personal comfort band" in d.reason


def test_the_cold_side_of_the_band_is_untouched():
    """Only the warm side was unmeasured. Below the cool edge stays refused."""
    c, cfg = _ctl()
    t0 = datetime(2026, 9, 21, 2, 0)
    c.restore_session_state("maintenance", t0 - timedelta(hours=3), {"deep_min": 20.0})
    c._last_target_f = 69.0
    c._last_settle_at = t0 - timedelta(minutes=90)
    from dataclasses import replace
    c.set_setpoints(replace(c.thermal.profile, neutral_f=60.0), keep_measured_neutral=False)
    recent = [_frame(t0 - timedelta(minutes=i)) for i in range(10, 0, -1)]
    d = c.decide(_frame(t0), ContextRecord(date="2026-09-20"), recent, t0)
    assert d.target_temp_f >= cfg.tunables.maintenance_floor_f - 1e-9


def test_an_awakening_warms_the_bed_instead_of_holding_neutral():
    """WAKE_RECOVERY resolves to SETTLE_COOL, and with settle cooling switched off that is
    exactly neutral -- so the one moment the system knew this user had woken, it did nothing
    about the cause they report."""
    c, cfg = _ctl()
    assert cfg.tunables.wake_recovery_warm_f > 0.0
    t0 = datetime(2026, 9, 21, 2, 0)
    c.restore_session_state("wake_recovery", t0 - timedelta(hours=3), {"deep_min": 20.0})
    c._last_target_f = 69.0
    recent = [_frame(t0 - timedelta(minutes=i), stage=SleepStage.AWAKE, hr=72.0)
              for i in range(10, 0, -1)]
    d = c.decide(_frame(t0, stage=SleepStage.AWAKE, hr=72.0),
                 ContextRecord(date="2026-09-20"), recent, t0)
    if d.state is ControllerState.WAKE_RECOVERY:
        assert d.target_temp_f > 69.0, f"an awakening still held neutral: {d.target_temp_f}"


def test_the_trial_only_explores_at_or_above_neutral():
    cfg = AppConfig()
    assert all(x >= 0.0 for x in cfg.thermal_trial.offset_ladder_f)
    assert max(cfg.thermal_trial.offset_ladder_f) >= 1.5
