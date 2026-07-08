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
    assert d.cycle.controller.thermal.profile.neutral_f == 70.0


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
