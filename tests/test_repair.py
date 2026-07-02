"""Tests for the safe, idempotent self-repair battery (``sleepctl.repair``) shared by
``POST /diag/repair`` and the standalone ``sleepctl repair`` CLI command.

Covers each sub-action in isolation (stuck commands, stuck-device re-init, stale-daemon
restart request, stale-alert clearing), that the full battery only ever enqueues commands from
the hardcoded SAFE_REPAIR_COMMANDS allowlist, that running it twice in a row is idempotent
(no duplicate enqueues / no repeated harmful writes), and a CLI smoke test.
"""

from __future__ import annotations

import io
import json
import os
import tempfile
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone

from sleepctl.repair import (
    SAFE_REPAIR_COMMANDS,
    clear_alert_if_healthy,
    clear_stuck_commands,
    ensure_schema,
    reenqueue_if_stuck,
    request_restart_if_stale,
    resolve_run_dir,
    run_repair,
)
from sleepctl.storage.repository import Repository


def _repo():
    repo = Repository(tempfile.mktemp(suffix=".db"))
    ensure_schema(repo.conn)
    return repo


def _insert_command(conn, ctype, status="pending", ts=None):
    ts = ts or datetime.now(timezone.utc).isoformat()
    cur = conn.execute(
        "INSERT INTO commands (ts, type, payload, status) VALUES (?,?,?,?)",
        (ts, ctype, json.dumps({}), status),
    )
    conn.commit()
    return cur.lastrowid


def _write_runtime_state(conn, extra: dict):
    conn.execute(
        "INSERT INTO runtime_state (id, updated, extra) VALUES (1,?,?) "
        "ON CONFLICT(id) DO UPDATE SET updated=excluded.updated, extra=excluded.extra",
        (datetime.now(timezone.utc).isoformat(), json.dumps(extra)),
    )
    conn.commit()


# ------------------------------------------------------------------ (a) stuck commands
def test_clear_stuck_commands_marks_old_pending_applied():
    repo = _repo()
    old_ts = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
    stuck_id = _insert_command(repo.conn, "set_temp", ts=old_ts)
    fresh_id = _insert_command(repo.conn, "prime")  # just enqueued, not stuck

    report = clear_stuck_commands(repo.conn, older_than_min=15)
    assert report["action"] == "clear_stuck_commands"
    assert report["done"] is True
    assert "marked 1 stuck" in report["detail"]

    rows = {r["id"]: r["status"] for r in repo.conn.execute("SELECT id, status FROM commands")}
    assert rows[stuck_id] == "applied"
    assert rows[fresh_id] == "pending"  # untouched
    repo.close()


def test_clear_stuck_commands_is_idempotent():
    repo = _repo()
    old_ts = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
    _insert_command(repo.conn, "set_temp", ts=old_ts)

    first = clear_stuck_commands(repo.conn, older_than_min=15)
    second = clear_stuck_commands(repo.conn, older_than_min=15)
    assert first["done"] is True and "marked 1 stuck" in first["detail"]
    assert second["done"] is True and "none stuck" in second["detail"]
    repo.close()


def test_clear_stuck_commands_missing_table_is_safe():
    repo = Repository(tempfile.mktemp(suffix=".db"))  # no ensure_schema() call
    report = clear_stuck_commands(repo.conn)
    assert report["done"] is False
    assert "not present" in report["detail"]
    repo.close()


# ------------------------------------------------------------------ (b) re-init a stuck device
def test_reenqueue_if_stuck_enqueues_prime_when_needs_priming():
    repo = _repo()
    _write_runtime_state(repo.conn, {"device": {"needs_priming": True}})
    report = reenqueue_if_stuck(repo.conn)
    assert report["done"] is True
    assert "enqueued 'prime'" in report["detail"]
    pending = repo.conn.execute(
        "SELECT type FROM commands WHERE status='pending'").fetchall()
    assert [r["type"] for r in pending] == ["prime"]
    repo.close()


def test_reenqueue_if_stuck_enqueues_safe_default_when_thermal_stalled():
    repo = _repo()
    _write_runtime_state(repo.conn, {"thermal_health": {"state": "stalled"}})
    report = reenqueue_if_stuck(repo.conn)
    assert report["done"] is True
    assert "safe_default" in report["detail"]
    repo.close()


def test_reenqueue_if_stuck_noop_when_not_stuck():
    repo = _repo()
    _write_runtime_state(repo.conn, {"device": {"needs_priming": False},
                                     "thermal_health": {"state": "ok"}})
    report = reenqueue_if_stuck(repo.conn)
    assert report["done"] is False
    assert repo.conn.execute("SELECT COUNT(*) c FROM commands").fetchone()["c"] == 0
    repo.close()


def test_reenqueue_if_stuck_dedupes_pending():
    repo = _repo()
    _write_runtime_state(repo.conn, {"device": {"needs_priming": True}})
    first = reenqueue_if_stuck(repo.conn)
    second = reenqueue_if_stuck(repo.conn)
    assert first["done"] is True and "enqueued" in first["detail"]
    assert second["done"] is True and "already pending" in second["detail"]
    count = repo.conn.execute(
        "SELECT COUNT(*) c FROM commands WHERE type='prime'").fetchone()["c"]
    assert count == 1  # not duplicated
    repo.close()


def test_reenqueue_if_stuck_no_runtime_state_yet():
    repo = _repo()
    report = reenqueue_if_stuck(repo.conn)
    assert report["done"] is False
    assert "never" in report["detail"] or "no runtime_state" in report["detail"]
    repo.close()


# ------------------------------------------------------------------ (c) stale daemon -> restart
def test_request_restart_if_stale_writes_flag_file(tmp_path):
    run_dir = str(tmp_path)
    hb = os.path.join(run_dir, "daemon.heartbeat")
    os.makedirs(run_dir, exist_ok=True)
    with open(hb, "w") as fh:
        fh.write("x")
    stale_mtime = datetime.now(timezone.utc).timestamp() - 500
    os.utime(hb, (stale_mtime, stale_mtime))

    report = request_restart_if_stale(run_dir, daemon_stale_s=90)
    assert report["done"] is True
    flag = os.path.join(run_dir, "restart.request")
    assert os.path.exists(flag)
    assert open(flag).read().strip() == "daemon"


def test_request_restart_if_stale_noop_when_fresh(tmp_path):
    run_dir = str(tmp_path)
    hb = os.path.join(run_dir, "daemon.heartbeat")
    os.makedirs(run_dir, exist_ok=True)
    with open(hb, "w") as fh:
        fh.write("x")  # fresh mtime

    report = request_restart_if_stale(run_dir, daemon_stale_s=90)
    assert report["done"] is False
    assert not os.path.exists(os.path.join(run_dir, "restart.request"))


def test_request_restart_if_stale_missing_heartbeat_is_safe(tmp_path):
    report = request_restart_if_stale(str(tmp_path), daemon_stale_s=90)
    assert report["done"] is False
    assert "not found" in report["detail"]


# ------------------------------------------------------------------ (d) clear a stale alert
def test_clear_alert_if_healthy_removes_when_heartbeats_fresh(tmp_path):
    run_dir = str(tmp_path)
    os.makedirs(run_dir, exist_ok=True)
    for name in ("daemon.heartbeat", "watchdog.heartbeat"):
        with open(os.path.join(run_dir, name), "w") as fh:
            fh.write("x")
    alert = os.path.join(run_dir, "watchdog.alert")
    with open(alert, "w") as fh:
        fh.write("RESTART STORM: daemon restarted 6 times in 5 min")

    report = clear_alert_if_healthy(run_dir)
    assert report["done"] is True
    assert not os.path.exists(alert)


def test_clear_alert_if_healthy_keeps_when_daemon_stale(tmp_path):
    run_dir = str(tmp_path)
    os.makedirs(run_dir, exist_ok=True)
    with open(os.path.join(run_dir, "watchdog.heartbeat"), "w") as fh:
        fh.write("x")
    stale_hb = os.path.join(run_dir, "daemon.heartbeat")
    with open(stale_hb, "w") as fh:
        fh.write("x")
    stale_mtime = datetime.now(timezone.utc).timestamp() - 500
    os.utime(stale_hb, (stale_mtime, stale_mtime))
    alert = os.path.join(run_dir, "watchdog.alert")
    with open(alert, "w") as fh:
        fh.write("still storming")

    report = clear_alert_if_healthy(run_dir)
    assert report["done"] is False
    assert os.path.exists(alert)  # left alone -- something may still be wrong


def test_clear_alert_if_healthy_no_file_present(tmp_path):
    report = clear_alert_if_healthy(str(tmp_path))
    assert report["done"] is True
    assert "no watchdog.alert" in report["detail"]


# ------------------------------------------------------------------ full battery
def test_run_repair_only_enqueues_safe_commands(tmp_path):
    repo = _repo()
    _write_runtime_state(repo.conn, {"device": {"needs_priming": True}})
    run_repair(repo.conn, str(tmp_path))
    types = {r["type"] for r in repo.conn.execute("SELECT type FROM commands")}
    assert types <= SAFE_REPAIR_COMMANDS
    assert types  # at least one command was actually enqueued in this scenario
    repo.close()


def test_run_repair_is_idempotent(tmp_path):
    repo = _repo()
    run_dir = str(tmp_path)
    os.makedirs(run_dir, exist_ok=True)
    old_ts = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
    _insert_command(repo.conn, "set_temp", ts=old_ts)
    _write_runtime_state(repo.conn, {"device": {"needs_priming": True}})
    with open(os.path.join(run_dir, "daemon.heartbeat"), "w") as fh:
        fh.write("x")
    stale_mtime = datetime.now(timezone.utc).timestamp() - 500
    os.utime(os.path.join(run_dir, "daemon.heartbeat"), (stale_mtime, stale_mtime))

    first = run_repair(repo.conn, run_dir)
    second = run_repair(repo.conn, run_dir)

    assert len(first["actions"]) == 4
    assert len(second["actions"]) == 4
    for a in first["actions"] + second["actions"]:
        assert set(a) == {"action", "done", "detail"}

    # re-running produced no duplicate 'prime' enqueue and didn't re-mark anything already applied
    prime_count = repo.conn.execute(
        "SELECT COUNT(*) c FROM commands WHERE type='prime'").fetchone()["c"]
    assert prime_count == 1
    repo.close()


def test_run_repair_never_raises_on_totally_empty_db(tmp_path):
    repo = Repository(tempfile.mktemp(suffix=".db"))
    report = run_repair(repo.conn, str(tmp_path))  # ensure_schema() runs inside run_repair
    assert len(report["actions"]) == 4
    repo.close()


def test_resolve_run_dir_matches_db_directory():
    db_path = "/some/dir/sleepctl.db"
    assert resolve_run_dir(db_path) == os.path.join("/some/dir", ".run")


# ------------------------------------------------------------------ CLI
def test_cli_repair_runs_and_prints_without_error(monkeypatch):
    from sleepctl.cli import build_parser

    db_path = tempfile.mktemp(suffix=".db")
    parser = build_parser()
    args = parser.parse_args(["repair", "--db", db_path])
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = args.func(args)
    assert rc == 0
    out = buf.getvalue()
    assert "sleepctl repair" in out
    assert "clear_stuck_commands" in out


def test_cli_repair_json_mode(monkeypatch):
    from sleepctl.cli import build_parser

    db_path = tempfile.mktemp(suffix=".db")
    parser = build_parser()
    args = parser.parse_args(["repair", "--db", db_path, "--json"])
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = args.func(args)
    assert rc == 0
    report = json.loads(buf.getvalue())
    assert len(report["actions"]) == 4
