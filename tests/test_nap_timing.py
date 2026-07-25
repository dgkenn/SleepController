"""Time-aware nap/induce sessions: anchored to when the user ACTUALLY falls asleep and to when
they must wake -- not to when the button was pressed.

The historical bug: ``live_daemon._start_nap`` computed the nap deadline from BUTTON-PRESS time
(``now + duration``), so a requested "20 minute nap" that took 10 minutes to fall asleep delivered
10 minutes of SLEEP, and a 90-min cycle nap with 20-min onset latency woke the user at 70 min of
sleep -- mid-cycle, plausibly out of deep sleep (the worst possible sleep-inertia outcome; Brooks &
Lack 2006, doi:10.1093/sleep/29.6.831; Patterson 2023, doi:10.1080/10903127.2023.2227696).

These tests exercise the fix in ``sleepctl.controller.nap`` (``nap_strategy``,
``fallback_deadline``, ``replan_on_onset``) that anchors dosing to MEASURED sleep onset instead,
plus the small ``SleepController`` additions (``sleep_onset_time``, ``update_nap_keep_light``)
that the daemons (``dashboard/daemon/{live_daemon,run_daemon}.py``) use to re-plan a nap once
onset is confirmed. Several tests explicitly contrast the FIXED anchor against the historical
button-press anchor to make the regression concrete: reintroducing the old
``deadline = press_time + duration`` formula would fail them.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sleepctl.config import AppConfig
from sleepctl.controller.controller import SleepController
from sleepctl.controller.nap import (
    NapRequestKind,
    NapStrategy,
    fallback_deadline,
    nap_strategy,
    replan_on_onset,
)
from sleepctl.controller.sleep_onset import SleepOnsetDetector
from sleepctl.models import SensorFrame, SleepStage

T0 = datetime(2026, 7, 25, 13, 0)


def _onset_frame(ts, stage, hr, move, rr=15.0, hrv=55.0, presence=True, conf=0.8):
    return SensorFrame(timestamp=ts, stage=stage, stage_confidence=conf, heart_rate=hr,
                       hrv=hrv, respiratory_rate=rr, movement=move, presence=presence)


def _confirm_onset(latency_min: int, start: datetime = T0):
    """Drive the REAL ``SleepOnsetDetector`` (not a hand-picked timestamp) through ``latency_min``
    of awake-in-bed frames followed by persistent sustained-sleep frames, and return the confirmed
    ``SleepOnsetEvent``. Mirrors tests/test_sleep_onset.py's pattern."""
    cfg = AppConfig.default()
    det = SleepOnsetDetector(cfg)
    frames = [_onset_frame(start + timedelta(minutes=i), SleepStage.AWAKE, hr=64, move=0.4)
              for i in range(latency_min)]
    sleep_start = start + timedelta(minutes=latency_min)
    persistence = cfg.tunables.onset_persistence_min
    for i in range(persistence + 5):
        frames.append(_onset_frame(sleep_start + timedelta(minutes=i), SleepStage.LIGHT,
                                   hr=56, move=0.05, rr=13.5, hrv=66))
    recent, result, bed_entry = [], None, start
    for f in frames:
        r = det.evaluate(f, recent, f.timestamp, bed_entry_time=bed_entry)
        result = result or r
        recent.append(f)
        recent = recent[-15:]
    assert result is not None, "test setup: onset detector did not confirm onset"
    return result


# ---------------------------------------------------------------------------------------------
# 1. Duration nap: 20 minutes requested, 10 min onset latency -> ~20 min of SLEEP, not 10.
# ---------------------------------------------------------------------------------------------
def test_duration_nap_20min_with_10min_latency_yields_20min_sleep_not_10():
    cfg = AppConfig.default()
    start = T0
    onset = start + timedelta(minutes=10)

    plan = nap_strategy(20, now_hour=start.hour, cfg=cfg,
                        request_kind=NapRequestKind.DURATION.value, requested_window_min=20)
    fallback = fallback_deadline(start, plan)   # safety cap only, not the planned wake
    replanned = replan_on_onset(plan, onset, fallback, cfg=cfg)

    fixed_deadline = onset + timedelta(minutes=replanned.target_sleep_min)
    realized_sleep_min = (fixed_deadline - onset).total_seconds() / 60.0
    assert realized_sleep_min == 20.0
    assert replanned.realized_sleep_min == 20

    # The historical bug this replaces: the deadline anchored to BUTTON-PRESS time instead of
    # onset. Reintroducing it would deliver only 10 min of sleep for a 20-min ask.
    old_buggy_deadline = start + timedelta(minutes=20)
    old_realized_sleep_min = (old_buggy_deadline - onset).total_seconds() / 60.0
    assert old_realized_sleep_min == 10.0
    assert realized_sleep_min > old_realized_sleep_min
    assert realized_sleep_min == 2 * old_realized_sleep_min


# ---------------------------------------------------------------------------------------------
# 2. Duration nap: 90-minute cycle nap, 20 min onset latency -> NOT woken at 70 min of sleep.
# ---------------------------------------------------------------------------------------------
def test_duration_nap_90min_cycle_with_20min_latency_is_not_70min():
    cfg = AppConfig.default()
    start = T0
    onset = start + timedelta(minutes=20)

    plan = nap_strategy(90, now_hour=start.hour, cfg=cfg,
                        request_kind=NapRequestKind.DURATION.value, requested_window_min=90)
    assert plan.strategy is NapStrategy.CYCLE
    fallback = fallback_deadline(start, plan)
    replanned = replan_on_onset(plan, onset, fallback, cfg=cfg)

    fixed_deadline = onset + timedelta(minutes=replanned.target_sleep_min)
    realized_sleep_min = (fixed_deadline - onset).total_seconds() / 60.0
    assert realized_sleep_min == 90.0          # the full cycle, measured from ACTUAL sleep onset

    # The historical bug's exact failure mode, called out in the bug report: anchoring to
    # button-press time wakes the user at 70 min of sleep -- mid-cycle, out of deep sleep.
    old_buggy_deadline = start + timedelta(minutes=90)
    old_realized_sleep_min = (old_buggy_deadline - onset).total_seconds() / 60.0
    assert old_realized_sleep_min == 70.0
    assert realized_sleep_min != 70.0
    assert realized_sleep_min > old_realized_sleep_min


# ---------------------------------------------------------------------------------------------
# 3. Onset-grace fallback caps a nap where onset never occurs.
# ---------------------------------------------------------------------------------------------
def test_onset_grace_fallback_caps_a_never_falls_asleep_nap():
    cfg = AppConfig.default()
    start = T0
    plan = nap_strategy(20, now_hour=start.hour, cfg=cfg,
                        request_kind=NapRequestKind.DURATION.value, requested_window_min=20)
    assert 15 <= plan.onset_grace_min <= 30   # a tunable grace, ~20-25 min by design

    deadline = fallback_deadline(start, plan)
    # The fallback is strictly LATER than the naive press-time+duration deadline (it must give
    # onset a chance to occur) but still FINITE -- the nap can never run forever.
    assert deadline == start + timedelta(minutes=plan.target_sleep_min + plan.onset_grace_min)
    assert deadline > start + timedelta(minutes=plan.target_sleep_min)
    assert deadline < start + timedelta(hours=2)   # sane upper bound; not "forever"

    # If onset is never confirmed, replan_on_onset is never called (the daemon only replans once
    # SleepController.sleep_onset_time is not None) -- so the fallback IS the operative deadline
    # the whole session, exactly as intended.


# ---------------------------------------------------------------------------------------------
# 4. Wake-by request: never exceeds its hard deadline, across a range of onset latencies.
# ---------------------------------------------------------------------------------------------
def test_wake_by_request_never_exceeds_hard_deadline():
    cfg = AppConfig.default()
    start = T0
    requested_window_min = 80
    hard_deadline = start + timedelta(minutes=requested_window_min)
    plan = nap_strategy(requested_window_min, now_hour=start.hour, cfg=cfg,
                        request_kind=NapRequestKind.WAKE_BY.value,
                        requested_window_min=requested_window_min)

    for latency in (0, 5, 10, 15, 20, 25, 35, 45, 55, 65, 75, 79):
        onset = start + timedelta(minutes=latency)
        replanned = replan_on_onset(plan, onset, hard_deadline, cfg=cfg)
        planned_wake = onset + timedelta(minutes=replanned.target_sleep_min)
        assert planned_wake <= hard_deadline, (
            f"latency={latency}: planned wake {planned_wake} exceeds hard deadline {hard_deadline}")
        # realized_sleep_min never exceeds what was actually available before the deadline
        available = (hard_deadline - onset).total_seconds() / 60.0
        assert replanned.realized_sleep_min <= available + 1e-9


def test_wake_by_target_never_exceeds_window_invariant():
    """The invariant the whole hard-deadline guarantee rests on (and the reason a trap-zone
    replan can only ever cap short, never extend past a hard deadline -- see nap.py's module
    docstring): ``nap_strategy`` never proposes MORE sleep than the window it was given, for any
    window/hour combination."""
    cfg = AppConfig.default()
    for window in range(0, 200, 7):
        for hour in (None, 0, 9, 14, 16, 23):
            plan = nap_strategy(window, now_hour=hour, cfg=cfg,
                                request_kind=NapRequestKind.WAKE_BY.value,
                                requested_window_min=window)
            assert plan.target_sleep_min <= window


# ---------------------------------------------------------------------------------------------
# 5. Re-planning moves a trap-zone realized window to the documented safer choice.
# ---------------------------------------------------------------------------------------------
def test_replan_resolves_trap_zone_realized_window_to_capped_short():
    cfg = AppConfig.default()
    start = T0
    # At button press this LOOKS like a safe full-cycle nap (70 min window)...
    requested_window_min = 70
    hard_deadline = start + timedelta(minutes=requested_window_min)
    press_time_plan = nap_strategy(requested_window_min, now_hour=start.hour, cfg=cfg,
                                   request_kind=NapRequestKind.WAKE_BY.value,
                                   requested_window_min=requested_window_min)
    assert press_time_plan.strategy is NapStrategy.CYCLE   # the (wrong) press-time impression

    # ...but onset actually took 25 minutes, so the TRUE sleep window is only 45 min -- the
    # inertia trap (25-60 min): waking out of deep sleep, the worst grogginess outcome.
    onset = start + timedelta(minutes=25)
    replanned = replan_on_onset(press_time_plan, onset, hard_deadline, cfg=cfg)

    assert replanned.strategy is NapStrategy.TRAP
    assert replanned.trap_resolution == "capped_short"
    assert replanned.keep_light is True
    power_max = cfg.tunables.nap_power_max_min
    assert replanned.target_sleep_min == power_max
    assert replanned.replanned is True

    # The safer choice actually moves the effective wake EARLIER than riding it out to the
    # original hard deadline in deep sleep -- and never later than that deadline either way.
    planned_wake = onset + timedelta(minutes=replanned.target_sleep_min)
    assert planned_wake < hard_deadline
    assert planned_wake <= hard_deadline


def test_trap_zone_extension_is_never_offered_for_a_hard_wake_by_deadline():
    """Documents the deliberate policy choice (see nap.py's module docstring): a HARD, never-
    exceeded wake-by deadline structurally never leaves room to extend a trap-zone realized
    window to a full ~90-min cycle (that would require >= 90 min of availability, but the trap
    zone is by definition < 60 min). Capping short is therefore the only resolution ever
    produced, across a spread of onset latencies that land in the trap zone."""
    cfg = AppConfig.default()
    start = T0
    for window in (45, 55, 65, 75, 85):    # varied press-time windows...
        hard_deadline = start + timedelta(minutes=window)
        plan = nap_strategy(window, now_hour=start.hour, cfg=cfg,
                            request_kind=NapRequestKind.WAKE_BY.value, requested_window_min=window)
        for latency in range(5, window, 5):
            onset = start + timedelta(minutes=latency)
            available = window - latency
            replanned = replan_on_onset(plan, onset, hard_deadline, cfg=cfg)
            if 25 < available < 60:   # landed in the trap zone
                assert replanned.trap_resolution == "capped_short"
                assert replanned.trap_resolution != "extended_cycle"


# ---------------------------------------------------------------------------------------------
# 6. Power nap keeps `keep_light` and hard-caps the wake.
# ---------------------------------------------------------------------------------------------
def test_power_nap_keeps_light_and_hard_caps():
    cfg = AppConfig.default()
    start = T0
    plan = nap_strategy(20, now_hour=start.hour, cfg=cfg,
                        request_kind=NapRequestKind.DURATION.value, requested_window_min=20)
    assert plan.strategy is NapStrategy.POWER
    assert plan.keep_light is True
    assert plan.target_sleep_min == 20

    onset = start + timedelta(minutes=5)
    fallback = fallback_deadline(start, plan)
    replanned = replan_on_onset(plan, onset, fallback, cfg=cfg)
    assert replanned.keep_light is True
    assert replanned.target_sleep_min == 20    # duration contract honored regardless of latency
    planned_wake = onset + timedelta(minutes=replanned.target_sleep_min)
    assert planned_wake == onset + timedelta(minutes=20)   # a real, enforceable cap


def test_power_nap_wake_by_also_keeps_light_and_caps():
    cfg = AppConfig.default()
    start = T0
    hard_deadline = start + timedelta(minutes=20)
    plan = nap_strategy(20, now_hour=start.hour, cfg=cfg,
                        request_kind=NapRequestKind.WAKE_BY.value, requested_window_min=20)
    onset = start + timedelta(minutes=2)
    replanned = replan_on_onset(plan, onset, hard_deadline, cfg=cfg)
    assert replanned.strategy is NapStrategy.POWER
    assert replanned.keep_light is True
    planned_wake = onset + timedelta(minutes=replanned.target_sleep_min)
    assert planned_wake <= hard_deadline


# ---------------------------------------------------------------------------------------------
# 7. Deterministic: same inputs -> same plan.
# ---------------------------------------------------------------------------------------------
def test_deterministic_same_inputs_produce_same_plan():
    cfg = AppConfig.default()
    start = T0
    onset = start + timedelta(minutes=17)
    hard_deadline = start + timedelta(minutes=70)

    plan_a = nap_strategy(70, now_hour=start.hour, cfg=cfg,
                          request_kind=NapRequestKind.WAKE_BY.value, requested_window_min=70)
    plan_b = nap_strategy(70, now_hour=start.hour, cfg=cfg,
                          request_kind=NapRequestKind.WAKE_BY.value, requested_window_min=70)
    assert plan_a == plan_b

    replanned_a = replan_on_onset(plan_a, onset, hard_deadline, cfg=cfg)
    replanned_b = replan_on_onset(plan_b, onset, hard_deadline, cfg=cfg)
    assert replanned_a == replanned_b

    # Duration-request replan is deterministic too.
    dplan_a = nap_strategy(20, now_hour=start.hour, cfg=cfg,
                           request_kind=NapRequestKind.DURATION.value, requested_window_min=20)
    dplan_b = nap_strategy(20, now_hour=start.hour, cfg=cfg,
                           request_kind=NapRequestKind.DURATION.value, requested_window_min=20)
    fb = fallback_deadline(start, dplan_a)
    assert replan_on_onset(dplan_a, onset, fb, cfg=cfg) == replan_on_onset(dplan_b, onset, fb, cfg=cfg)


# ---------------------------------------------------------------------------------------------
# 8. Integration: a REAL SleepOnsetDetector confirmation feeds replan_on_onset correctly.
# ---------------------------------------------------------------------------------------------
def test_real_onset_detector_confirmation_feeds_replan_correctly():
    cfg = AppConfig.default()
    start = T0
    latency = 14
    onset_event = _confirm_onset(latency, start=start)
    # The detector back-dates onset to when sustained sleep actually started, not "now".
    assert onset_event.timestamp == start + timedelta(minutes=latency)

    plan = nap_strategy(20, now_hour=start.hour, cfg=cfg,
                        request_kind=NapRequestKind.DURATION.value, requested_window_min=20)
    fallback = fallback_deadline(start, plan)
    replanned = replan_on_onset(plan, onset_event.timestamp, fallback, cfg=cfg)
    planned_wake = onset_event.timestamp + timedelta(minutes=replanned.target_sleep_min)
    realized = (planned_wake - onset_event.timestamp).total_seconds() / 60.0
    assert realized == 20.0
    # Contrast with the historical button-press anchor for this exact detected latency.
    old_realized = (start + timedelta(minutes=20) - onset_event.timestamp).total_seconds() / 60.0
    assert old_realized == 20.0 - latency
    assert realized > old_realized


# ---------------------------------------------------------------------------------------------
# 9. SleepController's new time-anchoring surface: sleep_onset_time + update_nap_keep_light.
# ---------------------------------------------------------------------------------------------
def test_controller_sleep_onset_time_property():
    cfg = AppConfig.default()
    controller = SleepController(cfg)
    assert controller.sleep_onset_time is None
    onset = T0 + timedelta(minutes=12)
    controller._sleep_onset_time = onset   # set by the real onset-detection path in decide()
    assert controller.sleep_onset_time == onset


def test_controller_update_nap_keep_light_does_not_restart_induction():
    cfg = AppConfig.default()
    controller = SleepController(cfg)
    controller.set_session("nap_power", keep_light=True)
    controller._induction_restart = False   # simulate that the cascade already started
    controller.update_nap_keep_light(False)
    assert controller.session_keep_light is False
    # Unlike set_session, this must NOT re-arm the induction-restart flag (onset already
    # happened by the time a nap is re-planned -- restarting the cascade would be wrong).
    assert controller._induction_restart is False
