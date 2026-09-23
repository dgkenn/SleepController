"""LiveDashboardDaemon over the SimulatedLiveClient: commands reach the (mock) device,
runtime_state is written from real frames, and dry-run sends nothing."""

from __future__ import annotations

import asyncio
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "dashboard", "daemon"))

from sleepctl.config import AppConfig  # noqa: E402
from sleepctl.loop.live import SimulatedLiveClient  # noqa: E402

from app import bridge  # noqa: E402
from app.db import get_repo  # noqa: E402
from live_daemon import LiveDashboardDaemon  # noqa: E402


def _daemon(dry_run: bool = False):
    repo = get_repo()
    # clear any leftover pending commands so tests are independent
    repo.conn.execute("UPDATE commands SET status='applied' WHERE status='pending'")
    repo.conn.commit()
    client = SimulatedLiveClient(scenario="normal", seed=7)
    daemon = LiveDashboardDaemon(AppConfig.default(), client, repo, dry_run=dry_run,
                                 verbose=False)
    return daemon, client, repo


def _run(coro):
    asyncio.new_event_loop().run_until_complete(coro)


def test_startup_loads_learned_profiles_not_silently_skipped():
    # Regression: __init__ used to call _attach_profiles BEFORE _pending_wake existed, so the whole
    # profile load (every per-phase learner) threw AttributeError and was silently swallowed — the
    # live Pod would run on config defaults. Constructing the daemon must fully load profiles.
    d, client, repo = _daemon()
    assert d._deepen_policy is not None           # deepening-response policy was learned + applied
    assert d.cycle.controller.last_precursor_profile is not None  # precursor profile applied
    assert d._onset_warm_f is not None            # onset maneuver loaded
    # the deepen actuation gate was actually set on the controller (default True on thin data)
    assert isinstance(d.cycle.controller.steer_actuate, bool)


def test_live_set_temp_reaches_device():
    d, client, repo = _daemon()
    bridge.enqueue_command(repo.conn, "set_temp", {"target_f": 64})

    async def go():
        await client.connect()
        await d.command_tick()
    _run(go())

    assert client.level_set_count >= 1  # a heating command was actually sent
    rt = bridge.read_runtime_state(repo.conn)
    assert rt["mode"] == "manual"
    assert abs(rt["target_temp_f"] - 64) < 0.01


def test_live_dry_run_sends_no_device_commands():
    d, client, repo = _daemon(dry_run=True)
    bridge.enqueue_command(repo.conn, "set_temp", {"target_f": 62})

    async def go():
        await client.connect()
        await d.command_tick()
    _run(go())

    assert client.level_set_count == 0  # read-only: nothing sent to the bed
    rt = bridge.read_runtime_state(repo.conn)
    assert abs(rt["target_temp_f"] - 62) < 0.01  # but the intended target is shown
    assert rt["extra"]["dry_run"] is True


def test_live_emergency_stop_turns_off_side():
    d, client, repo = _daemon()
    bridge.enqueue_command(repo.conn, "stop")

    async def go():
        await client.connect()
        await d.command_tick()
    _run(go())

    assert client.off_count == 1  # Emergency Stop hard-offs the side
    rt = bridge.read_runtime_state(repo.conn)
    assert rt["state"] == "OFF" and rt["extra"]["power_on"] is False


def test_live_emergency_stop_works_even_in_dry_run():
    # Safety override: dry-run blocks every OTHER write, but Emergency Stop must still hard-off.
    d, client, repo = _daemon(dry_run=True)
    bridge.enqueue_command(repo.conn, "stop")

    async def go():
        await client.connect()
        await d.command_tick()
    _run(go())

    assert client.off_count == 1  # turned off the side despite dry-run


def test_live_away_and_prime_call_device():
    d, client, repo = _daemon()
    bridge.enqueue_command(repo.conn, "away_on")
    bridge.enqueue_command(repo.conn, "prime")

    async def go():
        await client.connect()
        await d.command_tick()
    _run(go())

    assert client.away is True
    assert client.prime_count == 1


class _FlakyClient(SimulatedLiveClient):
    """Simulated client whose update() raises a few times, then recovers."""
    def __init__(self, *a, fails: int = 2, **k):
        super().__init__(*a, **k)
        self._fails = fails

    async def update(self):
        if self._fails > 0:
            self._fails -= 1
            raise RuntimeError("transient cloud error")
        return await super().update()


def test_live_daemon_survives_transient_device_errors():
    repo = get_repo()
    repo.conn.execute("UPDATE commands SET status='applied' WHERE status='pending'")
    repo.conn.commit()
    client = _FlakyClient(scenario="normal", seed=3, fails=2)
    d = LiveDashboardDaemon(AppConfig.default(), client, repo, verbose=False)
    # poll_seconds=0 -> every iteration is a control tick; the first two raise and must be
    # swallowed, after which two real ticks complete (max_ticks=2) without the loop dying.
    _run(d.run(poll_seconds=0, command_poll_seconds=0, max_ticks=2))
    assert d._consec_errors == 0           # recovered after the transient failures
    rt = bridge.read_runtime_state(repo.conn)
    assert rt["daemon_alive"] is True      # daemon stayed alive through the errors


class _AlwaysFlakyClient(SimulatedLiveClient):
    """Simulated client whose update() ALWAYS raises (never recovers) -- unlike _FlakyClient
    above, so a test can observe a persistent run of consecutive errors before anything resets
    the counter. Note: with an always-failing client, `ticks` (incremented only on a SUCCESSFUL
    control_tick) never advances, so `max_ticks` can never be reached -- that would spin the
    `run()` loop forever. Sets `stop_event` once `stop_after` calls have been made so the test
    can bound the run via `shutdown_event` instead."""
    def __init__(self, *a, stop_after: int, stop_event: asyncio.Event, **k):
        super().__init__(*a, **k)
        self._stop_after = stop_after
        self._calls = 0
        self._stop_event = stop_event

    async def update(self):
        self._calls += 1
        if self._calls >= self._stop_after:
            self._stop_event.set()
        raise RuntimeError(f"persistent cloud error #{self._calls}")


def test_live_daemon_persists_consec_errors_into_runtime_state_extra():
    """A sustained (non-recovering) run of tick errors must be visible in runtime_state.extra
    so app.services.evaluate_and_sync_health_alerts can see it and push a critical alert --
    see health_monitor.evaluate_health's recent_errors path (item #4 of the reliability audit)."""
    repo = get_repo()
    repo.conn.execute("UPDATE commands SET status='applied' WHERE status='pending'")
    repo.conn.commit()
    stop_event = asyncio.Event()
    client = _AlwaysFlakyClient(scenario="normal", seed=3, stop_after=3, stop_event=stop_event)
    d = LiveDashboardDaemon(AppConfig.default(), client, repo, verbose=False)

    async def go():
        # poll_seconds=0 -> every iteration is a control tick; all of them fail. Bounded by
        # shutdown_event (set by the client on its 3rd call), NOT max_ticks (see _AlwaysFlakyClient
        # docstring for why max_ticks would never fire here).
        await d.run(poll_seconds=0, command_poll_seconds=0, shutdown_event=stop_event)
    _run(go())

    assert d._consec_errors == 3
    rt = bridge.read_runtime_state(repo.conn)
    assert rt["extra"]["consec_errors"] == 3
    assert len(rt["extra"]["recent_errors"]) == 3
    assert all("persistent cloud error" in e for e in rt["extra"]["recent_errors"])
    repo.close()


def test_live_telemetry_tick_refreshes_snapshot_without_actuating():
    d, client, repo = _daemon()

    n_before = client.level_set_count  # 0 at init (set() not called yet)

    async def go():
        await client.connect()
        await d.telemetry_tick()        # fast refresh in isolation: no device writes
    _run(go())

    assert client.level_set_count == n_before  # telemetry tick sends nothing to the bed
    rt = bridge.read_runtime_state(repo.conn)
    assert rt["daemon_alive"] is True
    assert rt["bed_temp_f"] is not None            # fresh sensor frame published
    assert "data_age_s" in rt["extra"]             # freshness surfaced for the UI


def test_live_control_tick_writes_real_frame():
    d, client, repo = _daemon()

    async def go():
        await client.connect()
        await d.control_tick()
    _run(go())

    rt = bridge.read_runtime_state(repo.conn)
    assert rt["daemon_alive"] is True
    assert rt["bed_temp_f"] is not None        # the real (simulated) frame is surfaced
    assert rt["extra"]["live"] is True


def test_live_daemon_fuses_wearable_when_attached():
    from datetime import datetime as _dt

    from sleepctl.adapters.wearable import SimulatedWearableSource, WearableSample
    repo = get_repo()
    repo.conn.execute("UPDATE commands SET status='applied' WHERE status='pending'")
    repo.conn.commit()
    client = SimulatedLiveClient(scenario="normal", seed=7)
    wear = SimulatedWearableSource(fixed=WearableSample(
        timestamp=_dt(2026, 6, 27, 3, 0), heart_rate=71.0, movement=0.5, age_seconds=2.0))
    d = LiveDashboardDaemon(AppConfig.default(), client, repo, verbose=False, wearable=wear)
    out = {}

    async def go():
        await client.connect()
        out["f"] = d._read_frame()        # fast wearable overlays the Pod frame
    _run(go())
    assert out["f"].heart_rate == 71.0 and out["f"].movement == 0.5


def test_live_daemon_phone_fusion_is_presence_gated():
    """Out of bed (presence False) -> the phone feed is ignored automatically; in bed -> fused."""
    from datetime import datetime as _dt

    from sleepctl.adapters.wearable import SimulatedWearableSource, WearableSample
    repo = get_repo()
    repo.conn.execute("UPDATE commands SET status='applied' WHERE status='pending'")
    repo.conn.commit()
    client = SimulatedLiveClient(scenario="normal", seed=7)
    wear = SimulatedWearableSource(fixed=WearableSample(
        timestamp=_dt(2026, 6, 27, 3, 0), heart_rate=71.0, movement=0.5, age_seconds=2.0))
    d = LiveDashboardDaemon(AppConfig.default(), client, repo, verbose=False, wearable=wear)
    out = {}

    async def go():
        await client.connect()
        base = client.read_frame()
        base.presence = False              # got out of bed
        client.read_frame = lambda: base   # type: ignore[assignment]
        out["frame"] = d._read_frame()
    _run(go())
    # the wearable's HR/movement did NOT overlay; the daemon flags it as not fused
    assert out["frame"].heart_rate != 71.0
    assert d._phone_fused is False


def test_live_self_test_runs_and_leaves_side_off():
    """The on-bed self-test command runs the battery over the (mock) device, publishes a report,
    and always powers the side OFF at the end (paused, awaiting a manual Power On)."""
    d, client, repo = _daemon()
    bridge.enqueue_command(repo.conn, "self_test", {"mode": "full"})

    async def go():
        await client.connect()
        await d._apply_commands()          # runs the battery inline
    _run(go())

    rep = bridge.read_self_test(repo.conn)
    assert rep is not None and rep["running"] is False
    names = {c["name"] for c in rep["checks"]}
    assert {"connectivity", "presence", "heart_rate", "safe_off"} <= names
    assert client.off_count >= 1           # SAFE-OFF actuated the side off
    assert d.power_on is False and d.paused is True   # holds until the user resumes


def test_live_self_test_cancel_is_a_known_command():
    """A cancel command is accepted (not an 'unknown command') even with no battery running."""
    d, client, repo = _daemon()
    bridge.enqueue_command(repo.conn, "self_test_cancel")

    async def go():
        await client.connect()
        await d._apply_commands()
    _run(go())
    # nothing to assert beyond: it applied without raising / wedging the queue
    pending = repo.conn.execute(
        "SELECT COUNT(*) c FROM commands WHERE status='pending'").fetchone()["c"]
    assert pending == 0


def test_live_comfort_calibration_sweeps_and_saves_neutral():
    """The interactive comfort sweep holds each step, then derives + saves a neutral setpoint and
    applies it to the controller."""
    d, client, repo = _daemon()
    bridge.enqueue_command(repo.conn, "comfort_cal_start", {"steps_f": [64, 68, 72, 76]})

    async def go():
        await client.connect()
        await d._apply_commands()
        assert d.comfort is not None and d.comfort.current_target_f() == 64
        for rating in (-2, -1, 1, 2):
            bridge.enqueue_command(repo.conn, "comfort_cal_rate", {"rating": rating})
            await d._apply_commands()
    _run(go())

    assert d.comfort is None                          # sweep finished
    prof = repo.get_comfort_profile()
    assert prof and prof["neutral_f"] == 70.0
    # the sweep is stored as measured; the controller steers it re-anchored (see
    # sleepctl.learning.comfort_feedback), since a sweep is taken awake
    assert d.cycle.controller.thermal.profile.neutral_f == 70.0 + d.cfg.tunables.comfort_neutral_offset_f


def test_live_comfort_cancel_stops_and_holds_off():
    d, client, repo = _daemon()
    bridge.enqueue_command(repo.conn, "comfort_cal_start", {"steps_f": [64, 72]})

    async def go():
        await client.connect()
        await d._apply_commands()
        bridge.enqueue_command(repo.conn, "comfort_cal_cancel")
        await d._apply_commands()
    _run(go())
    assert d.comfort is None and d.paused is True and d.power_on is False


def test_log_never_raises_on_unencodable_char():
    """Regression: on Windows the cp1252 console couldn't encode "⚠"/"°", so the daemon's
    ``_log`` raised UnicodeEncodeError inside the control loop — and its crash handler logged
    the exception repr (which still held the offending char) and died too, crash-looping the
    whole daemon every few minutes and freezing all telemetry. ``_log`` must never raise, even
    when stdout cannot encode the message."""
    import io
    repo = get_repo()
    client = SimulatedLiveClient(scenario="normal", seed=7)
    d = LiveDashboardDaemon(AppConfig.default(), client, repo, verbose=True)
    old = sys.stdout
    # a strict ASCII stream mimics the Windows cp1252 console that triggered the crash loop
    sys.stdout = io.TextIOWrapper(io.BytesIO(), encoding="ascii", errors="strict")
    try:
        d._log("⚠ thermal: not enough history — target 55 °F")  # must not raise
        d._log("plain ascii still logs fine")
    finally:
        sys.stdout = old
    repo.close()


# ------------------------------------------------------------------ onset-event logging
# Before _maybe_log_onset existed, SleepOnsetDetector's confirmed result (WHICH signals fired --
# stillness, hr_drop, hrv_rise, respiration_regular...) was kept on the controller as
# last_onset_event and never read by anything else: no log line, no DB row. A later question like
# "did fall-asleep detection use the accelerometer last night" had no evidence to answer it from,
# for any night, past or future -- the timestamp survived into sleep_onset_latency_min, but the
# reasoning behind it was computed live and thrown away every night.
def _onset_event(signals=("stillness", "hr_drop", "hr_trend_down"), confidence=0.6):
    from datetime import datetime
    from sleepctl.controller.sleep_onset import SleepOnsetEvent
    return SleepOnsetEvent(timestamp=datetime(2026, 8, 23, 22, 45), confidence=confidence,
                           signals=list(signals), latency_min=12.0)


def _clear_sleep_events(repo):
    repo.conn.execute("DELETE FROM events WHERE category='sleep'")
    repo.conn.commit()


def test_confirmed_onset_is_logged_with_its_signals():
    d, client, repo = _daemon()
    _clear_sleep_events(repo)
    d.cycle.controller.last_onset_event = _onset_event()
    d._maybe_log_onset()

    events = repo.recent_events(category="sleep")
    assert len(events) == 1
    e = events[0]
    assert e["code"] == "onset_confirmed"
    assert e["data"]["signals"] == ["stillness", "hr_drop", "hr_trend_down"]
    assert e["data"]["confidence"] == 0.6
    assert e["data"]["latency_min"] == 12.0
    repo.close()


def test_the_same_onset_is_not_logged_twice():
    d, client, repo = _daemon()
    _clear_sleep_events(repo)
    event = _onset_event()
    d.cycle.controller.last_onset_event = event
    d._maybe_log_onset()
    d._maybe_log_onset()   # same event object, e.g. the very next tick
    d._maybe_log_onset()

    assert len(repo.recent_events(category="sleep")) == 1
    repo.close()


def test_a_fresh_onset_after_a_session_reset_logs_again():
    """_end_session / _start_induce clear _onset_logged_ts -- a new bed session (out of bed and
    back in, or a fresh "help me fall asleep") must be able to log its own onset, not be
    permanently silenced by an earlier night's timestamp."""
    d, client, repo = _daemon()
    _clear_sleep_events(repo)
    d.cycle.controller.last_onset_event = _onset_event()
    d._maybe_log_onset()
    assert len(repo.recent_events(category="sleep")) == 1

    d._end_session()
    from datetime import datetime
    from sleepctl.controller.sleep_onset import SleepOnsetEvent
    d.cycle.controller.last_onset_event = SleepOnsetEvent(
        timestamp=datetime(2026, 8, 24, 3, 10), confidence=0.8,
        signals=["stillness", "respiration_regular", "hrv_rise"], latency_min=8.0)
    d._maybe_log_onset()

    events = repo.recent_events(category="sleep")
    assert len(events) == 2
    repo.close()


def test_no_onset_yet_logs_nothing():
    d, client, repo = _daemon()
    _clear_sleep_events(repo)
    assert d.cycle.controller.last_onset_event is None
    d._maybe_log_onset()
    assert repo.recent_events(category="sleep") == []
    repo.close()


# ------------------------------------------- wake-therapy smart plug (2026-08-27 request)
def test_the_plug_is_driven_from_the_same_wake_decision_as_the_hue_lamp():
    """Both transports ride the ORCHESTRATOR's decision, so a Hue lamp and a generic Wi-Fi plug
    can never disagree about whether it is time to get up -- and neither can fire mid-sleep,
    which is the one behaviour that would harm the sleep this system exists to protect."""
    from live_daemon import LiveDashboardDaemon

    class _Plug:
        def __init__(self):
            self.calls = []

        def set_therapy(self, on):
            self.calls.append(bool(on))

    class _Daemon:
        hue_driver = None

        def __init__(self):
            self.plug_driver = _Plug()

        def _log(self, *a):
            pass

    class _Dec:
        def __init__(self, la):
            self.log_payload = {"wake_action": la} if la is not None else {}

    d = _Daemon()
    drive = LiveDashboardDaemon._drive_dawn
    drive(d, _Dec(None))                        # outside the wake window
    drive(d, _Dec({"should_wake": False}))      # in-window, not time yet
    drive(d, _Dec({"should_wake": True}))       # the wake moment
    assert d.plug_driver.calls == [False, False, True]


def test_no_configured_plug_is_a_clean_no_op():
    from live_daemon import LiveDashboardDaemon

    class _Daemon:
        hue_driver = None
        plug_driver = None

        def _log(self, *a):
            pass

    LiveDashboardDaemon._drive_dawn(_Daemon(), None)     # must not raise


def test_a_plug_failure_never_breaks_the_dawn_drive():
    """The lamp is a nice-to-have; the control loop is not. A plug that throws must not take
    the tick down."""
    from live_daemon import LiveDashboardDaemon

    class _Boom:
        def set_therapy(self, on):
            raise RuntimeError("plug exploded")

    class _Daemon:
        hue_driver = None

        def __init__(self):
            self.plug_driver = _Boom()
            self.logged = []

        def _log(self, m):
            self.logged.append(m)

    class _Dec:
        log_payload = {"wake_action": {"should_wake": True}}

    d = _Daemon()
    LiveDashboardDaemon._drive_dawn(d, _Dec())
    assert any("plug" in m for m in d.logged)


def test_a_session_started_without_an_alarm_still_gets_steering_targets():
    """2026-09-18: bed entry on wearable evidence, no alarm, night_targets None all night --
    the steerer returned on its first gate for 1970/1970 ticks."""
    d, client, repo = _daemon()
    d.cycle.controller.night_targets = None
    d._start_induce()
    assert d.cycle.controller.night_targets is not None
    # with no alarm the planner may leave est_sleep_min unset; the steerer falls back to the
    # targets' own total_sleep_target_min in that case


def test_night_targets_are_planned_once_and_not_replaced():
    d, client, repo = _daemon()
    d.cycle.controller.night_targets = None
    d._ensure_night_targets("test")
    first = d.cycle.controller.night_targets
    d._ensure_night_targets("again")
    assert d.cycle.controller.night_targets is first


def test_the_daemon_publishes_its_session_state_for_the_watchdog(tmp_path, monkeypatch):
    """A redeploy during INDUCTION re-runs the warm opener on someone falling asleep, and
    session recovery only covers states past onset. The watchdog defers on this file."""
    import os
    from app import bridge
    from sleepctl.models import ControllerState

    d, client, repo = _daemon()
    monkeypatch.setattr(bridge, "run_dir", lambda: str(tmp_path))

    d.cycle.controller.sm.state = ControllerState.INDUCTION
    d._publish_session_state()
    with open(os.path.join(str(tmp_path), "session.state")) as fh:
        # " protect" marks the states a restart cannot be recovered from; the watchdog
        # defers on that flag alone (see _restart_would_damage)
        assert fh.read().strip() == "induction protect"

    d.cycle.controller.sm.state = ControllerState.IDLE
    d._publish_session_state()
    with open(os.path.join(str(tmp_path), "session.state")) as fh:
        assert fh.read().strip() == "idle"


def test_publishing_the_session_state_never_raises(monkeypatch):
    from app import bridge
    d, client, repo = _daemon()
    monkeypatch.setattr(bridge, "run_dir", lambda: "/nonexistent/path/that/cannot/be/written")
    d._publish_session_state()          # must not raise


def test_a_refused_alarm_write_is_remembered_across_a_restart():
    """The flag lived in memory and reset on every deploy, so the wake page said vibration was
    available on exactly the nights it was not."""
    d, client, repo = _daemon()
    assert d._alarm_write_denied is False
    d._save_alarm_write_denied(True, "403 Subscription required")
    d2 = LiveDashboardDaemon(AppConfig.default(), client, repo, dry_run=False, verbose=False)
    assert d2._alarm_write_denied is True
    d2._save_alarm_write_denied(False)
    d3 = LiveDashboardDaemon(AppConfig.default(), client, repo, dry_run=False, verbose=False)
    assert d3._alarm_write_denied is False


# ------------------------------------------- 2026-09-21: a stuck session stranded a deploy
def test_only_unrecoverable_states_are_protected_from_a_restart():
    """restore_session_state covers every state past onset, so a restart in MAINTENANCE /
    WAKE_RECOVERY / WAKE_WINDOW costs a tick. Induction re-arms from zero, and a restart
    mid-nap loses the deadline that ends it."""
    class _Stub:
        nap_deadline = None
    stub = _Stub()
    for state in ("induction", "calibration"):
        assert LiveDashboardDaemon._restart_would_damage(stub, state) is True, state
    for state in ("idle", "maintenance", "wake_recovery", "wake_window"):
        assert LiveDashboardDaemon._restart_would_damage(stub, state) is False, state


def test_an_armed_nap_is_protected_in_any_state():
    from datetime import datetime

    class _Stub:
        nap_deadline = datetime(2026, 9, 21, 14, 0)
    assert LiveDashboardDaemon._restart_would_damage(_Stub(), "maintenance") is True


def test_a_recoverable_state_does_not_hold_back_a_deploy(tmp_path, monkeypatch):
    """2026-09-21: a WAKE_RECOVERY that failed to end -- the band had been on its charger
    since 05:28 -- held the day's deploy for over ten hours, including the accelerometer fix
    that the previous night's data loss had been waiting for."""
    import os
    from app import bridge
    from sleepctl.models import ControllerState
    d, client, repo = _daemon()
    monkeypatch.setattr(bridge, "run_dir", lambda: str(tmp_path))
    for state in (ControllerState.MAINTENANCE, ControllerState.WAKE_RECOVERY,
                  ControllerState.WAKE_WINDOW):
        d.cycle.controller.sm.state = state
        d.nap_deadline = None
        d._publish_session_state()
        with open(os.path.join(str(tmp_path), "session.state")) as fh:
            assert "protect" not in fh.read()


def test_an_armed_nap_holds_back_a_deploy_in_any_state(tmp_path, monkeypatch):
    """A restart mid-nap loses the in-memory deadline that ends it."""
    import os
    from datetime import datetime
    from app import bridge
    from sleepctl.models import ControllerState
    d, client, repo = _daemon()
    monkeypatch.setattr(bridge, "run_dir", lambda: str(tmp_path))
    d.cycle.controller.sm.state = ControllerState.MAINTENANCE
    d.nap_deadline = datetime(2026, 9, 21, 14, 0)
    d._publish_session_state()
    with open(os.path.join(str(tmp_path), "session.state")) as fh:
        assert fh.read().strip() == "maintenance protect"
    d.nap_deadline = None


def test_a_too_cold_morning_warms_the_next_session():
    """2026-09-22: "the bed wakes me up in the middle of the night because it's so cold". The
    morning review's temperature answer moves the neutral the NEXT "help me fall asleep" steers
    around, re-read at session start (the review is filed after the close-out)."""
    d, client, repo = _daemon()
    repo.save_comfort_profile({"neutral_f": 69.0, "cool_edge_f": 67.0, "warm_edge_f": 69.5,
                               "ratings": [], "source": "test"})
    repo.conn.execute("DELETE FROM wake_review")
    repo.conn.commit()
    d._start_induce()
    base = d.cycle.controller.thermal.profile.neutral_f
    assert base == 69.0 + d.cfg.tunables.comfort_neutral_offset_f
    assert d._comfort_anchor["neutral_f"] == base
    try:
        repo.conn.execute(
            "INSERT OR REPLACE INTO wake_review (night_date, ts, temperature) VALUES (?, ?, ?)",
            ("2099-01-01", "2099-01-02T07:00:00", "too_cold"))
        repo.conn.commit()
        d._start_induce()
        assert d.cycle.controller.thermal.profile.neutral_f == base + 1.0
        # the comfort band moved with it, so the clamp does not pull the warmer target back
        assert d.cycle.controller.comfort_profile["cool_edge_f"] == 67.0 + (base + 1.0 - 69.0)
    finally:
        repo.conn.execute("DELETE FROM wake_review")
        repo.conn.commit()


def test_im_awake_gives_the_morning_light_dose_and_naps_do_not():
    """"I'm awake" ends the session and holds the therapy lamp on for the dose; ending a nap,
    or an "I'm awake" in the small hours, never lights the room."""
    from datetime import datetime, timedelta
    d, client, repo = _daemon()

    class _Plug:
        def __init__(self):
            self.calls = []

        def set_therapy(self, on):
            self.calls.append(bool(on))

        def off(self):
            self.calls.append(False)

    plug = _Plug()
    d.plug_driver = plug
    morning = datetime(2026, 9, 23, 7, 5)
    d._clock_now = lambda: morning
    d.session_mode = "induce"
    assert d._start_light_dose("woke_up") is True
    d._drive_dawn(None)
    assert plug.calls[-1] is True
    assert d._wake_light_status()["on_until"] is not None
    # the dose ends on its own
    d._clock_now = lambda: morning + timedelta(minutes=d.cfg.tunables.wake_light_dose_min + 1)
    d._drive_dawn(None)
    assert plug.calls[-1] is False
    # 03:00 is before the body clock's minimum: no automatic light
    d._clock_now = lambda: datetime(2026, 9, 23, 3, 0)
    assert d._start_light_dose("woke_up") is False
    # ...but a manual request is honoured, and "off" reaches the lamp at once
    assert d._start_light_dose("manual", minutes=10, manual=True) is True
    d._stop_light_dose("manual")
    assert plug.calls[-1] is False and d._light_dose_until is None


def test_the_wake_up_command_lights_only_a_night_session():
    d, client, repo = _daemon()
    started = []
    d._start_light_dose = lambda why, **k: started.append(why) or True
    for mode, expect in (("nap", []), ("induce", ["woke_up"])):
        started.clear()
        d.session_mode = mode
        repo.conn.execute("INSERT INTO commands (ts, type, payload, status) "
                          "VALUES (datetime('now'), 'woke_up', '{}', 'pending')")
        repo.conn.commit()
        _run(d._apply_commands())
        assert started == expect, mode
        assert d.session_mode == "night"


def test_the_lamp_comes_on_at_the_alarm_time_set_in_the_app():
    """The wake time the user set is when the light goes on -- once, not for a nap deadline,
    not an hour late after a restart, and a toggled test is immediate."""
    from datetime import datetime, timedelta
    d, client, repo = _daemon()

    class _Plug:
        def __init__(self):
            self.calls = []

        def set_therapy(self, on):
            self.calls.append(bool(on))

        def off(self):
            self.calls.append(False)

    plug = _Plug()
    d.plug_driver = plug
    alarm = datetime(2026, 9, 23, 6, 45)
    d.context.required_wake_time = alarm
    d.session_mode = "induce"
    assert d._maybe_alarm_light(alarm - timedelta(minutes=1)) is False       # not yet
    assert d._maybe_alarm_light(alarm + timedelta(seconds=40)) is True       # the alarm
    assert plug.calls[-1] is True                                            # at once
    assert d._maybe_alarm_light(alarm + timedelta(minutes=2)) is False       # only once
    # an alarm the daemon only sees long after it passed does not fire
    d._alarm_light_fired = None
    assert d._maybe_alarm_light(alarm + timedelta(minutes=60)) is False
    # a nap's deadline is not an alarm for the lamp
    d._alarm_light_fired = None
    d.session_mode = "nap"
    assert d._maybe_alarm_light(alarm + timedelta(minutes=1)) is False
    # the test toggle: on is immediate, off is immediate
    d.session_mode = "night"
    d._stop_light_dose("test")
    assert plug.calls[-1] is False
    assert d._start_light_dose("manual", minutes=2, manual=True) is True
    assert plug.calls[-1] is True
    d._stop_light_dose("manual")
    assert plug.calls[-1] is False


def test_a_plug_set_up_from_the_phone_takes_effect_without_a_restart(monkeypatch):
    """The plug refresh used to sit behind the Hue config's unchanged-signature early return,
    so it ran once at startup and never again."""
    import sys
    import types
    from app import services
    d, client, repo = _daemon()
    repo.conn.execute("DELETE FROM settings_kv WHERE key IN ('wake_plug_config', 'wake_plug_scan')")
    repo.conn.commit()
    scans = []
    monkeypatch.setitem(sys.modules, "tinytuya", types.SimpleNamespace(find_device=lambda i: {}))
    monkeypatch.setattr(services, "plug_scan", lambda r: scans.append(1) or {"devices": []})
    d._plug_sig = None
    d._plug_scan_mono = None
    d._refresh_hue()
    d._refresh_hue()                                   # Hue unchanged: plug still refreshed
    import time as _t
    for _ in range(50):
        if scans:
            break
        _t.sleep(0.02)
    assert scans, "the unconfigured plug was never looked for"
    assert d.plug_driver is None
    services.plug_config_update(repo, {"enabled": True, "backend": "http",
                                       "config": {"on_url": "http://x/on", "off_url": "http://x/off"}})
    d._refresh_hue()
    assert d.plug_driver is not None
    repo.conn.execute("DELETE FROM settings_kv WHERE key IN ('wake_plug_config', 'wake_plug_scan')")
    repo.conn.commit()
