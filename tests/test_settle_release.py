"""The settle nudge must be released, not ratcheted.

``STABILIZE`` resolves to "hold the last target", so every settle nudge moved the bed down and
nothing ever moved it back. Measured on 2026-09-18: 828 of 949 maintenance ticks (87%) sat at
68F while pre-emption was active on 21% of them, and 194 of 196 pre-empt firings resolved to
"hold" -- the bed was already at the settle temperature, so the one tool sleep maintenance has
had nothing left to give.
"""
from datetime import datetime, timedelta

from sleepctl.config import AppConfig
from sleepctl.controller.maintenance import MaintenanceRoutine
from sleepctl.models import NightObjective, SensorFrame, SleepStage, ThermalIntent


def _frame(stage=SleepStage.LIGHT):
    return SensorFrame(timestamp=datetime(2026, 9, 19, 2, 0), stage=stage,
                       stage_confidence=0.7, heart_rate=62.0, movement=0.02,
                       data_age_seconds=5.0)


def _routine():
    cfg = AppConfig()
    cfg.tunables.stage_label_actuation = True     # the label-driven mapping these tests pin
    return MaintenanceRoutine(cfg), NightObjective.RECOVERY


def test_a_quiet_stretch_releases_the_bed_back_to_neutral():
    r, obj = _routine()
    assert r.step(_frame(), obj, release=True) is ThermalIntent.NEUTRAL


def test_without_the_release_the_old_hold_is_unchanged():
    r, obj = _routine()
    assert r.step(_frame(), obj) is ThermalIntent.STABILIZE


def test_prevention_always_outranks_the_release():
    r, obj = _routine()
    assert r.step(_frame(), obj, preempt_cool=True, release=True) is ThermalIntent.SETTLE_COOL


def test_deepening_outranks_the_release():
    r, obj = _routine()
    assert r.step(_frame(), obj, deepen=True, release=True) is ThermalIntent.DEEP_BIAS_COOL


def test_deep_and_rem_keep_their_own_targets():
    r, obj = _routine()
    assert r.step(_frame(SleepStage.DEEP), obj, release=True) is ThermalIntent.DEEP_BIAS_COOL
    assert r.step(_frame(SleepStage.REM), obj, release=True) is ThermalIntent.REM_NEUTRAL


# ------------------------------------------------------------------ the controller's clock
def _controller():
    """The 2026-09-18 anchors: measured neutral 69.0F, learned settle nudge -0.7F. That night
    ran the policy that allowed a cooling settle; the shipped default no longer does (68F woke
    this user cold on 09-19), so the release mechanics are exercised with cooling opted back in."""
    from sleepctl.controller.controller import SleepController
    cfg = AppConfig()
    cfg.tunables.settle_cooling_allowed = True
    cfg.tunables.stage_label_actuation = True     # the label-driven release semantics
    c = SleepController(cfg)
    c.thermal.set_measured_neutral(69.0)
    c.thermal.settle_nudge_f = -0.7
    return c, cfg


def test_the_clock_requires_a_real_quiet_stretch():
    c, cfg = _controller()
    t0 = datetime(2026, 9, 19, 2, 0)
    c._last_target_f = 68.3                      # a settle is in force
    c._last_settle_at = t0
    assert c._should_release_settle(t0 + timedelta(minutes=5), cfg) is False
    assert c._should_release_settle(t0 + timedelta(minutes=25), cfg) is True


def test_a_bed_already_at_neutral_is_never_warmed_past_it():
    c, cfg = _controller()
    t0 = datetime(2026, 9, 19, 2, 0)
    c._last_target_f = 69.0
    c._last_settle_at = t0
    assert c._should_release_settle(t0 + timedelta(minutes=60), cfg) is False


def test_the_release_can_be_switched_off():
    c, cfg = _controller()
    cfg.tunables.settle_release_enabled = False
    t0 = datetime(2026, 9, 19, 2, 0)
    c._last_target_f = 68.3
    c._last_settle_at = t0
    assert c._should_release_settle(t0 + timedelta(minutes=60), cfg) is False


def test_nothing_releases_before_the_first_settle_of_the_night():
    c, cfg = _controller()
    c._last_target_f = 68.3
    assert c._should_release_settle(datetime(2026, 9, 19, 2, 0), cfg) is False


def test_the_release_gives_pre_emption_its_headroom_back():
    """The whole point: with the bed back at neutral, a pre-empt is a MOVE again, not a hold."""
    c, cfg = _controller()
    def water(intent, last):
        return c.thermal.resolve(intent, NightObjective.RECOVERY,
                                 hot_sleeper=True, last_target_f=last)[0]

    # The bed already sitting at the settle temperature: a pre-empt resolves to the same
    # number it is already at. That is the 194-of-196 "hold" of 2026-09-18.
    assert water(ThermalIntent.STABILIZE, 68.3) == 68.3
    assert water(ThermalIntent.SETTLE_COOL, 68.3) == 68.3

    # Released to neutral, the same pre-empt is a real 0.7F move again.
    released = water(ThermalIntent.NEUTRAL, 68.3)
    assert released == 69.0
    assert water(ThermalIntent.SETTLE_COOL, released) == 68.3
