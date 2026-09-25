"""Start-up learners must scale with the data, not with its square.

2026-09-25: the daemon's start-up (``_attach_profiles``) stalled for well over an hour on the box
after the comfort-anchor log line. ``awakening_precursor_profile`` re-scanned the whole night for
every awake TICK, and ``wake_causation_audit`` ran one unindexed COUNT(*) over raw_samples per
intervention (~1000 a night). The watchdog and the self-updater's smoke test then took the slow
start for a dead daemon and rolled back good builds. Both now binary-search; these pin the
results and the speed."""
import random
import time
from datetime import datetime, timedelta

from sleepctl.learning.wake_causation import (
    awakening_precursor_profile, wake_causation_audit)
from sleepctl.storage.repository import Repository


def _repo(tmp_path, nights=3):
    repo = Repository(str(tmp_path / "s.db"))
    c = repo.conn
    rng = random.Random(1)
    raw, ivs = [], []
    start = datetime(2026, 9, 1, 12)
    t = start
    while t < start + timedelta(days=nights + 1):
        nd = (t - timedelta(hours=12)).date().isoformat()
        in_bed = 23 <= t.hour or t.hour < 7
        raw.append((t.isoformat(), nd, "light", 0.8, 60 + rng.random(), 50.0, 14.0,
                    rng.random(), 1, 70.0, 70.0, -10,
                    "maintenance" if in_bed else "idle", 0, 5.0, t.isoformat()))
        if in_bed and t.second == 0:
            ivs.append((t.isoformat(), nd, "maintenance", "hold", 0.0, "stabilize"))
        t += timedelta(seconds=15)
    c.executemany("INSERT INTO raw_samples (ts,night_date,stage,stage_confidence,heart_rate,hrv,"
                  "respiratory_rate,movement,presence,bed_temp_f,room_temp_f,commanded_level,"
                  "controller_state,wake_event,data_age_seconds,sample_ts) "
                  "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", raw)
    c.executemany("INSERT INTO interventions (ts,night_date,controller_state,action,magnitude_f,"
                  "reason) VALUES (?,?,?,?,?,?)", ivs)
    for k in range(nights):
        d = (start + timedelta(days=k)).date()
        s = datetime(d.year, d.month, d.day, 23)
        for m in (40, 200, 330):                      # three 10-min awake bouts, every tick flagged
            c.execute("UPDATE raw_samples SET wake_event=1 WHERE ts>=? AND ts<?",
                      ((s + timedelta(minutes=m)).isoformat(),
                       (s + timedelta(minutes=m + 10)).isoformat()))
        c.execute("INSERT INTO nightly_summaries (date, total_sleep_min) VALUES (?, 420)",
                  (d.isoformat(),))
    c.commit()
    return repo


def test_wake_audit_counts_wakes_after_each_intervention(tmp_path):
    repo = _repo(tmp_path)
    t0 = time.time()
    audit = wake_causation_audit(repo, horizon_min=15.0)
    assert time.time() - t0 < 10
    hold = audit["maneuvers"]["hold"]
    # one intervention a minute, 480 a night; a 15-min horizon catches a bout from 15 min before
    # it starts until it ends: (15 + 10) min x 3 bouts = 75 a night
    assert hold["n"] == 3 * 480
    assert hold["woke"] == 3 * 75


def test_precursor_profile_windows_and_speed(tmp_path):
    repo = _repo(tmp_path)
    t0 = time.time()
    prof = awakening_precursor_profile(repo)
    assert time.time() - t0 < 10
    # every flagged tick is an "awakening" (40 ticks x 3 bouts x 3 nights) with a full 6-min
    # pre-window of 24 ticks behind it
    assert prof["n_awakenings"] == 3 * 3 * 40
    assert prof["features"]["hr_mean"]["n_pre"] == 360
    assert prof["features"]["hr_mean"]["n_base"] > 0
