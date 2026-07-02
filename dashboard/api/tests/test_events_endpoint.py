"""/diag/events: same token gating as /diag, seeded events roundtrip, and a live-daemon tick
actually emits at least one structured event (reusing the live_daemon test harness)."""

from __future__ import annotations

import asyncio
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "dashboard", "daemon"))


def test_diag_events_requires_token(client, monkeypatch):
    # disabled by default (no DIAG_TOKEN) -> 404 even with a token
    monkeypatch.delenv("DIAG_TOKEN", raising=False)
    assert client.get("/diag/events?token=whatever").status_code == 404

    monkeypatch.setenv("DIAG_TOKEN", "s3cret-xyz")
    assert client.get("/diag/events").status_code == 404          # no token
    assert client.get("/diag/events?token=nope").status_code == 404  # wrong token


def test_diag_events_returns_seeded_event(client, monkeypatch):
    monkeypatch.setenv("DIAG_TOKEN", "s3cret-xyz")
    from app.db import get_repo
    repo = get_repo()
    try:
        repo.log_event("device", "info", "set_temp", "target set to 68", {"target_f": 68})
    finally:
        repo.close()

    r = client.get("/diag/events?token=s3cret-xyz&limit=5")
    assert r.status_code == 200
    rows = r.json()
    assert isinstance(rows, list) and len(rows) >= 1
    match = next(row for row in rows if row["code"] == "set_temp")
    assert match["category"] == "device"
    assert match["severity"] == "info"
    assert match["data"] == {"target_f": 68}


def test_diag_events_filters_by_category_and_severity(client, monkeypatch):
    monkeypatch.setenv("DIAG_TOKEN", "s3cret-xyz")
    from app.db import get_repo
    repo = get_repo()
    try:
        repo.log_event("thermal", "warn", "thermal_stalled", "stalled for filter test")
    finally:
        repo.close()

    r = client.get("/diag/events?token=s3cret-xyz&category=thermal&severity=warn")
    assert r.status_code == 200
    rows = r.json()
    assert all(row["category"] == "thermal" and row["severity"] == "warn" for row in rows)
    assert any(row["code"] == "thermal_stalled" for row in rows)


def test_live_daemon_tick_emits_at_least_one_event(client, monkeypatch):
    """The live daemon's control tick must emit structured events (lifecycle at minimum) into
    the SAME events table /diag/events reads — end-to-end proof the hooks are wired up."""
    from sleepctl.config import AppConfig
    from sleepctl.loop.live import SimulatedLiveClient

    from app import bridge
    from app.db import get_repo
    from live_daemon import LiveDashboardDaemon

    monkeypatch.setenv("DIAG_TOKEN", "s3cret-xyz")

    repo = get_repo()
    repo.conn.execute("UPDATE commands SET status='applied' WHERE status='pending'")
    repo.conn.commit()
    daemon_client = SimulatedLiveClient(scenario="normal", seed=7)
    daemon = LiveDashboardDaemon(AppConfig.default(), daemon_client, repo, verbose=False)
    bridge.enqueue_command(repo.conn, "prime")

    async def go():
        await daemon_client.connect()
        await daemon.control_tick()   # applies the 'prime' command -> a device event
    asyncio.new_event_loop().run_until_complete(go())

    r = client.get("/diag/events?token=s3cret-xyz&limit=50")
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) >= 1
    assert any(row["code"] == "prime" and row["category"] == "device" for row in rows)
