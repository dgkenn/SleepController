"""`raw_samples.ts` carries the POD FRAME's timestamp, which refreshes about every 60 s while
the daemon ticks every ~30 s. Two consecutive rows therefore routinely share one `ts` while
their wearable data is up to a minute apart: measured on 2026-08-27, 673 of 682 distinct
timestamps had two rows. Anything measuring an INTERVAL from `ts` reads zero for half of them.

`sample_ts` records when the row was actually observed. `ts` is deliberately left alone --
night bucketing, rollups and every existing query depend on it.
"""
from datetime import datetime, timedelta

from sleepctl.models import SensorFrame, SleepStage
from sleepctl.storage.repository import Repository


def _frame(ts, hr):
    return SensorFrame(timestamp=ts, stage=SleepStage.LIGHT, stage_confidence=0.6,
                       heart_rate=hr, hrv=40.0, respiratory_rate=14.0, movement=0.02,
                       presence=None, bed_temp_f=None, commanded_level=-70)


def test_two_ticks_sharing_one_pod_frame_are_still_distinguishable_in_time():
    repo = Repository(":memory:")
    pod_ts = datetime(2026, 8, 27, 23, 30, 0)      # ONE Pod frame...
    repo.log_sample(_frame(pod_ts, 66.0), "maintenance", False, "2026-08-27")
    repo.log_sample(_frame(pod_ts, 71.0), "maintenance", False, "2026-08-27")   # ...two ticks
    rows = repo.conn.execute(
        "SELECT ts, sample_ts, heart_rate FROM raw_samples ORDER BY id").fetchall()
    assert len(rows) == 2
    assert rows[0][0] == rows[1][0], "the shared Pod timestamp is preserved, as intended"
    assert rows[0][1] is not None and rows[1][1] is not None
    assert rows[0][1] <= rows[1][1], "sample_ts must be monotonic in write order"


def test_sample_ts_is_the_observation_time_not_the_pod_frame_time():
    repo = Repository(":memory:")
    stale = datetime.now() - timedelta(hours=3)     # a Pod frame from hours ago
    repo.log_sample(_frame(stale, 66.0), "maintenance", False, "2026-08-27")
    ts, sample_ts = repo.conn.execute(
        "SELECT ts, sample_ts FROM raw_samples").fetchone()
    assert ts.startswith(stale.strftime("%Y-%m-%dT%H:%M")) or \
        ts.startswith(stale.strftime("%Y-%m-%d %H:%M"))
    observed = datetime.fromisoformat(sample_ts)
    assert abs((datetime.now() - observed).total_seconds()) < 60, \
        "sample_ts should be now, not the stale Pod frame time"


def test_ts_semantics_are_unchanged_so_existing_queries_keep_working():
    """The whole point of adding a column rather than redefining `ts`: night bucketing, rollups
    and every existing query must behave exactly as before."""
    repo = Repository(":memory:")
    pod_ts = datetime(2026, 8, 27, 23, 30, 0)
    repo.log_sample(_frame(pod_ts, 66.0), "maintenance", False, "2026-08-27")
    row = repo.conn.execute(
        "SELECT ts, night_date, heart_rate, controller_state FROM raw_samples").fetchone()
    assert row[0].startswith("2026-08-27")
    assert row[1] == "2026-08-27" and row[2] == 66.0 and row[3] == "maintenance"


def test_the_column_is_added_to_an_existing_database_without_data_loss():
    """The migration is additive: an older DB gains the column with NULLs, keeping its rows."""
    import sqlite3
    from sleepctl.storage.schema import init_db
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE raw_samples (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                 "ts TEXT NOT NULL, night_date TEXT, heart_rate REAL)")
    conn.execute("INSERT INTO raw_samples (ts, night_date, heart_rate) "
                 "VALUES ('2026-08-01T23:00:00','2026-08-01',60.0)")
    init_db(conn)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(raw_samples)").fetchall()}
    assert "sample_ts" in cols
    old = conn.execute("SELECT ts, heart_rate, sample_ts FROM raw_samples").fetchone()
    assert old[0] == "2026-08-01T23:00:00" and old[1] == 60.0 and old[2] is None
