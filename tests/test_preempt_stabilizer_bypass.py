"""The target stabilizer's reversal dwell was swallowing awakening-prevention moves.

A pre-emptive move is almost always a REVERSAL: the bed is drifting toward the cool edge and
prevention wants to warm. The 12-minute dwell therefore blocked exactly the moves that were most
time-critical. Measured on 2026-08-27: 235 of 294 pre-empting ticks resolved to HOLD, and the
prevention-timing check reported the pre-cool arriving at a median 7.8 min against a median
4.6 min to the awakening -- the awakening was beating the dwell clock.
"""
from datetime import datetime, timedelta

from sleepctl.config import AppConfig
from sleepctl.controller.controller import SleepController


def _ctl(last_f=66.0, last_dir=-1, last_move_min_ago=1.0):
    cfg = AppConfig.default()
    c = SleepController(cfg)
    c._last_target_f = last_f
    c._stab_last_dir = last_dir
    c._stab_last_move_at = datetime(2026, 8, 27, 23, 0) - timedelta(minutes=last_move_min_ago)
    return c, cfg


NOW = datetime(2026, 8, 27, 23, 0)


def test_a_reversal_is_held_during_the_dwell_when_not_preempting():
    """The dwell still does its job for ordinary thermal hunting."""
    c, cfg = _ctl()
    held, why = c._stabilize_target(68.0, NOW, cfg, preempting=False)
    assert held == 66.0 and "dwell" in why


def test_a_preemptive_reversal_is_allowed_straight_through():
    c, cfg = _ctl()
    held, why = c._stabilize_target(68.0, NOW, cfg, preempting=True)
    assert held is None and why is None


def test_the_deadband_still_applies_while_preempting():
    """A move the bed cannot physically resolve is not worth making at any urgency."""
    c, cfg = _ctl()
    held, why = c._stabilize_target(66.1, NOW, cfg, preempting=True)
    assert held == 66.0 and "deadband" in why


def test_same_direction_moves_were_never_delayed_either_way():
    c, cfg = _ctl(last_dir=1)
    for pre in (False, True):
        held, _ = c._stabilize_target(68.0, NOW, cfg, preempting=pre)
        assert held is None


def test_the_bypass_can_be_turned_off():
    c, cfg = _ctl()
    cfg.tunables.stabilizer_preempt_bypass = False
    held, why = c._stabilize_target(68.0, NOW, cfg, preempting=True)
    assert held == 66.0 and "dwell" in why


def test_a_reversal_after_the_dwell_expires_is_allowed_without_the_bypass():
    c, cfg = _ctl(last_move_min_ago=30.0)
    held, _ = c._stabilize_target(68.0, NOW, cfg, preempting=False)
    assert held is None


def test_the_default_is_bypass_on():
    assert AppConfig.default().tunables.stabilizer_preempt_bypass is True
