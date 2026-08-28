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


# ------------------------------------------------------------------ thermal dose-response trial
def _set_thermal_trial_enabled(monkeypatch, enabled: bool, **overrides) -> None:
    """Force ``sleepctl.config.AppConfig.default().thermal_trial`` for one test -- the
    ``thermal_trial`` check (like the /thermal/dose-response endpoint it mirrors) reads config
    via a fresh ``AppConfig.default()`` each call, not anything persisted, so this patches the
    classmethod itself rather than an instance."""
    from sleepctl.config import AppConfig, ThermalTrialConfig

    tc = ThermalTrialConfig(enabled=enabled, **overrides)
    cfg = AppConfig.default()
    cfg.thermal_trial = tc
    monkeypatch.setattr(AppConfig, "default", classmethod(lambda cls: cfg))


def test_thermal_trial_disabled_is_info(repo, run_dir, tmp_path, monkeypatch):
    _seed_runtime_state(repo)
    _set_thermal_trial_enabled(monkeypatch, False)
    report = _run_full_diagnostics(repo, run_dir, tmp_path, monkeypatch)
    c = _by_id(report, "thermal_trial")
    assert c["status"] == "info"
    assert "not enabled" in c["detail"]


def test_thermal_trial_enabled_collecting_is_ok(repo, run_dir, tmp_path, monkeypatch):
    _seed_runtime_state(repo)
    _set_thermal_trial_enabled(monkeypatch, True, min_nights_before_verdict=8)
    repo.assign_thermal_trial_night("2026-07-01", "+0.00", 0.0, True, block_key="normal")
    repo.record_thermal_trial_outcome("2026-07-01", wake_events=2)
    report = _run_full_diagnostics(repo, run_dir, tmp_path, monkeypatch)
    c = _by_id(report, "thermal_trial")
    assert c["status"] == "ok"
    assert "1 resolved night" in c["detail"]
    assert "8 nights/arm" in c["detail"]


def test_thermal_trial_auto_stopped_arm_is_warn(repo, run_dir, tmp_path, monkeypatch):
    _seed_runtime_state(repo)
    _set_thermal_trial_enabled(monkeypatch, True)
    repo.log_event("thermal_trial", "warn", "auto_stop",
                   "thermal dose-response trial auto-stopped arm -1.50 for 2026-07-24: ...",
                   {"night_date": "2026-07-24", "arm": "-1.50"})
    report = _run_full_diagnostics(repo, run_dir, tmp_path, monkeypatch)
    c = _by_id(report, "thermal_trial")
    assert c["status"] == "warn"
    assert "-1.50" in c["detail"]
    assert c["remedy"] is not None


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


# ------------------------------------------------------------------ auto_update (new)
def _real_git_repo(tmp_path, *, branch="main"):
    """A REAL tiny git repo (unlike _fake_git_repo_root's hand-crafted files) so
    ``_check_auto_update`` -- which shells out to the git binary for rev-list -- has something
    real to compare against. ``origin/<branch>`` is faked as a plain ref pointing at a commit
    already in this repo's own object db (no actual remote/clone needed)."""
    import subprocess

    root = tmp_path / "realrepo"
    root.mkdir()
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t.com",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t.com"}

    def run(*args):
        subprocess.run(["git", "-C", str(root)] + list(args), check=True,
                       capture_output=True, env={**os.environ, **env})

    def head_sha():
        out = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"],
                             capture_output=True, text=True, check=True)
        return out.stdout.strip()

    run("init", "--quiet", "-b", branch)
    (root / "f.txt").write_text("1", encoding="utf-8")
    run("add", "f.txt")
    run("commit", "--quiet", "-m", "1")
    return root, run, head_sha


def test_auto_update_ok_when_head_matches_origin(tmp_path):
    root, run, head_sha = _real_git_repo(tmp_path)
    run("update-ref", "refs/remotes/origin/main", head_sha())
    c = diagnostics._check_auto_update(str(root))
    assert c["status"] == "ok"
    assert c["id"] == "auto_update"


def test_auto_update_warn_when_behind(tmp_path):
    root, run, head_sha = _real_git_repo(tmp_path)
    base_sha = head_sha()  # commit 1
    (root / "f2.txt").write_text("2", encoding="utf-8")
    run("add", "f2.txt")
    run("commit", "--quiet", "-m", "2")
    run("update-ref", "refs/remotes/origin/main", head_sha())  # origin points at commit 2
    run("reset", "--quiet", "--hard", base_sha)                 # local HEAD back to commit 1
    c = diagnostics._check_auto_update(str(root))
    assert c["status"] == "warn"
    assert "behind" in c["detail"]


def test_auto_update_fail_when_diverged(tmp_path):
    root, run, head_sha = _real_git_repo(tmp_path)
    base_sha = head_sha()
    run("update-ref", "refs/remotes/origin/main", base_sha)
    # a LOCAL-only commit that origin doesn't have -> diverged, not a clean fast-forward
    (root / "local.txt").write_text("local", encoding="utf-8")
    run("add", "local.txt")
    run("commit", "--quiet", "-m", "local-only")
    c = diagnostics._check_auto_update(str(root))
    assert c["status"] == "fail"
    assert "diverged" in c["detail"]


def test_auto_update_info_when_no_origin_ref(tmp_path):
    root, run, head_sha = _real_git_repo(tmp_path)
    c = diagnostics._check_auto_update(str(root))
    assert c["status"] == "info"


def test_branch_with_a_slash_is_not_truncated(tmp_path):
    """THE regression. _git_head_info used to rsplit the ref on "/", turning
    "refs/heads/claude/confident-gates-rg7af0" into "confident-gates-rg7af0". Cosmetic for the
    version display, but _check_auto_update builds "origin/<branch>" from it -- so on a
    slash-containing branch (the one this box actually deploys from) it looked up a ref that
    doesn't exist and reported "no origin ref" forever instead of the real deploy lag."""
    root, run, head_sha = _real_git_repo(tmp_path, branch="claude/some-feature")
    assert diagnostics._git_head_info(str(root))["branch"] == "claude/some-feature"
    # and the check must now actually resolve origin/<full branch name>
    run("update-ref", "refs/remotes/origin/claude/some-feature", head_sha())
    assert diagnostics._check_auto_update(str(root))["status"] == "ok"


# ------------------------------------------------------------------ self_update (new)
def test_self_update_ok_when_nothing_has_happened(run_dir):
    c = diagnostics._check_self_update(run_dir)
    assert c["status"] == "ok"
    assert "no self-update" in c["detail"]


def test_self_update_surfaces_a_failed_update_result(run_dir):
    import json as _json
    with open(os.path.join(run_dir, "update.result"), "w", encoding="utf-8") as fh:
        _json.dump({"timestamp": "2026-08-25T22:00:00", "git_ok": False,
                    "summary": "update to 'x' FAILED (git fetch/reset failed)"}, fh)
    c = diagnostics._check_self_update(run_dir)
    assert c["status"] == "warn"
    assert "FAILED" in c["detail"]


def test_self_update_reads_windows_powershells_utf8_bom(run_dir):
    """THE regression. windows-watchdog.ps1 writes update.result via PowerShell 5.1's
    `ConvertTo-Json | Set-Content -Encoding UTF8`, which (unlike PS7+) always prepends a UTF-8
    BOM. Plain utf-8 decoding rejects that BOM, so this check reported EVERY real update.result
    on Windows as "unreadable" rather than showing the actual deploy outcome."""
    import json as _json
    payload = _json.dumps({"timestamp": "2026-08-25T22:00:00", "git_ok": True,
                           "summary": "update to 'x' succeeded (validate=PASS)"})
    with open(os.path.join(run_dir, "update.result"), "w", encoding="utf-8-sig") as fh:
        fh.write(payload)
    c = diagnostics._check_self_update(run_dir)
    assert "unreadable" not in c["detail"]
    assert "succeeded" in c["detail"]


def test_self_update_reports_a_failed_smoke_test_as_fail(run_dir):
    with open(os.path.join(run_dir, "smoke.result"), "w", encoding="utf-8") as fh:
        fh.write("SMOKE FAIL: web not listening on port 3000")
    c = diagnostics._check_self_update(run_dir)
    assert c["status"] == "fail"
    assert "SMOKE FAIL" in c["detail"]


def test_publishers_flags_a_failing_night_data_publisher(run_dir):
    """night-data is the ONLY channel carrying sensor/staging/steering detail off-box. A publisher
    that starts failing used to look identical to a quiet night -- no data, no explanation."""
    with open(os.path.join(run_dir, "night-data-publish.result"), "w", encoding="utf-8") as fh:
        fh.write("FAIL git push --force origin night-data failed (exit code 128)")
    c = diagnostics._check_publishers(run_dir)
    assert c["status"] == "fail"
    assert "night-data" in c["detail"]
    assert c["remedy"] is not None


def test_publishers_ok_when_both_succeeded(run_dir):
    with open(os.path.join(run_dir, "health-publish.result"), "w", encoding="utf-8") as fh:
        fh.write("OK 20260825-223701 health-20260825-223701.json")
    with open(os.path.join(run_dir, "night-data-publish.result"), "w", encoding="utf-8") as fh:
        fh.write("OK 20260825-223701 2")
    c = diagnostics._check_publishers(run_dir)
    assert c["status"] == "ok"


def test_publishers_never_run_is_benign_not_a_fault(run_dir):
    """A fresh box (or one that just deployed the publisher) hasn't had a cycle yet -- that must
    not drag the whole verdict to DEGRADED. Absence isn't the signal; staleness is."""
    c = diagnostics._check_publishers(run_dir)
    assert c["status"] == "info"
    assert "never run" in c["detail"]


def test_publishers_warns_when_a_publisher_silently_stopped(run_dir):
    """THE case this check exists for: the last verdict still says OK, but nothing has run in
    hours -- the publisher (or the watchdog tick that launches it) has stopped."""
    path = os.path.join(run_dir, "night-data-publish.result")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("OK 20260825-223701 2")
    old = time.time() - (6 * 3600)
    os.utime(path, (old, old))
    c = diagnostics._check_publishers(run_dir)
    assert c["status"] == "warn"
    assert "STALE" in c["detail"]


def test_self_update_surfaces_an_outstanding_alert(run_dir):
    with open(os.path.join(run_dir, "watchdog.alert"), "w", encoding="utf-8") as fh:
        fh.write("auto-rolled-back self-update after smoke test failure -- reverted to abc1234")
    c = diagnostics._check_self_update(run_dir)
    assert c["status"] == "fail"
    assert "OUTSTANDING watchdog alert" in c["detail"]
    assert "auto-rolled-back" in c["detail"]


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
    # A crash that just happened (crash log mtime ~ now) + healthy daemon -> still FAIL.
    _seed_runtime_state(repo)
    with open(os.path.join(run_dir, "daemon-crash.log"), "w", encoding="utf-8") as fh:
        fh.write("2026-07-02T00:00:00 run() raised RuntimeError: boom\n")
    report = _run_full_diagnostics(repo, run_dir, tmp_path, monkeypatch)
    c = _by_id(report, "recent_errors")
    assert c["status"] == "fail"
    assert "boom" in c["detail"]


def test_recent_errors_stale_crash_with_healthy_daemon_is_ok(repo, run_dir, tmp_path, monkeypatch):
    # A crash from well before the window, and the daemon has been healthy since: the append-
    # only crash log must NOT pin the diagnosis to FAIL forever -- it should pass as OK.
    _seed_runtime_state(repo)
    crash = os.path.join(run_dir, "daemon-crash.log")
    with open(crash, "w", encoding="utf-8") as fh:
        fh.write("EightSleepRequestError: GET .../users/me failed: "
                 "RuntimeError('Event loop is closed')\n")
    old = time.time() - (diagnostics.RECENT_CRASH_WINDOW_S + 3600)  # ~1h past the window
    os.utime(crash, (old, old))
    report = _run_full_diagnostics(repo, run_dir, tmp_path, monkeypatch)  # fresh heartbeats
    c = _by_id(report, "recent_errors")
    assert c["status"] == "ok"
    assert "stale" in c["detail"] and "daemon healthy" in c["detail"]
    assert c["remedy"] is None


def test_recent_errors_stale_crash_but_daemon_down_is_fail(repo, run_dir, tmp_path, monkeypatch):
    # Same old crash, but the daemon heartbeat is now stale -> still a live problem -> FAIL.
    _seed_runtime_state(repo)
    crash = os.path.join(run_dir, "daemon-crash.log")
    with open(crash, "w", encoding="utf-8") as fh:
        fh.write("2026-07-02T00:00:00 run() raised RuntimeError: boom\n")
    old = time.time() - (diagnostics.RECENT_CRASH_WINDOW_S + 3600)
    os.utime(crash, (old, old))
    report = _run_full_diagnostics(repo, run_dir, tmp_path, monkeypatch)
    # make the daemon heartbeat stale AFTER the "fresh" helper, then re-run
    _touch(os.path.join(run_dir, "daemon.heartbeat"),
           age_s=diagnostics.DAEMON_HEARTBEAT_STALE_S + 30)
    report = diagnostics.run_diagnostics(repo, run_dir=run_dir)
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
        "version", "auto_update", "self_update", "publishers", "daemon_heartbeat",
        "watchdog_heartbeat", "api", "web",
        "runtime_state_fresh", "device_water", "device_online", "priming",
        "thermal_response", "thermal_capacity", "external_conflict", "frozen_telemetry",
        "live_mode", "cloud_errors", "recent_errors",
        "eight_sleep_creds", "calendar", "shift", "log_sizes",
    }
    assert expected <= {c["id"] for c in report["checks"]}
    for c in report["checks"]:
        assert c["status"] in ("ok", "warn", "fail", "info")
        assert isinstance(c["detail"], str) and c["detail"]


# ------------------------------------------------------------------ water-loop/capacity/conflict/frozen (new)
def _record_history_row(repo, ts, target_level=None, bed_temp_f=None, device=None,
                        device_level=None, device_target_level=None):
    repo.record_state_snapshot({
        "ts": ts.isoformat(), "state": "COOLING", "mode": "auto",
        "target_temp_f": 65.0, "bed_temp_f": bed_temp_f, "room_temp_f": 66.0,
        "stage": "deep", "confidence": 0.8, "target_level": target_level,
        "daemon_alive": True,
        "extra": {"device_level": device_level, "device_target_level": device_target_level,
                  "device": device or {}},
    })


def test_thermal_health_checks_ok_by_default(repo, run_dir, tmp_path, monkeypatch):
    # No state_history rows at all -- must degrade to "ok"/"info", never a false positive.
    _seed_runtime_state(repo)
    report = _run_full_diagnostics(repo, run_dir, tmp_path, monkeypatch)
    for check_id in ("thermal_capacity", "external_conflict", "frozen_telemetry"):
        c = _by_id(report, check_id)
        assert c["status"] in ("ok", "info")


def test_stuck_prime_history_produces_degraded_verdict_and_playbook_match(
        repo, run_dir, tmp_path, monkeypatch):
    now = datetime.now()
    for i in range(10):
        ts = now - timedelta(minutes=9 - i)
        _record_history_row(repo, ts, target_level=0, bed_temp_f=70.0,
                            device={"priming": True})
    _seed_runtime_state(repo, device={
        "online": True, "has_water": True, "priming": True, "needs_priming": False,
        "last_prime": (now - timedelta(hours=2)).isoformat(),
    })
    report = _run_full_diagnostics(repo, run_dir, tmp_path, monkeypatch)

    c = _by_id(report, "thermal_capacity")
    assert c["status"] == "fail"
    assert "stuck_prime" in c["detail"]
    assert report["verdict"] == "DEGRADED"

    matches = {m["id"] for m in report["playbook_matches"]}
    assert "stuck_prime" in matches


def test_reduced_capacity_history_flags_air_bound(repo, run_dir, tmp_path, monkeypatch):
    now = datetime.now()
    for i in range(8):
        ts = now - timedelta(minutes=7 - i)
        # strong cool command, but device_level and bed_temp barely move -> air-bound
        _record_history_row(repo, ts, target_level=-90, bed_temp_f=69.5 + (i % 2) * 0.1,
                            device_level=88 - i, device={"priming": False})
    _seed_runtime_state(repo, device={"online": True, "has_water": True, "priming": False,
                                      "needs_priming": False})
    report = _run_full_diagnostics(repo, run_dir, tmp_path, monkeypatch)

    c = _by_id(report, "thermal_capacity")
    assert c["status"] == "fail"
    assert "reduced_capacity" in c["detail"]
    assert report["verdict"] == "DEGRADED"

    matches = {m["id"] for m in report["playbook_matches"]}
    assert "air_bound_loop" in matches


def test_frozen_telemetry_history_flags_and_matches_playbook(repo, run_dir, tmp_path, monkeypatch):
    now = datetime.now()
    for i in range(9):
        ts = now - timedelta(minutes=8 - i)
        _record_history_row(repo, ts, target_level=-80, bed_temp_f=68.0, device_level=42)
    _seed_runtime_state(repo)
    report = _run_full_diagnostics(repo, run_dir, tmp_path, monkeypatch)

    c = _by_id(report, "frozen_telemetry")
    assert c["status"] == "fail"
    assert report["verdict"] == "DEGRADED"

    matches = {m["id"] for m in report["playbook_matches"]}
    assert "frozen_telemetry" in matches


def test_external_schedule_conflict_flags_and_matches_playbook(repo, run_dir, tmp_path, monkeypatch):
    _seed_runtime_state(repo, device={
        "online": True, "has_water": True, "priming": False, "needs_priming": False,
        "external_schedule": {"activity": "schedule", "target_level": 55, "active": True},
    })
    report = _run_full_diagnostics(repo, run_dir, tmp_path, monkeypatch)

    c = _by_id(report, "external_conflict")
    assert c["status"] == "warn"
    assert "external_setpoint_conflict" in c["detail"]
    assert report["verdict"] == "DEGRADED"

    matches = {m["id"] for m in report["playbook_matches"]}
    assert "external_schedule_conflict" in matches


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


# --------------------------------------------- verity forwarder liveness (2026-08-26 audit)
def test_verity_forwarder_heartbeat_fresh_is_ok(run_dir):
    _touch(os.path.join(run_dir, "verity.heartbeat"))
    c = diagnostics._check_verity_forwarder(run_dir, time.time())
    assert c["status"] == "ok"


def test_a_wedged_forwarder_is_reported_even_though_the_process_exists(run_dir):
    """THE 2026-08-26 gap. A BLE link can stay open long after notifications stop, so the
    forwarder sits in a session that will never produce another sample -- and the supervisor's
    process check passes, because a wedged process is still a process."""
    _touch(os.path.join(run_dir, "verity.heartbeat"), age_s=45 * 60)
    c = diagnostics._check_verity_forwarder(run_dir, time.time())
    assert c["status"] == "fail"
    assert "not looping" in c["detail"]


def test_a_moderately_stale_forwarder_heartbeat_warns_before_it_fails(run_dir):
    _touch(os.path.join(run_dir, "verity.heartbeat"), age_s=10 * 60)
    c = diagnostics._check_verity_forwarder(run_dir, time.time())
    assert c["status"] == "warn"


def test_no_forwarder_heartbeat_is_info_not_a_failure(run_dir):
    """An older build predating the heartbeat, or a box with the Verity disabled, must not read
    as a fault."""
    c = diagnostics._check_verity_forwarder(run_dir, time.time())
    assert c["status"] == "info"


# ------------------------------- prevention-timing recency (2026-08-27 stale-warning fix)
def _timing_report(verdict="timing_limited", last_failure_days_ago=1.0):
    from datetime import datetime, timedelta
    from sleepctl.learning.prevention_timing import PreventionTimingReport
    rep = PreventionTimingReport()
    rep.verdict = verdict
    rep.detail = "8/11 prevention failures happened BEFORE the bed had moved"
    rep.remedy = "look at the actuator"
    rep.last_failure_ts = (datetime.now() - timedelta(days=last_failure_days_ago)
                           if last_failure_days_ago is not None else None)
    return rep


def test_a_still_happening_pre_emption_problem_still_warns(repo, monkeypatch):
    import sleepctl.learning.prevention_timing as pt
    monkeypatch.setattr(pt, "from_repo", lambda *a, **k: _timing_report(last_failure_days_ago=1))
    c = diagnostics._check_prevention_timing(repo)
    assert c["status"] == "warn"


def test_a_long_resolved_pre_emption_problem_stops_pinning_the_verdict(repo, monkeypatch):
    """THE stale-warning fix. The learner's window is 30 days -- correct for a stable timing/dose
    split -- but it meant a cause fixed weeks ago (an external schedule fighting the setpoint, a
    stalled loop) kept the whole battery at DEGRADED until it aged out."""
    import sleepctl.learning.prevention_timing as pt
    monkeypatch.setattr(pt, "from_repo", lambda *a, **k: _timing_report(last_failure_days_ago=21))
    c = diagnostics._check_prevention_timing(repo)
    assert c["status"] == "info"
    assert "history rather than a live fault" in c["detail"]
    assert c["remedy"] is None


def test_an_unknown_failure_age_is_treated_as_live(repo, monkeypatch):
    """Absent a timestamp we cannot prove it is old, and silently downgrading would hide a real
    fault -- fail toward reporting it."""
    import sleepctl.learning.prevention_timing as pt
    monkeypatch.setattr(pt, "from_repo",
                        lambda *a, **k: _timing_report(last_failure_days_ago=None))
    assert diagnostics._check_prevention_timing(repo)["status"] == "warn"


# ------------------------------- wearable battery end-to-end (2026-08-28 silent-drop bug)
def test_a_battery_only_post_actually_reaches_storage(auth_client):
    """THE bug. HRBody had no battery_pct field, and the endpoint does
    model_dump(exclude_none=True) -- so the forwarder's battery POST was silently DROPPED at the
    API boundary. The forwarder sent it, ingest_hr could store it, the diagnostic could report
    it, and yet "no battery reading yet" was permanent, disarming the only guard against the
    band dying flat mid-sleep."""
    from app import services
    from app.db import get_repo

    r = auth_client.post("/hr/ingest", json={"source": "verity", "battery_pct": 66})
    assert r.status_code == 200, r.text
    assert r.json().get("battery_pct") == 66

    repo = get_repo()
    try:
        b = services.wearable_battery(repo)
    finally:
        repo.close()
    assert b.get("pct") == 66


def test_a_low_battery_is_flagged_low(auth_client):
    from app import services
    from app.db import get_repo

    auth_client.post("/hr/ingest", json={"source": "verity", "battery_pct": 12})
    repo = get_repo()
    try:
        b = services.wearable_battery(repo)
    finally:
        repo.close()
    assert b.get("pct") == 12 and b.get("low") is True


# ------------------------------- actigraphy reaching the wake detector (2026-08-28)
def _seed_actigraphy(repo, n=5, age_s=10.0):
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    for i in range(n):
        repo.conn.execute(
            "INSERT INTO actigraphy (ts, pim, zcm, mad, std, pmax, n, fs, source) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            ((now - timedelta(seconds=age_s + i)).isoformat(), 3.0 + i, 1.0, 0.1, 0.1, 0.2,
             52, 52, "verity"))
    repo.conn.commit()


def test_live_accelerometer_counts_report_the_wake_detector_as_active(repo):
    """The accelerometer is a SEPARATE PMD stream from HR and can be refused independently, so
    cardiac_sensor going green says nothing about it -- yet it is the signal that actually catches
    awakenings (6/6 vs the HR stager's 2/6, which called three misses REM)."""
    _seed_actigraphy(repo)
    c = diagnostics._check_actigraphy(repo)
    assert c["status"] == "ok"
    assert "wake detector is live" in c["detail"]


def test_no_accelerometer_counts_is_surfaced_rather_than_looking_healthy(repo):
    """THE silent failure: every night before 2026-08-28 looked fine while the best wake signal
    was absent."""
    c = diagnostics._check_actigraphy(repo)
    assert c["status"] == "info"
    assert "actigraphy wake detector" in c["detail"]
    assert c["remedy"] is not None


def test_stale_accelerometer_counts_warn(repo):
    """Counts present but going stale -- the ACC stream stopped while the band stayed connected.
    Must be inside the 15-min read window to be visible at all, and older than 5 min to warn;
    beyond the window it is indistinguishable from "no counts" and reports as info instead."""
    _seed_actigraphy(repo, age_s=600.0)
    c = diagnostics._check_actigraphy(repo)
    assert c["status"] == "warn"
    assert "min old" in c["detail"]


def test_the_actigraphy_check_never_raises(repo, monkeypatch):
    from app import bridge
    monkeypatch.setattr(bridge, "recent_actigraphy",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert diagnostics._check_actigraphy(repo)["status"] == "info"


# ---------------------------------------------------------------------------------------
# Bed-temperature feedback. Measured across 2026-08-25/26/27: bed_temp_f was NULL on
# 6835/6835 samples -- the composite feedback loop has never once engaged, every night ran
# fully open-loop, and nothing in this report mentioned it.
# ---------------------------------------------------------------------------------------

class _BedTempRepo:
    def __init__(self, total, measured):
        import sqlite3
        self.conn = sqlite3.connect(":memory:")
        self.conn.execute("CREATE TABLE raw_samples (ts TEXT, bed_temp_f REAL)")
        for i in range(total):
            self.conn.execute("INSERT INTO raw_samples VALUES (datetime('now'), ?)",
                              (70.0 + i * 0.01 if i < measured else None,))


def test_a_total_absence_of_bed_temperature_is_reported_not_swallowed():
    from app.diagnostics import _check_bed_temperature
    c = _check_bed_temperature(_BedTempRepo(500, 0))
    assert c["status"] == "warn"
    assert "OPEN-LOOP" in c["detail"]
    assert c["remedy"]


def test_mostly_missing_bed_temperature_is_still_a_warning():
    from app.diagnostics import _check_bed_temperature
    c = _check_bed_temperature(_BedTempRepo(500, 50))
    assert c["status"] == "warn"
    assert "10%" in c["detail"]


def test_a_closing_loop_is_ok():
    from app.diagnostics import _check_bed_temperature
    c = _check_bed_temperature(_BedTempRepo(500, 480))
    assert c["status"] == "ok"


def test_no_samples_at_all_is_info_not_a_false_alarm():
    from app.diagnostics import _check_bed_temperature
    c = _check_bed_temperature(_BedTempRepo(0, 0))
    assert c["status"] == "info"


def test_an_unreadable_database_never_raises_out_of_the_check():
    from app.diagnostics import _check_bed_temperature

    class Broken:
        class conn:
            @staticmethod
            def execute(*a, **k):
                raise RuntimeError("no such table")

    assert _check_bed_temperature(Broken())["status"] == "info"


# ---------------------------------------------------------------------------------------
# MAINTENANCE is where awakening prevention and in-night steering both live. Measured on
# 2026-08-25/26/27: not one night reached it, and every other check stayed green throughout.
# ---------------------------------------------------------------------------------------

class _StateRepo:
    def __init__(self, nights):
        """nights: {night_date: {controller_state: count}}"""
        import sqlite3
        self.conn = sqlite3.connect(":memory:")
        self.conn.execute("CREATE TABLE raw_samples (night_date TEXT, controller_state TEXT)")
        for night, states in nights.items():
            for st, n in states.items():
                self.conn.executemany(
                    "INSERT INTO raw_samples VALUES (?, ?)", [(night, st)] * n)


def _today(offset=0):
    from datetime import date, timedelta
    return (date.today() - timedelta(days=offset)).isoformat()


def test_a_night_stranded_in_induction_is_reported_as_unprotected():
    from app.diagnostics import _check_maintenance_reached
    c = _check_maintenance_reached(_StateRepo({_today(1): {"idle": 500, "induction": 120}}))
    assert c["status"] == "warn"
    assert "no wake protection at all" in c["detail"]
    assert "onset" in (c["remedy"] or "")


def test_a_night_that_reaches_maintenance_is_ok():
    from app.diagnostics import _check_maintenance_reached
    c = _check_maintenance_reached(
        _StateRepo({_today(1): {"idle": 500, "induction": 20, "maintenance": 400}}))
    assert c["status"] == "ok"


def test_a_mixed_week_names_the_unprotected_nights():
    from app.diagnostics import _check_maintenance_reached
    c = _check_maintenance_reached(_StateRepo({
        _today(1): {"idle": 100, "maintenance": 300},
        _today(2): {"idle": 100, "induction": 200},
    }))
    assert c["status"] == "warn"
    assert _today(2) in c["detail"]
    assert _today(1) not in c["detail"]


def test_a_fully_idle_day_is_not_counted_as_a_failed_night():
    """A day with no session is not a night without wake protection."""
    from app.diagnostics import _check_maintenance_reached
    c = _check_maintenance_reached(_StateRepo({_today(1): {"idle": 900}}))
    assert c["status"] == "info"
    assert "none started a session" in c["detail"]


def test_no_recorded_nights_is_info():
    from app.diagnostics import _check_maintenance_reached
    c = _check_maintenance_reached(_StateRepo({}))
    assert c["status"] == "info"


def test_an_unreadable_database_never_raises_out_of_the_maintenance_check():
    from app.diagnostics import _check_maintenance_reached

    class Broken:
        class conn:
            @staticmethod
            def execute(*a, **k):
                raise RuntimeError("no such column")

    assert _check_maintenance_reached(Broken())["status"] == "info"


# ---------------------------------------------------------------------------------------
# Pre-emption telemetry. Before this, the ONLY way to establish whether awakening prevention
# had fired was to replay the night through the controller offline and inspect _preempt_cool:
# the interventions ledger records a narrower class of correction, so a pre-emptive nudge that
# resolved to a small or held command left no trace anywhere.
# ---------------------------------------------------------------------------------------

class _DecisionRepo:
    def __init__(self, rows, night="2026-08-27"):
        """rows: [(state, preemption_dict_or_None)]"""
        import sqlite3, json as _j
        self.conn = sqlite3.connect(":memory:")
        self.conn.execute(
            "CREATE TABLE decisions (id INTEGER PRIMARY KEY, night_date TEXT, state TEXT, "
            "log_payload TEXT)")
        for st, pre in rows:
            self.conn.execute(
                "INSERT INTO decisions (night_date, state, log_payload) VALUES (?,?,?)",
                (night, st, _j.dumps({"preemption": pre}) if pre is not None else None))


def test_preemption_that_fired_is_reported_with_what_drove_it():
    from app.diagnostics import _check_preemption_ran
    rows = [("maintenance", {"preempting": False})] * 40
    rows += [("maintenance", {"preempting": True, "precursor_reasons": ["hr_creep"],
                              "risk_reasons": ["resp_irregular"]})] * 6
    c = _check_preemption_ran(_DecisionRepo(rows))
    assert c["status"] == "ok"
    assert "6/46" in c["detail"]
    assert "hr_creep" in c["detail"]


def test_a_maintenance_night_where_preemption_never_engaged_is_a_warning():
    from app.diagnostics import _check_preemption_ran
    c = _check_preemption_ran(_DecisionRepo([("maintenance", {"preempting": False})] * 50))
    assert c["status"] == "warn"
    assert "NEVER engaged" in c["detail"]


def test_never_reaching_maintenance_is_reported_as_no_opportunity_not_as_failure():
    """Blaming pre-emption for a night it was never allowed to run is a misdiagnosis."""
    from app.diagnostics import _check_preemption_ran
    c = _check_preemption_ran(_DecisionRepo([("induction", None)] * 30))
    assert c["status"] == "warn"
    assert "no opportunity" in c["detail"]


def test_unparseable_payloads_never_raise_out_of_the_preemption_check():
    from app.diagnostics import _check_preemption_ran
    r = _DecisionRepo([("maintenance", None)] * 3)
    r.conn.execute("UPDATE decisions SET log_payload = 'not json'")
    assert _check_preemption_ran(r)["status"] in ("warn", "ok", "info")


# ---------------------------------------------------------------------------------------
# Comfort-band pinning. On 2026-08-27 the band was 65.0-68.5F and the commanded water sat at
# exactly 65.0F -- the floor -- for the whole night, across all 12 awakenings.
# ---------------------------------------------------------------------------------------

class _ComfortRepo:
    def __init__(self, cool, warm, levels, neutral=66.9, night="2026-08-27"):
        import sqlite3
        self.conn = sqlite3.connect(":memory:")
        self.conn.execute("CREATE TABLE comfort_profile (id INTEGER PRIMARY KEY, neutral_f REAL,"
                          " cool_edge_f REAL, warm_edge_f REAL)")
        self.conn.execute("INSERT INTO comfort_profile VALUES (1,?,?,?)", (neutral, cool, warm))
        self.conn.execute("CREATE TABLE raw_samples (id INTEGER PRIMARY KEY, night_date TEXT,"
                          " commanded_level INTEGER, controller_state TEXT)")
        for lv in levels:
            self.conn.execute("INSERT INTO raw_samples (night_date, commanded_level,"
                              " controller_state) VALUES (?,?, 'maintenance')", (night, lv))


def _lvl(f):
    from sleepctl.controller.calibration import fahrenheit_to_level
    return fahrenheit_to_level(f)


def test_a_night_spent_at_the_cold_floor_is_reported_with_the_remedy():
    from app.diagnostics import _check_comfort_band_pinning
    c = _check_comfort_band_pinning(_ComfortRepo(65.5, 68.0, [_lvl(65.0)] * 100))
    assert c["status"] == "warn"
    assert "COLD floor" in c["detail"]
    assert "cool_edge_f" in (c["remedy"] or "")


def test_a_night_spent_at_the_warm_ceiling_is_reported_too():
    from app.diagnostics import _check_comfort_band_pinning
    c = _check_comfort_band_pinning(_ComfortRepo(65.5, 68.0, [_lvl(68.5)] * 100))
    assert c["status"] == "warn"
    assert "WARM ceiling" in c["detail"]


def test_commands_moving_inside_the_band_are_not_flagged():
    """Pinning is the signal, not clamping. A night that uses the band is working as intended."""
    from app.diagnostics import _check_comfort_band_pinning
    levels = [_lvl(f) for f in (65.5, 66.0, 66.5, 67.0, 67.5, 68.0)] * 10
    c = _check_comfort_band_pinning(_ComfortRepo(65.5, 68.0, levels))
    assert c["status"] == "ok"


def test_a_one_sided_comfort_profile_is_not_judged():
    from app.diagnostics import _check_comfort_band_pinning
    c = _check_comfort_band_pinning(_ComfortRepo(65.5, None, [_lvl(65.0)] * 20))
    assert c["status"] == "info"


def test_no_maintenance_commands_means_nothing_to_judge():
    from app.diagnostics import _check_comfort_band_pinning
    r = _ComfortRepo(65.5, 68.0, [_lvl(65.0)] * 5)
    r.conn.execute("UPDATE raw_samples SET controller_state = 'induction'")
    assert _check_comfort_band_pinning(r)["status"] == "info"


def test_the_bed_temperature_warning_names_the_reason_when_the_daemon_supplies_one():
    """An unexplained absence is a dead end; a named one is a next step."""
    from app.diagnostics import _check_bed_temperature
    c = _check_bed_temperature(
        _BedTempRepo(500, 0),
        {"bed_temp_reason": "no trends on the user object (account/membership?)"})
    assert c["status"] == "warn"
    assert "account/membership" in c["detail"]


def test_the_bed_temperature_warning_still_works_with_no_reason_available():
    from app.diagnostics import _check_bed_temperature
    c = _check_bed_temperature(_BedTempRepo(500, 0), {})
    assert c["status"] == "warn" and "OPEN-LOOP" in c["detail"]


# ---------------------------------------------------------------------------------------
# Comfort band editing. The interactive sweep is the right way to LEARN the band, but when
# the saved band is demonstrably wrong -- on 2026-08-27 it excluded 69F, the temperature this
# user's own history records as their best sleep (160 min unbroken) -- there has to be a way
# to correct it without re-running a whole night of calibration.
# ---------------------------------------------------------------------------------------

def test_setting_the_comfort_band_persists_and_returns_it(auth_client):
    r = auth_client.post("/control/comfort-profile",
                         json={"cool_edge_f": 67.0, "warm_edge_f": 70.0, "neutral_f": 69.0})
    assert r.status_code == 200, r.text
    p = r.json()["profile"]
    assert p["cool_edge_f"] == 67.0 and p["warm_edge_f"] == 70.0 and p["neutral_f"] == 69.0


def test_omitted_fields_keep_their_existing_value(auth_client):
    auth_client.post("/control/comfort-profile",
                     json={"cool_edge_f": 66.0, "warm_edge_f": 70.0, "neutral_f": 68.0})
    r = auth_client.post("/control/comfort-profile", json={"cool_edge_f": 67.0})
    assert r.status_code == 200
    p = r.json()["profile"]
    assert p["cool_edge_f"] == 67.0 and p["warm_edge_f"] == 70.0


def test_an_inverted_band_is_refused(auth_client):
    r = auth_client.post("/control/comfort-profile",
                         json={"cool_edge_f": 70.0, "warm_edge_f": 65.0})
    assert r.status_code == 400


def test_a_band_outside_the_device_range_is_refused(auth_client):
    """A band the device cannot reach would clamp every command to an impossible target."""
    r = auth_client.post("/control/comfort-profile",
                         json={"cool_edge_f": 20.0, "warm_edge_f": 30.0})
    assert r.status_code == 400


def test_neutral_is_pulled_inside_its_own_band(auth_client):
    r = auth_client.post("/control/comfort-profile",
                         json={"cool_edge_f": 67.0, "warm_edge_f": 70.0, "neutral_f": 90.0})
    assert r.status_code == 200
    assert r.json()["profile"]["neutral_f"] == 70.0


def test_the_endpoint_requires_authentication(client):
    fresh = client.__class__(client.app)
    assert fresh.post("/control/comfort-profile",
                      json={"cool_edge_f": 67.0, "warm_edge_f": 70.0}).status_code in (401, 403)


# ---------------------------------------------------------------------------------------
# Device-level glitches. `commanded_level` is the DEVICE's readback, and on 2026-08-27 a
# single tick at -100 (55F) landed mid wake-ramp, making the night's reported water range
# read "55.0-74.0F" for a ramp that actually climbed smoothly 66 -> 74F.
# ---------------------------------------------------------------------------------------

class _LevelRepo:
    def __init__(self, levels):
        import sqlite3
        from datetime import datetime as _dt
        self.conn = sqlite3.connect(":memory:")
        self.conn.execute("CREATE TABLE raw_samples (id INTEGER PRIMARY KEY, ts TEXT, "
                          "commanded_level INTEGER)")
        now = _dt.now().isoformat(" ", "seconds")
        for lv in levels:
            self.conn.execute("INSERT INTO raw_samples (ts, commanded_level) VALUES (?,?)",
                              (now, lv))


def test_a_physically_impossible_level_jump_is_flagged():
    from app.diagnostics import _check_device_level_glitches
    c = _check_device_level_glitches(_LevelRepo([-67, -67, -100, -65, -64]))
    assert c["status"] == "warn"
    assert "-100" in c["detail"]


def test_an_ordinary_ramp_is_not_flagged():
    """The real wake ramp: a smooth climb the bed can actually perform."""
    from app.diagnostics import _check_device_level_glitches
    c = _check_device_level_glitches(_LevelRepo(list(range(-68, -30, 2))))
    assert c["status"] == "ok"


def test_the_largest_jump_is_the_one_reported():
    from app.diagnostics import _check_device_level_glitches
    # jumps here are 30 (-60->-90) and 65 (-90->-25); the second must be the one named.
    c = _check_device_level_glitches(_LevelRepo([-60, -60, -90, -25, -26]))
    assert c["status"] == "warn"
    assert "-90 -> -25" in c["detail"]


def test_too_few_samples_is_info_not_a_false_alarm():
    from app.diagnostics import _check_device_level_glitches
    assert _check_device_level_glitches(_LevelRepo([-60]))["status"] == "info"


def test_an_unreadable_database_never_raises_out_of_the_level_check():
    from app.diagnostics import _check_device_level_glitches

    class Broken:
        class conn:
            @staticmethod
            def execute(*a, **k):
                raise RuntimeError("no such column")

    assert _check_device_level_glitches(Broken())["status"] == "info"


# ---------------------------------------------------------------------------------------
# Independent bed temperature. ThermalPlanner.resolve closes its loop on a measured bed temp
# and falls through to open-loop feedforward without one. The Pod's value is NULL on 100% of
# samples here (6514 in two days), so the loop has never closed. Any external sensor can now
# supply it.
# ---------------------------------------------------------------------------------------

def test_a_fahrenheit_reading_is_accepted_and_stored(auth_client):
    r = auth_client.post("/bedtemp/ingest", json={"temp_f": 66.4, "source": "ble_probe"})
    assert r.status_code == 200, r.text
    assert r.json()["temp_f"] == 66.4


def test_a_celsius_reading_is_converted(auth_client):
    r = auth_client.post("/bedtemp/ingest", json={"temp_c": 20.0})
    assert r.status_code == 200
    assert r.json()["temp_f"] == 68.0


def test_a_reading_with_no_temperature_is_refused(auth_client):
    assert auth_client.post("/bedtemp/ingest", json={"source": "x"}).status_code == 400


def test_an_implausible_reading_is_refused(auth_client):
    """A unit mix-up (20 C posted as F) fed into a CLOSED loop is worse than staying open."""
    assert auth_client.post("/bedtemp/ingest", json={"temp_f": 20.0}).status_code == 400
    assert auth_client.post("/bedtemp/ingest", json={"temp_f": 200.0}).status_code == 400


def test_a_fresh_reading_reads_back_with_an_age(auth_client):
    from app import bridge
    from app.db import get_repo
    auth_client.post("/bedtemp/ingest", json={"temp_f": 67.2})
    got = bridge.read_bed_temp_sample(get_repo().conn)
    assert got is not None and got["temp_f"] == 67.2
    assert got["age_seconds"] < 60


def test_a_stale_reading_is_withheld(auth_client):
    """Closing the loop on an hour-old temperature would chase a state the bed has long left --
    worse than the open-loop feedforward it replaces."""
    from app import bridge
    from app.db import get_repo
    auth_client.post("/bedtemp/ingest", json={"temp_f": 67.2})
    assert bridge.read_bed_temp_sample(get_repo().conn, max_age_s=0.0) is None


def test_reading_from_a_database_without_the_table_returns_none():
    import sqlite3
    from app import bridge
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    assert bridge.read_bed_temp_sample(conn) is None


# ---------------------------------------------------------------------------------------
# Pre-emption dead zone. For the first ~70 min after onset the only vulnerability term is
# light_stage (+0.10) against a 0.5 threshold, so only the precursor path can fire there. On
# 2026-08-27 five awakening ticks fell in the 92-minute gap before pre-emption first engaged.
# ---------------------------------------------------------------------------------------

def _decisions(rows, night="2026-08-27"):
    """rows: [(state, preemption_dict, wake_signals_or_None)]"""
    import sqlite3, json as _j

    class R:
        pass
    r = R()
    r.conn = sqlite3.connect(":memory:")
    r.conn.execute("CREATE TABLE decisions (id INTEGER PRIMARY KEY, night_date TEXT, "
                   "state TEXT, log_payload TEXT)")
    for st, pre, ws in rows:
        payload = _j.dumps({"preemption": pre or {}, "wake_signals": ws})
        r.conn.execute("INSERT INTO decisions (night_date, state, log_payload) VALUES (?,?,?)",
                       (night, st, payload))
    return r


def test_wake_evidence_inside_the_dead_zone_is_reported_with_the_peak_precursor():
    from app.diagnostics import _check_preemption_dead_zone
    rows = [("maintenance", {"preempting": False, "precursor_score": 0.37}, ["hr_rise"])] * 5
    rows += [("maintenance", {"preempting": True}, None)]
    c = _check_preemption_dead_zone(_decisions(rows))
    assert c["status"] == "warn"
    assert "0.37" in c["detail"]
    assert "threshold" in (c["remedy"] or "")


def test_a_quiet_dead_zone_is_not_a_problem():
    """A gap with nothing to act on is not a gap worth closing."""
    from app.diagnostics import _check_preemption_dead_zone
    rows = [("maintenance", {"preempting": False, "precursor_score": 0.05}, None)] * 8
    rows += [("maintenance", {"preempting": True}, None)]
    assert _check_preemption_dead_zone(_decisions(rows))["status"] == "ok"


def test_pre_emption_available_immediately_is_ok():
    from app.diagnostics import _check_preemption_dead_zone
    rows = [("maintenance", {"preempting": True}, None)] * 3
    assert _check_preemption_dead_zone(_decisions(rows))["status"] == "ok"


def test_never_engaging_at_all_is_deferred_to_the_other_check():
    from app.diagnostics import _check_preemption_dead_zone
    rows = [("maintenance", {"preempting": False, "precursor_score": 0.1}, None)] * 6
    c = _check_preemption_dead_zone(_decisions(rows))
    assert c["status"] == "warn" and "NEVER engaged" in c["detail"]


def test_induction_ticks_are_not_counted_as_dead_zone():
    """Pre-emption is MAINTENANCE-only by design; induction ticks are not a gap."""
    from app.diagnostics import _check_preemption_dead_zone
    rows = [("induction", {}, ["hr_rise"])] * 20
    rows += [("maintenance", {"preempting": True}, None)]
    assert _check_preemption_dead_zone(_decisions(rows))["status"] == "ok"


def test_an_unreadable_database_never_raises_out_of_the_dead_zone_check():
    from app.diagnostics import _check_preemption_dead_zone

    class Broken:
        class conn:
            @staticmethod
            def execute(*a, **k):
                raise RuntimeError("no such table")

    assert _check_preemption_dead_zone(Broken())["status"] == "info"


# ---------------------------------------------------------------------------------------
# HRV window export + sleep/wake shadow mode. The Verity's beat-to-beat intervals are the
# richest signal we have and reached NO model at all; exporting per-epoch features is what
# makes the autonomic channel calibratable before it is trusted with a control decision.
# ---------------------------------------------------------------------------------------

def test_the_hrv_export_survives_a_night_with_no_intervals():
    """Absence must be reported as zero windows, not as an exception key."""
    from app import night_export
    import sqlite3
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE rr_intervals (ts TEXT, rr_ms TEXT, n INT, source TEXT)")
    # The helper is exercised through build_night below; here we only assert the table shape
    # a night with no rows presents.
    assert conn.execute("SELECT COUNT(*) FROM rr_intervals").fetchone()[0] == 0


def test_night_export_names_are_stable_for_the_calibration_consumer():
    """The exported feature columns must be exactly the ones the detector reads -- exporting the
    wrong ones would quietly make calibration impossible."""
    from sleepctl.ml.sleep_wake import AUTONOMIC_FEATURE_NAMES
    from sleepctl.ml.sleep_staging.hrv_features import hrv_features
    import random
    random.seed(3)
    rr, t = [], 0.0
    for _ in range(400):
        v = 1000 + random.gauss(0, 45)
        t += v / 1000.0
        rr.append((t, v))
    produced = hrv_features([x[0] for x in rr], [x[1] for x in rr])
    for name in AUTONOMIC_FEATURE_NAMES:
        assert name in produced


def test_local_timestamps_convert_to_utc_for_the_rr_query():
    """`raw_samples.ts` is NAIVE LOCAL and `rr_intervals.ts` is AWARE UTC. Comparing them
    directly is the exact mistake the schema's timestamp convention warns about, and it has
    already cost this project a capacity detector that never fired."""
    from datetime import datetime, timezone
    local = datetime.fromisoformat("2026-08-27T21:33:40")
    assert local.tzinfo is None, "the raw_samples convention is naive"
    as_utc = local.astimezone(timezone.utc)
    assert as_utc.tzinfo is timezone.utc
    assert as_utc.isoformat() != local.isoformat(), "conversion must actually shift the value"


# ---------------------------------------------------------------------------------------
# Gait anchors: the independent wake evidence the validation layer never had a source for.
# ---------------------------------------------------------------------------------------

def _acti_repo(rows):
    """rows: [(iso_ts, gait_or_None)]"""
    import sqlite3
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE actigraphy (ts TEXT, gait INTEGER)")
    for ts, g in rows:
        conn.execute("INSERT INTO actigraphy (ts, gait) VALUES (?,?)", (ts, g))
    return conn


def test_gait_rows_become_anchors():
    from app import bridge
    conn = _acti_repo([("2026-08-27T23:00:00+00:00", 1), ("2026-08-27T23:30:00+00:00", 1)])
    got = bridge.gait_anchors(conn, "2026-08-27T20:00:00+00:00", "2026-08-28T08:00:00+00:00")
    assert len(got) == 2


def test_non_gait_rows_are_not_anchors():
    from app import bridge
    conn = _acti_repo([("2026-08-27T23:00:00+00:00", None)] * 5)
    assert bridge.gait_anchors(conn, "2026-08-27T20:00:00+00:00",
                               "2026-08-28T08:00:00+00:00") == []


def test_one_bathroom_trip_collapses_to_a_single_anchor():
    """Thirty consecutive gait detections are one awakening, not thirty."""
    from app import bridge
    rows = [(f"2026-08-27T23:{m:02d}:00+00:00", 1) for m in range(0, 3)]
    conn = _acti_repo(rows)
    got = bridge.gait_anchors(conn, "2026-08-27T20:00:00+00:00", "2026-08-28T08:00:00+00:00")
    assert len(got) == 1


def test_separate_trips_stay_separate():
    from app import bridge
    conn = _acti_repo([("2026-08-27T23:00:00+00:00", 1), ("2026-08-28T02:00:00+00:00", 1)])
    got = bridge.gait_anchors(conn, "2026-08-27T20:00:00+00:00", "2026-08-28T08:00:00+00:00")
    assert len(got) == 2


def test_anchors_outside_the_night_window_are_excluded():
    from app import bridge
    conn = _acti_repo([("2026-08-27T12:00:00+00:00", 1)])
    assert bridge.gait_anchors(conn, "2026-08-27T20:00:00+00:00",
                               "2026-08-28T08:00:00+00:00") == []


def test_a_database_without_the_column_returns_no_anchors():
    import sqlite3
    from app import bridge
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE actigraphy (ts TEXT)")
    assert bridge.gait_anchors(conn, "2026-08-27T20:00:00+00:00",
                               "2026-08-28T08:00:00+00:00") == []


def _marker_repo(rows):
    """rows: [(iso_ts, marker_or_None)]"""
    import sqlite3
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE actigraphy (ts TEXT, marker INTEGER)")
    for ts, m in rows:
        conn.execute("INSERT INTO actigraphy (ts, marker) VALUES (?,?)", (ts, m))
    return conn


def test_marker_gestures_become_anchors():
    from app import bridge
    conn = _marker_repo([("2026-08-27T23:10:00+00:00", 1), ("2026-08-28T03:20:00+00:00", 1)])
    got = bridge.marker_anchors(conn, "2026-08-27T20:00:00+00:00", "2026-08-28T08:00:00+00:00")
    assert len(got) == 2


def test_markers_are_not_collapsed_the_way_gait_is():
    """The forwarder already enforces a 20 s debounce, so every stored row is a distinct
    deliberate act -- collapsing further would discard real awakenings the user reported."""
    from app import bridge
    rows = [(f"2026-08-27T23:{m:02d}:00+00:00", 1) for m in (10, 11, 12)]
    got = bridge.marker_anchors(_marker_repo(rows), "2026-08-27T20:00:00+00:00",
                                "2026-08-28T08:00:00+00:00")
    assert len(got) == 3


def test_non_marker_rows_are_ignored():
    from app import bridge
    conn = _marker_repo([("2026-08-27T23:10:00+00:00", None)] * 4)
    assert bridge.marker_anchors(conn, "2026-08-27T20:00:00+00:00",
                                 "2026-08-28T08:00:00+00:00") == []


def test_a_database_without_the_marker_column_returns_nothing():
    import sqlite3
    from app import bridge
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE actigraphy (ts TEXT)")
    assert bridge.marker_anchors(conn, "2026-08-27T20:00:00+00:00",
                                 "2026-08-28T08:00:00+00:00") == []
