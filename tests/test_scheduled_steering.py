"""Scheduled steering (2026-09-25 audit): the bed follows neutral + late-phase warmth, settles
when an awakening is brewing, and returns to the schedule after a quiet spell. Stage labels no
longer move it."""
from datetime import datetime, timedelta

from sleepctl.config import AppConfig
from sleepctl.controller.controller import SleepController
from sleepctl.controller.maintenance import MaintenanceRoutine
from sleepctl.models import NightObjective, SensorFrame, SleepStage, ThermalIntent


def _frame(stage=SleepStage.LIGHT):
    return SensorFrame(timestamp=datetime(2026, 9, 26, 2, 0), stage=stage)


def test_labels_do_not_move_the_bed_by_default():
    cfg = AppConfig.default()
    assert cfg.tunables.stage_label_actuation is False
    m = MaintenanceRoutine(cfg)
    obj = NightObjective.OPTIMIZE
    for st in (SleepStage.DEEP, SleepStage.REM, SleepStage.LIGHT):
        assert m.step(_frame(st), obj) is ThermalIntent.STABILIZE
        assert m.step(_frame(st), obj, deepen=True) is ThermalIntent.STABILIZE
        assert m.step(_frame(st), obj, release=True) is ThermalIntent.NEUTRAL
        assert m.step(_frame(st), obj, preempt_cool=True) is ThermalIntent.SETTLE_COOL


def _ctl():
    cfg = AppConfig.default()
    c = SleepController(cfg)
    c.thermal.set_measured_neutral(70.0)
    c._sleep_onset_time = datetime(2026, 9, 25, 23, 30)
    return c, cfg


def test_late_phase_warmth_ramps_in_over_the_last_three_hours():
    c, cfg = _ctl()
    wake = datetime(2026, 9, 26, 7, 0)
    assert c._phase_offset_f(wake - timedelta(minutes=200), wake, cfg) == 0.0
    assert c._phase_offset_f(wake - timedelta(minutes=165), wake, cfg) == 0.25
    assert c._phase_offset_f(wake - timedelta(minutes=60), wake, cfg) == 0.5
    assert c._phase_offset_f(wake + timedelta(minutes=5), wake, cfg) == 0.0


def test_the_habitual_wake_anchors_it_when_no_alarm_is_set():
    c, cfg = _ctl()
    c.habitual_wake_min_of_day = 7 * 60
    assert c._phase_offset_f(datetime(2026, 9, 26, 5, 0), None, cfg) == 0.5
    assert c._phase_offset_f(datetime(2026, 9, 26, 1, 0), None, cfg) == 0.0
    c._sleep_onset_time = None                       # not asleep: no schedule
    assert c._phase_offset_f(datetime(2026, 9, 26, 5, 0), None, cfg) == 0.0


def test_the_phase_moves_the_scheduled_neutral_and_the_settle():
    c, cfg = _ctl()
    obj = NightObjective.OPTIMIZE
    c.thermal.phase_offset_f = 0.5
    assert c.thermal.target_for(ThermalIntent.NEUTRAL, obj, True) == 70.5
    assert c.thermal.target_for(ThermalIntent.SETTLE_COOL, obj, True,
                                settle_nudge_f=0.5) == 71.0


def test_after_a_quiet_spell_the_bed_returns_to_the_schedule_from_either_side():
    c, cfg = _ctl()
    now = datetime(2026, 9, 26, 2, 0)
    c._last_target_f = 70.5                           # a warm settle in force
    c._last_settle_at = now - timedelta(minutes=5)
    assert c._should_release_settle(now, cfg) is False
    c._last_settle_at = now - timedelta(minutes=25)
    assert c._should_release_settle(now, cfg) is True
    c._last_target_f = 70.0                           # already on schedule: nothing to do
    assert c._should_release_settle(now, cfg) is False
    c._last_settle_at = None
    c._last_target_f = 72.0                           # e.g. the induction's warm opener
    assert c._should_release_settle(now, cfg) is True
