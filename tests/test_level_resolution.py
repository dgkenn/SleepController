"""The bed can be set between whole degrees.

The vendored table has one entry per whole degree and the lookup used to snap to the nearest,
so every target was rounded before it reached the bed. Measured 2026-09-22: 68.75-69.5 F all
mapped to -54, so the 0.5 F wake-recovery warmth never changed the bed at all, and the thermal
trial's 0.0 / 0.5 and 1.0 / 1.5 F arms were physically the same dose -- two of its five arms
were placebo copies of the others. The Pod accepts every integer level in between.
"""
from sleepctl.config import AppConfig
from sleepctl.controller.calibration import fahrenheit_to_level, level_to_fahrenheit
from sleepctl.controller.thermal import ThermalController


def test_whole_degrees_still_map_exactly_to_their_anchors():
    for level, temp in ((-68, 66), (-63, 67), (-58, 68), (-54, 69), (-49, 70), (-44, 71), (-40, 72)):
        assert fahrenheit_to_level(temp) == level
        assert level_to_fahrenheit(level) == float(temp)


def test_half_degrees_land_between_the_anchors():
    assert -58 < fahrenheit_to_level(68.5) < -54
    assert -54 < fahrenheit_to_level(69.5) < -49
    assert -49 < fahrenheit_to_level(70.5) < -44


def test_the_mapping_is_monotonic_across_the_night_range():
    levels = [fahrenheit_to_level(60 + 0.25 * i) for i in range(1, 60)]
    assert levels == sorted(levels)


def test_the_wake_recovery_warmth_now_actually_changes_the_bed():
    cfg = AppConfig()
    t = ThermalController(cfg)
    neutral = 69.0
    assert t.to_level(neutral + cfg.tunables.wake_recovery_warm_f) != t.to_level(neutral)


def test_every_trial_arm_is_a_different_physical_dose():
    cfg = AppConfig()
    t = ThermalController(cfg)
    doses = [t.to_level(69.0 + x) for x in cfg.thermal_trial.offset_ladder_f]
    assert len(set(doses)) == len(doses), f"arms collapse onto the same level: {doses}"


def test_round_trip_is_within_a_quarter_degree():
    for i in range(0, 40):
        f = 66.0 + 0.2 * i
        assert abs(level_to_fahrenheit(fahrenheit_to_level(f)) - f) <= 0.26
