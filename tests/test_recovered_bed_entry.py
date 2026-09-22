"""A bed entry recovered across a restart must describe THIS session.

2026-09-21: the daemon restarted at 08:51 for a deploy. Under the noon cutoff that is still
the night of the 20th, so recovery found that night's first running sample -- 20:51 the
evening before -- and held it. The session then ended, and at 22:50 the NEXT night's bed
entry consumed the held value. Onset was reported 1,562 minutes after bed entry, and the
stager's minutes-since-start feature read ~1,500 minutes all night: a clock 26 hours fast,
on a model that leans on that clock to place deep sleep. That night scored 3% deep.
"""
from datetime import datetime, timedelta

from sleepctl.config import AppConfig
from sleepctl.controller.controller import SleepController
from sleepctl.models import ControllerState, SensorFrame, SleepStage


def _frame(ts, hr=64.0, stage=SleepStage.LIGHT):
    return SensorFrame(timestamp=ts, stage=stage, stage_confidence=0.6, heart_rate=hr,
                       movement=0.02, data_age_seconds=5.0)


def test_a_recovered_entry_from_the_previous_evening_is_refused():
    c = SleepController(AppConfig())
    last_night = datetime(2026, 9, 20, 20, 51)
    c.restore_bed_entry(last_night)
    tonight = datetime(2026, 9, 21, 22, 50)
    assert c._usable_recovered_bed_entry(tonight) is None


def test_a_recovered_entry_from_earlier_tonight_is_kept():
    """The case recovery exists for: a mid-night restart must not re-anchor bed entry."""
    c = SleepController(AppConfig())
    entry = datetime(2026, 9, 21, 22, 50)
    c.restore_bed_entry(entry)
    assert c._usable_recovered_bed_entry(entry + timedelta(hours=4)) == entry


def test_a_new_session_after_the_restart_anchors_on_its_own_entry():
    c = SleepController(AppConfig())
    c.restore_bed_entry(datetime(2026, 9, 20, 20, 51))
    tonight = datetime(2026, 9, 21, 22, 50)
    c.set_session("induce", keep_light=False)
    recent = [_frame(tonight - timedelta(minutes=i)) for i in range(5, 0, -1)]
    c.decide(_frame(tonight), None, recent, tonight)
    assert c._bed_entry_time is not None
    assert (tonight - c._bed_entry_time) < timedelta(minutes=5), \
        f"anchored on {c._bed_entry_time}, not tonight"


def test_ending_a_session_spends_the_recovered_anchor():
    c = SleepController(AppConfig())
    t0 = datetime(2026, 9, 21, 1, 0)
    c.restore_session_state("maintenance", t0 - timedelta(hours=2), {"deep_min": 10.0})
    c.restore_bed_entry(t0 - timedelta(hours=3))
    c._recovered_bed_entry = t0 - timedelta(hours=3)
    c.sm.state = ControllerState.IDLE
    c.decide(_frame(t0, hr=None), None, [], t0)
    # whatever the idle path does, a stale anchor is never carried into the next evening
    assert c._usable_recovered_bed_entry(t0 + timedelta(hours=20)) is None
