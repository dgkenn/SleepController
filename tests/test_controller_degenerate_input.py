"""The controller against sensor input that has gone wrong, not merely gone missing.

The Verity is an optical sensor on a moving arm behind a BLE link and an auto-reconnect loop. Its
output degrades in specific, ugly ways: values freeze while the device holds the last good reading
through movement, a reconnect replays stale timestamps, a decode error yields a physiologically
impossible number. The existing suites cover the ABSENT case well (None everywhere). These cover
the PRESENT-BUT-WRONG case, which is the one that silently steers the bed.

The bar throughout: never raise, never command something unsafe, never let a non-finite value out.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta

import pytest

from sleepctl.config import AppConfig
from sleepctl.controller.controller import SleepController
from sleepctl.models import ContextRecord, SensorFrame, SleepStage

START = datetime(2026, 7, 28, 23, 0)


def _frame(now, **kw):
    base = dict(timestamp=now, stage=SleepStage.UNKNOWN, presence=True, heart_rate=58.0,
                hrv=55.0, respiratory_rate=14.0, movement=0.05, bed_temp_f=72.0,
                room_temp_f=68.0, data_age_seconds=10)
    base.update(kw)
    return SensorFrame(**base)


def _drive(frames):
    """Run frames through a fresh controller; returns the decisions."""
    cfg = AppConfig.default()
    ctl = SleepController(cfg)
    ctx = ContextRecord(date=START.date())
    recent, out = [], []
    for f in frames:
        out.append(ctl.decide(f, ctx, recent, f.timestamp))
        recent.append(f)
        if len(recent) > 60:
            recent.pop(0)
    return out


def _assert_sane(decisions):
    for d in decisions:
        t = getattr(d, "target_temp_f", None)
        assert t is None or (math.isfinite(t) and 55.0 <= t <= 110.0), t
        lvl = getattr(d, "target_level", None)
        assert lvl is None or (isinstance(lvl, (int, float)) and math.isfinite(lvl)), lvl


# ------------------------------------------------------------------ impossible values
@pytest.mark.parametrize("hr", [0.0, -40.0, 1e9, 500.0])
def test_impossible_heart_rates_do_not_produce_unsafe_commands(hr):
    _assert_sane(_drive([_frame(START + timedelta(minutes=i), heart_rate=hr) for i in range(40)]))


@pytest.mark.parametrize("field,value", [
    ("heart_rate", float("nan")), ("heart_rate", float("inf")),
    ("hrv", float("nan")), ("hrv", float("-inf")),
    ("movement", float("nan")), ("movement", float("inf")),
    ("bed_temp_f", float("nan")), ("room_temp_f", float("inf")),
    ("respiratory_rate", float("nan")),
])
def test_non_finite_sensor_values_never_reach_the_command(field, value):
    """A NaN that propagates into the setpoint would silently break every comparison downstream."""
    frames = [_frame(START + timedelta(minutes=i), **{field: value}) for i in range(40)]
    _assert_sane(_drive(frames))


def test_absurd_bed_temperature_does_not_drive_the_setpoint_out_of_range():
    frames = [_frame(START + timedelta(minutes=i), bed_temp_f=-200.0 if i % 2 else 500.0)
              for i in range(40)]
    _assert_sane(_drive(frames))


# ------------------------------------------------------------------ frozen / stale feeds
def test_a_completely_frozen_feed_is_survivable():
    """The Verity freezes HR through movement; a stuck value must not become a control signal."""
    frames = [_frame(START + timedelta(minutes=i), heart_rate=61.0, hrv=61.0, movement=0.0)
              for i in range(120)]
    _assert_sane(_drive(frames))


def test_repeated_identical_timestamps_do_not_wedge_the_loop():
    """A reconnect can replay a batch; identical timestamps must not divide by zero anywhere."""
    _assert_sane(_drive([_frame(START) for _ in range(60)]))


def test_timestamps_going_backwards_are_survivable():
    """Clock correction / NTP step mid-night, or a replayed buffer."""
    frames = [_frame(START + timedelta(minutes=i)) for i in range(30)]
    frames += [_frame(START + timedelta(minutes=i)) for i in range(15)]
    _assert_sane(_drive(frames))


def test_very_stale_data_is_survivable():
    frames = [_frame(START + timedelta(minutes=i), data_age_seconds=86400) for i in range(40)]
    _assert_sane(_drive(frames))


def test_negative_data_age_is_survivable():
    """A clock skew between the forwarder and the box can make a sample look like it's from the
    future."""
    frames = [_frame(START + timedelta(minutes=i), data_age_seconds=-500) for i in range(40)]
    _assert_sane(_drive(frames))


# ------------------------------------------------------------------ everything missing
def test_a_totally_empty_frame_is_survivable():
    frames = [SensorFrame(timestamp=START + timedelta(minutes=i), stage=SleepStage.UNKNOWN,
                          presence=None, heart_rate=None, hrv=None, respiratory_rate=None,
                          movement=None, bed_temp_f=None, room_temp_f=None,
                          data_age_seconds=None)
              for i in range(60)]
    _assert_sane(_drive(frames))


def test_presence_flapping_does_not_thrash_the_setpoint():
    """A flaky presence sensor must not produce a jittery bed."""
    frames = [_frame(START + timedelta(minutes=i), presence=(i % 2 == 0)) for i in range(60)]
    decisions = _drive(frames)
    _assert_sane(decisions)
    temps = [d.target_temp_f for d in decisions if getattr(d, "target_temp_f", None) is not None]
    if len(temps) > 2:
        steps = [abs(b - a) for a, b in zip(temps, temps[1:])]
        cap = AppConfig.default().tunables.max_step_f
        assert max(steps) <= cap + 1e-6, f"max step {max(steps)} exceeds slew cap {cap}"


# ------------------------------------------------------------------ the safety chain holds
def test_the_slew_cap_holds_under_wildly_swinging_input():
    """Whatever the sensors claim, the bed can never jump — this is the property that protects
    sleep from every upstream bug at once."""
    frames = []
    for i in range(80):
        frames.append(_frame(START + timedelta(minutes=i),
                             heart_rate=40.0 if i % 2 else 120.0,
                             hrv=10.0 if i % 2 else 200.0,
                             movement=0.0 if i % 2 else 1.0,
                             bed_temp_f=60.0 if i % 2 else 95.0))
    decisions = _drive(frames)
    _assert_sane(decisions)
    temps = [d.target_temp_f for d in decisions if getattr(d, "target_temp_f", None) is not None]
    steps = [abs(b - a) for a, b in zip(temps, temps[1:])]
    cap = AppConfig.default().tunables.max_step_f
    assert not steps or max(steps) <= cap + 1e-6, f"max step {max(steps)} exceeds slew cap {cap}"
