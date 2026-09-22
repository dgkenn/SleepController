"""The bed may not be commanded back to the temperature that woke this user cold.

2026-09-19: the settle temperature was 68.3F; the user woke from cold at 00:17 and 01:01 and set
80F by hand. Three things follow. A maintenance floor above 68F. A user's manual change treated
as an instruction that moves tonight's floor or ceiling. And no cooling as prevention until the
temperature trial shows a direction that actually helps.
"""
from datetime import datetime

from sleepctl.config import AppConfig
from sleepctl.controller.controller import SleepController
from sleepctl.models import ControllerState


def _c():
    cfg = AppConfig()
    c = SleepController(cfg)
    c.thermal.set_measured_neutral(69.0)
    return c, cfg


def test_the_maintenance_floor_holds_in_maintenance_and_recovery_only():
    c, cfg = _c()
    assert cfg.tunables.maintenance_floor_f == 69.0
    t, lv = c._apply_session_bounds(ControllerState.MAINTENANCE, 67.0, c.thermal.to_level(67.0))
    assert t == 69.0 and lv == c.thermal.to_level(69.0)
    t, _ = c._apply_session_bounds(ControllerState.WAKE_RECOVERY, 66.0, 0)
    assert t == 69.0
    t, _ = c._apply_session_bounds(ControllerState.INDUCTION, 67.0, 0)   # the onset dip survives
    assert t == 67.0


def test_a_warmer_override_raises_the_floor_from_where_they_fled():
    c, cfg = _c()
    c.note_user_override(68.3, warmer=True, level=-3)
    assert c.session_floor_f == 69.3
    t, _ = c._apply_session_bounds(ControllerState.MAINTENANCE, 68.6, 0)
    assert t == 69.3
    c.note_user_override(67.0, warmer=True)                 # a second, lower one never lowers it
    assert c.session_floor_f == 69.3
    assert len(c.user_overrides) == 2


def test_a_cooler_override_lowers_the_ceiling():
    c, cfg = _c()
    c.note_user_override(72.0, warmer=False)
    assert c.session_ceiling_f == 71.0
    t, _ = c._apply_session_bounds(ControllerState.MAINTENANCE, 73.0, 0)
    assert t == 71.0


def test_the_bounds_are_cleared_when_the_session_resets():
    c, cfg = _c()
    c.note_user_override(68.3, warmer=True)
    c._reset_architecture()
    assert c.session_floor_f is None and c.user_overrides == []


def test_deep_sleep_no_longer_cools_below_the_measured_neutral():
    c, cfg = _c()
    assert SleepController.DEEP_BIAS_DEFAULT_BELOW_NEUTRAL_F == 0.0


def test_pre_emption_does_not_cool_while_cooling_is_disallowed():
    c, cfg = _c()
    assert cfg.tunables.settle_cooling_allowed is False
    c.thermal.settle_nudge_f = -0.7
    assert c._preempt_nudge_f(cfg) == cfg.tunables.preempt_warm_f > 0.0   # it warms instead
    cfg.tunables.preempt_warm_f = 0.0
    assert c._preempt_nudge_f(cfg) == 0.0
    cfg.tunables.settle_cooling_allowed = True
    assert c._preempt_nudge_f(cfg) < 0.0


def test_a_learned_cooling_nudge_is_clamped_at_zero_while_cooling_is_disallowed():
    c, cfg = _c()
    c.thermal.set_settle_nudge(-0.7)
    assert c.thermal.settle_nudge_f == 0.0
    c.thermal.set_settle_nudge(0.4)
    assert c.thermal.settle_nudge_f == 0.4


def test_the_summary_publishes_the_bounds():
    c, cfg = _c()
    c.note_user_override(68.3, warmer=True)
    s = c.thermal_profile_summary()
    assert s["maintenance_floor_f"] == 69.0 and s["session_floor_f"] == 69.3
    assert s["user_overrides"] == 1


def test_a_settle_never_cools_a_warmer_bed_while_cooling_is_disallowed():
    """2026-09-21 replayed: every pre-empt that began in REM (neutral + REM warmth) resolved to
    neutral -- a cooling move at the moment an awakening was predicted, in a user who wakes
    cold. The settle now holds the warmer bed (up to the REM-warm target) instead."""
    from sleepctl.models import NightObjective, ThermalIntent
    c, cfg = _c()
    th = c.thermal
    neutral = th.profile.neutral_f
    rem = neutral + th.profile.rem_warm_offset_f
    t, _ = th.resolve(ThermalIntent.SETTLE_COOL, NightObjective.OPTIMIZE, True, rem, None, None,
                      settle_nudge_f=0.0)
    assert t == rem
    # a bed below neutral is warmed by the pre-empt dose
    t, _ = th.resolve(ThermalIntent.SETTLE_COOL, NightObjective.OPTIMIZE, True, neutral - 1.0,
                      None, None, settle_nudge_f=cfg.tunables.preempt_warm_f)
    assert t > neutral - 1.0
    # the hold is capped: the induction's warm opener is not carried into the night
    t, _ = th.resolve(ThermalIntent.SETTLE_COOL, NightObjective.OPTIMIZE, True, neutral + 4.0,
                      None, None, settle_nudge_f=0.0)
    assert t < neutral + 4.0
    # with cooling allowed the old behaviour stands
    cfg.tunables.settle_cooling_allowed = True
    t, _ = th.resolve(ThermalIntent.SETTLE_COOL, NightObjective.OPTIMIZE, True, rem, None, None,
                      settle_nudge_f=0.0)
    assert t < rem
