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
    intervention_summary TEXT,
    setpoint_version INTEGER,
    outcome_score REAL
);

CREATE TABLE IF NOT EXISTS context (
    date TEXT PRIMARY KEY,
    required_wake_time TEXT,
    work_start_time TEXT,
    first_commitment TEXT,
    outdoor_temp_f REAL,
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
    routine_complete INTEGER,
    subjective_quality REAL,
    grogginess REAL,
    daytime_performance REAL
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

-- Versioned snapshots of the learnable composite setpoint (the object the ML tailors).
CREATE TABLE IF NOT EXISTS setpoints (
    version INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    source TEXT,
    profile TEXT
);

-- Measured thermal-response calibration from the in-bed self-test (singleton). Records how fast
-- the bed actually COOLS and HEATS against the real in-bed thermal mass (levels/min and the
-- derived °F/min + minutes-of-lag), so the timing modules (pre-cool lead, smart-wake warm-up)
-- start from a controlled measurement instead of inferring it from noisy overnight data.
CREATE TABLE IF NOT EXISTS thermal_calibration (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    ts TEXT,
    cool_levels_per_min REAL,
    heat_levels_per_min REAL,
    cool_f_per_min REAL,
    heat_f_per_min REAL,
    cool_lag_min REAL,        -- measured minutes for a cool command to fully take effect (plateau)
    heat_lag_min REAL,        -- measured minutes for a heat command to fully take effect (plateau)
    source TEXT
);

-- Action ledger: the learning action chosen per night + its predictions and observed reward.
CREATE TABLE IF NOT EXISTS actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    night_date TEXT,
    action_name TEXT,
    params TEXT,
    predicted TEXT,
    confidence REAL,
    reward_observed REAL,
    applied INTEGER,
    source TEXT,
    creates_version INTEGER
);
CREATE INDEX IF NOT EXISTS idx_actions_night ON actions(night_date);

-- Anticipatory pre-cool efficacy ledger: each time the controller pre-cools ahead of a
-- vulnerable window, log it; after the window passes, label whether an awakening was
-- prevented. The lead-time learner optimises lead-times against this measured prevention.
CREATE TABLE IF NOT EXISTS precool_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    night_date TEXT,
    ts TEXT,
    window_type TEXT,
    lead_used_min REAL,
    eta_min REAL,
    prevented INTEGER,        -- 1 = no awakening in the window, 0 = awakening occurred, NULL = unresolved
    resolved INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_precool_night ON precool_events(night_date);

-- In-night architecture-steering ledger: each time the controller starts a "nudge me deeper"
-- maneuver (light-but-behind-the-deep-curve, wake-risk low), log it; after the response horizon
-- passes, label whether the stage actually went DEEP and whether it caused an awakening. The
-- (Phase 2) deepening-response learner uses this to learn whether cool-to-deepen works for YOU.
CREATE TABLE IF NOT EXISTS steer_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    night_date TEXT,
    ts TEXT,
    maneuver TEXT,            -- 'deepen' | 'rem_warm'
    stage_before TEXT,
    deep_deficit_min REAL,
    frac_of_night REAL,
    horizon_min REAL,
    applied INTEGER DEFAULT 1, -- 1 = the maneuver was ACTUATED, 0 = shadow/control (the steerer
                               -- would have acted but didn't) — the n-of-1 control arm
    deepened INTEGER,         -- 1 = reached DEEP within horizon, 0 = not, NULL = unresolved
    succeeded INTEGER,        -- reached the maneuver's TARGET stage (deep for deepen, REM for
                              -- rem_warm) within horizon — generic success for either direction
    caused_wake INTEGER,      -- 1 = wake event within horizon, 0 = none, NULL = unresolved
    resolved INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_steer_night ON steer_events(night_date);

CREATE INDEX IF NOT EXISTS idx_raw_samples_night ON raw_samples(night_date);
CREATE INDEX IF NOT EXISTS idx_interventions_night ON interventions(night_date);
CREATE INDEX IF NOT EXISTS idx_decisions_night ON decisions(night_date);

-- n-of-1 self-experiments: a randomized two-arm trial the user runs on themselves. Each
-- night is assigned an arm (a config tweak); outcomes are compared across arms with stats.
CREATE TABLE IF NOT EXISTS experiments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT,
    hypothesis TEXT,
    variable TEXT,
    arm_a TEXT,
    arm_b TEXT,
    metric TEXT,
    min_nights_per_arm INTEGER DEFAULT 5,
    washout_nights INTEGER DEFAULT 1,
    status TEXT DEFAULT 'active',
    created TEXT,
    assignments TEXT,
    result TEXT
);
CREATE INDEX IF NOT EXISTS idx_experiments_status ON experiments(status);
"""


# Idempotent column additions for tables that predate a field (CREATE TABLE IF NOT EXISTS won't
# add a column to an existing table). Each entry: (table, column, DDL type/default).
_MIGRATIONS = [
    ("steer_events", "applied", "INTEGER DEFAULT 1"),
    ("steer_events", "succeeded", "INTEGER"),
]


def _apply_migrations(conn: sqlite3.Connection) -> None:
    for table, column, decl in _MIGRATIONS:
        try:
            cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        except sqlite3.Error:
            continue
        if cols and column not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def init_db(conn: sqlite3.Connection) -> None:
    """Create all tables/indexes if they do not exist, then apply additive column migrations."""
    conn.executescript(_DDL)
    _apply_migrations(conn)
    conn.commit()


def connect(path: str, check_same_thread: bool = True) -> sqlite3.Connection:
    """Open a connection with sane pragmas and initialize the schema.

    ``check_same_thread=False`` lets a per-request connection be created and torn down across
    different worker threads (FastAPI runs sync dependency setup and cleanup in separate
    threadpool threads). Safe here because each connection is used by a single request.
    """
    conn = sqlite3.connect(path, check_same_thread=check_same_thread)
    conn.row_factory = sqlite3.Row
    if path != ":memory:":
        conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    init_db(conn)
    return conn
