"""Reach-time model (ThermalLatencyModel) + reach-aware induction cascade sizing."""

from datetime import datetime

from sleepctl.config import AppConfig
from sleepctl.controller.induction import InductionRoutine, WARM_PULSE_MAX_MIN
from sleepctl.controller.thermal_latency import (
    DEFAULT_COOL_LAG_MIN,
    DEFAULT_COOL_RATE,
    DEFAULT_HEAT_LAG_MIN,
    DEFAULT_HEAT_RATE,
    ThermalLatencyModel,
    minutes_to_reach_f,
)
from sleepctl.models import NightObjective, SensorFrame, SleepStage, ThermalIntent


def _frame(stage=SleepStage.AWAKE):
    return SensorFrame(timestamp=datetime(2026, 6, 24, 23, 0), stage=stage,
                       bed_temp_f=72.0, room_temp_f=68.0, presence=True, data_age_seconds=5)


# ---- minutes_to_reach --------------------------------------------------------
def test_minutes_to_reach_zero_for_noop():
    m = ThermalLatencyModel()
    assert m.minutes_to_reach(-50, -50) == 0.0
    assert m.minutes_to_reach(-50.0, -50.2) == 0.0  # within epsilon


def test_minutes_to_reach_scales_with_delta():
    m = ThermalLatencyModel(heat_rate=4.0, cool_rate=1.5,
                            heat_lag_min=0.0, cool_lag_min=0.0)
    small = m.minutes_to_reach(0, 10)   # warming 10 levels
    big = m.minutes_to_reach(0, 40)     # warming 40 levels
    assert big > small
    # linear in |delta| when lag is 0
    assert abs(big - 4 * small) < 1e-9


def test_minutes_to_reach_uses_direction_rate_and_lag():
    m = ThermalLatencyModel(heat_rate=4.0, cool_rate=1.5,
                            heat_lag_min=2.0, cool_lag_min=3.0)
    warm = m.minutes_to_reach(-40, 0)   # +40 levels warming
    cool = m.minutes_to_reach(0, -40)   # -40 levels cooling
    # same |delta| but cooling is slower AND has a bigger lag -> strictly longer
    assert cool > warm
    assert warm == 2.0 + 40 / 4.0
    assert cool == 3.0 + 40 / 1.5


def test_lead_minutes_adds_margin_and_floors_at_zero():
    m = ThermalLatencyModel(heat_lag_min=0.0)
    base = m.minutes_to_reach(0, 20)
    assert m.lead_minutes(0, 20, margin_min=2.0) == base + 2.0
    # a no-op with zero margin can't go negative
    assert m.lead_minutes(-50, -50, margin_min=0.0) == 0.0


def test_minutes_to_reach_f_uses_calibration():
    m = ThermalLatencyModel()
    # a warm move in °F should cost time; round-trips through the level map
    assert minutes_to_reach_f(60.0, 60.0, m) == 0.0
    assert minutes_to_reach_f(60.0, 75.0, m) > 0.0


# ---- from_rates: None-safe + clamping ---------------------------------------
def test_from_rates_none_safe_defaults():
    m = ThermalLatencyModel.from_rates(None, None, None, None)
    assert m.heat_rate == DEFAULT_HEAT_RATE
    assert m.cool_rate == DEFAULT_COOL_RATE
    assert m.heat_lag_min == DEFAULT_HEAT_LAG_MIN
    assert m.cool_lag_min == DEFAULT_COOL_LAG_MIN


def test_from_rates_takes_magnitude_of_negative_cool_rate():
    # the self-test records cooling as a NEGATIVE levels/min
    m = ThermalLatencyModel.from_rates(4.0, -1.5, 2.0, 3.0)
    assert m.cool_rate == 1.5


def test_from_rates_clamps_insane_values():
    # absurdly fast + absurdly slow both get clamped into the sane bounds
    m = ThermalLatencyModel.from_rates(999.0, 0.001, 999.0, None)
    assert m.heat_rate == 10.0    # HEAT_RATE_BOUNDS high
    assert m.cool_rate == 0.3     # COOL_RATE_BOUNDS low
    assert m.heat_lag_min == 15.0  # LAG_BOUNDS high


def test_from_rates_nonpositive_rate_falls_back_to_default():
    m = ThermalLatencyModel.from_rates(0.0, None, None, None)
    assert m.heat_rate == DEFAULT_HEAT_RATE


# ---- from_repo defensive fallback -------------------------------------------
def test_from_repo_defaults_when_no_data():
    class _Repo:
        conn = None

        def get_thermal_calibration(self):
            return None

    m = ThermalLatencyModel.from_repo(_Repo())
    assert m.heat_rate == DEFAULT_HEAT_RATE
    assert m.cool_rate == DEFAULT_COOL_RATE


def test_from_repo_prefers_calibration():
    class _Repo:
        conn = None

        def get_thermal_calibration(self):
            return {"heat_levels_per_min": 3.0, "cool_levels_per_min": -1.2,
                    "heat_lag_min": 1.0, "cool_lag_min": 4.0}

    m = ThermalLatencyModel.from_repo(_Repo())
    assert m.heat_rate == 3.0
    assert m.cool_rate == 1.2
    assert m.heat_lag_min == 1.0
    assert m.cool_lag_min == 4.0


# ---- induction cascade: reach-aware warm-pulse ------------------------------
def _cold_warm_levels(cfg):
    """The cold-settle and warm-pulse device levels for a default profile (OPTIMIZE)."""
    from sleepctl.controller.calibration import fahrenheit_to_level
    from sleepctl.controller.thermal import ThermalController
    th = ThermalController(cfg)
    cold_f = th.target_for(ThermalIntent.ONSET_COLD_SETTLE, NightObjective.OPTIMIZE, True)
    warm_f = th.target_for(ThermalIntent.ONSET_WARM, NightObjective.OPTIMIZE, True)
    return fahrenheit_to_level(cold_f), fahrenheit_to_level(warm_f)


def test_slow_warm_rate_extends_the_warm_pulse():
    cfg = AppConfig.default()
    base_lvl, warm_lvl = _cold_warm_levels(cfg)
    warm_min = cfg.tunables.induction_warm_pulse_min   # 10
    ind = InductionRoutine(cfg)
    # A slow warm rate (1.3 levels/min) with a big baseline->warm gap makes the reach time exceed
    # the fixed 10-min opener, so the warm-first cascade keeps warming until the bed arrives.
    ind.set_latency(ThermalLatencyModel(heat_rate=1.3, cool_rate=1.5,
                                        heat_lag_min=2.0, cool_lag_min=3.0))
    ind.set_phase_levels(base_lvl, warm_lvl, base_lvl)
    reach = ind.latency.minutes_to_reach(base_lvl, warm_lvl)
    assert reach > warm_min  # precondition: the reach time really is longer than the fixed opener
    # At just past the OLD end of the opener, the reach-aware cascade is STILL warming (not cooling).
    just_past_fixed = warm_min + 1
    assert just_past_fixed < reach
    assert ind.step(_frame(), NightObjective.OPTIMIZE, just_past_fixed) is ThermalIntent.ONSET_WARM
    # ...and once the reach time is satisfied it hands off to consolidate.
    assert ind.step(_frame(), NightObjective.OPTIMIZE,
                    reach + 1) is ThermalIntent.INDUCTION_COOL


def test_latency_none_keeps_fixed_behavior():
    cfg = AppConfig.default()
    ind = InductionRoutine(cfg)  # no latency, no phase levels
    warm_min = cfg.tunables.induction_warm_pulse_min
    # exactly the fixed warm-first windows: warm opener, then cool (never a cold opener)
    assert ind.step(_frame(), NightObjective.OPTIMIZE, 1) is ThermalIntent.ONSET_WARM
    assert ind.step(_frame(), NightObjective.OPTIMIZE,
                    warm_min + 1) is ThermalIntent.INDUCTION_COOL


def test_warm_pulse_is_capped():
    cfg = AppConfig.default()
    base_lvl, warm_lvl = _cold_warm_levels(cfg)
    ind = InductionRoutine(cfg)
    # An absurdly slow warm rate would demand a runaway opener; the cap holds it to WARM_PULSE_MAX_MIN.
    ind.set_latency(ThermalLatencyModel(heat_rate=0.5, cool_rate=1.5,
                                        heat_lag_min=15.0, cool_lag_min=3.0))
    ind.set_phase_levels(base_lvl, warm_lvl, base_lvl)
    eff = ind._warm_min_effective(cfg.tunables.induction_warm_pulse_min)
    assert eff == WARM_PULSE_MAX_MIN
    # well past the cap -> consolidate (the opener cannot run forever)
    assert ind.step(_frame(), NightObjective.OPTIMIZE,
                    WARM_PULSE_MAX_MIN + 1) is ThermalIntent.INDUCTION_COOL
