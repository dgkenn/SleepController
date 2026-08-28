"""`_bed_entry_time` is process-local, and this system auto-deploys and restarts the daemon by
design -- so a restart mid-night re-anchored bed entry to the restart moment.

Measured on 2026-08-27: all three sleep onsets reported a latency of ~1.0 min (one exactly 0.0)
against a rollup-computed SOL of 36.3 min, because each followed a restart. The wrong latency is
the visible half. `minutes_since_start` is also a stager feature, and the code's own measurement
of losing it is REM 27% -> 0% with the whole hypnogram collapsing onto LIGHT -- so a restart was
silently degrading staging for the rest of the night.
"""
from datetime import datetime, timedelta

from sleepctl.config import AppConfig
from sleepctl.controller.controller import SleepController
from sleepctl.models import ContextRecord, SensorFrame, SleepStage

T0 = datetime(2026, 8, 27, 21, 30)


def _frame(i, hr=64.0):
    return SensorFrame(timestamp=T0 + timedelta(minutes=i), stage=SleepStage.UNKNOWN,
                       presence=None, heart_rate=hr, hrv=55.0, respiratory_rate=14.0,
                       movement=0.03, data_age_seconds=20)


def _ctl():
    return SleepController(AppConfig.default())


def test_a_recovered_anchor_is_used_instead_of_now():
    c = _ctl()
    c.restore_bed_entry(T0)
    c.decide(_frame(100), ContextRecord(date="2026-08-27"), [], T0 + timedelta(minutes=100))
    assert c._bed_entry_time == T0, "bed entry should be the recovered value, not the tick time"


def test_without_recovery_the_live_path_still_stamps_now():
    c = _ctl()
    now = T0 + timedelta(minutes=100)
    c.decide(_frame(100), ContextRecord(date="2026-08-27"), [], now)
    assert c._bed_entry_time == now


def test_a_recovered_anchor_never_overrides_a_live_session():
    """Safe to call unconditionally at start-up: an established anchor always wins."""
    c = _ctl()
    now = T0 + timedelta(minutes=10)
    c.decide(_frame(10), ContextRecord(date="2026-08-27"), [], now)
    c.restore_bed_entry(T0)
    c.decide(_frame(11), ContextRecord(date="2026-08-27"), [], T0 + timedelta(minutes=11))
    assert c._bed_entry_time == now


def test_none_is_ignored():
    c = _ctl()
    c.restore_bed_entry(None)
    now = T0 + timedelta(minutes=5)
    c.decide(_frame(5), ContextRecord(date="2026-08-27"), [], now)
    assert c._bed_entry_time == now


def test_minutes_in_bed_reflects_the_recovered_anchor():
    """This is the number the stager's time features and the onset latency both come from."""
    c = _ctl()
    c.restore_bed_entry(T0)
    d = c.decide(_frame(90), ContextRecord(date="2026-08-27"), [], T0 + timedelta(minutes=90))
    assert d.log_payload["minutes_in_bed"] >= 89.0


def test_the_recovered_value_is_consumed_once():
    c = _ctl()
    c.restore_bed_entry(T0)
    c.decide(_frame(50), ContextRecord(date="2026-08-27"), [], T0 + timedelta(minutes=50))
    assert c._recovered_bed_entry is None
