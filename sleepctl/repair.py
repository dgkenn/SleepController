"""Safe, idempotent self-repair battery -- shared by the dashboard API's ``POST /diag/repair``
and the standalone ``sleepctl repair`` CLI command.

Lives in the ENGINE package (not ``dashboard``) on purpose, matching the existing layering rule
documented in ``sleepctl/diagnostics.py`` ("never imports from dashboard") -- this lets the CLI
run the repair battery directly against the SQLite DB + ``.run`` directory with no dashboard API
process required, while the dashboard API reuses the exact same logic instead of re-implementing
it. The dashboard's ``commands``/``runtime_state`` tables (defined in
``dashboard/api/app/db.py``) are duplicated here as minimal ``CREATE TABLE IF NOT EXISTS`` DDL
so the CLI works standalone even against a DB the dashboard has never touched; when the dashboard
HAS already created them, this is a harmless no-op (``IF NOT EXISTS`` never touches an existing
table), so the dashboard's schema stays authoritative.

Every action here is deliberately narrow and SAFE:
  * it never sends a device command outside :data:`SAFE_REPAIR_COMMANDS` (a hardcoded subset of
    ``dashboard/api/app/bridge.py``'s ``VALID_COMMANDS`` -- kept in sync by hand since this
    module cannot import that dashboard module; both are simple, rarely-changed sets),
  * it only ever *reads* ``.run`` heartbeat files and writes the two well-known flag files
    (``restart.request`` / removing ``watchdog.alert``) that the watchdog already consumes,
  * every sub-action is wrapped so one failing action can never take down the rest of the
    battery, and each is safe to call repeatedly (see each function's docstring for exactly why).
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone

# Subset of dashboard/api/app/bridge.py's VALID_COMMANDS that a one-click repair is allowed to
# enqueue -- both are benign re-init/settle commands, never anything that changes a setpoint or
# runs an interactive sequence. Kept explicit (not imported) so this module has zero dependency
# on the dashboard package; both sets confirmed to intersect as intended, see repo grep of
# VALID_COMMANDS.
SAFE_REPAIR_COMMANDS = {"safe_default", "prime"}

DEFAULT_STUCK_MINUTES = 15
DEFAULT_DAEMON_STALE_S = 90     # mirrors dashboard/api/app/diagnostics.DAEMON_HEARTBEAT_STALE_S
DEFAULT_WATCHDOG_STALE_S = 60   # mirrors dashboard/api/app/diagnostics.WATCHDOG_HEARTBEAT_STALE_S

_COMMANDS_DDL = """
CREATE TABLE IF NOT EXISTS commands (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT, type TEXT, payload TEXT,
    status TEXT DEFAULT 'pending', applied_ts TEXT
);
CREATE INDEX IF NOT EXISTS idx_commands_status ON commands(status);
"""

_RUNTIME_STATE_DDL = """
CREATE TABLE IF NOT EXISTS runtime_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    updated TEXT,
    state TEXT, objective TEXT, mode TEXT,
    target_temp_f REAL, bed_temp_f REAL, room_temp_f REAL,
    stage TEXT, confidence REAL,
    target_level INTEGER, daemon_alive INTEGER,
    extra TEXT
);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Idempotently make sure the (dashboard-owned) ``commands``/``runtime_state`` tables exist,
    so the repair battery works standalone even on a DB the dashboard API has never opened."""
    conn.executescript(_COMMANDS_DDL)
    conn.executescript(_RUNTIME_STATE_DDL)
    conn.commit()


def resolve_run_dir(db_path: str) -> str:
    """The ``.run`` directory alongside ``db_path`` -- same convention as
    ``dashboard/api/app/bridge.run_dir`` (next to the SQLite DB), just driven by an explicit
    path instead of the ``SLEEPCTL_DB`` env var, since CLI callers already pass ``--db``."""
    root = os.path.dirname(os.path.abspath(db_path)) if db_path not in (None, ":memory:") else os.getcwd()
    return os.path.join(root, ".run")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _report(action: str, done: bool, detail: str) -> dict:
    return {"action": action, "done": bool(done), "detail": detail}


# ---------------------------------------------------------------- (a) stuck commands queue
def clear_stuck_commands(conn: sqlite3.Connection, older_than_min: int = DEFAULT_STUCK_MINUTES,
                         now: datetime | None = None) -> dict:
    """Mark PENDING commands older than ``older_than_min`` as 'applied' (abandoned) so a wedged
    entry can't block/confuse the daemon's queue forever. Idempotent: a second run finds nothing
    left to mark (already-applied rows aren't touched), so it's always safe to re-run."""
    now = now or _now()
    try:
        rows = conn.execute("SELECT id, ts FROM commands WHERE status='pending'").fetchall()
    except sqlite3.OperationalError:
        return _report("clear_stuck_commands", False, "commands table not present")

    cutoff = now - timedelta(minutes=older_than_min)
    stuck_ids: list[int] = []
    for r in rows:
        d = dict(r)
        try:
            ts = datetime.fromisoformat(d["ts"])
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
        except Exception:
            continue
        if ts < cutoff:
            stuck_ids.append(d["id"])

    total_pending = len(rows)
    if not stuck_ids:
        return _report("clear_stuck_commands", True,
                       f"queue has {total_pending} pending command(s), none stuck "
                       f"(older than {older_than_min}min)")

    qmarks = ",".join("?" * len(stuck_ids))
    conn.execute(f"UPDATE commands SET status='applied', applied_ts=? WHERE id IN ({qmarks})",
                [_iso(now), *stuck_ids])
    conn.commit()
    return _report("clear_stuck_commands", True,
                   f"queue had {total_pending} pending; marked {len(stuck_ids)} stuck "
                   f"(older than {older_than_min}min) as abandoned/applied")


# ---------------------------------------------------------------- (b) re-init a stuck device
def _enqueue_safe(conn: sqlite3.Connection, ctype: str, now: datetime) -> int:
    if ctype not in SAFE_REPAIR_COMMANDS:
        raise ValueError(f"{ctype!r} is not in SAFE_REPAIR_COMMANDS")
    cur = conn.execute(
        "INSERT INTO commands (ts, type, payload, status) VALUES (?,?,?,'pending')",
        (_iso(now), ctype, json.dumps({})),
    )
    conn.commit()
    return cur.lastrowid


def reenqueue_if_stuck(conn: sqlite3.Connection, now: datetime | None = None) -> dict:
    """If the last-reported device state looks stuck (needs priming, or thermal control
    reports 'stalled'), enqueue the matching SAFE re-init command (``prime`` / ``safe_default``)
    -- unless one of that type is already pending, so repeated calls don't pile up duplicate
    commands (idempotent)."""
    now = now or _now()
    try:
        row = conn.execute("SELECT extra FROM runtime_state WHERE id=1").fetchone()
    except sqlite3.OperationalError:
        return _report("reenqueue_if_stuck", False, "runtime_state table not present")
    if row is None:
        return _report("reenqueue_if_stuck", False, "no runtime_state has been reported yet")

    try:
        extra = json.loads(dict(row)["extra"]) if dict(row).get("extra") else {}
    except Exception:
        extra = {}
    if not isinstance(extra, dict):
        extra = {}

    device = extra.get("device") if isinstance(extra.get("device"), dict) else {}
    thermal = extra.get("thermal_health") if isinstance(extra.get("thermal_health"), dict) else {}

    ctype = None
    why = None
    if device.get("needs_priming"):
        ctype, why = "prime", "device reports needs_priming=true"
    elif thermal.get("state") == "stalled":
        ctype, why = "safe_default", "thermal_health.state='stalled'"

    if ctype is None:
        return _report("reenqueue_if_stuck", False, "device does not look stuck; nothing enqueued")

    existing = conn.execute(
        "SELECT 1 FROM commands WHERE type=? AND status='pending' LIMIT 1", (ctype,)
    ).fetchone()
    if existing:
        return _report("reenqueue_if_stuck", True,
                       f"{why} -> a '{ctype}' command is already pending; not duplicating")

    _enqueue_safe(conn, ctype, now)
    return _report("reenqueue_if_stuck", True, f"{why} -> enqueued '{ctype}'")


# ---------------------------------------------------------------- (c) stale daemon -> restart
def request_restart_if_stale(run_dir: str, daemon_stale_s: int = DEFAULT_DAEMON_STALE_S,
                             now_ts: float | None = None) -> dict:
    """Write ``.run/restart.request`` = ``daemon`` when ``daemon.heartbeat`` is stale -- the
    windows-watchdog.ps1 remote-restart protocol (it reads this file, force-stops the named
    component, deletes the flag, and lets its normal supervise loop restart it). Idempotent:
    writing the same one-word content twice has no additional effect; the watchdog consumes and
    deletes the flag on its own within one supervise tick."""
    now_ts = now_ts if now_ts is not None else time.time()
    hb_path = os.path.join(run_dir, "daemon.heartbeat")
    try:
        age = now_ts - os.path.getmtime(hb_path)
    except OSError:
        return _report("request_restart_if_stale", False,
                       "daemon.heartbeat not found -- cannot judge staleness, leaving it alone")

    if age <= daemon_stale_s:
        return _report("request_restart_if_stale", False,
                       f"daemon heartbeat is fresh ({age:.0f}s ago); no restart needed")

    req_path = os.path.join(run_dir, "restart.request")
    try:
        os.makedirs(run_dir, exist_ok=True)
        with open(req_path, "w", encoding="utf-8") as fh:
            fh.write("daemon")
    except Exception as exc:
        return _report("request_restart_if_stale", False,
                       f"daemon heartbeat stale ({age:.0f}s ago) but could not write "
                       f"restart.request: {exc}")
    return _report("request_restart_if_stale", True,
                   f"daemon heartbeat stale ({age:.0f}s ago) -> wrote .run/restart.request=daemon")


# ---------------------------------------------------------------- (d) clear a stale alert
def clear_alert_if_healthy(run_dir: str, daemon_stale_s: int = DEFAULT_DAEMON_STALE_S,
                           watchdog_stale_s: int = DEFAULT_WATCHDOG_STALE_S,
                           now_ts: float | None = None) -> dict:
    """Remove a stale ``.run/watchdog.alert`` -- but ONLY when both the daemon and watchdog
    heartbeats currently look healthy (the same "nothing is currently holding" signal the
    watchdog itself uses to auto-clear the marker -- see ``Clear-AlertIfNoneStorming`` in
    ``scripts/windows-watchdog.ps1``). If either heartbeat is stale, something may still be
    storming, so the alert is deliberately left in place. Idempotent: a missing file is a
    trivial success; clearing an already-clear file is a no-op on the next call."""
    alert_path = os.path.join(run_dir, "watchdog.alert")
    if not os.path.exists(alert_path):
        return _report("clear_alert_if_healthy", True, "no watchdog.alert present")

    now_ts = now_ts if now_ts is not None else time.time()

    def _age(name: str) -> float | None:
        try:
            return now_ts - os.path.getmtime(os.path.join(run_dir, name))
        except OSError:
            return None

    daemon_age = _age("daemon.heartbeat")
    watchdog_age = _age("watchdog.heartbeat")
    healthy = (
        daemon_age is not None and daemon_age <= daemon_stale_s and
        watchdog_age is not None and watchdog_age <= watchdog_stale_s
    )
    if not healthy:
        return _report("clear_alert_if_healthy", False,
                       "watchdog.alert present and heartbeats aren't both currently fresh -- "
                       "leaving it (something may still be storming)")

    try:
        os.remove(alert_path)
    except Exception as exc:
        return _report("clear_alert_if_healthy", False,
                       f"heartbeats look healthy but could not remove watchdog.alert: {exc}")
    return _report("clear_alert_if_healthy", True,
                   "heartbeats look healthy -> cleared stale watchdog.alert")


# ---------------------------------------------------------------- entry point
def run_repair(conn: sqlite3.Connection, run_dir: str, *,
               stuck_minutes: int = DEFAULT_STUCK_MINUTES,
               daemon_stale_s: int = DEFAULT_DAEMON_STALE_S,
               watchdog_stale_s: int = DEFAULT_WATCHDOG_STALE_S) -> dict:
    """Run the full safe-repair battery. Never raises: each sub-action is independently
    try/except-safe internally (see each function), and this just sequences them. Every call is
    safe to repeat -- see each action's docstring for why."""
    ensure_schema(conn)
    now = _now()
    now_ts = now.timestamp()

    def _safe(fn, action_name):
        try:
            return fn()
        except Exception as exc:  # a single buggy action must never break the battery
            return _report(action_name, False, f"action crashed: {exc!r}")

    actions = [
        _safe(lambda: clear_stuck_commands(conn, stuck_minutes, now), "clear_stuck_commands"),
        _safe(lambda: reenqueue_if_stuck(conn, now), "reenqueue_if_stuck"),
        _safe(lambda: request_restart_if_stale(run_dir, daemon_stale_s, now_ts),
              "request_restart_if_stale"),
        _safe(lambda: clear_alert_if_healthy(run_dir, daemon_stale_s, watchdog_stale_s, now_ts),
              "clear_alert_if_healthy"),
    ]
    return {"ran_at": _iso(now), "actions": actions}
