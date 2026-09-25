"""Data the system stored but lost, mis-dated or deleted (2026-09-25 data-flow audit)."""
from datetime import datetime, timedelta

from sleepctl.models import ContextRecord
from sleepctl.storage.repository import Repository


def _repo():
    return Repository(":memory:")


def test_a_second_close_out_does_not_erase_the_morning_check_in():
    r = _repo()
    r.save_context(ContextRecord(date="2026-09-24", subjective_quality=4.0, caffeine=True))
    r.save_context(ContextRecord(date="2026-09-24", sleep_opportunity_min=420.0))
    c = r.get_context("2026-09-24")
    assert c.subjective_quality == 4.0 and c.caffeine and c.sleep_opportunity_min == 420.0


def test_night_type_is_stored_and_read_back():
    r = _repo()
    r.save_context(ContextRecord(date="2026-09-24", night_type="recovery"))
    assert r.get_context("2026-09-24").night_type == "recovery"


def test_ground_truth_markers_outlive_event_pruning():
    r = _repo()
    old = (datetime.now() - timedelta(days=20)).isoformat()
    r.conn.execute("INSERT INTO events (ts, category, severity, code, message) "
                   "VALUES (?, 'staging', 'info', 'marker_vs_stage', 'marker')", (old,))
    r.conn.execute("INSERT INTO events (ts, category, severity, code, message) "
                   "VALUES (?, 'device', 'info', 'prime', 'x')", (old,))
    r.conn.commit()
    r.prune_events(keep_days=14)
    codes = [row["code"] for row in r.conn.execute("SELECT code FROM events")]
    assert codes == ["marker_vs_stage"]


def test_the_rollup_counts_awake_time_from_observation_time_not_frame_time():
    """Two rows share each Pod frame `ts`; durations from `ts` gave every other row 0 minutes."""
    from sleepctl.loop.night_rollup import reconstruct_night_summary
    r = _repo()
    t0 = datetime(2026, 9, 24, 23, 0)
    rows = []
    for i in range(240):                         # 2 h at 30 s ticks
        obs = t0 + timedelta(seconds=30 * i)
        frame = t0 + timedelta(seconds=60 * (i // 2))
        # awake on the first row of each frame pair -- the row a frame-time gap credits 0 min
        awake = 100 <= i < 160 and i % 2 == 0
        rows.append((frame.isoformat(), "2026-09-24", "awake" if awake else "light", 60.0,
                     "wake_recovery" if awake else "maintenance", obs.isoformat()))
    r.conn.executemany(
        "INSERT INTO raw_samples (ts, night_date, stage, heart_rate, controller_state, "
        "sample_ts) VALUES (?,?,?,?,?,?)", rows)
    r.conn.commit()
    ns = reconstruct_night_summary(r, "2026-09-24")
    assert ns.waso_min is not None and ns.waso_min >= 10.0
