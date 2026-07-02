"""/diag/history (48h state_history trend) and /diag/blackbox (crash pre-history dump): same
token gating as /diag, a seeded-rows roundtrip, and a live-daemon tick actually populating both
(reusing the live_daemon test harness, same pattern as test_events_endpoint.py)."""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timedelta

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "dashboard", "daemon"))

from app.main import _run_dir  # noqa: E402


# ------------------------------------------------------------------ /diag/history
def test_diag_history_requires_token(client, monkeypatch):
    monkeypatch.delenv("DIAG_TOKEN", raising=False)
    assert client.get("/diag/history?token=whatever").status_code == 404

    monkeypatch.setenv("DIAG_TOKEN", "s3cret-xyz")
    assert client.get("/diag/history").status_code == 404          # no token
    assert client.get("/diag/history?token=nope").status_code == 404  # wrong token


def test_diag_history_returns_seeded_rows_within_window(client, monkeypatch):
    monkeypatch.setenv("DIAG_TOKEN", "s3cret-xyz")
    from app.db import get_repo
    repo = get_repo()
    now = datetime.now()
    try:
        repo.record_state_snapshot({
            "ts": (now - timedelta(hours=1)).isoformat(),
            "state": "maintenance", "mode": "auto", "target_temp_f": 68.0,
            "bed_temp_f": 69.0, "room_temp_f": 66.0, "stage": "deep", "confidence": 0.7,
            "target_level": -15, "daemon_alive": True, "extra": {"marker": "recent"},
        })
        repo.record_state_snapshot({
            "ts": (now - timedelta(hours=72)).isoformat(),
            "state": "idle", "mode": "auto", "target_temp_f": None,
            "daemon_alive": True, "extra": {"marker": "old"},
        })
    finally:
        repo.close()

    r = client.get("/diag/history?token=s3cret-xyz&hours=48")
    assert r.status_code == 200
    rows = r.json()
    assert isinstance(rows, list) and len(rows) >= 1
    markers = {row["extra"].get("marker") for row in rows}
    assert "recent" in markers
    assert "old" not in markers   # outside the 48h window
    match = next(row for row in rows if row["extra"].get("marker") == "recent")
    assert match["state"] == "maintenance"
    assert match["bed_temp_f"] == 69.0


def test_diag_history_limit_param(client, monkeypatch):
    monkeypatch.setenv("DIAG_TOKEN", "s3cret-xyz")
    from app.db import get_repo
    repo = get_repo()
    try:
        for i in range(5):
            repo.record_state_snapshot({"state": f"limit_marker_{i}"})
    finally:
        repo.close()

    r = client.get("/diag/history?token=s3cret-xyz&hours=48&limit=2")
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == 2


# ------------------------------------------------------------------ /diag/blackbox
def test_diag_blackbox_requires_token(client, monkeypatch):
    monkeypatch.delenv("DIAG_TOKEN", raising=False)
    assert client.get("/diag/blackbox").status_code == 404
    monkeypatch.setenv("DIAG_TOKEN", "s3cret-xyz")
    assert client.get("/diag/blackbox").status_code == 404
    assert client.get("/diag/blackbox?token=nope").status_code == 404


def test_diag_blackbox_no_dump_is_placeholder(client, monkeypatch, tmp_path):
    monkeypatch.setenv("DIAG_TOKEN", "s3cret-xyz")
    monkeypatch.setenv("SLEEPCTL_DB", os.path.join(str(tmp_path), "no-blackbox-here.db"))
    r = client.get("/diag/blackbox?token=s3cret-xyz")
    assert r.status_code == 200
    assert r.text == "(no blackbox dump found)"


def test_diag_blackbox_returns_latest_dump_verbatim(client, monkeypatch):
    monkeypatch.setenv("DIAG_TOKEN", "s3cret-xyz")
    from sleepctl.diagnostics_blackbox import BlackBoxRecorder

    run_dir = _run_dir()
    rec = BlackBoxRecorder(run_dir, maxlen=200, keep=5)
    rec.record({"state": "maintenance", "intent": "deep_bias_cool", "target_temp_f": 66.0,
               "hr": 58.0, "commands": []})
    rec.dump_latest()

    r = client.get("/diag/blackbox?token=s3cret-xyz")
    assert r.status_code == 200
    assert "maintenance" in r.text
    assert "deep_bias_cool" in r.text


# ------------------------------------------------------------ live-daemon integration
def test_live_daemon_tick_populates_history_and_blackbox(client, monkeypatch):
    """One live-daemon control tick must (a) append a row to state_history (readable via
    /diag/history) and (b) record a black-box ring-buffer entry that survives a dump (readable
    via /diag/blackbox) -- end-to-end proof the hooks in live_daemon.py are wired up."""
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
    # force the throttle open so this single tick is guaranteed to record (mirrors real
    # first-tick behavior, where _last_history_ts starts at 0.0)
    daemon._last_history_ts = 0.0

    async def go():
        await daemon_client.connect()
        await daemon.control_tick()
    asyncio.new_event_loop().run_until_complete(go())

    # (a) state_history got a row
    r = client.get("/diag/history?token=s3cret-xyz&hours=48")
    assert r.status_code == 200
    assert len(r.json()) >= 1

    # (b) the ring buffer has at least one entry; dump it and confirm /diag/blackbox reads it
    assert len(daemon.blackbox._buf) >= 1
    daemon.blackbox.dump_latest()
    r = client.get("/diag/blackbox?token=s3cret-xyz")
    assert r.status_code == 200
    assert r.text != "(no blackbox dump found)"
