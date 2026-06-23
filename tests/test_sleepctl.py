"""Unit + end-to-end tests for sleepctl.

Covers the safety invariants that matter for this user: small/gradual thermal steps,
wake-recovery on awakenings, smart-wake firing, the 3-layer dataset getting populated,
and the learning loop resisting a single bad night.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from sleepctl.adapters.calendar import ManualCalendarSource
from sleepctl.adapters.simulator import SimulatorActuator, SimulatorSource
from sleepctl.config import AppConfig
from sleepctl.controller.controller import SleepController
from sleepctl.controller.thermal import ThermalController
from sleepctl.controller.wake_detection import WakeDetector
from sleepctl.learning.policy import TieredPolicy
from sleepctl.loop.nightly import NightlyUpdater
from sleepctl.loop.runtime import Runtime
from sleepctl.models import (
    Baselines,
    ContextRecord,
    ControllerState,
    NightSummary,
    SensorFrame,
    SleepStage,
)
from sleepctl.storage.repository import Repository


# --------------------------------------------------------------------------- storage


def test_storage_roundtrip():
    from sleepctl.models import (
        CorrectionAction,
        Decision,
        Intervention,
        NightObjective,
        ThermalIntent,
    )

    r = Repository(":memory:")
    now = datetime(2026, 6, 23, 23, 0)
    r.log_sample(SensorFrame(timestamp=now, stage=SleepStage.DEEP, heart_rate=55, data_age_seconds=10),
                 "MAINTENANCE", False, "2026-06-23")
    r.log_decision(Decision(now, ControllerState.MAINTENANCE, NightObjective.OPTIMIZE,
                            ThermalIntent.DEEP_BIAS_COOL, 66.0, -40, CorrectionAction.COOLER,
                            "x", 0.9, {"k": 1}), "2026-06-23")
    r.log_intervention(Intervention(now, ControllerState.MAINTENANCE, CorrectionAction.COOLER,
                                    1.5, "x", held=True), "2026-06-23")
    r.save_night_summary(NightSummary(date="2026-06-23", total_sleep_min=470, deep_min=100,
                                      wake_events=1, temp_profile_summary={"min": 64}))
    r.save_context(ContextRecord(date="2026-06-23", steps=8000))
    r.save_baselines(Baselines(metrics={"avg_hrv_7d_median": 68.0}, updated=now))

    assert r.recent_nights(5)[-1].temp_profile_summary == {"min": 64}
    assert r.recent_interventions(5)[0].held is True
    assert r.latest_baselines().get("avg_hrv_7d_median") == 68.0
    assert r.get_context("2026-06-23").steps == 8000
    assert r.samples_for_night("2026-06-23")[0].stage is SleepStage.DEEP


# --------------------------------------------------------------------------- thermal


def test_thermal_slew_limit():
    tc = ThermalController(AppConfig.default())
    # large requested move is capped to max_step_f per call
    out = tc.slew_limit(70.0, 50.0)
    assert abs(out - 70.0) <= AppConfig.default().tunables.max_step_f + 1e-9


def test_thermal_to_level_clamped():
    tc = ThermalController(AppConfig.default())
    assert -100 <= tc.to_level(20.0) <= 100
    assert -100 <= tc.to_level(120.0) <= 100


def test_calibration_matches_real_eight_sleep_table():
    """The vendored Eight Sleep lookup table maps known points correctly."""
    from sleepctl.controller.calibration import (
        MAX_TEMP_F,
        MIN_TEMP_F,
        fahrenheit_to_level,
        level_to_fahrenheit,
    )

    # Authoritative reference points from pyEight RAW_TO_FAHRENHEIT_MAP.
    assert fahrenheit_to_level(55) == -100
    assert fahrenheit_to_level(66) == -68
    assert fahrenheit_to_level(70) == -49
    assert fahrenheit_to_level(74) == -31
    assert level_to_fahrenheit(0) == 81  # NOT 70 (the old wrong assumption)
    assert level_to_fahrenheit(-100) == 55
    # out-of-range targets clamp to the device's supported window
    assert MIN_TEMP_F <= level_to_fahrenheit(fahrenheit_to_level(40)) <= MAX_TEMP_F
    assert fahrenheit_to_level(200) == fahrenheit_to_level(MAX_TEMP_F)


def test_composite_blend_and_inversion():
    tc = ThermalController(AppConfig.default())
    a = AppConfig.default().tunables.composite_bed_weight
    # blend: covered body (bed) weighted with exposed-skin ambient
    assert tc.composite_temp(80.0, 60.0) == a * 80.0 + (1 - a) * 60.0
    assert tc.composite_temp(None, 60.0) is None         # no bed temp -> unknown
    assert tc.composite_temp(80.0, None) == 80.0         # no ambient -> just bed surface
    # feedforward inversion is the exact inverse of the blend
    eff, ambient = 70.0, 60.0
    water = tc.required_water_open_loop(eff, ambient)
    assert abs((a * water + (1 - a) * ambient) - eff) < 1e-6


def test_cold_exposed_skin_drives_bed_warmer():
    """Cold room (exposed head) should push the commanded water WARMER to compensate."""
    cfg = AppConfig.default()
    tc = ThermalController(cfg)
    from sleepctl.models import NightObjective, ThermalIntent

    last = cfg.tunables.neutral_temp_f
    # same intent, same bed temp, but a cold vs warm room
    warm_room, _ = tc.resolve(ThermalIntent.NEUTRAL, NightObjective.OPTIMIZE, True,
                              last, bed_temp_f=70.0, ambient_temp_f=75.0)
    tc2 = ThermalController(cfg)
    cold_room, _ = tc2.resolve(ThermalIntent.NEUTRAL, NightObjective.OPTIMIZE, True,
                               last, bed_temp_f=70.0, ambient_temp_f=55.0)
    assert cold_room > warm_room  # colder exposed skin -> warmer bed command


def test_controller_prefers_bedroom_temp_over_outdoor():
    from sleepctl.models import ContextRecord

    cfg = AppConfig.default()
    c = SleepController(cfg)
    now = datetime(2026, 6, 23, 23, 0)
    ctx = ContextRecord(date="2026-06-23", outdoor_temp_f=95.0)
    # frame reports a cool bedroom; controller should use 68F (bedroom) as exposed-skin ambient
    frame = SensorFrame(timestamp=now, stage=SleepStage.LIGHT, presence=True, movement=0.05,
                        bed_temp_f=70.0, room_temp_f=68.0, data_age_seconds=30)
    d = c.decide(frame, ctx, [], now)
    assert d.log_payload["ambient_temp_f"] == 68.0
    assert d.log_payload["composite_temp_f"] is not None


def test_controller_uses_outdoor_when_no_bedroom_temp():
    from sleepctl.models import ContextRecord

    cfg = AppConfig.default()
    c = SleepController(cfg)
    now = datetime(2026, 6, 23, 23, 30)
    ctx = ContextRecord(date="2026-06-23", outdoor_temp_f=95.0)  # hot night
    # no room_temp_f on the frame -> falls back to outdoor weather for exposed-skin ambient
    frame = SensorFrame(timestamp=now, stage=SleepStage.LIGHT, presence=True, movement=0.05,
                        bed_temp_f=72.0, room_temp_f=None, data_age_seconds=30)
    d = c.decide(frame, ctx, [], now)
    assert d.log_payload["ambient_temp_f"] == 95.0


def test_context_outdoor_temp_roundtrip():
    from sleepctl.models import ContextRecord

    r = Repository(":memory:")
    r.save_context(ContextRecord(date="2026-06-23", outdoor_temp_f=72.5))
    assert r.get_context("2026-06-23").outdoor_temp_f == 72.5


def test_rem_gets_small_warm_bias():
    """REM target is warmer than deep (evidence: warmth promotes REM)."""
    from sleepctl.models import NightObjective, ThermalIntent

    tc = ThermalController(AppConfig.default())
    deep = tc.target_for(ThermalIntent.DEEP_BIAS_COOL, NightObjective.OPTIMIZE, hot_sleeper=True)
    rem = tc.target_for(ThermalIntent.REM_NEUTRAL, NightObjective.OPTIMIZE, hot_sleeper=True)
    assert rem > deep


# ---------------------------------------------------------------------- wake detect


def test_wake_detection_requires_multiple_signals():
    det = WakeDetector(min_signals=3)
    base = [SensorFrame(timestamp=datetime(2026, 6, 23, 1, m), stage=SleepStage.DEEP,
                        heart_rate=52, hrv=72, respiratory_rate=12, movement=0.02,
                        stage_confidence=0.9) for m in range(10)]
    # a clear awakening: spike movement, hr up, confidence drop, stage regress
    wake = SensorFrame(timestamp=datetime(2026, 6, 23, 1, 11), stage=SleepStage.AWAKE,
                       heart_rate=70, hrv=45, respiratory_rate=18, movement=0.9,
                       stage_confidence=0.3)
    assert det.evaluate(wake, base) is not None
    # a single benign blip should not trigger
    blip = SensorFrame(timestamp=datetime(2026, 6, 23, 1, 11), stage=SleepStage.DEEP,
                       heart_rate=53, hrv=71, respiratory_rate=12, movement=0.05,
                       stage_confidence=0.88)
    assert det.evaluate(blip, base) is None


# ----------------------------------------------------------------------- controller


def _run_scenario(scenario: str):
    cfg = AppConfig.default()
    start = datetime(2026, 6, 23, 23, 0)
    source = SimulatorSource(scenario, seed=7, start=start)
    actuator = SimulatorActuator(source)
    repo = Repository(":memory:")
    required_wake = start + timedelta(minutes=source.length)
    ctx = ManualCalendarSource(required_wake_time=required_wake, bedtime=start).get_context(
        start.date().isoformat()
    )
    runtime = Runtime(cfg, source, actuator, repo, controller=SleepController(cfg))
    decisions = runtime.replay(ctx)
    return cfg, actuator, repo, decisions


def test_controller_state_progression_normal():
    _, _, _, decisions = _run_scenario("normal")
    states = {d.state.value for d in decisions}
    assert "induction" in states
    assert "maintenance" in states
    assert "wake_window" in states


def test_wake_recovery_triggers_on_awakenings():
    _, _, _, decisions = _run_scenario("clustered_awakenings")
    states = [d.state.value for d in decisions]
    assert "wake_recovery" in states


def test_slew_limit_never_violated_end_to_end():
    # Assert the true invariant in °F (table-agnostic): the commanded target never moves
    # more than max_step_f between consecutive ticks.
    cfg, _, _, decisions = _run_scenario("normal")
    targets = [d.target_temp_f for d in decisions]
    for a, b in zip(targets, targets[1:]):
        assert abs(b - a) <= cfg.tunables.max_step_f + 1e-6


def test_dataset_all_three_layers_populated():
    _, _, repo, _ = _run_scenario("normal")
    night_date = "2026-06-23"
    assert len(repo.samples_for_night(night_date)) > 0           # layer 1
    repo.save_context(ContextRecord(date=night_date, steps=1))   # layer 3 write path
    assert repo.get_context(night_date) is not None
    # decisions + interventions ledgers
    n_decisions = repo.conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0]
    n_iv = repo.conn.execute("SELECT COUNT(*) FROM interventions").fetchone()[0]
    assert n_decisions > 0 and n_iv > 0


def test_smart_wake_fires_in_window():
    _, _, _, decisions = _run_scenario("normal")
    assert any(d.log_payload.get("should_wake") for d in decisions)


# ------------------------------------------------------------------------- learning


def test_single_bad_night_does_not_revert():
    cfg = AppConfig.default()
    p = TieredPolicy(cfg)
    p.recommend(Baselines(), {}, {})  # start a trial

    def good(d):
        return NightSummary(date=d, wake_events=1, sleep_efficiency=0.88, deep_min=100, avg_hrv=66)

    def bad(d):
        return NightSummary(date=d, wake_events=5, sleep_efficiency=0.6, deep_min=40, avg_hrv=40)

    p.register_outcome(good("2026-06-10"))
    p.register_outcome(good("2026-06-11"))
    p.register_outcome(bad("2026-06-12"))
    rec = p.recommend(Baselines(), {}, {})
    assert rec["action"] != "revert"


def test_policy_low_stage_triggers():
    """Autopilot-style triggers: low deep -> cooling trial; low REM -> warming trial."""
    cfg = AppConfig.default()
    # low deep (10% of night): expect a deep-cooling trial
    p = TieredPolicy(cfg)
    p.register_outcome(NightSummary(date="d", total_sleep_min=400, deep_min=40, rem_min=120,
                                    wake_events=1))
    assert p.recommend(Baselines(), {}, {})["target"] == "deep_bias_cooling"
    # adequate deep but low REM (10%): expect a REM-warming trial
    p2 = TieredPolicy(cfg)
    p2.register_outcome(NightSummary(date="d", total_sleep_min=400, deep_min=100, rem_min=40,
                                     wake_events=1))
    assert p2.recommend(Baselines(), {}, {})["target"] == "rem_warming"


def test_sustained_worsening_reverts():
    cfg = AppConfig.default()
    p = TieredPolicy(cfg)
    p.recommend(Baselines(), {}, {})
    p.register_outcome(NightSummary(date="2026-06-10", wake_events=1, sleep_efficiency=0.9,
                                    deep_min=100, avg_hrv=66))
    for d in ("2026-06-11", "2026-06-12", "2026-06-13"):
        p.register_outcome(NightSummary(date=d, wake_events=5, sleep_efficiency=0.6,
                                        deep_min=40, avg_hrv=40))
    assert p.recommend(Baselines(), {}, {})["action"] == "revert"


def test_setpoint_profile_roundtrip():
    from sleepctl.models import SetpointProfile

    r = Repository(":memory:")
    assert r.latest_setpoints() is None
    p = SetpointProfile(neutral_f=70, deep_bias_f=66, rem_warm_offset_f=1.5, wake_ramp_f=74,
                        composite_bed_weight=0.75, version=3, source="policy")
    r.save_setpoints(p)
    got = r.latest_setpoints()
    assert got.version == 3 and got.deep_bias_f == 66 and got.source == "policy"


def test_apply_recommendation_evolves_setpoint():
    from sleepctl.learning.setpoints import apply_recommendation

    cfg = AppConfig.default()
    base = cfg.default_setpoints()
    # a deep-cooling trial lowers the deep setpoint and bumps the version
    after = apply_recommendation(base, {"action": "try", "target": "deep_bias_cooling",
                                        "magnitude_f": 1.0}, cfg)
    assert after.deep_bias_f == base.deep_bias_f - 1.0
    assert after.version == base.version + 1 and after.source == "policy"
    # a rem-warming trial raises the REM offset
    rem = apply_recommendation(base, {"action": "try", "target": "rem_warming",
                                      "magnitude_f": 1.0}, cfg)
    assert rem.rem_warm_offset_f == base.rem_warm_offset_f + 1.0
    # "hold" makes no change (no version churn)
    held = apply_recommendation(base, {"action": "hold", "target": "deep_bias_cooling"}, cfg)
    assert held.version == base.version


def test_nightly_stamps_and_evolves_setpoint():
    cfg = AppConfig.default()
    repo = Repository(":memory:")
    updater = NightlyUpdater(cfg, repo)
    # low-deep night -> policy starts a deep-cooling trial -> setpoint should evolve
    night = NightSummary(date="2026-06-10", total_sleep_min=400, deep_min=40, rem_min=120,
                         wake_events=1, sleep_efficiency=0.88, avg_hrv=66)
    result = updater.run(night)
    # the night is attributed to the active (v0) setpoint
    assert repo.recent_nights(1)[0].setpoint_version == 0
    # a new, evolved setpoint version was persisted for the next night
    assert repo.latest_setpoints().version == result["next_setpoint_version"]
    assert repo.latest_setpoints().version >= 1


def test_controller_uses_injected_setpoint():
    from sleepctl.models import NightObjective, SetpointProfile, ThermalIntent

    cfg = AppConfig.default()
    # a custom profile with a much cooler deep target should change the effective target
    custom = SetpointProfile(neutral_f=70, deep_bias_f=60, rem_warm_offset_f=1.5,
                             wake_ramp_f=74, composite_bed_weight=0.75, version=9)
    c = SleepController(cfg, setpoints=custom)
    t = c.thermal.target_for(ThermalIntent.DEEP_BIAS_COOL, NightObjective.OPTIMIZE,
                             hot_sleeper=False)
    assert t == 60.0  # uses the injected profile's deep_bias_f, not the config default (66)


def test_baselines_update_and_nightly_pipeline():
    cfg = AppConfig.default()
    repo = Repository(":memory:")
    updater = NightlyUpdater(cfg, repo)
    for i in range(4):
        result = updater.run(NightSummary(date=f"2026-06-1{i}", total_sleep_min=470,
                                          deep_min=100, wake_events=1, sleep_efficiency=0.88,
                                          avg_hrv=66))
    assert "recommendation" in result
    assert repo.latest_baselines() is not None


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
