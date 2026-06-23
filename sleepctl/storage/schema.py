"""SQLite schema for the 3-layer dataset + ledgers.

Layers: (1) ``raw_samples`` windowed time-series, (2) ``nightly_summaries``,
(3) ``context`` antecedents. Plus an ``interventions`` ledger, a per-tick
``decisions`` log, and ``baselines`` snapshots. The shape is deliberately flat and
ML-friendly (one row per sample / per night / per intervention).
"""

from __future__ import annotations

import sqlite3

_DDL = """
CREATE TABLE IF NOT EXISTS raw_samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    night_date TEXT,
    stage TEXT,
    stage_confidence REAL,
    heart_rate REAL,
    hrv REAL,
    respiratory_rate REAL,
    movement REAL,
    presence INTEGER,
    bed_temp_f REAL,
    room_temp_f REAL,
    commanded_level INTEGER,
    controller_state TEXT,
    wake_event INTEGER,
    data_age_seconds REAL
);

CREATE TABLE IF NOT EXISTS nightly_summaries (
    date TEXT PRIMARY KEY,
    bedtime TEXT,
    wake_time TEXT,
    total_sleep_min REAL,
    sleep_onset_latency_min REAL,
    deep_min REAL,
    rem_min REAL,
    light_min REAL,
    wake_events INTEGER,
    waso_min REAL,
    sleep_efficiency REAL,
    avg_hr REAL,
    avg_hrv REAL,
    avg_respiratory_rate REAL,
    temp_profile_summary TEXT,
    intervention_summary TEXT
);

CREATE TABLE IF NOT EXISTS context (
    date TEXT PRIMARY KEY,
    required_wake_time TEXT,
    work_start_time TEXT,
    first_commitment TEXT,
    sleep_opportunity_min REAL,
    is_short_sleep_day INTEGER,
    schedule_variable INTEGER,
    steps INTEGER,
    workout_timing TEXT,
    workout_intensity REAL,
    resting_hr_trend REAL,
    hr_recovery REAL,
    strain REAL,
    caffeine INTEGER,
    alcohol INTEGER,
    screen_time_min REAL,
    stress REAL,
    travel INTEGER,
    illness INTEGER,
    late_night_work INTEGER,
    routine_complete INTEGER
);

CREATE TABLE IF NOT EXISTS interventions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    night_date TEXT,
    controller_state TEXT,
    action TEXT,
    magnitude_f REAL,
    reason TEXT,
    held INTEGER,
    reverted INTEGER,
    outcome_delta REAL
);

CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    night_date TEXT,
    state TEXT,
    objective TEXT,
    thermal_intent TEXT,
    target_temp_f REAL,
    target_level INTEGER,
    action TEXT,
    reason TEXT,
    confidence REAL,
    log_payload TEXT
);

CREATE TABLE IF NOT EXISTS baselines (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    metrics TEXT
);

CREATE INDEX IF NOT EXISTS idx_raw_samples_night ON raw_samples(night_date);
CREATE INDEX IF NOT EXISTS idx_interventions_night ON interventions(night_date);
CREATE INDEX IF NOT EXISTS idx_decisions_night ON decisions(night_date);
"""


def init_db(conn: sqlite3.Connection) -> None:
    """Create all tables/indexes if they do not exist."""
    conn.executescript(_DDL)
    conn.commit()


def connect(path: str) -> sqlite3.Connection:
    """Open a connection with sane pragmas and initialize the schema."""
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    if path != ":memory:":
        conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    init_db(conn)
    return conn
