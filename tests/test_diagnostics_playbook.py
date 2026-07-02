"""Tests for the known-issue playbook matcher (sleepctl.diagnostics_playbook).

Deliberately engine-side and dashboard-free: feeds synthetic diagnostics-result dicts (the
same ``{"checks": [{"id", "status", "detail", "remedy"}, ...]}`` shape both
``app.diagnostics.run_diagnostics()`` and ``sleepctl.diagnostics.data_diagnostics()`` produce)
rather than importing the dashboard app.
"""

from __future__ import annotations

from sleepctl.diagnostics_playbook import PLAYBOOK, match_playbook, playbook_catalog

_REQUIRED_MATCH_KEYS = {"id", "symptom", "likely_cause", "fix", "auto_fixable"}


def _check(id_, status, detail="", remedy=None):
    return {"id": id_, "title": id_, "status": status, "detail": detail, "remedy": remedy}


def test_water_reservoir_empty_matches_on_has_water_false():
    result = {"checks": [
        _check("device_water", "fail", "has_water=false — the bed can't heat or cool",
               "fill the Hub reservoir + PRIME"),
        _check("device_online", "ok", "device reports online"),
    ]}
    matches = match_playbook(result, events=[], run_dir=None, env={})

    ids = {m["id"] for m in matches}
    assert "water_reservoir_empty" in ids
    match = next(m for m in matches if m["id"] == "water_reservoir_empty")
    assert _REQUIRED_MATCH_KEYS.issubset(match.keys())
    assert match["auto_fixable"] is False
    assert "reservoir" in match["likely_cause"].lower()


def test_clean_result_has_no_matches():
    result = {"checks": [
        _check("device_water", "ok", "reservoir has water"),
        _check("device_online", "ok", "device reports online"),
        _check("daemon_heartbeat", "ok", "last heartbeat 2s ago"),
        _check("watchdog_heartbeat", "ok", "last heartbeat 3s ago"),
        _check("eight_sleep_creds", "ok", "EIGHTSLEEP_EMAIL/EIGHTSLEEP_PASSWORD are set"),
        _check("live_mode", "info", "live=True dry_run=False"),
        _check("recent_errors", "ok", "daemon.err and daemon-crash.log are empty"),
        _check("cloud_errors", "ok", "no cloud/timeout errors in the last 500 log lines"),
    ]}
    matches = match_playbook(result, events=[], run_dir=None, env={})
    assert matches == []


def test_watchdog_restart_storm_detected_via_alert_file(tmp_path):
    (tmp_path / "watchdog.alert").write_text(
        "2026-07-02 12:00:00  CRITICAL: RESTART STORM: daemon restarted 6 times in 5 min"
    )
    matches = match_playbook({"checks": []}, run_dir=str(tmp_path), env={})
    assert any(m["id"] == "watchdog_restart_storm" for m in matches)


def test_watchdog_restart_storm_absent_without_alert_file(tmp_path):
    matches = match_playbook({"checks": []}, run_dir=str(tmp_path), env={})
    assert not any(m["id"] == "watchdog_restart_storm" for m in matches)


def test_dry_run_left_on_detected_via_env():
    matches = match_playbook({"checks": []},
                             env={"SLEEPCTL_LIVE": "1", "SLEEPCTL_DRY_RUN": "1"})
    assert any(m["id"] == "dry_run_left_on" for m in matches)


def test_dry_run_not_flagged_when_live_is_off():
    matches = match_playbook({"checks": []},
                             env={"SLEEPCTL_LIVE": "0", "SLEEPCTL_DRY_RUN": "1"})
    assert not any(m["id"] == "dry_run_left_on" for m in matches)


def test_no_credentials_configured_matches_creds_warn():
    result = {"checks": [
        _check("eight_sleep_creds", "warn",
               "EIGHTSLEEP_EMAIL/EIGHTSLEEP_PASSWORD not both set",
               "daemon will fall back to SIMULATOR"),
    ]}
    matches = match_playbook(result, env={})
    assert any(m["id"] == "no_credentials_configured" for m in matches)


def test_device_offline_matches_device_online_fail():
    result = {"checks": [
        _check("device_online", "fail", "the bed/hub is reporting offline"),
    ]}
    matches = match_playbook(result, env={})
    assert any(m["id"] == "device_offline" for m in matches)


def test_db_locked_keyword_detected_in_check_detail():
    result = {"checks": [
        _check("recent_errors", "fail",
               "daemon-crash.log last: sqlite3.OperationalError: database is locked"),
    ]}
    matches = match_playbook(result, env={})
    assert any(m["id"] == "db_locked" for m in matches)


def test_port_in_use_keyword_detected_in_events():
    events = [{"category": "api", "severity": "error", "code": "startup_failed",
              "message": "OSError: [Errno 98] error while attempting to bind on address: "
                         "address already in use"}]
    matches = match_playbook({"checks": []}, events=events, env={})
    assert any(m["id"] == "port_in_use" for m in matches)


def test_match_playbook_never_raises_on_malformed_result():
    assert match_playbook(None, env={}) == []  # type: ignore[arg-type]
    assert match_playbook({}, env={}) == []
    assert match_playbook({"checks": "not-a-list"}, env={}) == []  # type: ignore[dict-item]


def test_playbook_catalog_covers_seeded_ids_and_has_unique_ids():
    expected = {
        "water_reservoir_empty", "watchdog_restart_storm", "daemon_heartbeat_stale",
        "dry_run_left_on", "pyeight_auth_failure", "no_credentials_configured",
        "db_locked", "port_in_use", "calendar_ics_unreachable", "device_offline",
    }
    catalog = playbook_catalog()
    ids = [e["id"] for e in catalog]
    assert expected.issubset(set(ids))
    assert len(ids) == len(set(ids)), "playbook ids must be unique"
    assert len(catalog) == len(PLAYBOOK)
    for entry in catalog:
        assert _REQUIRED_MATCH_KEYS.issubset(entry.keys())
