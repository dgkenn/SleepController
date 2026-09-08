"""The measured neutral wins over a learned setpoint profile (2026-09-07: every maintenance
intent resolved at or above the comfort ceiling because the learned profile's warmer neutral
silently replaced the sweep's 69.0 F)."""
from dataclasses import replace

from sleepctl.config import AppConfig
from sleepctl.controller.controller import SleepController
from sleepctl.controller.thermal import ThermalIntent
from sleepctl.models import NightObjective


def _ctl():
    c = SleepController(AppConfig.default())
    c.thermal.set_measured_neutral(69.0)
    return c


def test_a_learned_profile_cannot_move_the_measured_neutral():
    c = _ctl()
    learned = replace(c.thermal.profile, neutral_f=71.5, deep_bias_f=70.5, source="ml")
    c.set_setpoints(learned)
    assert c.thermal.profile.neutral_f == 69.0
    assert c.thermal.profile.source == "ml"            # the rest of the profile still applies
    assert c.last_setpoint_override["learned_neutral_f"] == 71.5
    assert c.last_setpoint_override["kept_neutral_f"] == 69.0
    settle = c.thermal.target_for(ThermalIntent.SETTLE_COOL, NightObjective.OPTIMIZE, True, 69.0, -2.0)
    assert settle == 67.0


def test_a_deep_bias_warmer_than_neutral_is_bounded_below_it():
    c = _ctl()
    c.set_setpoints(replace(c.thermal.profile, deep_bias_f=70.5))
    assert c.thermal.profile.deep_bias_f == 69.0 - SleepController.DEEP_BIAS_DEFAULT_BELOW_NEUTRAL_F
    assert c.last_setpoint_override["learned_deep_bias_f"] == 70.5
    deep = c.thermal.target_for(ThermalIntent.DEEP_BIAS_COOL, NightObjective.OPTIMIZE, True, 69.0)
    assert deep < 69.0


def test_the_dose_trial_may_still_shift_neutral_on_purpose():
    c = _ctl()
    c.set_setpoints(replace(c.thermal.profile, neutral_f=68.0), keep_measured_neutral=False)
    assert c.thermal.profile.neutral_f == 68.0


def test_without_a_measured_neutral_the_learned_one_applies():
    c = SleepController(AppConfig.default())
    c.set_setpoints(replace(c.thermal.profile, neutral_f=71.5))
    assert c.thermal.profile.neutral_f == 71.5


def test_the_decision_log_carries_the_profile_in_force_and_the_onset_state():
    from datetime import datetime, timedelta
    from sleepctl.models import ContextRecord, SensorFrame, SleepStage
    c = _ctl()
    c.set_session("induce", keep_light=False)
    t0 = datetime(2026, 9, 7, 21, 41)
    f = SensorFrame(timestamp=t0, stage=SleepStage.LIGHT, stage_confidence=0.6, heart_rate=68.0,
                    presence=None, data_age_seconds=10)
    d = c.decide(f, ContextRecord(date="2026-09-07"), [], t0)
    tp = d.log_payload["thermal_profile"]
    assert tp["neutral_f"] == 69.0 and tp["neutral_is_measured"] is True
    o = d.log_payload["onset"]
    assert o is not None and "signals" in o and o["confirmed"] is False


def test_the_deepen_dose_is_one_degree_by_default_and_never_more_than_two():
    c = _ctl()
    c.set_setpoints(replace(c.thermal.profile, deep_bias_f=72.5))    # nonsense learned value
    assert c.thermal.profile.deep_bias_f == 68.0
    c.set_setpoints(replace(c.thermal.profile, deep_bias_f=63.0))    # colder than the evidence allows
    assert c.thermal.profile.deep_bias_f == 67.0
    c.set_setpoints(replace(c.thermal.profile, deep_bias_f=67.5))    # a learned dose inside the bounds
    assert c.thermal.profile.deep_bias_f == 67.5


def test_the_preempt_settle_uses_the_learned_nudge_within_bounds():
    c = _ctl()
    cfg = c.cfg
    c.set_settle_nudge(-0.7)
    assert c._preempt_nudge_f(cfg) == -0.7
    c.set_settle_nudge(-1.9)                    # colder than the pre-empt dose: capped at it
    assert c._preempt_nudge_f(cfg) == max(cfg.tunables.preempt_settle_nudge_f, -1.9)
    c.set_settle_nudge(0.8)                     # a warming learned nudge: the pre-empt still cools
    assert c._preempt_nudge_f(cfg) == -0.5
