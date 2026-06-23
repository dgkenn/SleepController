"""Control bridge: the API↔daemon contract.

The daemon OWNS the device. The API never calls pyEight; it enqueues a command the daemon
applies on its next tick, and reads the daemon's ``runtime_state`` snapshot for status. This
keeps control race-free and means a UI/API crash can never disrupt the closed loop.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

VALID_COMMANDS = {
    "start", "pause", "resume", "stop", "safe_default",
    "set_mode", "set_temp", "nudge_temp", "set_wake", "clear_wake",
    # Eight Sleep app parity
    "power_on", "power_off", "away_on", "away_off", "prime",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---- API side ----------------------------------------------------------------
def enqueue_command(conn: sqlite3.Connection, ctype: str, payload: dict | None = None) -> int:
    if ctype not in VALID_COMMANDS:
        raise ValueError(f"unknown command {ctype!r}")
    cur = conn.execute(
        "INSERT INTO commands (ts, type, payload, status) VALUES (?,?,?,'pending')",
        (_now(), ctype, json.dumps(payload or {})),
    )
    conn.commit()
    return cur.lastrowid


def read_runtime_state(conn: sqlite3.Connection, stale_seconds: int = 180) -> dict:
    row = conn.execute("SELECT * FROM runtime_state WHERE id = 1").fetchone()
    if row is None:
        return {"daemon_alive": False, "state": "UNKNOWN", "stale": True, "updated": None}
    d = dict(row)
    d["daemon_alive"] = bool(d.get("daemon_alive"))
    d["extra"] = json.loads(d["extra"]) if d.get("extra") else {}
    # freshness check
    stale = True
    if d.get("updated"):
        try:
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(d["updated"])).total_seconds()
            stale = age > stale_seconds
        except Exception:
            stale = True
    d["stale"] = stale
    if stale:
        d["daemon_alive"] = False
    return d


# ---- daemon side -------------------------------------------------------------
def next_pending_command(conn: sqlite3.Connection) -> dict | None:
    row = conn.execute(
        "SELECT * FROM commands WHERE status = 'pending' ORDER BY id ASC LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    d = dict(row)
    d["payload"] = json.loads(d["payload"]) if d["payload"] else {}
    return d


def mark_applied(conn: sqlite3.Connection, command_id: int) -> None:
    conn.execute(
        "UPDATE commands SET status='applied', applied_ts=? WHERE id=?",
        (_now(), command_id),
    )
    conn.commit()


def write_runtime_state(conn: sqlite3.Connection, snapshot: dict) -> None:
    conn.execute(
        """INSERT INTO runtime_state
        (id, updated, state, objective, mode, target_temp_f, bed_temp_f, room_temp_f,
         stage, confidence, target_level, daemon_alive, extra)
        VALUES (1,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(id) DO UPDATE SET
         updated=excluded.updated, state=excluded.state, objective=excluded.objective,
         mode=excluded.mode, target_temp_f=excluded.target_temp_f,
         bed_temp_f=excluded.bed_temp_f, room_temp_f=excluded.room_temp_f,
         stage=excluded.stage, confidence=excluded.confidence,
         target_level=excluded.target_level, daemon_alive=excluded.daemon_alive,
         extra=excluded.extra""",
        (
            _now(), snapshot.get("state"), snapshot.get("objective"), snapshot.get("mode", "auto"),
            snapshot.get("target_temp_f"), snapshot.get("bed_temp_f"), snapshot.get("room_temp_f"),
            snapshot.get("stage"), snapshot.get("confidence"), snapshot.get("target_level"),
            int(bool(snapshot.get("daemon_alive", True))),
            json.dumps(snapshot.get("extra", {})),
        ),
    )
    conn.commit()
