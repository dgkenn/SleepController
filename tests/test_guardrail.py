"""Decision guardrail: the trajectory-level ``DecisionGuardrail`` invariant monitor, plus its
wiring into ``SleepController.decide`` (a CRITICAL finding forces a safe hold toward neutral;
a quiet/in-bounds trajectory produces no finding and leaves normal control untouched)."""

from dataclasses import dataclass
from datetime import datetime, timedelta

from sleepctl.config import AppConfig
from sleepctl.controller.controller import SleepController
from sleepctl.controller.guardrail import DecisionGuardrail
from sleepctl.models import (
    ContextRecord,
    ControllerState,
    CorrectionAction,
    Decision,
    NightObjective,
    SensorFrame,
    SleepStage,
    ThermalIntent,
)


def _frame(now, **overrides):
    defaults = dict(
        timestamp=now, stage=SleepStage.LIGHT, stage_confidence=0.9,
        heart_rate=58.0, hrv=65.0, respiratory_rate=14.0, movement=0.05,
        presence=True, bed_temp_f=68.0, room_temp_f=68.0, data_age_seconds=5.0,
    )
    defaults.update(overrides)
    return SensorFrame(**defaults)


def _decision(now, target_f, action=CorrectionAction.COOLER):
    return Decision(
        timestamp=now, state=ControllerState.MAINTENANCE, objective=NightObjective.OPTIMIZE,
        thermal_intent=ThermalIntent.SETTLE_COOL, target_temp_f=target_f, target_level=0,
        action=action, reason="test", confidence=0.9,
    )


@dataclass
class _FakeThermalHealth:
    state: str
    reason: str = "test"


# --------------------------------------------------------------------------- pure unit checks
def test_quiet_trajectory_produces_no_findings():
    """Good-data / in-bounds trajectory: the guardrail must be silent (do-no-harm floor)."""
    cfg = AppConfig.default()
    guardrail = DecisionGuardrail(cfg)
    now = datetime(2026, 6, 24, 2, 0)
    frames = [_frame(now - timedelta(minutes=m), heart_rate=58.0) for m in range(20, 0, -1)]
    decisions = [_decision(now - timedelta(minutes=m), 70.0, CorrectionAction.HOLD)
                for m in range(20, 0, -1)]
    result = guardrail.evaluate(frames, decisions, current_target_f=70.0, now=now,
                                sleep_hr_baseline=58.0)
    assert result.triggered is False
    assert result.critical is False
    assert result.findings == []


def test_driving_arousal_flagged_on_sustained_cooling_with_hr_rise():
    cfg = AppConfig.default()
    guardrail = DecisionGuardrail(cfg)
    now = datetime(2026, 6, 24, 2, 0)
    baseline = 58.0
    # Sustained COOLER run + HR held well above baseline (over the guardrail's own bar, which
    # sits above the routine ArousalDetector's threshold so this is a genuinely strong signal).
    frames = [_frame(now - timedelta(minutes=m), heart_rate=baseline + 12.0)
             for m in range(6, 0, -1)]
    decisions = [_decision(now - timedelta(minutes=m), 66.0 - m * 0.1, CorrectionAction.COOLER)
                for m in range(6, 0, -1)]
    result = guardrail.evaluate(frames, decisions, current_target_f=64.0, now=now,
                                sleep_hr_baseline=baseline)
    assert result.critical is True
    codes = {f.code for f in result.findings}
    assert "driving_arousal" in codes


def test_driving_arousal_not_flagged_without_sustained_cooling():
    """A single cooling tick with a high HR reading is not "sustained" -- must not trigger."""
    cfg = AppConfig.default()
    guardrail = DecisionGuardrail(cfg)
    now = datetime(2026, 6, 24, 2, 0)
    baseline = 58.0
    frames = [_frame(now - timedelta(minutes=m), heart_rate=baseline + 12.0)
             for m in range(6, 0, -1)]
    decisions = [_decision(now - timedelta(minutes=m), 66.0, CorrectionAction.HOLD)
                for m in range(5, 0, -1)] + [_decision(now, 65.5, CorrectionAction.COOLER)]
    result = guardrail.evaluate(frames, decisions, current_target_f=65.5, now=now,
                                sleep_hr_baseline=baseline)
    assert "driving_arousal" not in {f.code for f in result.findings}


def test_outside_comfort_band_flagged():
    cfg = AppConfig.default()
    guardrail = DecisionGuardrail(cfg)
    now = datetime(2026, 6, 24, 2, 0)
    comfort = {"neutral_f": 70.0, "cool_edge_f": 65.0, "warm_edge_f": 75.0}
    result = guardrail.evaluate([], [], current_target_f=55.0, now=now,
                                comfort_profile=comfort)
    codes = {f.code for f in result.findings}
    assert "outside_comfort_band" in codes
    # Non-critical: a comfort-band excursion is surfaced but doesn't itself force the hold.
    assert result.critical is False


def test_inside_comfort_band_not_flagged():
    cfg = AppConfig.default()
    guardrail = DecisionGuardrail(cfg)
    now = datetime(2026, 6, 24, 2, 0)
    comfort = {"neutral_f": 70.0, "cool_edge_f": 65.0, "warm_edge_f": 75.0}
    result = guardrail.evaluate([], [], current_target_f=70.0, now=now,
                                comfort_profile=comfort)
    assert "outside_comfort_band" not in {f.code for f in result.findings}


def test_no_comfort_profile_skips_that_check():
    cfg = AppConfig.default()
    guardrail = DecisionGuardrail(cfg)
    now = datetime(2026, 6, 24, 2, 0)
    result = guardrail.evaluate([], [], current_target_f=55.0, now=now, comfort_profile=None)
    assert "outside_comfort_band" not in {f.code for f in result.findings}


def test_thermal_oscillation_flagged_on_rapid_reversals():
    cfg = AppConfig.default()
    guardrail = DecisionGuardrail(cfg)
    now = datetime(2026, 6, 24, 2, 0)
    # Alternate targets far enough apart, often enough, within the oscillation window.
    targets = [70.0, 66.0, 70.0, 66.0, 70.0, 66.0, 70.0]
    decisions = [_decision(now - timedelta(minutes=(len(targets) - i) * 3), t)
                for i, t in enumerate(targets)]
    result = guardrail.evaluate([], decisions, current_target_f=targets[-1], now=now)
    assert result.critical is True
    assert "thermal_oscillation" in {f.code for f in result.findings}


def test_steady_ramp_not_flagged_as_oscillation():
    """A monotonic (or normal small-step) trajectory must never be mistaken for hunting."""
    cfg = AppConfig.default()
    guardrail = DecisionGuardrail(cfg)
    now = datetime(2026, 6, 24, 2, 0)
    targets = [70.0, 69.0, 68.0, 67.0, 66.0, 65.0]
    decisions = [_decision(now - timedelta(minutes=(len(targets) - i) * 3), t)
                for i, t in enumerate(targets)]
    result = guardrail.evaluate([], decisions, current_target_f=targets[-1], now=now)
    assert "thermal_oscillation" not in {f.code for f in result.findings}


def test_device_divergence_flagged_when_thermal_health_stalled():
    cfg = AppConfig.default()
    guardrail = DecisionGuardrail(cfg)
    now = datetime(2026, 6, 24, 2, 0)
    stalled = _FakeThermalHealth(state="stalled", reason="commanded to cool but flat")
    result = guardrail.evaluate([], [], current_target_f=65.0, now=now,
                                thermal_health=stalled)
    assert "device_divergence" in {f.code for f in result.findings}
    # A stall is a warning (device-health concern), not itself grounds for a critical override.
    assert all(f.severity != "critical" for f in result.findings if f.code == "device_divergence")


def test_device_divergence_not_flagged_when_healthy_or_absent():
    cfg = AppConfig.default()
    guardrail = DecisionGuardrail(cfg)
    now = datetime(2026, 6, 24, 2, 0)
    ok = _FakeThermalHealth(state="ok")
    result = guardrail.evaluate([], [], current_target_f=65.0, now=now, thermal_health=ok)
    assert "device_divergence" not in {f.code for f in result.findings}
    result2 = guardrail.evaluate([], [], current_target_f=65.0, now=now, thermal_health=None)
    assert "device_divergence" not in {f.code for f in result2.findings}


# ---------------------------------------------------------------- wiring into SleepController
def _good_frame(now, **overrides):
    defaults = dict(
        timestamp=now, stage=SleepStage.LIGHT, stage_confidence=0.9,
        heart_rate=58.0, hrv=65.0, respiratory_rate=14.0, movement=0.05,
        presence=True, bed_temp_f=70.0, room_temp_f=68.0, data_age_seconds=5.0,
    )
    defaults.update(overrides)
    return SensorFrame(**defaults)


def _advance_to_maintenance(controller, now):
    recent = []
    ctx = ContextRecord(date=now.date().isoformat())
    for i in range(40):
        frame = _good_frame(now, stage=SleepStage.LIGHT if i > 15 else SleepStage.AWAKE)
        controller.decide(frame, ctx, recent, now)
        recent.append(frame)
        now += timedelta(minutes=1)
    return now, recent, ctx


def test_good_night_never_triggers_guardrail_through_the_controller():
    """End-to-end do-no-harm floor: a calm, in-bounds trajectory through the real controller
    must never trip the guardrail (normal control on good nights is untouched)."""
    cfg = AppConfig.default()
    controller = SleepController(cfg)
    now = datetime(2026, 6, 24, 0, 0)
    now, recent, ctx = _advance_to_maintenance(controller, now)
    for _ in range(30):
        frame = _good_frame(now)
        decision = controller.decide(frame, ctx, recent, now)
        assert decision.log_payload["guardrail"]["critical"] is False
        recent.append(frame)
        now += timedelta(minutes=1)
    summary = controller.guardrail_summary()
    assert summary["critical"] is False


def test_critical_guardrail_forces_safe_hold_through_the_controller():
    """Drive HR up while the controller is actively cooling (settle-cool during maintenance),
    long enough for the driving-arousal guardrail to trip, and confirm it forces a HOLD toward
    neutral rather than letting the aggressive cooling continue."""
    cfg = AppConfig.default()
    controller = SleepController(cfg)
    now = datetime(2026, 6, 24, 0, 0)
    now, recent, ctx = _advance_to_maintenance(controller, now)

    forced_hold = False
    for i in range(30):
        # Rising HR well above the settled sleep baseline while movement stays low (so the
        # frame itself is trustworthy -- this must be the GUARDRAIL catching it, not the
        # data-quality gate).
        frame = _good_frame(now, heart_rate=58.0 + min(25.0, i * 2.0))
        decision = controller.decide(frame, ctx, recent, now)
        recent.append(frame)
        now += timedelta(minutes=1)
        if decision.log_payload["guardrail"]["critical"]:
            forced_hold = True
            assert decision.action is CorrectionAction.HOLD
            assert "guardrail critical" in decision.reason
            break

    assert forced_hold, "expected the driving-arousal guardrail to eventually trip"


def test_guardrail_override_respects_slew_limit_on_the_next_tick():
    """Regression guard: after a guardrail override reverts the target, ThermalController's
    internal bookkeeping must stay in sync (see ThermalController.note_override) so the VERY
    NEXT tick's resolve() doesn't fight stale pre-override history and blow the slew limit."""
    cfg = AppConfig.default()
    controller = SleepController(cfg)
    now = datetime(2026, 6, 24, 0, 0)
    now, recent, ctx = _advance_to_maintenance(controller, now)

    prev_target = None
    max_step = cfg.tunables.max_step_f
    for i in range(60):
        frame = _good_frame(now, heart_rate=58.0 + min(25.0, i * 2.0))
        decision = controller.decide(frame, ctx, recent, now)
        recent.append(frame)
        now += timedelta(minutes=1)
        if prev_target is not None:
            assert abs(decision.target_temp_f - prev_target) <= max_step + 0.01
        prev_target = decision.target_temp_f
