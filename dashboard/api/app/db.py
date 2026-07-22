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
-- Singleton: latest DEDICATED cardiac sample from a separate BLE HR sensor (e.g. a Polar Verity
-- Sense armband forwarded by scripts/verity_forwarder.py). Kept SEPARATE from live_sensor so the
-- Verity's authoritative HR/HRV and the iPhone accelerometer's movement never clobber each other:
-- bridge.read_fused_sensor MERGES the two channels per-field (Verity authoritative for HR/HRV,
-- phone authoritative for movement, each gated by its own freshness). Zero device risk — an
-- independent sensor; the Pod is never touched. ``hr`` bpm, ``hrv`` = RMSSD ms from RR intervals.
CREATE TABLE IF NOT EXISTS live_cardiac (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    updated TEXT, hr REAL, hrv REAL, source TEXT
);
-- One row per night recording HOW you were woken (stage, how early, window, forced), joined
-- with the morning check-in grogginess to personalize the wake tuning.
CREATE TABLE IF NOT EXISTS wake_log (
    date TEXT PRIMARY KEY,
    woke_from_stage TEXT, minutes_early REAL, window_min INTEGER,
    forced INTEGER, p_wake REAL, wake_thermal_f REAL, created TEXT,
    onset_warm_f REAL, night_type TEXT,
    onset_cold_settle_f REAL, warm_pulse_on INTEGER
);
CREATE TABLE IF NOT EXISTS push_subscriptions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    endpoint TEXT UNIQUE, p256dh TEXT, auth TEXT, created TEXT
);
-- Timestamped thermal-response samples captured every control tick WHILE the bed is actively
-- heating/cooling toward a target (|target-device| > 3). Feeds fine-tuning of the controller's
-- lead-time / pre-compensation model; parked-at-setpoint ticks are skipped to keep it lean.
CREATE TABLE IF NOT EXISTS thermal_samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    device_level INTEGER,      -- actual Pod water-side level
    target_level INTEGER,      -- commanded/accepted target level
    delta_level INTEGER,       -- target_level - device_level (signed gap; +=needs warming, -=needs cooling)
    direction TEXT,            -- 'heating' | 'cooling' | 'hold'
    bed_temp_f REAL,
    room_temp_f REAL,          -- key for the ambient-limited cooling model
    state TEXT,                -- controller state (induction/maintenance/...)
    session_mode TEXT          -- night | induce | nap_*
);
CREATE INDEX IF NOT EXISTS idx_thermal_samples_ts ON thermal_samples(ts);
-- Append-only history of phone/independent-sensor (accelerometer-derived BCG) samples. The
-- ``live_sensor`` table above is a singleton overwritten on every /bcg/ingest so the daemon can
-- read "latest" cheaply; this table instead accumulates every sample overnight so there's a
-- time-series dataset for later model training / nightly learning. Pruned to a rolling window
-- on write (see ``bridge.append_sensor_sample``) so it can't grow forever; the underlying SQLite
-- file is already covered by the off-box encrypted backup, so this history is durably saved
-- off-box automatically regardless of local retention.
CREATE TABLE IF NOT EXISTS sensor_samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    hr REAL, hrv REAL, movement REAL, source TEXT,
    fs REAL,                   -- sample rate (Hz) of the accel batch this sample was derived from
    n_samples INTEGER          -- raw accel readings ingested in this batch
);
CREATE INDEX IF NOT EXISTS idx_sensor_samples_ts ON sensor_samples(ts);
CREATE INDEX IF NOT EXISTS idx_commands_status ON commands(status);
CREATE INDEX IF NOT EXISTS idx_notes_date ON notes(date);
CREATE INDEX IF NOT EXISTS idx_alerts_ack ON alerts(acknowledged);
"""


# Idempotent column adds for tables that predate a column (CREATE TABLE IF NOT EXISTS won't add
# columns to an existing table). Each entry: (table, column, type).
_MIGRATIONS = [
    ("wake_log", "onset_warm_f", "REAL"),
    ("wake_log", "night_type", "TEXT"),
    ("wake_log", "onset_cold_settle_f", "REAL"),
    ("wake_log", "warm_pulse_on", "INTEGER"),
]


def _apply_migrations(conn: sqlite3.Connection) -> None:
    for table, col, typ in _MIGRATIONS:
        try:
            cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
            if col not in cols:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
        except Exception:
            pass  # table may not exist yet on a brand-new DB; the DDL above creates it current
    conn.commit()


def connect(path: str | None = None) -> sqlite3.Connection:
    """Open the shared DB with the engine schema + dashboard tables applied."""
    # check_same_thread=False: FastAPI runs sync dependency setup/teardown across different
    # threadpool threads, and each request uses its own connection (no shared concurrent use).
    conn = engine_schema.connect(path or settings.db_path, check_same_thread=False)
    conn.executescript(_DASHBOARD_DDL)
    _apply_migrations(conn)
    conn.commit()
    return conn


# Set once ``init_schema()`` has run (see ``main.py``'s startup event). Guards ``get_repo()``
# against re-running the full engine DDL + dashboard DDL + migrations (a executescript of a
# dozen CREATE TABLE/INDEX statements plus several PRAGMA table_info migration checks) on every
# API request, SSE tick, and /bcg/ingest call -- previously that ran once per call on a hot path.
_schema_initialized = False


def init_schema(path: str | None = None) -> None:
    """Run the engine schema + dashboard DDL/migrations ONCE. Call at API startup (and the
    daemon's own init, via the lazy fallback in ``get_repo()`` below) so tables are guaranteed to
    exist before the first request/tick -- not re-verified on every single one. Idempotent (safe
    to call more than once, e.g. from tests that import ``app.db`` directly)."""
    global _schema_initialized
    conn = connect(path)
    conn.close()
    _schema_initialized = True


def get_repo() -> Repository:
    """A sleepctl Repository over the shared DB, for per-request/per-tick use.

    Schema/DDL/migrations are NOT re-run here: ``init_schema()`` (called once at API/daemon
    startup) already guarantees the tables exist, so each call just opens a lightweight
    connection (see ``schema.connect_light``) instead of repeating an ``executescript`` +
    migration pass. The lazy call below is a safety net for any path that reaches ``get_repo()``
    before startup has run (e.g. a script importing ``app.db`` directly) so correctness never
    depends on call order.
    """
    if not _schema_initialized:
        init_schema()
    return Repository(settings.db_path, check_same_thread=False, ensure_schema=False)
