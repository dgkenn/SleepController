"""In-bed comfort mapping: the sweep collects ratings and derives neutral + comfort band."""

from sleepctl.controller.comfort import (
    ComfortCalibration, build_comfort_profile, steps_around)


def test_sweep_collects_and_advances():
    c = ComfortCalibration(steps_f=[64, 68, 72, 76])
    seen = []
    for rating in (-2, -1, 1, 2):
        seen.append(c.current_target_f())
        c.rate(rating)
    assert seen == [64, 68, 72, 76]
    assert c.done and c.current_target_f() is None


def test_neutral_interpolates_zero_crossing():
    # rating goes -1 at 68 -> +1 at 72, so "just right" interpolates to 70
    p = build_comfort_profile([{"f": 68, "rating": -1}, {"f": 72, "rating": 1}])
    assert p.neutral_f == 70.0
    assert p.cool_edge_f == 68 and p.warm_edge_f == 72


def test_all_too_warm_picks_coolest_acceptable():
    p = build_comfort_profile([{"f": 64, "rating": 0}, {"f": 68, "rating": 1},
                               {"f": 72, "rating": 2}])
    assert p.neutral_f == 64.0          # the just-right one
    assert p.warm_edge_f == 68 and p.cool_edge_f == 64  # 72 (too warm) excluded from band


def test_rating_is_clamped():
    c = ComfortCalibration(steps_f=[70])
    c.rate(9)                            # out of range -> clamped to +2
    assert c.ratings[0]["rating"] == 2


def test_cancel_ends_the_sweep():
    c = ComfortCalibration(steps_f=[64, 68])
    c.cancel()
    assert c.done and c.cancelled


def test_steps_around_centers_on_neutral():
    s = steps_around(70.0, spread_f=6.0, n=4)
    assert s[0] == 64.0 and s[-1] == 76.0 and len(s) == 4
    assert steps_around(None)            # falls back to defaults


def test_comfort_clamp_bounds_the_target_in_maintenance():
    """REGRESSION for a real night. With sensed bed temperature paywalled the thermal loop is
    open-loop, and nothing bounded the commanded target: the guardrail only WARNS about an
    out-of-band value, and the only hard clamp is the device's 55-110 F range -- meaningless when
    the usable range spans ~2 F. The bed drifted to the too-warm edge for hours (awakenings every
    ~20 min), then overshot to the too-cold edge (three more within 7 minutes).
    """
    from datetime import datetime, timedelta
    from sleepctl.config import AppConfig
    from sleepctl.controller.controller import SleepController
    from sleepctl.controller.state_machine import SleepStateMachine
    from sleepctl.models import ContextRecord, ControllerState, SensorFrame, SleepStage

    cfg = AppConfig.default()
    now = datetime(2026, 8, 5, 2, 0, 0)
    ctrl = SleepController(cfg)
    ctrl.sm = SleepStateMachine(cfg, state=ControllerState.MAINTENANCE)
    ctrl.sm.state = ControllerState.MAINTENANCE
    ctrl.set_comfort_profile({"neutral_f": 65.5, "cool_edge_f": 64.0, "warm_edge_f": 66.5})

    recent = [SensorFrame(timestamp=now - timedelta(minutes=10 - i), stage=SleepStage.LIGHT,
                          stage_confidence=0.7, heart_rate=60.0, hrv=40.0, movement=0.02,
                          presence=None, data_age_seconds=5.0)
              for i in range(10)]
    frame = SensorFrame(timestamp=now, stage=SleepStage.LIGHT, stage_confidence=0.7,
                        heart_rate=60.0, hrv=40.0, movement=0.02, presence=None,
                        data_age_seconds=5.0)

    # drive the controller from far outside the band in BOTH directions
    for start in (75.0, 55.0):
        ctrl._last_target_f = start
        d = ctrl.decide(frame, ContextRecord(date="2026-08-05"), recent, now)
        assert 63.5 - 1e-6 <= d.target_temp_f <= 67.0 + 1e-6, (
            f"target {d.target_temp_f} escaped the clamped comfort band from {start}")


def test_comfort_clamp_does_not_muzzle_the_wake_ramp():
    """The wake ramp is deliberately ABOVE the comfort band -- warmth is how the controller
    assists waking. Clamping WAKE_WINDOW would break designed behaviour rather than protect
    sleep, so the clamp is confined to the sleep-holding states."""
    from datetime import datetime, timedelta
    from sleepctl.config import AppConfig
    from sleepctl.controller.controller import SleepController
    from sleepctl.controller.state_machine import SleepStateMachine
    from sleepctl.models import ContextRecord, ControllerState, SensorFrame, SleepStage

    cfg = AppConfig.default()
    now = datetime(2026, 8, 5, 5, 45, 0)
    ctrl = SleepController(cfg)
    ctrl.sm = SleepStateMachine(cfg, state=ControllerState.WAKE_WINDOW)
    ctrl.sm.state = ControllerState.WAKE_WINDOW
    ctrl.set_comfort_profile({"neutral_f": 65.5, "cool_edge_f": 64.0, "warm_edge_f": 66.5})
    ctrl._last_target_f = 68.0

    recent = [SensorFrame(timestamp=now - timedelta(minutes=10 - i), stage=SleepStage.LIGHT,
                          stage_confidence=0.7, heart_rate=62.0, hrv=35.0, movement=0.05,
                          presence=None, data_age_seconds=5.0)
              for i in range(10)]
    frame = SensorFrame(timestamp=now, stage=SleepStage.LIGHT, stage_confidence=0.7,
                        heart_rate=62.0, hrv=35.0, movement=0.05, presence=None,
                        data_age_seconds=5.0)
    ctx = ContextRecord(date="2026-08-05")
    ctx.required_wake_time = now + timedelta(minutes=15)
    d = ctrl.decide(frame, ctx, recent, now)
    assert "clamped to personal comfort band" not in (d.reason or "")


def test_measured_neutral_suppresses_the_population_hot_sleeper_bias():
    """REGRESSION. hot_sleeper_cool_bias_f is a POPULATION prior for "runs warm". Once the
    neutral is measured FROM this user it already encodes that, so stacking the prior on top
    double-counts and drives the bed below anything they were observed to tolerate.

    Measured on a real night: calibrated neutral 65.5 F + the -1.5 prior + a -1.48 weather bias
    resolved to 62.5 F -- colder than the water temperature that woke this user three times in
    seven minutes. With the neutral marked as measured it resolves to 65.4 F, inside the band.
    """
    from sleepctl.config import AppConfig
    from sleepctl.controller.thermal import ThermalController
    from sleepctl.models import NightObjective, ThermalIntent

    cfg = AppConfig.default()

    population = ThermalController(cfg)
    population.set_ambient_bias(-1.48)
    stacked = population.target_for(ThermalIntent.NEUTRAL, NightObjective.OPTIMIZE,
                                    hot_sleeper=True)

    personal = ThermalController(cfg)
    personal.set_ambient_bias(-1.48)
    personal.set_measured_neutral(population.profile.neutral_f)   # same neutral, now MEASURED
    unstacked = personal.target_for(ThermalIntent.NEUTRAL, NightObjective.OPTIMIZE,
                                    hot_sleeper=True)

    assert personal.neutral_is_measured is True
    assert unstacked > stacked, "measured neutral must not also absorb the population prior"
    assert abs((unstacked - stacked) - abs(cfg.tunables.hot_sleeper_cool_bias_f)) < 1e-6

    # a NON-measured neutral must still get the prior -- this is not a blanket removal
    assert population.neutral_is_measured is False
