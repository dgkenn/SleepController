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
    cfg, actuator, _, _ = _run_scenario("normal")
    levels = actuator.commands
    max_f_per_level = 0.2  # default calibration
    max_levels = cfg.tunables.max_step_f / max_f_per_level
    for a, b in zip(levels, levels[1:]):
        assert abs(b - a) <= max_levels + 1e-6


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
