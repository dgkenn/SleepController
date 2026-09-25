"""Session-lifecycle regressions from the 2026-09-25 controller audit.

Each test replays the audit's reproduction of one failure through the real ``decide`` path:
an armed alarm lost to the abandon rule, wake confirmation and the bed-exit baseline leaking
into the next night, a power nap woken before onset, a restored onset wiped on the first tick,
a bed-entry clock stamped at breakfast, a warm-forecast bias cooling the wake window under the
floor, a chosen wake window only the orchestrator heard about, a warm rescue eaten by the
ambient bias, and the DST fall-back resetting the night at 02:00.
"""
from datetime import datetime, timedelta

from sleepctl.config import AppConfig
from sleepctl.controller.architecture import ArchitectureSteering
from sleepctl.controller.controller import SleepController
from sleepctl.controller.nap import fallback_deadline, nap_strategy, replan_on_onset
from sleepctl.controller.smart_wake import SmartWakeRoutine
from sleepctl.controller.state_machine import SleepStateMachine
from sleepctl.models import ContextRecord, ControllerState, SensorFrame, SleepStage, ThermalIntent


def _fr(ts, stage=SleepStage.LIGHT, hr=60, mv=0.0, presence=None, off=None, age=10):
    return SensorFrame(timestamp=ts, stage=stage, stage_confidence=0.9, heart_rate=hr, hrv=50,
                       respiratory_rate=14, movement=mv, presence=presence,
                       data_age_seconds=age, wearable_off_arm=off)


# -- 1. the abandon rule must not throw away an armed alarm -----------------------------------
def test_a_dropped_wearable_does_not_abandon_a_session_with_an_armed_alarm():
    """2026-08-06 by another route: band drops at 00:01, 08:30 wake armed. The abandon rule
    used to go IDLE at 01:00, and IDLE has no path to WAKE_WINDOW."""
    c = SleepController(AppConfig.default())
    wake = datetime(2026, 8, 6, 8, 30)
    ctx = ContextRecord(date="2026-08-05", required_wake_time=wake)
    c._bed_entry_time = datetime(2026, 8, 5, 23, 0)
    c._sleep_onset_time = datetime(2026, 8, 5, 23, 20)
    c.sm.state = ControllerState.MAINTENANCE
    t, recent, states, woke = datetime(2026, 8, 6, 0, 0), [], [], False
    while t <= wake + timedelta(minutes=70):
        hr = 56 if t < datetime(2026, 8, 6, 0, 1) else None
        f = SensorFrame(timestamp=t, stage=SleepStage.UNKNOWN, heart_rate=hr, presence=None,
                        data_age_seconds=10)
        d = c.decide(f, ctx, recent[-30:], t)
        recent.append(f)
        states.append((t, d.state))
        woke |= bool(d.log_payload["should_wake"])
        t += timedelta(minutes=1)
    before_window = [s for ts, s in states if ts < wake - timedelta(minutes=30)]
    assert ControllerState.IDLE not in before_window
    assert woke, "the armed alarm never fired"
    # ...and once the window has closed the dead session still ends.
    assert states[-1][1] is ControllerState.IDLE


def test_the_abandon_rule_still_fires_with_no_alarm_armed():
    c = SleepController(AppConfig.default())
    c.sm.state = ControllerState.MAINTENANCE
    now = datetime(2026, 8, 6, 3, 0)
    c._last_physio_at = now - timedelta(minutes=61)
    f = SensorFrame(timestamp=now, stage=SleepStage.UNKNOWN, heart_rate=None, presence=None,
                    data_age_seconds=10)
    c.decide(f, ContextRecord(date="2026-08-05"), [], now)
    assert c.sm.state is ControllerState.IDLE


# -- 2. every way a session ends performs the full reset --------------------------------------
def test_a_bed_exit_does_not_carry_wake_confirmation_into_the_next_night():
    c = SleepController(AppConfig.default())
    wake1 = datetime(2026, 9, 25, 7, 0)
    ctx = ContextRecord(date="2026-09-24", required_wake_time=wake1)
    c.sm.state = ControllerState.MAINTENANCE
    c._sleep_onset_time = wake1 - timedelta(hours=7)
    recent, t = [], wake1 - timedelta(minutes=10)
    for _ in range(6):   # awake + moving in the window -> confirmed up
        f = _fr(t, SleepStage.AWAKE, hr=75, mv=0.6)
        c.decide(f, ctx, recent, t)
        recent.append(f)
        t += timedelta(seconds=30)
    assert c.wake_orch._confirmed
    d = c.decide(_fr(t, SleepStage.UNKNOWN, hr=None, off=True), ctx, recent, t)  # on charger
    assert d.state is ControllerState.IDLE
    assert c.wake_orch._confirmed is False
    # next night: the window opens on a sleeper, and the alarm has NOT already "stood down"
    wake2 = wake1 + timedelta(days=1)
    ctx2 = ContextRecord(date="2026-09-25", required_wake_time=wake2)
    c.sm.state = ControllerState.MAINTENANCE
    c._sleep_onset_time = wake2 - timedelta(hours=7)
    t, recent = wake2 - timedelta(minutes=30), []
    f = _fr(t, SleepStage.DEEP, hr=52)
    d = c.decide(f, ctx2, recent, t)
    wa = d.log_payload["wake_action"]
    assert d.state is ControllerState.WAKE_WINDOW
    assert wa["phase"] != "done" and wa["should_wake"] is False


def test_the_abandon_path_resets_the_wake_orchestrator_too():
    c = SleepController(AppConfig.default())
    c.sm.state = ControllerState.MAINTENANCE
    c.wake_orch._confirmed = True
    c.bed_exit_detector.observe_sleeping(52.0)
    now = datetime(2026, 9, 25, 14, 0)
    c._last_physio_at = now - timedelta(minutes=90)
    f = SensorFrame(timestamp=now, stage=SleepStage.UNKNOWN, heart_rate=None, presence=None,
                    data_age_seconds=10)
    c.decide(f, ContextRecord(date="2026-09-25"), [], now)
    assert c.sm.state is ControllerState.IDLE
    assert c.wake_orch._confirmed is False
    assert c.bed_exit_detector.lying_baseline is None


def test_the_ordinary_idle_transition_resets_the_bed_exit_baseline():
    """Night 1's 52 bpm lying baseline judged night 2's settling-in (72 bpm) as standing up."""
    c = SleepController(AppConfig.default())
    wake = datetime(2026, 9, 26, 7, 0)
    ctx = ContextRecord(date="2026-09-25", required_wake_time=wake)
    c._bed_entry_time = wake - timedelta(hours=8)
    c.sm.state = ControllerState.MAINTENANCE
    c._sleep_onset_time = wake - timedelta(hours=7)
    t, recent = wake - timedelta(minutes=60), []
    for _ in range(25):
        f = _fr(t, SleepStage.LIGHT, 52, 0.0)
        c.decide(f, ctx, recent[-30:], t)
        recent.append(f)
        t += timedelta(minutes=1)
    assert c.bed_exit_detector.lying_baseline is not None
    while t <= wake + timedelta(minutes=61):   # the window closes -> IDLE the ordinary way
        f = _fr(t, SleepStage.LIGHT, 55, 0.0)
        d = c.decide(f, ctx, recent[-30:], t)
        recent.append(f)
        t += timedelta(minutes=1)
    assert d.state is ControllerState.IDLE
    assert c.bed_exit_detector.lying_baseline is None
    # night 2 settles in exactly as a fresh controller would
    t, recent = datetime(2026, 9, 26, 22, 30), []
    c.set_session("induce")
    for i in range(20):
        f = _fr(t, SleepStage.LIGHT, 72 + (i % 3), 0.3 if i % 4 else 0.1)
        d = c.decide(f, ContextRecord(date="2026-09-26"), recent[-30:], t)
        recent.append(f)
        t += timedelta(minutes=1)
    assert d.state is not ControllerState.IDLE
    assert c.bed_exit_events == []


# -- 3. a power nap is not woken before onset -------------------------------------------------
def test_a_power_nap_wake_window_is_five_minutes_not_thirty():
    cfg = AppConfig.default()
    c = SleepController(cfg)
    start = datetime(2026, 9, 26, 14, 0)
    plan = nap_strategy(20, now_hour=14, cfg=cfg)
    deadline = fallback_deadline(start, plan)
    ctx = ContextRecord(date="2026-09-26", required_wake_time=deadline)
    c.set_session("nap_power", keep_light=plan.keep_light)
    t, recent, first_window = start, [], None
    for i in range(40):
        stage = SleepStage.AWAKE if i < 8 else SleepStage.LIGHT
        f = SensorFrame(timestamp=t, stage=stage, stage_confidence=0.8,
                        heart_rate=68 - min(i, 10) * 0.6, hrv=45, respiratory_rate=14,
                        movement=0.02, presence=True, data_age_seconds=10)
        d = c.decide(f, ctx, recent[-30:], t)
        recent.append(f)
        if c.sleep_onset_time and ctx.required_wake_time == deadline:   # daemon's replan
            np_ = replan_on_onset(plan, c.sleep_onset_time, deadline, cfg)
            ctx.required_wake_time = c.sleep_onset_time + timedelta(minutes=np_.target_sleep_min)
        if d.state is ControllerState.WAKE_WINDOW and first_window is None:
            first_window = t
        if d.state is ControllerState.INDUCTION:
            assert d.log_payload["should_wake"] is False
        t += timedelta(minutes=1)
    assert c.sleep_onset_time is not None
    assert first_window is not None
    assert first_window > c.sleep_onset_time
    assert first_window >= ctx.required_wake_time - timedelta(minutes=5)
    assert c.effective_wake_window_min() == 5.0


def test_the_state_machine_honours_the_window_it_is_given():
    cfg = AppConfig.default()
    sm = SleepStateMachine(cfg, ControllerState.INDUCTION)
    deadline = datetime(2026, 9, 26, 14, 42)
    f = _fr(deadline - timedelta(minutes=30), SleepStage.AWAKE)
    assert sm.transition(f, f.timestamp, False, deadline, onset_confirmed=False,
                         wake_window_min=5) is ControllerState.INDUCTION
    f = _fr(deadline - timedelta(minutes=5), SleepStage.AWAKE)
    assert sm.transition(f, f.timestamp, False, deadline, onset_confirmed=False,
                         wake_window_min=5) is ControllerState.WAKE_WINDOW


# -- 4. a restored onset survives the first tick ----------------------------------------------
def test_a_restored_session_keeps_its_onset_on_the_first_tick():
    t0 = datetime(2026, 9, 24, 23, 0)
    c = SleepController(AppConfig.default())
    c.set_session("induce", keep_light=False)
    c.restore_bed_entry(t0)
    onset = t0 + timedelta(minutes=10)
    c.restore_session_state("maintenance", onset,
                            {"deep_min": 30.0, "rem_min": 12.0, "light_min": 40.0})
    assert c._bed_entry_time == t0
    now, recent = t0 + timedelta(minutes=180), []
    for _ in range(4):
        f = _fr(now, SleepStage.DEEP, hr=52)
        d = c.decide(f, ContextRecord(date="2026-09-24"), recent, now)
        recent.append(f)
        now += timedelta(seconds=30)
    assert d.state is ControllerState.MAINTENANCE
    assert c.sleep_onset_time == onset
    assert c._bed_entry_time == t0
    assert c._arch_deep_min > 30.0


def test_a_restored_session_without_a_recovered_entry_anchors_on_onset():
    c = SleepController(AppConfig.default())
    onset = datetime(2026, 9, 24, 23, 10)
    c.restore_session_state("maintenance", onset, None)
    assert c._bed_entry_time == onset


# -- 5. the bed-entry clock starts when the session does --------------------------------------
def test_a_daytime_idle_stamp_is_replaced_when_the_session_starts():
    import sleepctl.controller.controller as C
    seen = []
    orig = C.estimate_sleep_stage

    def spy(frame, *a, **k):
        seen.append(k.get("minutes_since_start"))
        return orig(frame, *a, **k)

    C.estimate_sleep_stage = spy
    try:
        c = C.SleepController(AppConfig.default())
        ctx = ContextRecord(date="2026-09-25", required_wake_time=datetime(2026, 9, 26, 7, 0))
        t = datetime(2026, 9, 25, 9, 0)
        f = SensorFrame(timestamp=t, heart_rate=85, hrv=40, respiratory_rate=15, movement=0.1,
                        presence=None, data_age_seconds=10)
        c.decide(f, ctx, [], t)
        recent = [f]
        t = datetime(2026, 9, 25, 22, 50)
        for i in range(12):
            f = SensorFrame(timestamp=t, heart_rate=62 + (i % 3) - 1, hrv=40,
                            respiratory_rate=15, movement=0.0, presence=None,
                            data_age_seconds=10)
            d = c.decide(f, ctx, recent[-20:], t)
            recent.append(f)
            t += timedelta(seconds=30)
    finally:
        C.estimate_sleep_stage = orig
    assert d.state is ControllerState.INDUCTION
    assert c._bed_entry_time >= datetime(2026, 9, 25, 22, 50)
    assert seen and all(m is not None and m < 10 for m in seen), seen


def test_a_recovered_entry_is_kept_across_the_session_start_edge():
    """A mid-night restart that comes up IDLE: the wearable re-opens the session, and the
    recovered 23:00 entry -- not the re-entry tick -- stays the anchor."""
    c = SleepController(AppConfig.default())
    entry = datetime(2026, 9, 25, 23, 0)
    c.restore_bed_entry(entry)
    t, recent = datetime(2026, 9, 26, 1, 0), []
    for i in range(12):
        f = SensorFrame(timestamp=t, heart_rate=58 + (i % 3), hrv=40, respiratory_rate=15,
                        movement=0.0, presence=None, data_age_seconds=10)
        d = c.decide(f, ContextRecord(date="2026-09-25"), recent[-20:], t)
        recent.append(f)
        t += timedelta(seconds=30)
    assert d.state is not ControllerState.IDLE
    assert c._bed_entry_time == entry


# -- 6. the wake window respects the floors -----------------------------------------------------
def test_a_warm_forecast_does_not_cool_the_wake_window_under_the_floor():
    cfg = AppConfig.default()
    c = SleepController(cfg)
    c.thermal.set_measured_neutral(70.0)
    c.thermal.set_ambient_bias(-2.0)
    wake = datetime(2026, 9, 26, 7, 0)
    ctx = ContextRecord(date="2026-09-25", required_wake_time=wake)
    c._bed_entry_time = wake - timedelta(hours=8)
    c.sm.state = ControllerState.MAINTENANCE
    c._sleep_onset_time = wake - timedelta(hours=7, minutes=40)
    t, recent, in_window = wake - timedelta(minutes=40), [], []
    for i in range(40):
        stage = SleepStage.REM if i < 30 else SleepStage.DEEP
        f = _fr(t, stage, hr=56, mv=0.0)
        d = c.decide(f, ctx, recent[-30:], t)
        recent.append(f)
        if d.state is ControllerState.WAKE_WINDOW:
            in_window.append(d.target_temp_f)
        t += timedelta(minutes=1)
    assert in_window
    assert min(in_window) >= cfg.tunables.maintenance_floor_f


def test_the_post_wake_cold_snap_is_exempt_from_the_floor():
    c = SleepController(AppConfig.default())
    t, _ = c._apply_session_bounds(ControllerState.WAKE_WINDOW, 65.0, 0,
                                   intent=ThermalIntent.WAKE_COLD_SNAP)
    assert t == 65.0
    c.note_user_override(74.0, warmer=False)   # ceiling 73 F: never blunts the wake ramp
    t, _ = c._apply_session_bounds(ControllerState.WAKE_WINDOW, 76.0, 0,
                                   intent=ThermalIntent.WAKE_RAMP)
    assert t == 76.0


# -- 7. the chosen wake window reaches every consumer -----------------------------------------
def test_a_chosen_wake_window_opens_the_state_machine_window():
    for win, opened in ((45, True), (15, False)):
        c = SleepController(AppConfig.default())
        c.set_wake_window(win)
        wake = datetime(2026, 9, 26, 7, 0)
        ctx = ContextRecord(date="2026-09-25", required_wake_time=wake)
        c._bed_entry_time = wake - timedelta(hours=8)
        c.sm.state = ControllerState.MAINTENANCE
        c._sleep_onset_time = wake - timedelta(hours=7, minutes=40)
        t = wake - timedelta(minutes=40)
        d = c.decide(_fr(t, SleepStage.LIGHT, mv=0.05), ctx, [], t)
        assert (d.state is ControllerState.WAKE_WINDOW) is opened, (win, d.state)
    c = SleepController(AppConfig.default())
    c.set_wake_window(45)
    assert c.effective_wake_window_min() == 45.0
    assert c.wake_orch.cfg.window_min == 45


def test_the_alarm_spec_and_steering_standoff_take_the_chosen_window():
    cfg = AppConfig.default()
    wake = datetime(2026, 9, 26, 7, 0)
    swr = SmartWakeRoutine(cfg)
    assert swr.alarm_spec(wake - timedelta(minutes=40), wake) is None
    spec = swr.alarm_spec(wake - timedelta(minutes=40), wake, window_min=45)
    assert spec is not None and spec.window_min == 45
    steer = ArchitectureSteering(cfg)
    guard = cfg.tunables.steer_prewake_guard_min
    assert steer.prewake_standoff_min(45) == 45 + guard
    assert steer.prewake_standoff_min() == cfg.tunables.wake_window_min + guard


# -- 8. a warm rescue is not eaten by a warm-forecast bias ------------------------------------
def test_the_recovery_warmth_survives_a_negative_ambient_bias():
    results = {}
    for bias in (0.0, -1.0, -2.0):
        c = SleepController(AppConfig.default())
        c.thermal.set_measured_neutral(70.0)
        c.thermal.set_ambient_bias(bias)
        t0 = datetime(2026, 9, 26, 2, 0)
        c._bed_entry_time = t0 - timedelta(hours=4)
        c._sleep_onset_time = t0 - timedelta(hours=3, minutes=40)
        c.sm.state = ControllerState.MAINTENANCE
        t, recent, rec = t0, [], []
        for i in range(24):
            awake = 10 <= i < 16
            f = _fr(t, SleepStage.AWAKE if awake else SleepStage.LIGHT,
                    hr=72 if awake else 56, mv=0.3 if awake else 0.0)
            d = c.decide(f, ContextRecord(date="2026-09-25"), recent[-30:], t)
            recent.append(f)
            if d.state is ControllerState.WAKE_RECOVERY:
                rec.append(d.target_temp_f)
            t += timedelta(seconds=30)
        assert rec, bias
        results[bias] = max(rec)
    assert results[-1.0] == results[-2.0] == results[0.0] > 70.0


# -- 9. the DST fall-back is not a new night --------------------------------------------------
def test_the_dst_fall_back_keeps_the_night_and_its_clocks():
    c = SleepController(AppConfig.default())
    ctx = ContextRecord(date="2026-10-31", required_wake_time=datetime(2026, 11, 1, 7, 0))
    c._bed_entry_time = datetime(2026, 10, 31, 23, 0)
    c.sm.state = ControllerState.MAINTENANCE
    c._sleep_onset_time = datetime(2026, 10, 31, 23, 30)
    c._arch_deep_min, c._arch_rem_min, c._arch_light_min = 60.0, 40.0, 60.0
    c.note_user_override(68.5, warmer=True)   # woke cold earlier -> floor 69.5
    clock = [datetime(2026, 11, 1, 1, 40) + timedelta(seconds=30 * i) for i in range(40)]
    clock += [datetime(2026, 11, 1, 1, 0) + timedelta(seconds=30 * i) for i in range(200)]
    recent, recovery_ticks = [], 0
    for i, t in enumerate(clock):
        if i < 40 and datetime(2026, 11, 1, 1, 55) <= t < datetime(2026, 11, 1, 1, 57):
            f = _fr(t, SleepStage.AWAKE, hr=85, mv=0.6, presence=True)
        else:
            f = _fr(t, SleepStage.LIGHT, hr=55, presence=True)
        d = c.decide(f, ctx, recent[-30:], t)
        recent.append(f)
        recovery_ticks += d.state is ControllerState.WAKE_RECOVERY
        assert c.session_floor_f == 69.5, t
        assert c._arch_deep_min == 60.0 and c._arch_rem_min == 40.0, t
    # wake_recovery_minutes (20) of REAL time, plus the stable streak -- not 80 minutes.
    assert recovery_ticks / 2.0 <= 25.0
    assert d.state is ControllerState.MAINTENANCE


def test_a_recovery_clock_in_the_future_is_re_anchored():
    cfg = AppConfig.default()
    sm = SleepStateMachine(cfg, ControllerState.WAKE_RECOVERY)
    now = datetime(2026, 11, 1, 1, 0)
    sm._recovery_started = now + timedelta(minutes=56)
    sm.transition(_fr(now, SleepStage.LIGHT), now, False, None)
    assert sm._recovery_started == now
