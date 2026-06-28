"""Dashboard database layer.

Reuses the sleepctl SQLite database (same file) so the dashboard reads/writes the exact same
dataset the controller does. Adds the dashboard-only tables (users, sessions, notes, alerts,
settings, runtime_state, commands, data_sync, push_subscriptions) on top of the sleepctl
schema. ``Repository`` (from sleepctl) is used for all sleep-data reads.
"""

from __future__ import annotations

import sqlite3

from sleepctl.storage import schema as engine_schema
from sleepctl.storage.repository import Repository

from app.config import settings

_DASHBOARD_DDL = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    role TEXT DEFAULT 'owner',
    created TEXT
);
CREATE TABLE IF NOT EXISTS notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    text TEXT,
    created TEXT
);
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    type TEXT,
    severity TEXT,
    message TEXT,
    acknowledged INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS settings_kv (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS settings_changes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT, key TEXT, old_value TEXT, new_value TEXT
);
-- Singleton live snapshot written by the control daemon, read by the API/SSE.
CREATE TABLE IF NOT EXISTS runtime_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    updated TEXT,
    state TEXT, objective TEXT, mode TEXT,
    target_temp_f REAL, bed_temp_f REAL, room_temp_f REAL,
    stage TEXT, confidence REAL,
    target_level INTEGER, daemon_alive INTEGER,
    extra TEXT
);
-- Override queue: API enqueues, daemon applies on its next tick.
CREATE TABLE IF NOT EXISTS commands (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT, type TEXT, payload TEXT,
    status TEXT DEFAULT 'pending', applied_ts TEXT
);
CREATE TABLE IF NOT EXISTS data_sync (
    source TEXT PRIMARY KEY,
    last_sync TEXT, status TEXT, message TEXT
);
-- Singleton: latest phone/independent-sensor sample (iPhone accelerometer → BCG-derived
-- HR/HRV/movement). API writes it from /bcg/ingest; the daemon's BridgeWearableSource reads it.
CREATE TABLE IF NOT EXISTS live_sensor (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    updated TEXT, hr REAL, hrv REAL, movement REAL, source TEXT
);
CREATE TABLE IF NOT EXISTS push_subscriptions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    endpoint TEXT UNIQUE, p256dh TEXT, auth TEXT, created TEXT
);
CREATE INDEX IF NOT EXISTS idx_commands_status ON commands(status);
CREATE INDEX IF NOT EXISTS idx_notes_date ON notes(date);
CREATE INDEX IF NOT EXISTS idx_alerts_ack ON alerts(acknowledged);
"""


def connect(path: str | None = None) -> sqlite3.Connection:
    """Open the shared DB with the engine schema + dashboard tables applied."""
    # check_same_thread=False: FastAPI runs sync dependency setup/teardown across different
    # threadpool threads, and each request uses its own connection (no shared concurrent use).
    conn = engine_schema.connect(path or settings.db_path, check_same_thread=False)
    conn.executescript(_DASHBOARD_DDL)
    conn.commit()
    return conn


def get_repo() -> Repository:
    """A sleepctl Repository over the shared DB (ensures dashboard tables exist too)."""
    repo = Repository(settings.db_path, check_same_thread=False)
    repo.conn.executescript(_DASHBOARD_DDL)
    repo.conn.commit()
    return repo
