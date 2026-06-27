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
