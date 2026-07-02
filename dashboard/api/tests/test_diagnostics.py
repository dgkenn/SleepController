"""Unit + integration tests for the self-diagnosis battery (app/diagnostics.py) and its
wiring into /diag (app/main.py).

Unit tests build a throwaway Repository over a temp SQLite file + a temp ``.run`` dir with
fake heartbeat/log files, so they never touch the shared test DB the other API tests use.
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timedelta, timezone

import pytest

from app import bridge, diagnostics


# ------------------------------------------------------------------ fixtures / helpers
@pytest.fixture()
def repo(tmp_path):
    """A fresh Repository with the dashboard tables applied, isolated per test."""
    from sleepctl.storage.repository import Repository
    from app import db as app_db

    r = Repository(str(tmp_path / "diag_test.db"), check_same_thread=False)
    r.conn.executescript(app_db._DASHBOARD_DDL)
    app_db._apply_migrations(r.conn)
    r.conn.commit()
    yield r
    r.close()


@pytest.fixture()
def run_dir(tmp_path):
    d = tmp_path / ".run"
    d.mkdir()
    return str(d)


def _seed_runtime_state(repo, **extra_overrides) -> None:
    extra = {
        "live": True, "dry_run": False,
        "device": {"online": True, "has_water": True, "priming": False, "needs_priming": False},
        "thermal_health": {"state": "ok", "responding": True, "reason": "at setpoint"},
        "telemetry_stale": False, "data_age_s": 5.0, "device_error": None,
    }
    extra.update(extra_overrides)
    bridge.write_runtime_state(repo.conn, {"state": "COOLING", "extra": extra})


def _touch(path: str, age_s: float = 0.0) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(datetime.now(timezone.utc).isoformat())
    if age_s:
        t = time.time() - age_s
        os.utime(path, (t, t))


def _fresh_heartbeats(run_dir: str) -> None:
    _touch(os.path.join(run_dir, "daemon.heartbeat"))
    _touch(os.path.join(run_dir, "watchdog.heartbeat"))


def _fake_git_repo_root(tmp_path, build_stale: bool = False, missing_build: bool = False) -> str:
    """A minimal fake checkout (.git/HEAD + refs, optional dashboard/web/.next) so the
    ``version`` check can be exercised without touching the real repo this test runs inside
    (which may itself be a worktree with no production build — see diagnostics.py's
    fallback-to-`git`-binary path for why that's handled separately in production)."""
    # idempotent -- callers may invoke this (indirectly, via a monkeypatched _repo_root)
    # more than once per test, since run_diagnostics() re-resolves the repo root every call.
    root = tmp_path / "fakerepo"
    (root / ".git" / "refs" / "heads").mkdir(parents=True, exist_ok=True)
    (root / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (root / ".git" / "refs" / "heads" / "main").write_text("deadbeef1234567890abcdef\n",
                                                            encoding="utf-8")
    if not missing_build:
        web_next = root / "dashboard" / "web" / ".next"
        web_next.mkdir(parents=True, exist_ok=True)
        (web_next / "BUILD_ID").write_text("build123", encoding="utf-8")
        if build_stale:
            ref_mtime = (root / ".git" / "refs" / "heads" / "main").stat().st_mtime
            old = ref_mtime - 999999
            os.utime(web_next / "BUILD_ID", (old, old))
    return str(root)


def _run_full_diagnostics(repo, run_dir, tmp_path, monkeypatch, *, dry_run=False,
                          eight_sleep_creds=True, build_stale=False):
    """Run the full battery with every non-target check forced healthy, so a single test can
    assert on exactly the check it cares about without fighting the environment (no port 3000
    listener, no real EIGHTSLEEP_* creds, no production web build in this checkout, etc)."""
    monkeypatch.setattr(diagnostics, "_port_open", lambda *a, **k: True)
    monkeypatch.setattr(diagnostics, "_repo_root",
                        lambda: _fake_git_repo_root(tmp_path, build_stale=build_stale))
    if eight_sleep_creds:
        monkeypatch.setenv("EIGHTSLEEP_EMAIL", "user@example.com")
        monkeypatch.setenv("EIGHTSLEEP_PASSWORD", "hunter2")
    else:
        monkeypatch.delenv("EIGHTSLEEP_EMAIL", raising=False)
        monkeypatch.delenv("EIGHTSLEEP_PASSWORD", raising=False)
    _fresh_heartbeats(run_dir)
    return diagnostics.run_diagnostics(repo, run_dir=run_dir)


def _by_id(report, check_id):
    return next(c for c in report["checks"] if c["id"] == check_id)


# ------------------------------------------------------------------ never raises
def test_run_diagnostics_never_raises_on_empty_repo(repo, run_dir):
    # No runtime_state row ever written, no log files -- the worst-case "brand new install".
    report = diagnostics.run_diagnostics(repo, run_dir=run_dir)
    assert report["verdict"] in ("HEALTHY", "DEGRADED", "DOWN")
    assert isinstance(report["checks"], list) and report["checks"]


def test_run_diagnostics_never_raises_on_garbage_run_dir(repo):
    # A run_dir that doesn't exist at all must degrade gracefully, not raise.
    report = diagnostics.run_diagnostics(repo, run_dir="/nonexistent/path/does/not/exist")
    assert report["verdict"] in ("HEALTHY", "DEGRADED", "DOWN")


# ------------------------------------------------------------------ individual checks
def test_healthy_path_is_all_green(repo, run_dir, tmp_path, monkeypatch):
    _seed_runtime_state(repo)
    report = _run_full_diagnostics(repo, run_dir, tmp_path, monkeypatch)
    assert report["verdict"] == "HEALTHY", report
    assert report["headline"] == "all systems nominal"
    assert report["primary_remedy"] is None
    statuses = {c["id"]: c["status"] for c in report["checks"]}
    assert statuses["daemon_heartbeat"] == "ok"
    assert statuses["watchdog_heartbeat"] == "ok"
    assert statuses["api"] == "ok"
    assert statuses["web"] == "ok"
    assert statuses["device_water"] == "ok"
    assert statuses["thermal_response"] == "ok"
    assert statuses["live_mode"] == "info"
    assert statuses["eight_sleep_creds"] == "ok"
    assert statuses["version"] == "info"


def test_no_water_is_fail(repo, run_dir, tmp_path, monkeypatch):
    _seed_runtime_state(repo, device={"online": True, "has_water": False, "priming": False})
    report = _run_full_diagnostics(repo, run_dir, tmp_path, monkeypatch)
    c = _by_id(report, "device_water")
    assert c["status"] == "fail"
    assert "prime" in c["remedy"].lower() and "fill" in c["remedy"].lower()
    assert report["verdict"] == "DEGRADED"  # not a DOWN-trigger check


def test_thermal_stalled_is_fail_with_reason(repo, run_dir, tmp_path, monkeypatch):
    _seed_runtime_state(repo, thermal_health={"state": "stalled", "responding": False,
                                              "reason": "bed temp flat for 20 min"})
    report = _run_full_diagnostics(repo, run_dir, tmp_path, monkeypatch)
    c = _by_id(report, "thermal_response")
    assert c["status"] == "fail"
    assert "bed temp flat for 20 min" in c["detail"]
    assert report["verdict"] == "DEGRADED"


def test_dry_run_is_warn(repo, run_dir, tmp_path, monkeypatch):
    _seed_runtime_state(repo, dry_run=True)
    report = _run_full_diagnostics(repo, run_dir, tmp_path, monkeypatch)
    c = _by_id(report, "live_mode")
    assert c["status"] == "warn"
    assert "SLEEPCTL_DRY_RUN" in c["remedy"]
    assert report["verdict"] == "DEGRADED"


def test_stale_daemon_heartbeat_is_fail_and_verdict_down(repo, run_dir, tmp_path, monkeypatch):
    _seed_runtime_state(repo)
    monkeypatch.setattr(diagnostics, "_port_open", lambda *a, **k: True)
    monkeypatch.setattr(diagnostics, "_repo_root", lambda: _fake_git_repo_root(tmp_path))
    monkeypatch.setenv("EIGHTSLEEP_EMAIL", "user@example.com")
    monkeypatch.setenv("EIGHTSLEEP_PASSWORD", "hunter2")
    _touch(os.path.join(run_dir, "watchdog.heartbeat"))
    # daemon heartbeat is 200s stale -> past the 90s threshold
    _touch(os.path.join(run_dir, "daemon.heartbeat"), age_s=200)

    report = diagnostics.run_diagnostics(repo, run_dir=run_dir)
    c = _by_id(report, "daemon_heartbeat")
    assert c["status"] == "fail"
    assert "watchdog" in c["remedy"].lower()
    assert report["verdict"] == "DOWN"
    assert "daemon_heartbeat" in report["headline"] or "heartbeat" in report["headline"].lower()
    assert report["primary_remedy"] == c["remedy"]


def test_missing_daemon_heartbeat_file_is_fail(repo, run_dir, tmp_path, monkeypatch):
    _seed_runtime_state(repo)
    monkeypatch.setattr(diagnostics, "_port_open", lambda *a, **k: True)
    monkeypatch.setattr(diagnostics, "_repo_root", lambda: _fake_git_repo_root(tmp_path))
    _touch(os.path.join(run_dir, "watchdog.heartbeat"))
    # no daemon.heartbeat written at all
    report = diagnostics.run_diagnostics(repo, run_dir=run_dir)
    c = _by_id(report, "daemon_heartbeat")
    assert c["status"] == "fail"
    assert report["verdict"] == "DOWN"


def test_watchdog_stale_is_fail_but_not_down(repo, run_dir, tmp_path, monkeypatch):
    _seed_runtime_state(repo)
    report = _run_full_diagnostics(repo, run_dir, tmp_path, monkeypatch)
    # override just the watchdog heartbeat to be stale after the "fresh" helper ran
    _touch(os.path.join(run_dir, "watchdog.heartbeat"), age_s=120)
    report = diagnostics.run_diagnostics(repo, run_dir=run_dir)
    c = _by_id(report, "watchdog_heartbeat")
    assert c["status"] == "fail"
    # watchdog isn't a DOWN-trigger: the daemon+api can still be fine -> DEGRADED, not DOWN
    assert report["verdict"] == "DEGRADED"


def test_runtime_state_never_reported_is_fail(repo, run_dir, tmp_path, monkeypatch):
    # no bridge.write_runtime_state call at all
    report = _run_full_diagnostics(repo, run_dir, tmp_path, monkeypatch)
    c = _by_id(report, "runtime_state_fresh")
    assert c["status"] == "fail"
    assert "ever been published" in c["detail"].lower()


def test_stale_runtime_state_is_fail(repo, run_dir, tmp_path, monkeypatch):
    old = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    bridge.write_runtime_state(repo.conn, {"state": "IDLE", "extra": {}})
    repo.conn.execute("UPDATE runtime_state SET updated=? WHERE id=1", (old,))
    repo.conn.commit()
    report = _run_full_diagnostics(repo, run_dir, tmp_path, monkeypatch)
    c = _by_id(report, "runtime_state_fresh")
    assert c["status"] == "fail"


def test_missing_eight_sleep_creds_is_warn(repo, run_dir, tmp_path, monkeypatch):
    _seed_runtime_state(repo)
    report = _run_full_diagnostics(repo, run_dir, tmp_path, monkeypatch, eight_sleep_creds=False)
    c = _by_id(report, "eight_sleep_creds")
    assert c["status"] == "warn"
    assert "SIMULATOR" in c["remedy"]


def test_web_port_down_is_warn(repo, run_dir, tmp_path, monkeypatch):
    _seed_runtime_state(repo)
    monkeypatch.setattr(diagnostics, "_port_open", lambda *a, **k: False)
    monkeypatch.setattr(diagnostics, "_repo_root", lambda: _fake_git_repo_root(tmp_path))
    monkeypatch.setenv("EIGHTSLEEP_EMAIL", "user@example.com")
    monkeypatch.setenv("EIGHTSLEEP_PASSWORD", "hunter2")
    _fresh_heartbeats(run_dir)
    report = diagnostics.run_diagnostics(repo, run_dir=run_dir)
    c = _by_id(report, "web")
    assert c["status"] == "warn"
    assert report["verdict"] == "DEGRADED"


def test_stale_web_build_is_warn(repo, run_dir, tmp_path, monkeypatch):
    _seed_runtime_state(repo)
    report = _run_full_diagnostics(repo, run_dir, tmp_path, monkeypatch, build_stale=True)
    c = _by_id(report, "version")
    assert c["status"] == "warn"
    assert "rebuild the UI" in c["remedy"]


def test_missing_web_build_is_warn(repo, run_dir, tmp_path, monkeypatch):
    _seed_runtime_state(repo)
    monkeypatch.setattr(diagnostics, "_port_open", lambda *a, **k: True)
    monkeypatch.setattr(diagnostics, "_repo_root",
                        lambda: _fake_git_repo_root(tmp_path, missing_build=True))
    _fresh_heartbeats(run_dir)
    report = diagnostics.run_diagnostics(repo, run_dir=run_dir)
    c = _by_id(report, "version")
    assert c["status"] == "warn"
    assert ".next" in c["detail"]


def test_cloud_errors_detected_in_daemon_log(repo, run_dir, tmp_path, monkeypatch):
    _seed_runtime_state(repo)
    with open(os.path.join(run_dir, "daemon.log"), "w", encoding="utf-8") as fh:
        for _ in range(3):
            fh.write("2026-07-02 RequestError: 504 Gateway Timeout talking to Eight Sleep\n")
        fh.write("2026-07-02 control_tick ok\n")
    report = _run_full_diagnostics(repo, run_dir, tmp_path, monkeypatch)
    c = _by_id(report, "cloud_errors")
    assert c["status"] == "warn"
    assert "3 cloud/timeout error" in c["detail"]


def test_recent_errors_surfaces_crash_log(repo, run_dir, tmp_path, monkeypatch):
    _seed_runtime_state(repo)
    with open(os.path.join(run_dir, "daemon-crash.log"), "w", encoding="utf-8") as fh:
        fh.write("2026-07-02T00:00:00 run() raised RuntimeError: boom\n")
    report = _run_full_diagnostics(repo, run_dir, tmp_path, monkeypatch)
    c = _by_id(report, "recent_errors")
    assert c["status"] == "fail"
    assert "boom" in c["detail"]


def test_log_sizes_warns_when_runaway(repo, run_dir, tmp_path, monkeypatch):
    _seed_runtime_state(repo)
    big = os.path.join(run_dir, "daemon.log")
    with open(big, "wb") as fh:
        fh.seek(60 * 1024 * 1024 - 1)
        fh.write(b"\0")
    report = _run_full_diagnostics(repo, run_dir, tmp_path, monkeypatch)
    c = _by_id(report, "log_sizes")
    assert c["status"] == "warn"
    assert "daemon.log" in c["remedy"]


def test_priming_states(repo, run_dir, tmp_path, monkeypatch):
    _seed_runtime_state(repo, device={"online": True, "has_water": True, "priming": True})
    report = _run_full_diagnostics(repo, run_dir, tmp_path, monkeypatch)
    assert _by_id(report, "priming")["status"] == "warn"

    _seed_runtime_state(repo, device={"online": True, "has_water": True, "priming": False,
                                      "needs_priming": True})
    report = _run_full_diagnostics(repo, run_dir, tmp_path, monkeypatch)
    assert _by_id(report, "priming")["status"] == "warn"


def test_device_offline_is_fail(repo, run_dir, tmp_path, monkeypatch):
    _seed_runtime_state(repo, device={"online": False, "has_water": True})
    report = _run_full_diagnostics(repo, run_dir, tmp_path, monkeypatch)
    assert _by_id(report, "device_online")["status"] == "fail"


def test_render_diagnosis_text_orders_fails_first(repo, run_dir, tmp_path, monkeypatch):
    _seed_runtime_state(repo, device={"online": True, "has_water": False, "priming": False},
                        dry_run=True)
    report = _run_full_diagnostics(repo, run_dir, tmp_path, monkeypatch)
    text = diagnostics.render_diagnosis_text(report)
    assert text.startswith("=== DIAGNOSIS: DEGRADED ===")
    assert "! " in text and "-> " in text
    fail_idx = text.index("[FAIL]")
    warn_idx = text.index("[WARN]")
    ok_idx = text.index("[OK")
    assert fail_idx < warn_idx < ok_idx


def test_all_expected_checks_present(repo, run_dir, tmp_path, monkeypatch):
    _seed_runtime_state(repo)
    report = _run_full_diagnostics(repo, run_dir, tmp_path, monkeypatch)
    expected = {
        "version", "daemon_heartbeat", "watchdog_heartbeat", "api", "web",
        "runtime_state_fresh", "device_water", "device_online", "priming",
        "thermal_response", "live_mode", "cloud_errors", "recent_errors",
        "eight_sleep_creds", "calendar", "shift", "log_sizes",
    }
    assert expected <= {c["id"] for c in report["checks"]}
    for c in report["checks"]:
        assert c["status"] in ("ok", "warn", "fail", "info")
        assert isinstance(c["detail"], str) and c["detail"]


# ------------------------------------------------------------------ /diag wiring (API)
def test_diag_json_format_returns_full_dict(client, monkeypatch):
    monkeypatch.setenv("DIAG_TOKEN", "s3cret-xyz")
    from app.db import get_repo
    r = get_repo()
    try:
        _seed_runtime_state(r)
    finally:
        r.close()
    resp = client.get("/diag?token=s3cret-xyz&format=json")
    assert resp.status_code == 200
    body = resp.json()
    assert body["verdict"] in ("HEALTHY", "DEGRADED", "DOWN")
    assert "checks" in body and isinstance(body["checks"], list)
    assert "headline" in body and "generated_at" in body


def test_diag_plaintext_has_diagnosis_block(client, monkeypatch):
    monkeypatch.setenv("DIAG_TOKEN", "s3cret-xyz")
    from app.db import get_repo
    r = get_repo()
    try:
        _seed_runtime_state(r)
    finally:
        r.close()
    resp = client.get("/diag?token=s3cret-xyz")
    assert resp.status_code == 200
    assert "=== DIAGNOSIS" in resp.text
    assert "=== STATUS ===" in resp.text  # existing section still present, unchanged
    assert "daemon.log" in resp.text


def test_diag_json_still_404s_without_token(client, monkeypatch):
    monkeypatch.delenv("DIAG_TOKEN", raising=False)
    assert client.get("/diag?format=json").status_code == 404
    monkeypatch.setenv("DIAG_TOKEN", "s3cret-xyz")
    assert client.get("/diag?format=json&token=nope").status_code == 404


# ------------------------------------------------------------------ /diagnostics (web-facing, auth-gated)
def test_diagnostics_requires_auth(client):
    from fastapi.testclient import TestClient
    from app.main import app
    assert TestClient(app).get("/diagnostics").status_code == 401


def test_diagnostics_returns_verdict_and_checks(auth_client):
    from app.db import get_repo
    r = get_repo()
    try:
        _seed_runtime_state(r)
    finally:
        r.close()
    resp = auth_client.get("/diagnostics")
    assert resp.status_code == 200
    body = resp.json()
    assert body["verdict"] in ("HEALTHY", "DEGRADED", "DOWN")
    assert "checks" in body and isinstance(body["checks"], list) and body["checks"]
    assert "generated_at" in body
    for c in body["checks"]:
        assert c["status"] in ("ok", "warn", "fail", "info")


def test_diagnostics_events_requires_auth(client):
    from fastapi.testclient import TestClient
    from app.main import app
    assert TestClient(app).get("/diagnostics/events").status_code == 401


def test_diagnostics_events_returns_list(auth_client):
    resp = auth_client.get("/diagnostics/events?limit=10")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)
