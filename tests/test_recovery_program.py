"""Apple-measured outcomes, the recovery score, Apple-graded nudges, the experiment program."""
from __future__ import annotations

import random
import time
from datetime import datetime, timedelta, timezone

import pytest

from sleepctl import experiments as ex
from sleepctl.config import AppConfig
from sleepctl.learning import auto_experiments as ae
from sleepctl.learning import recovery as rc
from sleepctl.storage.repository import Repository


@pytest.fixture()
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("TZ", "America/New_York")
    time.tzset()
    r = Repository(str(tmp_path / "r.db"))
    r.conn.executescript(
        "CREATE TABLE IF NOT EXISTS health_sleep (start TEXT NOT NULL, end TEXT NOT NULL,"
        " stage TEXT NOT NULL, source TEXT NOT NULL DEFAULT '', received TEXT NOT NULL,"
        " PRIMARY KEY (start, end, stage, source));"
        "CREATE TABLE IF NOT EXISTS apple_health (ts TEXT NOT NULL, metric TEXT NOT NULL,"
        " value REAL, extra TEXT, received TEXT NOT NULL, PRIMARY KEY (ts, metric));")
    yield r
    monkeypatch.undo()
    time.tzset()


def _u(local: datetime) -> str:
    return local.astimezone(timezone.utc).isoformat()


def _night(repo, date: str, *, deep=90, rem=100, core=230, awake_bouts=2, hrv=55.0, hr=52.0):
    d = datetime.fromisoformat(date)
    t = d.replace(hour=23)
    segs = [("in_bed", t - timedelta(minutes=10), t + timedelta(minutes=deep + rem + core + 30))]
    plan = [("core", core / 2), ("deep", deep), ("awake", 5)] + \
           [("rem", rem), ("core", core / 2)] + [("awake", 5)] * (awake_bouts - 1)
    cur = t
    for stage, m in plan:
        segs.append((stage, cur, cur + timedelta(minutes=m)))
        cur += timedelta(minutes=m)
    repo.conn.executemany("INSERT INTO health_sleep VALUES (?,?,?,?,?)",
                          [(_u(a), _u(b), s, "Apple Watch", "x") for s, a, b in segs])
    rows = [(_u(t + timedelta(minutes=m)), "heart_rate", hr + (m % 7), "{}", "x")
            for m in range(0, 400, 5)]
    rows.append((_u(t + timedelta(hours=3)), "heart_rate_variability", hrv, "{}", "x"))
    repo.conn.executemany("INSERT INTO apple_health VALUES (?,?,?,?,?)", rows)
    # the controller's own rows give the night its date and window
    repo.conn.executemany("INSERT INTO raw_samples (ts, night_date, stage) VALUES (?,?,?)",
                          [((t - timedelta(hours=1)).isoformat(), date, None),
                           ((t + timedelta(hours=9)).isoformat(), date, None)])
    repo.conn.commit()


def test_outcomes_come_from_apples_hypnogram(repo):
    _night(repo, "2026-11-01", deep=90, rem=100, core=230, awake_bouts=2)
    rc.build(repo.conn)
    row = repo.conn.execute("SELECT deep_min, rem_min, total_sleep_min, wake_events, waso_min, "
                            "hrv_mean, hr_min FROM night_reference WHERE date='2026-11-01'"
                            ).fetchone()
    assert tuple(row)[:5] == (90.0, 100.0, 420.0, 1, 5.0)    # last awake bout ends the night
    assert row[5] == 55.0 and 52.0 <= row[6] <= 54.0


def test_the_recovery_score_rises_with_a_better_night(repo):
    for i in range(8):
        _night(repo, f"2026-11-{i + 1:02d}", deep=80 + i % 3, hrv=50.0 + i % 3, hr=55.0)
    _night(repo, "2026-11-10", deep=130, hrv=70.0, hr=49.0)
    _night(repo, "2026-11-11", deep=40, hrv=35.0, hr=61.0)
    rc.build(repo.conn)
    s = dict(repo.conn.execute("SELECT date, recovery_score FROM night_reference").fetchall())
    assert s["2026-11-01"] is None                       # no history yet: no score
    assert s["2026-11-10"] > 60 > 40 > s["2026-11-11"]


def test_a_nudge_is_regraded_by_apple(repo):
    _night(repo, "2026-11-01")
    t = datetime(2026, 11, 1, 23, 0)
    for ts, maneuver in ((t + timedelta(minutes=120), "deepen"),    # Apple deep here
                         (t + timedelta(minutes=320), "deepen")):   # Apple core here
        repo.conn.execute("INSERT INTO steer_events (night_date, ts, maneuver, horizon_min, "
                          "resolved, applied, deepened, succeeded) VALUES (?,?,?,?,1,1,1,1)",
                          ("2026-11-01", ts.isoformat(), maneuver, 20))
    repo.conn.commit()
    rc.build(repo.conn)
    got = repo.conn.execute("SELECT ref_succeeded FROM steer_events ORDER BY id").fetchall()
    assert [r[0] for r in got] == [1, 0]
    recs = repo.maneuver_records("deepen")
    assert {r["graded_by"] for r in recs} == {"apple"}
    assert sorted(r["succeeded"] for r in recs) == [0, 1]   # the stager said 1 for both


def test_experiments_use_apple_only_when_it_covers_every_night(repo):
    for d in ("2026-11-01", "2026-11-02"):
        _night(repo, d)
    rc.build(repo.conn)
    vals, by = ex._measured(repo, "deep_min", ["2026-11-01", "2026-11-02"])
    assert by == "apple" and len(vals) == 2
    vals, by = ex._measured(repo, "recovery_score", ["2026-11-01"])
    assert by == "apple"


def test_the_program_waits_for_the_recovery_score(repo):
    assert ae.tick(repo)["action"] == "waiting"
    assert ex.list_experiments(repo) == []


def _scored_nights(repo, n):
    rc.ensure_schema(repo.conn)
    for i in range(n):
        repo.conn.execute("INSERT INTO night_reference (date, recovery_score) VALUES (?, ?)",
                          (f"2026-10-{i + 1:02d}", 50.0))
    repo.conn.commit()


def test_the_program_starts_runs_concludes_and_adopts(repo):
    repo.save_setpoints(AppConfig.default().default_setpoints())
    base = repo.latest_setpoints()
    _scored_nights(repo, ae.MIN_REFERENCE_NIGHTS)
    started = ae.tick(repo)
    assert started["action"] == "started" and "Cooler during deep sleep" in started["name"]
    exp = ex.list_experiments(repo, status="active")[0]
    rnd = random.Random(2)
    day = datetime(2026, 11, 1)
    for k in range(16):                                   # two full cycles
        date = (day + timedelta(days=k)).date().isoformat()
        arm = ex.assign_arm(repo, exp["id"], date)
        score = {"a": 50.0, "b": 58.0}.get(arm)
        if score is not None:
            repo.conn.execute("INSERT OR REPLACE INTO night_reference (date, recovery_score) "
                              "VALUES (?, ?)", (date, score + rnd.uniform(-1, 1)))
    repo.conn.commit()
    done = ae.tick(repo)
    assert done["action"] == "concluded" and done["adopted"]
    new = repo.latest_setpoints()
    assert new.deep_bias_f == pytest.approx(base.deep_bias_f - 1.5)
    assert new.source == "experiment"
    nxt = ae.tick(repo)
    assert nxt["action"] == "started" and "Cooler baseline" in nxt["name"]


def test_a_control_win_keeps_the_setting(repo):
    repo.save_setpoints(AppConfig.default().default_setpoints())
    base = repo.latest_setpoints()
    _scored_nights(repo, ae.MIN_REFERENCE_NIGHTS)
    ae.tick(repo)
    exp = ex.list_experiments(repo, status="active")[0]
    for k in range(16):
        date = (datetime(2026, 11, 1) + timedelta(days=k)).date().isoformat()
        arm = ex.assign_arm(repo, exp["id"], date)
        if arm in ("a", "b"):
            repo.conn.execute("INSERT OR REPLACE INTO night_reference (date, recovery_score) "
                              "VALUES (?, ?)", (date, 55.0 + k * 0.01 if arm == "a" else 47.0))
    repo.conn.commit()
    done = ae.tick(repo)
    assert done["action"] == "concluded" and not done["adopted"]
    assert repo.latest_setpoints().deep_bias_f == base.deep_bias_f


def test_a_manual_experiment_is_left_alone(repo):
    _scored_nights(repo, ae.MIN_REFERENCE_NIGHTS)
    ex.create_experiment(repo, {"name": "mine", "metric": "deep_min"})
    assert ae.tick(repo)["action"] == "manual experiment running"


def test_the_program_can_be_switched_off(repo, monkeypatch):
    monkeypatch.setenv("SLEEPCTL_AUTO_EXPERIMENTS", "0")
    _scored_nights(repo, ae.MIN_REFERENCE_NIGHTS)
    assert ae.tick(repo)["action"] == "off"
