"""End-to-end: a Verity-ONLY (stage-less) night must drive all four capabilities.

Covers, over a simulated night with NO Pod sleep stage and NO phone motion:
  1. sleep STAGING      -- a usable stage is derived every tick and varies with physiology
  2. sleep ONSET        -- onset is confirmed and the controller leaves INDUCTION
  3. AWAKENING          -- an arousal during maintenance is detected/graded
  4. TRAJECTORY         -- the ultradian cycle predictor accumulates history across the WHOLE
                           night (not only inside WAKE_WINDOW) and deep minutes accrue

Assertions are deliberately invariant-level (not tuned to synthetic stage dynamics) so they stay
meaningful as the learned stager is retrained.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sleepctl.config import AppConfig
from sleepctl.controller.controller import SleepController
from sleepctl.models import ContextRecord, ControllerState, SensorFrame, SleepStage


def _frame(now, hr, move=0.03, presence=True):
    return SensorFrame(timestamp=now, stage=SleepStage.UNKNOWN, presence=presence,
                       heart_rate=hr, hrv=60.0, respiratory_rate=14.0, movement=move,
                       bed_temp_f=72.0, room_temp_f=68.0, data_age_seconds=20)


def _night(c, ctx, start, minutes, hr_fn, recent=None):
    """Drive ``minutes`` ticks of a stage-less feed; returns (recent, decisions)."""
    recent = recent if recent is not None else []
    decisions = []
    for i in range(minutes):
        now = start + timedelta(minutes=i)
        f = _frame(now, hr_fn(i))
        decisions.append(c.decide(f, ctx, recent, now))
        recent.append(f)
        if len(recent) > 60:
            recent.pop(0)
    return recent, decisions


# A HR pattern with a real ultradian-style swing, so stages actually alternate.
def _swing(i):
    return 48.0 if (i // 20) % 2 == 0 else 64.0


def test_verity_only_night_drives_staging_onset_awakening_and_trajectory():
    cfg = AppConfig.default()
    c = SleepController(cfg)
    ctx = ContextRecord(date="2026-06-23")
    start = datetime(2026, 6, 23, 23, 0)

    recent, decisions = _night(c, ctx, start, 160, _swing)

    # 1. STAGING — every tick produced a usable, DERIVED stage (never a Pod label)
    sources = {d.log_payload["stage_source"] for d in decisions}
    assert sources <= {"model", "heuristic"}, f"unexpected stage sources: {sources}"
    stages = {d.log_payload["stage"] for d in decisions} - {None}
    assert stages, "no stage was ever derived from the wearable"
    assert len(stages) > 1, f"stage never varied across the night: {stages}"

    # 2. ONSET — confirmed from HR alone, and the controller advanced past INDUCTION
    assert c._sleep_onset_time is not None, "sleep onset was never confirmed from the wearable"
    assert c.sm.state is not ControllerState.INDUCTION, f"stuck in {c.sm.state}"

    # 4a. TRAJECTORY — the cycle predictor was fed OUTSIDE the wake window. Before the fix,
    # observe() ran only inside wake_orch.evaluate() (WAKE_WINDOW only), so a night spent in
    # MAINTENANCE left it with zero history, a generic bout estimate and pinned-low confidence.
    pred = c.wake_orch.predictor
    assert len(pred._transitions) >= 1, (
        "cycle predictor was never fed outside WAKE_WINDOW — trajectory is uninformed")
    cyc = decisions[-1].log_payload.get("cycle")
    assert cyc, "no ultradian trajectory in the decision payload"
    assert 0.0 <= cyc["confidence"] <= 1.0
    assert cyc["minutes_to_next_light"] >= 0.0
    assert "typical_deep_bout_min" in cyc

    # 4b. ARCHITECTURE — realized deep minutes accrued (this is what in-night steering compares
    # against the ideal curve; with stage=UNKNOWN it would stay 0 and fake a full deficit).
    assert c._arch_deep_min > 0.0, "no deep minutes accrued from wearable-derived stages"

    # 3. AWAKENING — a still-but-elevated-HR arousal in maintenance is detected and graded
    now = start + timedelta(minutes=165)
    c.decide(_frame(now, 78.0, move=0.06), ctx, recent, now)
    assert c.last_arousal is not None, "arousal detector never ran on the wearable feed"


def test_cycle_predictor_learns_bouts_when_fed_all_night():
    """The new observe_stage passthrough must actually close out deep bouts so the predictor
    personalizes to THIS night instead of the generic default. Driven with explicit stages so it
    is deterministic and independent of the learned stager."""
    from sleepctl.controller.sleep_cycle import _DEFAULT_DEEP_BOUT_MIN

    cfg = AppConfig.default()
    c = SleepController(cfg)
    t0 = datetime(2026, 6, 23, 23, 0)

    # deep for 30 min, then light -> one closed bout of 30 min (vs the 22 min default)
    for i in range(30):
        c.wake_orch.observe_stage(t0 + timedelta(minutes=i), SleepStage.DEEP)
    c.wake_orch.observe_stage(t0 + timedelta(minutes=30), SleepStage.LIGHT)

    pred = c.wake_orch.predictor
    assert pred._deep_bouts, "no deep bout closed out — trajectory cannot personalize"
    assert abs(pred._typical_deep_bout() - 30.0) < 1e-6
    assert pred._typical_deep_bout() != _DEFAULT_DEEP_BOUT_MIN

    state = c.wake_orch.cycle_state(t0 + timedelta(minutes=31), SleepStage.LIGHT)
    assert state["in_light"] is True and state["minutes_to_next_light"] == 0.0


def test_observe_stage_is_idempotent_for_a_repeated_stage():
    """Called every tick, it must only record on a stage CHANGE (no unbounded growth)."""
    cfg = AppConfig.default()
    c = SleepController(cfg)
    t0 = datetime(2026, 6, 23, 23, 0)
    for i in range(50):
        c.wake_orch.observe_stage(t0 + timedelta(minutes=i), SleepStage.LIGHT)
    assert len(c.wake_orch.predictor._transitions) == 1


def test_trajectory_reaches_wake_window_with_real_history_then_resets():
    """The payoff: the wake window is entered with a POPULATED cycle history (so the alarm can be
    anticipatory rather than reactive), and ending the night clears it so night N+1 starts fresh.

    Note the night only ends -- IDLE -- once out of bed PAST the required wake time; a 3am
    bathroom trip deliberately stays in WAKE_RECOVERY and keeps the night's history.
    """
    cfg = AppConfig.default()
    c = SleepController(cfg)
    start = datetime(2026, 6, 23, 23, 0)
    ctx = ContextRecord(date="2026-06-23",
                        required_wake_time=start + timedelta(minutes=100))

    recent, _ = _night(c, ctx, start, 120, _swing)
    assert c.sm.state is ControllerState.WAKE_WINDOW, f"expected wake window, got {c.sm.state}"
    assert c.wake_orch.predictor._transitions, (
        "entered the wake window with NO cycle history — the alarm would be reactive, not anticipatory")

    out = start + timedelta(minutes=121)
    c.decide(_frame(out, None, move=None, presence=False), ctx, recent, out)
    assert c.sm.state is ControllerState.IDLE
    assert not c.wake_orch.predictor._transitions, "cycle history leaked across nights"
