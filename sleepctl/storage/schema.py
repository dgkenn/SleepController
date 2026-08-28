"""SQLite schema for the 3-layer dataset + ledgers.

Layers: (1) ``raw_samples`` windowed time-series, (2) ``nightly_summaries``,
(3) ``context`` antecedents. Plus an ``interventions`` ledger, a per-tick
``decisions`` log, and ``baselines`` snapshots. The shape is deliberately flat and
ML-friendly (one row per sample / per night / per intervention).

TIMESTAMP CONVENTION -- read this before comparing timestamps across tables.

This database mixes two conventions, and the mismatch has produced real, silent bugs: a
capacity detector that read every row as 240 minutes stale and so never fired; a stuck-prime
duration inflated by the machine's whole UTC offset (297 min reported for a ~60 min episode);
and two analyses that compared local-time windows against UTC rows and drew the wrong
conclusion before the error was caught.

  * NAIVE LOCAL -- written with ``datetime.now()``:
        raw_samples, decisions, state_history, thermal_samples, precool_events,
        steer_events, events, interventions   (the engine/daemon tables)

  * AWARE UTC -- written with ``datetime.now(timezone.utc)``:
        sensor_samples, rr_intervals, actigraphy, live_cardiac, live_sensor,
        runtime_state, commands               (the dashboard ingest/bridge tables)

Rules:
  1. NEVER compare a timestamp from one group against the other without normalizing.
  2. When normalizing, a NAIVE value means LOCAL time -- never stamp it UTC. See
     ``sleepctl.diagnostics_thermal._to_dt`` and
     ``sleepctl.learning.prevention_timing._as_dt``, which both do this correctly.
  3. SQLite's ``datetime('now')`` is UTC, so a bare
     ``WHERE ts > datetime('now','-N minutes')`` silently matches NOTHING on the
     naive-local tables.
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
    data_age_seconds REAL,
    -- When this row was actually OBSERVED (naive local, like `ts`). `ts` carries the POD
    -- FRAME's timestamp, which refreshes about every 60 s while the daemon ticks every ~30 s
    -- -- so two consecutive rows routinely share one `ts` while their wearable data is up to a
    -- minute apart. Measured on the night of 2026-08-27: 673 of 682 distinct timestamps had two
    -- rows. `ts` is left alone because night bucketing, rollups and every existing query depend
    -- on it; anything measuring an INTERVAL should read `sample_ts` instead, where a 30 s gap
    -- shows up as 30 s rather than as zero.
    sample_ts TEXT
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
    warmback_levels_per_min REAL,  -- passive warm-back: how fast the bed drifts warm with the element off
    warmback_lag_min REAL,         -- minutes for the passive warm-back to return toward neutral
    source TEXT
);

-- Personal COMFORT mapping from the in-bed comfort sweep (singleton). What YOU feel at a few
-- commanded temperatures anchors the controller's neutral to the covered-body reality of this
-- mattress + sheets, not just the device's water scale.
CREATE TABLE IF NOT EXISTS comfort_profile (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    ts TEXT,
    neutral_f REAL,           -- the temperature you rated "just right"
    cool_edge_f REAL,         -- coldest still-comfortable temperature
    warm_edge_f REAL,         -- warmest still-comfortable temperature
    ratings TEXT,             -- JSON: [{f, rating}] raw sweep data
    source TEXT
);

-- Resting-physiology baseline captured quiet-and-awake in bed (singleton): anchors the arousal /
-- wake-risk / precursor detectors to YOUR numbers on THIS mattress, correct from night one.
CREATE TABLE IF NOT EXISTS resting_baseline (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    ts TEXT,
    hr REAL,
    hrv REAL,
    rr REAL,
    movement REAL,
    n_samples INTEGER,
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

-- Standing "does the controller help?" efficacy trial: every night is assigned CONTROLLED
-- (normal closed loop) or HELD (do-no-harm fixed-neutral baseline: steering/preemption off,
-- neutral setpoint, still clamped + smart-wake). Compared over many nights with significance
-- (sleepctl.eval.efficacy). Opt-in, defaults OFF (see the ``efficacy_config`` singleton below).
CREATE TABLE IF NOT EXISTS efficacy_nights (
    night_date TEXT PRIMARY KEY,
    arm TEXT NOT NULL,             -- 'controlled' | 'held'
    wake_events INTEGER,
    deep_pct REAL,
    efficiency REAL,
    outcome_score REAL,
    resolved INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_efficacy_nights_arm ON efficacy_nights(arm);

-- Efficacy-trial config (singleton). Engine-level (not the dashboard's settings_kv) so
-- sleepctl.eval.efficacy works standalone against a plain Repository, same as
-- thermal_calibration/comfort_profile/resting_baseline above.
CREATE TABLE IF NOT EXISTS efficacy_config (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    enabled INTEGER DEFAULT 0,     -- opt-in: defaults OFF
    block_nights INTEGER DEFAULT 3
);

-- Randomized efficacy MICRO-trials (sleepctl.ml.efficacy_trial): on a capped fraction of
-- ELIGIBLE (normal, full-length) nights, randomize the controller between 'active' (normal
-- closed loop) and 'sham' (do-no-harm neutral hold) to measure the controller's TRUE CAUSAL
-- effect on wake_events/deep%/HRV/efficiency, with a confidence interval, rather than assuming
-- it helps. Kept as its OWN table rather than extending efficacy_nights above: the two systems
-- use different arm vocabularies ('active'/'sham' vs 'controlled'/'held') and
-- efficacy_nights.night_date is a primary key, so sharing rows would let one system silently
-- clobber the other's assignment on any night both happened to be enabled. Every planned night
-- gets a row here (including ineligible ones, with eligible=0) so the schedule is fully
-- auditable, not just the randomized nights.
CREATE TABLE IF NOT EXISTS efficacy_trials (
    night_date TEXT PRIMARY KEY,
    arm TEXT NOT NULL,             -- 'active' | 'sham'
    eligible INTEGER NOT NULL DEFAULT 1,  -- 0 = ineligible (short/recovery/nap night); arm forced 'active'
    seed REAL,                     -- deterministic [0,1) draw from hash(night_date) -- audit trail
    wake_events INTEGER,
    deep_pct REAL,
    hrv REAL,
    efficiency REAL,
    outcome_score REAL,
    resolved INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_efficacy_trials_arm ON efficacy_trials(arm);

-- n-of-1 THERMAL DOSE-RESPONSE trial (sleepctl.ml.thermal_trial): on a capped, block-balanced
-- fraction of ELIGIBLE (normal, full-length) nights, randomize the MAINTENANCE-phase neutral
-- setpoint across a small offset ladder (e.g. -1.5..+0.8 F around the learned neutral) to find
-- which offset minimizes THIS user's wake_events -- deliberately including mild WARMING
-- (Raymann et al. 2008, DOI 10.1093/brain/awm315) against the controller's default cool bias,
-- not just more cooling. Kept as its OWN table rather than reusing efficacy_trials/efficacy_nights
-- for the same reason those two don't share one: this trial's arm vocabulary is a continuous
-- offset ladder (formatted '+0.40'/'-1.50'/'+0.00' strings), not a fixed 'active'/'sham' or
-- 'controlled'/'held' pair, and *_trials.night_date is a primary key elsewhere too, so sharing
-- rows would let one randomization scheme silently clobber another's assignment on any night
-- more than one trial is enabled. Every planned night gets a row (including ineligible ones,
-- eligible=0) so the schedule is fully auditable, not just the randomized nights.
CREATE TABLE IF NOT EXISTS thermal_trials (
    night_date TEXT PRIMARY KEY,
    arm TEXT NOT NULL,             -- formatted maintenance offset, e.g. '+0.40' ('+0.00' = control)
    offset_f REAL NOT NULL,        -- the actual (comfort-band-clamped) offset applied, degrees F
    eligible INTEGER NOT NULL DEFAULT 1,  -- 0 = ineligible (short/recovery/nap night); forced control
    block_key TEXT,                 -- night-type stratum used to balance arms WITHIN a block
    seed REAL,                      -- deterministic [0,1) draw -- audit trail, not itself the decision
    wake_events INTEGER,            -- primary outcome (the user's #1 problem: staying asleep)
    deep_min REAL,
    sleep_efficiency REAL,
    hrv REAL,
    subjective_rating REAL,         -- morning subjective quality check-in, if logged
    resolved INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_thermal_trials_arm ON thermal_trials(arm);

-- Structured, queryable event log: "what happened and when" as one query instead of grepping
-- unstructured text logs. Both daemons emit best-effort rows here at lifecycle/error/state/device
-- moments (see LiveDashboardDaemon._emit_event / DashboardDaemon._emit_event). severity is one of
-- info|warn|error|critical. ``data`` is a JSON blob of structured context (params, reasons, etc).
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    category TEXT,
    severity TEXT,
    code TEXT,
    message TEXT,
    data TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);

-- Randomized controller-policy trial (n-of-1, blinded). One row per night.
--
-- The endpoint of this system is whether tomorrow is better, NOT whether a sleep-stage label was
-- correct -- an estimator can be a poor stager and still a useful control signal, and a good
-- stager can make a terrible controller if small errors make the bed hunt all night. So efficacy
-- is judged by randomizing the CONTROLLER and reading morning outcomes, with stage accuracy kept
-- as a diagnostic (see sleepctl/eval/controller_sanity.py) rather than an endpoint.
--
-- BLINDING CONTRACT, enforced in sleepctl/eval/trial.py: ``policy`` must not be shown to the user
-- until ``outcome_locked`` is set by the morning check-in. ``revealed`` records that it has since
-- been surfaced. Assignment is block-randomized WITHIN night_type, because rotating shifts would
-- otherwise confound arms at n-of-1 sample sizes, and runs in multi-night blocks because sleep
-- debt carries over (a bad night deepens the next one regardless of policy).
CREATE TABLE IF NOT EXISTS trial_assignments (
    night_date TEXT PRIMARY KEY,
    policy TEXT NOT NULL,             -- arm label, e.g. "A_static" / "B_reactive" / "C_stabilized"
    block_id TEXT,                    -- which randomization block this night belongs to
    block_index INTEGER,              -- position within the block
    night_type TEXT,                  -- stratum ("work" / "off" / ...) -- arms balance within this
    controller_version TEXT,          -- git sha / version of the code that actually ran
    seed TEXT,                        -- randomization seed, so assignment is reproducible/auditable
    assigned_ts TEXT,
    outcome_locked INTEGER DEFAULT 0, -- morning check-in recorded -> outcome can no longer change
    outcome_locked_ts TEXT,
    revealed INTEGER DEFAULT 0,       -- policy has been shown to the user (only legal once locked)
    revealed_ts TEXT,
    notes TEXT
);
CREATE INDEX IF NOT EXISTS idx_trial_assignments_policy ON trial_assignments(policy);

-- Append-only trend history of runtime_state snapshots (a full row per throttled write, unlike
-- the singleton runtime_state table which only ever holds the latest one). Powers a 48h+
-- "what was the bed actually doing" trend view without re-deriving it from raw_samples/decisions.
-- Both daemons append here on a throttled (~60s) cadence (see DashboardDaemon/LiveDashboardDaemon
-- ._record_state_history); Repository.record_state_snapshot also prunes rows older than ~7 days
-- on every write so the table stays bounded regardless of tick cadence.
CREATE TABLE IF NOT EXISTS state_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    state TEXT,
    mode TEXT,
    target_temp_f REAL,
    bed_temp_f REAL,
    room_temp_f REAL,
    stage TEXT,
    confidence REAL,
    target_level INTEGER,
    daemon_alive INTEGER,
    extra TEXT
);
CREATE INDEX IF NOT EXISTS idx_state_history_ts ON state_history(ts);
"""


# Idempotent column additions for tables that predate a field (CREATE TABLE IF NOT EXISTS won't
# add a column to an existing table). Each entry: (table, column, DDL type/default).
_MIGRATIONS = [
    ("steer_events", "applied", "INTEGER DEFAULT 1"),
    ("steer_events", "succeeded", "INTEGER"),
    ("thermal_calibration", "warmback_levels_per_min", "REAL"),
    ("thermal_calibration", "warmback_lag_min", "REAL"),
    ("events", "data", "TEXT"),
    ("raw_samples", "sample_ts", "TEXT"),
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
        # NORMAL is the standard safe pairing with WAL (durable across app crashes; only a full
        # OS/power failure could lose the last commit) and skips an fsync on every commit.
        conn.execute("PRAGMA synchronous=NORMAL")
    # Let a writer wait for a locked DB instead of raising "database is locked" immediately --
    # matters once multiple connections (API requests + the daemon) touch the same file.
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    init_db(conn)
    return conn


def connect_light(path: str, check_same_thread: bool = True) -> sqlite3.Connection:
    """Open a connection with the same pragmas as ``connect()`` but WITHOUT running the schema
    DDL/migrations.

    For high-frequency per-request/per-tick connections where the schema is already guaranteed to
    exist (see ``dashboard/api/app/db.py``'s ``init_schema()``/``get_repo()``). Re-running
    ``executescript`` plus several ``PRAGMA table_info`` migration checks on every HTTP
    request/SSE tick/ingest was measurable overhead on a modest always-on box; the PRAGMAs below
    are cheap per-connection settings, not schema work, so they still run every time.
    """
    conn = sqlite3.connect(path, check_same_thread=check_same_thread)
    conn.row_factory = sqlite3.Row
    if path != ":memory:":
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn
