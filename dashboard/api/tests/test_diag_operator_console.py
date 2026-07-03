"""Tests for the Claude operator console additions appended to the end of app/main.py:
GET /diag/all (one-shot total context), GET /diag/manifest (capability catalog), the rounded-out
safe action set (self-test/backup/run-diagnostics), and the self-update flag-file protocol
(POST /diag/action/update + GET /diag/update-status). Same token gating as every other /diag*
endpoint (secret DIAG_TOKEN, 404 on missing/wrong token)."""

from __future__ import annotations

import json
import os

from app.bridge import VALID_COMMANDS
from app.main import _run_dir


SECRET_PASSWORD = "operator-console-secret-password"


def _clear_commands():
    from app.db import get_repo
    repo = get_repo()
    try:
        repo.conn.execute("DELETE FROM commands")
        repo.conn.commit()
    finally:
        repo.close()


def _remove(path):
    if os.path.exists(path):
        os.remove(path)


# ------------------------------------------------------------------ GET /diag/all
def test_diag_all_requires_token(client, monkeypatch):
    monkeypatch.delenv("DIAG_TOKEN", raising=False)
    assert client.get("/diag/all").status_code == 404
    monkeypatch.setenv("DIAG_TOKEN", "s3cret-xyz")
    assert client.get("/diag/all").status_code == 404          # no token
    assert client.get("/diag/all?token=nope").status_code == 404  # wrong token


def test_diag_all_composite_shape(client, monkeypatch):
    monkeypatch.setenv("DIAG_TOKEN", "s3cret-xyz")
    r = client.get("/diag/all?token=s3cret-xyz")
    assert r.status_code == 200
    body = r.json()
    expected_keys = {
        "manifest", "generated_at", "verdict", "headline", "primary_remedy", "checks",
        "playbook_matches", "playbook", "version", "runtime_state", "device",
        "config_redacted", "heartbeats", "recent_events", "state_history_recent",
        "blackbox_available", "self_test", "log_tails", "result_and_alert_files",
        "backups", "project_state",
    }
    assert expected_keys <= set(body)
    assert body["verdict"] in ("HEALTHY", "DEGRADED", "DOWN")
    assert "/diag/manifest" in body["manifest"]

    # project_state gathers the whole rest of the project, not just up/down health
    ps = body["project_state"]
    assert set(ps) >= {"status", "tonight_plan", "learning", "efficacy", "safety",
                       "calendar", "shift_plan", "calibration"}
    assert set(ps["safety"]) == {"data_quality", "guardrail"}
    assert set(ps["calibration"]) >= {"thermal_calibration", "comfort_profile",
                                      "resting_baseline", "self_test"}


def test_diag_all_never_leaks_a_seeded_secret(client, monkeypatch):
    monkeypatch.setenv("DIAG_TOKEN", "s3cret-xyz")
    monkeypatch.setenv("EIGHTSLEEP_PASSWORD", SECRET_PASSWORD)

    r = client.get("/diag/all?token=s3cret-xyz")
    assert r.status_code == 200
    assert SECRET_PASSWORD not in r.text
    assert "<redacted>" in json.dumps(r.json()["config_redacted"])


def test_diag_all_never_500s_even_if_a_subsection_is_broken(client, monkeypatch):
    """Break one deep sub-read (calendar) and confirm /diag/all still returns 200 with the rest
    of the payload intact -- degrade-one-field, not crash-the-endpoint."""
    monkeypatch.setenv("DIAG_TOKEN", "s3cret-xyz")
    from app import services

    def _boom(repo):
        raise RuntimeError("simulated calendar failure")

    monkeypatch.setattr(services, "calendar_config_view", _boom)
    r = client.get("/diag/all?token=s3cret-xyz")
    assert r.status_code == 200
    body = r.json()
    assert "error" in body["project_state"]["calendar"]["config"]
    # everything else is still present and sane
    assert body["verdict"] in ("HEALTHY", "DEGRADED", "DOWN")


# ------------------------------------------------------------------ GET /diag/manifest
def test_diag_manifest_requires_token(client, monkeypatch):
    monkeypatch.delenv("DIAG_TOKEN", raising=False)
    assert client.get("/diag/manifest").status_code == 404
    monkeypatch.setenv("DIAG_TOKEN", "s3cret-xyz")
    assert client.get("/diag/manifest").status_code == 404
    assert client.get("/diag/manifest?token=nope").status_code == 404


def test_diag_manifest_lists_every_diag_endpoint(client, monkeypatch):
    monkeypatch.setenv("DIAG_TOKEN", "s3cret-xyz")
    r = client.get("/diag/manifest?token=s3cret-xyz")
    assert r.status_code == 200
    body = r.json()
    assert set(body) >= {"generated_at", "note", "diag_endpoints", "operational_endpoints",
                         "coverage"}

    paths = {e["path"] for e in body["diag_endpoints"]}
    expected_paths = {
        "/diag", "/diag/events", "/diag/logs", "/diag/probe", "/diag/history",
        "/diag/blackbox", "/diag/bundle", "/diag/repair", "/diag/action/restart",
        "/diag/action/reconnect", "/diag/morning-report", "/diag/morning-report/send",
        "/diag/playbook", "/diag/all", "/diag/manifest", "/diag/action/self-test",
        "/diag/action/backup", "/diag/action/run-diagnostics", "/diag/action/update",
        "/diag/update-status",
    }
    assert expected_paths <= paths

    for entry in body["diag_endpoints"]:
        assert set(entry) >= {"method", "path", "gate", "params", "description", "example"}

    op_paths = {e["path"] for e in body["operational_endpoints"]}
    assert {"/status", "/tonight", "/efficacy", "/circadian"} <= op_paths
    for entry in body["operational_endpoints"]:
        assert entry["gate"] == "session"

    # the 12-feature coverage map
    assert set(body["coverage"]) == {str(i) for i in range(1, 13)}
    for entry in body["coverage"].values():
        assert "feature" in entry and "endpoint" in entry


# ------------------------------------------------------------------ POST /diag/action/self-test
def test_diag_action_self_test_requires_token(client, monkeypatch):
    monkeypatch.delenv("DIAG_TOKEN", raising=False)
    assert client.post("/diag/action/self-test").status_code == 404
    monkeypatch.setenv("DIAG_TOKEN", "s3cret-xyz")
    assert client.post("/diag/action/self-test?token=nope").status_code == 404


def test_diag_action_self_test_enqueues_self_test_command(client, monkeypatch):
    monkeypatch.setenv("DIAG_TOKEN", "s3cret-xyz")
    _clear_commands()

    r = client.post("/diag/action/self-test?token=s3cret-xyz")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["action"] == "self-test"
    assert body["result"]["mode"] == "full"
    assert body["result"]["command_id"] is not None
    assert body["verify_with"] == "/diag/all?token=s3cret-xyz"

    from app.db import get_repo
    repo = get_repo()
    try:
        rows = repo.conn.execute("SELECT type FROM commands WHERE status='pending'").fetchall()
    finally:
        repo.close()
    types = {row["type"] for row in rows}
    assert "self_test" in types
    assert types <= VALID_COMMANDS


def test_diag_action_self_test_invalid_mode_falls_back_to_full(client, monkeypatch):
    monkeypatch.setenv("DIAG_TOKEN", "s3cret-xyz")
    _clear_commands()
    r = client.post("/diag/action/self-test?token=s3cret-xyz&mode=bogus")
    assert r.status_code == 200
    assert r.json()["result"]["mode"] == "full"


# ------------------------------------------------------------------ POST /diag/action/backup
def test_diag_action_backup_requires_token(client, monkeypatch):
    monkeypatch.delenv("DIAG_TOKEN", raising=False)
    assert client.post("/diag/action/backup").status_code == 404
    monkeypatch.setenv("DIAG_TOKEN", "s3cret-xyz")
    assert client.post("/diag/action/backup?token=nope").status_code == 404


def test_diag_action_backup_creates_a_backup_file(client, monkeypatch):
    monkeypatch.setenv("DIAG_TOKEN", "s3cret-xyz")
    r = client.post("/diag/action/backup?token=s3cret-xyz")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["action"] == "backup"
    path = body["result"]["path"]
    assert os.path.exists(path)
    assert body["verify_with"] == "/diag/all?token=s3cret-xyz"
    os.remove(path)


# ------------------------------------------------------------------ POST /diag/action/run-diagnostics
def test_diag_action_run_diagnostics_requires_token(client, monkeypatch):
    monkeypatch.delenv("DIAG_TOKEN", raising=False)
    assert client.post("/diag/action/run-diagnostics").status_code == 404


def test_diag_action_run_diagnostics_returns_diag_all_payload(client, monkeypatch):
    monkeypatch.setenv("DIAG_TOKEN", "s3cret-xyz")
    r = client.post("/diag/action/run-diagnostics?token=s3cret-xyz")
    assert r.status_code == 200
    body = r.json()
    assert body["action"] == "run-diagnostics"
    assert "verdict" in body["result"]
    assert "project_state" in body["result"]
    assert body["verify_with"] == "/diag/all?token=s3cret-xyz"


# ------------------------------------------------------------------ verify_with retrofit
def test_verify_with_present_on_repair_and_reconnect(client, monkeypatch):
    monkeypatch.setenv("DIAG_TOKEN", "s3cret-xyz")
    _clear_commands()

    r = client.post("/diag/repair?token=s3cret-xyz")
    assert r.json()["verify_with"] == "/diag/all?token=s3cret-xyz"

    r = client.post("/diag/action/reconnect?token=s3cret-xyz")
    assert r.json()["verify_with"] == "/diag/all?token=s3cret-xyz"
    _clear_commands()


# ------------------------------------------------------------------ POST /diag/action/update
def test_diag_action_update_requires_token(client, monkeypatch):
    monkeypatch.delenv("DIAG_TOKEN", raising=False)
    assert client.post("/diag/action/update").status_code == 404
    monkeypatch.setenv("DIAG_TOKEN", "s3cret-xyz")
    assert client.post("/diag/action/update?token=nope").status_code == 404


def test_diag_action_update_writes_update_request_with_expected_branch(client, monkeypatch):
    monkeypatch.setenv("DIAG_TOKEN", "s3cret-xyz")
    monkeypatch.setenv("DEPLOY_BRANCH", "release/2026-07")
    flag = os.path.join(_run_dir(), "update.request")
    _remove(flag)

    r = client.post("/diag/action/update?token=s3cret-xyz")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["action"] == "update"
    assert body["result"]["branch"] == "release/2026-07"
    assert body["verify_with"] == "/diag/update-status?token=s3cret-xyz"
    assert os.path.exists(flag)
    assert open(flag, encoding="utf-8").read().strip() == "release/2026-07"
    _remove(flag)


def test_diag_action_update_defaults_branch_to_main(client, monkeypatch):
    monkeypatch.setenv("DIAG_TOKEN", "s3cret-xyz")
    monkeypatch.delenv("DEPLOY_BRANCH", raising=False)
    flag = os.path.join(_run_dir(), "update.request")
    _remove(flag)

    r = client.post("/diag/action/update?token=s3cret-xyz")
    assert r.status_code == 200
    assert r.json()["result"]["branch"] == "main"
    assert open(flag, encoding="utf-8").read().strip() == "main"
    _remove(flag)


def test_diag_action_update_rejects_a_disallowed_branch_value(client, monkeypatch):
    monkeypatch.setenv("DIAG_TOKEN", "s3cret-xyz")
    monkeypatch.setenv("DEPLOY_BRANCH", "main; rm -rf /")
    flag = os.path.join(_run_dir(), "update.request")
    _remove(flag)

    r = client.post("/diag/action/update?token=s3cret-xyz")
    assert r.status_code == 500
    assert not os.path.exists(flag)


def test_diag_action_update_logs_a_remote_action_event(client, monkeypatch):
    monkeypatch.setenv("DIAG_TOKEN", "s3cret-xyz")
    monkeypatch.setenv("DEPLOY_BRANCH", "main")
    flag = os.path.join(_run_dir(), "update.request")
    _remove(flag)

    client.post("/diag/action/update?token=s3cret-xyz")
    r = client.get("/diag/events?token=s3cret-xyz&category=remote_action&limit=10")
    assert r.status_code == 200
    rows = r.json()
    assert any(row["code"] == "update_request" and row["data"].get("branch") == "main"
              for row in rows)
    _remove(flag)


# ------------------------------------------------------------------ GET /diag/update-status
def test_diag_update_status_requires_token(client, monkeypatch):
    monkeypatch.delenv("DIAG_TOKEN", raising=False)
    assert client.get("/diag/update-status").status_code == 404
    monkeypatch.setenv("DIAG_TOKEN", "s3cret-xyz")
    assert client.get("/diag/update-status?token=nope").status_code == 404


def test_diag_update_status_no_result_yet(client, monkeypatch):
    monkeypatch.setenv("DIAG_TOKEN", "s3cret-xyz")
    result_path = os.path.join(_run_dir(), "update.result")
    _remove(result_path)

    r = client.get("/diag/update-status?token=s3cret-xyz")
    assert r.status_code == 200
    body = r.json()
    assert body["available"] is False


def test_diag_update_status_reads_the_watchdog_written_result(client, monkeypatch):
    monkeypatch.setenv("DIAG_TOKEN", "s3cret-xyz")
    run = _run_dir()
    os.makedirs(run, exist_ok=True)
    result_path = os.path.join(run, "update.result")
    record = {
        "timestamp": "2026-07-02T05:00:00+00:00", "branch": "main", "git_ok": True,
        "git_output": "Fast-forward\nHEAD is now at abc1234", "validate_verdict": "PASS",
        "restarted": True, "summary": "update to main succeeded; restart requested",
    }
    with open(result_path, "w", encoding="utf-8") as fh:
        json.dump(record, fh)

    r = client.get("/diag/update-status?token=s3cret-xyz")
    assert r.status_code == 200
    body = r.json()
    assert body["available"] is True
    assert body["branch"] == "main"
    assert body["git_ok"] is True
    assert body["validate_verdict"] == "PASS"
    assert body["restarted"] is True
    os.remove(result_path)


# --------------------------------------------------- POST /diag/action/restart-watchdog
def test_diag_action_restart_watchdog_requires_token(client, monkeypatch):
    monkeypatch.delenv("DIAG_TOKEN", raising=False)
    assert client.post("/diag/action/restart-watchdog").status_code == 404
    monkeypatch.setenv("DIAG_TOKEN", "s3cret-xyz")
    assert client.post("/diag/action/restart-watchdog").status_code == 404
    assert client.post("/diag/action/restart-watchdog?token=nope").status_code == 404


def test_diag_action_restart_watchdog_writes_watchdog_flag(client, monkeypatch):
    monkeypatch.setenv("DIAG_TOKEN", "s3cret-xyz")
    flag = os.path.join(_run_dir(), "restart.request")
    _remove(flag)

    r = client.post("/diag/action/restart-watchdog?token=s3cret-xyz")
    assert r.status_code == 200
    body = r.json()
    assert body["requested"] == "watchdog"
    assert body["verify_with"] == "/diag/all?token=s3cret-xyz"
    assert os.path.exists(flag)
    assert open(flag, encoding="utf-8").read().strip() == "watchdog"
    _remove(flag)


def test_diag_action_restart_watchdog_logs_a_remote_action_event(client, monkeypatch):
    monkeypatch.setenv("DIAG_TOKEN", "s3cret-xyz")
    flag = os.path.join(_run_dir(), "restart.request")
    _remove(flag)

    client.post("/diag/action/restart-watchdog?token=s3cret-xyz")
    r = client.get("/diag/events?token=s3cret-xyz&category=remote_action&limit=10")
    assert r.status_code == 200
    assert any(row["code"] == "restart_watchdog_request" for row in r.json())
    _remove(flag)


# --------------------------------------------------- POST /diag/action/rebuild-web
def test_diag_action_rebuild_web_requires_token(client, monkeypatch):
    monkeypatch.delenv("DIAG_TOKEN", raising=False)
    assert client.post("/diag/action/rebuild-web").status_code == 404
    monkeypatch.setenv("DIAG_TOKEN", "s3cret-xyz")
    assert client.post("/diag/action/rebuild-web").status_code == 404
    assert client.post("/diag/action/rebuild-web?token=nope").status_code == 404


def test_diag_action_rebuild_web_writes_webbuild_request(client, monkeypatch):
    monkeypatch.setenv("DIAG_TOKEN", "s3cret-xyz")
    flag = os.path.join(_run_dir(), "webbuild.request")
    _remove(flag)

    r = client.post("/diag/action/rebuild-web?token=s3cret-xyz")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["action"] == "rebuild-web"
    assert body["result"]["requested"] is True
    assert body["verify_with"] == "/diag/webbuild-status?token=s3cret-xyz"
    assert os.path.exists(flag)
    _remove(flag)


def test_diag_action_rebuild_web_logs_a_remote_action_event(client, monkeypatch):
    monkeypatch.setenv("DIAG_TOKEN", "s3cret-xyz")
    flag = os.path.join(_run_dir(), "webbuild.request")
    _remove(flag)

    client.post("/diag/action/rebuild-web?token=s3cret-xyz")
    r = client.get("/diag/events?token=s3cret-xyz&category=remote_action&limit=10")
    assert r.status_code == 200
    assert any(row["code"] == "rebuild_web_request" for row in r.json())
    _remove(flag)


# --------------------------------------------------- GET /diag/webbuild-status
def test_diag_webbuild_status_requires_token(client, monkeypatch):
    monkeypatch.delenv("DIAG_TOKEN", raising=False)
    assert client.get("/diag/webbuild-status").status_code == 404
    monkeypatch.setenv("DIAG_TOKEN", "s3cret-xyz")
    assert client.get("/diag/webbuild-status?token=nope").status_code == 404


def test_diag_webbuild_status_no_result_yet(client, monkeypatch):
    monkeypatch.setenv("DIAG_TOKEN", "s3cret-xyz")
    result_path = os.path.join(_run_dir(), "webbuild.result")
    _remove(result_path)

    r = client.get("/diag/webbuild-status?token=s3cret-xyz")
    assert r.status_code == 200
    assert r.json()["available"] is False


def test_diag_webbuild_status_reads_the_watchdog_written_result(client, monkeypatch):
    monkeypatch.setenv("DIAG_TOKEN", "s3cret-xyz")
    run = _run_dir()
    os.makedirs(run, exist_ok=True)
    result_path = os.path.join(run, "webbuild.result")
    record = {
        "timestamp": "2026-07-02T05:00:00+00:00", "exit_code": 0, "ok": True,
        "output": "> next build\nCompiled successfully", "summary": "web rebuild succeeded",
    }
    with open(result_path, "w", encoding="utf-8") as fh:
        json.dump(record, fh)

    r = client.get("/diag/webbuild-status?token=s3cret-xyz")
    assert r.status_code == 200
    body = r.json()
    assert body["available"] is True
    assert body["exit_code"] == 0
    assert body["ok"] is True
    os.remove(result_path)


# --------------------------------------------------- manifest lists the new endpoints
def test_diag_manifest_lists_the_new_remote_ops_endpoints(client, monkeypatch):
    monkeypatch.setenv("DIAG_TOKEN", "s3cret-xyz")
    r = client.get("/diag/manifest?token=s3cret-xyz")
    assert r.status_code == 200
    paths = {e["path"] for e in r.json()["diag_endpoints"]}
    assert {"/diag/action/restart-watchdog", "/diag/action/rebuild-web",
            "/diag/webbuild-status"} <= paths
