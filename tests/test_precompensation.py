"""Environmental pre-compensation: overnight forecast -> feed-forward bed bias."""

from datetime import datetime, timedelta

from sleepctl.adapters.weather import OpenMeteoWeather
from sleepctl.config import AppConfig
from sleepctl.controller.thermal import ThermalController
from sleepctl.models import NightObjective, ThermalIntent
from sleepctl.precompensation import compute_precompensation


class _FakeWeather(OpenMeteoWeather):
    def __init__(self, series):
        super().__init__()
        self._series = series

    def _fetch_hourly(self):
        return self._series


def _hourly(start_dt, temps):
    return [((start_dt + timedelta(hours=i)).strftime("%Y-%m-%dT%H:%M"), float(v))
            for i, v in enumerate(temps)]


def test_overnight_forecast_warming_trend():
    series = _hourly(datetime(2026, 7, 1, 21, 0), [70, 71, 72, 74, 76, 78, 80, 81, 82, 83, 84])
    w = _FakeWeather(series)
    fc = w.overnight_forecast(from_dt=datetime(2026, 7, 1, 21, 0), hours=11)
    assert fc is not None
    assert fc["start_f"] == 70 and fc["high_f"] == 84 and fc["trend"] == "warming"
    assert len(fc["hours"]) == 11


def test_precomp_hot_warming_biases_cooler_and_precools():
    fc = {"low_f": 74, "high_f": 84, "trend": "warming",
          "hours": [{"hour": "23:00", "temp_f": 78 + i} for i in range(6)]}
    pc = compute_precompensation(fc, AppConfig.default())
    assert pc["bias_f"] < 0 and pc["pre_cool"] is True
    assert pc["overnight_mean_f"] is not None


def test_precomp_cold_biases_warmer():
    fc = {"low_f": 28, "high_f": 36, "trend": "cooling",
          "hours": [{"hour": "23:00", "temp_f": 32} for _ in range(6)]}
    pc = compute_precompensation(fc, AppConfig.default())
    assert pc["bias_f"] > 0 and pc["pre_cool"] is False


def test_precomp_mild_no_bias():
    fc = {"low_f": 50, "high_f": 56, "trend": "stable",
          "hours": [{"hour": "23:00", "temp_f": 53} for _ in range(6)]}
    pc = compute_precompensation(fc, AppConfig.default())
    assert pc["bias_f"] == 0.0


def test_precomp_none_forecast_is_safe():
    pc = compute_precompensation(None, AppConfig.default())
    assert pc["bias_f"] == 0.0 and pc["pre_cool"] is False


def test_ambient_bias_shifts_target_and_clamps():
    cfg = AppConfig.default()
    th = ThermalController(cfg)
    base = th.target_for(ThermalIntent.NEUTRAL, NightObjective.OPTIMIZE, hot_sleeper=True)
    th.set_ambient_bias(-2.0)
    cooled = th.target_for(ThermalIntent.NEUTRAL, NightObjective.OPTIMIZE, hot_sleeper=True)
    assert abs((base - cooled) - 2.0) < 0.01
    th.set_ambient_bias(-10.0)  # beyond cap
    assert abs(th.ambient_bias_f) <= cfg.tunables.precomp_max_bias_f + 1e-9
