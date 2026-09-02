"""A cooling settle must cool, and it must be big enough to move the bed.

Both failures are from the published record. With this user's evidence-corrected profile
(neutral 69.0 F, cool edge 67.0 F, deep bias 66.0 F, settle nudge -1.0 F):

  * the settle target is ABSOLUTE (neutral + nudge = 68.0 F), so every pre-emption that began
    in deep sleep commanded the water WARMER, at the moment it was trying to prevent an
    awakening -- which is what nearly every moving pre-emption episode did on 2026-08-29 and
    2026-08-31;
  * and 68.0 F is only 40% of the way from neutral to the cool edge, small enough that 12 of 23
    prevention failures showed no measurable bed movement at all.
"""

from sleepctl.config import AppConfig
from sleepctl.controller.thermal import ThermalController
from sleepctl.models import NightObjective, ThermalIntent


def _thermal(neutral=69.0):
    cfg = AppConfig()
    t = ThermalController(cfg)
    t.profile.neutral_f = neutral
    t.neutral_is_measured = True          # suppress the population hot-sleeper prior
    return cfg, t


def _resolve(t, cfg, last_f, settle=None):
    water, _level = t.resolve(ThermalIntent.SETTLE_COOL, NightObjective.OPTIMIZE,
                              cfg.profile.hot_sleeper, last_f, None, None,
                              settle_nudge_f=settle)
    return water


def test_a_cooling_settle_never_warms_the_bed():
    """Coming out of deep sleep the bed is already colder than the settle setpoint."""
    cfg, t = _thermal()
    assert _resolve(t, cfg, last_f=66.0) <= 66.0


def test_a_cooling_settle_from_neutral_actually_cools():
    cfg, t = _thermal()
    assert _resolve(t, cfg, last_f=69.0) < 69.0


def test_the_preempt_settle_is_deeper_than_the_ordinary_one():
    cfg, t = _thermal()
    ordinary = _resolve(t, cfg, last_f=69.0)
    preempt = _resolve(t, cfg, last_f=69.0,
                       settle=cfg.tunables.preempt_settle_nudge_f)
    assert preempt < ordinary


def test_the_preempt_settle_reaches_the_measured_cool_edge():
    """69.0 neutral - 2.0 = 67.0 F, which is this user's evidence-backed cool edge."""
    cfg, t = _thermal()
    got = _resolve(t, cfg, last_f=69.0, settle=cfg.tunables.preempt_settle_nudge_f)
    assert abs(got - 67.0) <= cfg.tunables.max_step_f


def test_an_override_cannot_route_around_the_settle_cap():
    """The cap bounds the learned nudge; a deeper pre-emption must not be a way past it."""
    cfg, t = _thermal()
    cap = cfg.tunables.maintenance_settle_cap_f
    wild = t.target_for(ThermalIntent.SETTLE_COOL, NightObjective.OPTIMIZE,
                        cfg.profile.hot_sleeper, 69.0, settle_nudge_f=-40.0)
    assert wild >= 69.0 - cap - 1e-6


def test_a_warming_settle_is_left_alone():
    """`settle_nudge_f` is learnable and may go positive if warmth prevents THIS user's
    awakenings; the never-warm rule must only bind the cooling case."""
    cfg, t = _thermal()
    t.settle_nudge_f = 1.0
    assert _resolve(t, cfg, last_f=66.0) > 66.0
