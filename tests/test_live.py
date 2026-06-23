"""Tests for the live daemon, credentials, and the async pyEight mapping helpers.

The real device path needs credentials + network and cannot run here; these tests cover
the daemon mechanics via the simulator-backed ``SimulatedLiveClient``, plus credential
loading and the stage-mapping/bed-temp helpers.
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta

from sleepctl.adapters.credentials import Credentials, load_credentials, save_credentials
from sleepctl.adapters.eightsleep_cloud import map_stage
from sleepctl.config import AppConfig
from sleepctl.loop.live import LiveDaemon, SimulatedLiveClient
from sleepctl.models import SleepStage
from sleepctl.storage.repository import Repository


def _context(start: datetime, hours: float = 8.0):
    from sleepctl.adapters.calendar import ManualCalendarSource

    wake = start + timedelta(hours=hours)
    return ManualCalendarSource(required_wake_time=wake, bedtime=start).get_context(
        start.date().isoformat()
    )


def _run_daemon(dry_run: bool):
    cfg = AppConfig.default()
    start = datetime(2026, 6, 23, 23, 0)
    client = SimulatedLiveClient(scenario="normal", seed=7, start=start)
    repo = Repository(":memory:")
    daemon = LiveDaemon(cfg, client, repo, context=_context(start), verbose=False)
    max_ticks = client.source.length + 5
    decisions = asyncio.run(
        daemon.run(poll_seconds=0.0, dry_run=dry_run, max_ticks=max_ticks)
    )
    return client, repo, decisions


def test_live_daemon_actuates_when_not_dry_run():
    client, repo, decisions = _run_daemon(dry_run=False)
    assert len(client.actuator.commands) > 0  # commands were sent
    # slew invariant holds on the live path too (2F / 0.2F-per-level = 10 levels)
    levels = client.actuator.commands
    for a, b in zip(levels, levels[1:]):
        assert abs(b - a) <= 10 + 1e-6


def test_live_daemon_dry_run_sends_no_commands():
    client, repo, decisions = _run_daemon(dry_run=True)
    assert client.actuator.commands == []  # READ-ONLY: nothing sent to the device
    # but the controller still produced decisions and logged samples
    assert len(decisions) > 0
    assert repo.conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0] > 0


def test_live_daemon_logs_all_layers_and_closes_out_night():
    client, repo, decisions = _run_daemon(dry_run=False)
    night_date = "2026-06-23"
    assert len(repo.samples_for_night(night_date)) > 0          # layer 1
    assert repo.conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0] > 0
    assert repo.conn.execute("SELECT COUNT(*) FROM interventions").fetchone()[0] > 0
    # nightly close-out persisted a summary + baselines
    assert any(n.date == night_date for n in repo.recent_nights(5))
    assert repo.latest_baselines() is not None


def test_live_daemon_respects_max_ticks():
    cfg = AppConfig.default()
    start = datetime(2026, 6, 23, 23, 0)
    client = SimulatedLiveClient(scenario="normal", seed=7, start=start)
    repo = Repository(":memory:")
    daemon = LiveDaemon(cfg, client, repo, context=_context(start), verbose=False)
    decisions = asyncio.run(daemon.run(poll_seconds=0.0, dry_run=True, max_ticks=10))
    assert len(decisions) == 10


def test_live_daemon_shutdown_event_stops_loop():
    cfg = AppConfig.default()
    start = datetime(2026, 6, 23, 23, 0)
    client = SimulatedLiveClient(scenario="normal", seed=7, start=start)
    repo = Repository(":memory:")
    daemon = LiveDaemon(cfg, client, repo, context=_context(start), verbose=False)

    async def _go():
        ev = asyncio.Event()
        ev.set()  # already set -> loop should stop after the first tick
        return await daemon.run(poll_seconds=0.0, dry_run=True, shutdown_event=ev)

    decisions = asyncio.run(_go())
    assert len(decisions) == 1


def test_map_stage():
    assert map_stage("asleep:deep") is SleepStage.DEEP
    assert map_stage("light") is SleepStage.LIGHT
    assert map_stage("awake") is SleepStage.AWAKE
    assert map_stage(None) is SleepStage.UNKNOWN
    assert map_stage("weird-value") is SleepStage.UNKNOWN


def test_credentials_roundtrip_and_env_override(tmp_path, monkeypatch):
    path = tmp_path / "creds.json"
    save_credentials(Credentials(email="a@b.com", password="pw", timezone="UTC", side="left"),
                     str(path))
    # file is 0600
    assert (os.stat(path).st_mode & 0o777) == 0o600
    loaded = load_credentials(str(path))
    assert loaded.email == "a@b.com" and loaded.is_complete()
    # env overrides the file
    monkeypatch.setenv("EIGHTSLEEP_EMAIL", "env@b.com")
    assert load_credentials(str(path)).email == "env@b.com"


def test_credentials_missing_is_incomplete(tmp_path):
    assert not load_credentials(str(tmp_path / "nope.json")).is_complete()
