"""Our stager vs Apple vs Bevel: consensus labels, disagreement handling, calibration hand-off."""
from __future__ import annotations

import random
import time
from datetime import datetime, timedelta, timezone

import pytest

from sleepctl.learning import wearable_consensus as wc
from sleepctl.storage.repository import Repository

NIGHT = "2026-11-21"
N = 720                               # six hours of 30 s epochs
START = datetime(2026, 11, 21, 23, 0, 0)   # naive local


@pytest.fixture()
def eastern(monkeypatch):
    monkeypatch.setenv("TZ", "America/New_York")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


@pytest.fixture()
def repo(tmp_path, eastern):
    r = Repository(str(tmp_path / "c.db"))
    r.conn.executescript(
        "CREATE TABLE IF NOT EXISTS sensor_samples (id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " ts TEXT NOT NULL, hr REAL, hrv REAL, movement REAL, source TEXT);"
        "CREATE TABLE IF NOT EXISTS health_sleep (start TEXT NOT NULL, end TEXT NOT NULL,"
        " stage TEXT NOT NULL, source TEXT NOT NULL DEFAULT '', received TEXT NOT NULL,"
        " PRIMARY KEY (start, end, stage, source));")
    return r


def truth(i: int) -> str:
    m = (i // 2) % 90                 # minute within a 90-minute cycle
    if i < 20:
        return "awake"
    return "light" if m < 30 else "deep" if m < 50 else "light" if m < 65 else "rem"


def _utc(i: int) -> str:
    local = START + timedelta(seconds=30 * i)
    return local.astimezone(timezone.utc).isoformat()


def _write_source(repo, labels, source, health_name):
    names = {"light": "core", "deep": "deep", "rem": "rem", "awake": "awake"}
    rows, i = [], 0
    while i < len(labels):
        j = i
        while j < len(labels) and labels[j] == labels[i]:
            j += 1
        rows.append((_utc(i), _utc(j), names[labels[i]], health_name, "now"))
        i = j
    repo.conn.executemany("INSERT INTO health_sleep VALUES (?,?,?,?,?)", rows)
    repo.conn.commit()


def _write_ours(repo, labels):
    rows = []
    for i, s in enumerate(labels):
        t = START + timedelta(seconds=30 * i + 15)
        rows.append((t.isoformat(), NIGHT, s, 58.0, "maintenance", t.isoformat()))
    repo.conn.executemany("INSERT INTO raw_samples (ts, night_date, stage, heart_rate,"
                          " controller_state, sample_ts) VALUES (?,?,?,?,?,?)", rows)
    repo.conn.commit()


def _night(repo, *, bevel_rem_bias=0.5, ours_noise=0.3, seed=1):
    rnd = random.Random(seed)
    t = [truth(i) for i in range(N)]
    apple = [s if rnd.random() > 0.05 else rnd.choice(wc.CLASSES) for s in t]
    # Bevel's habit: calls a run of light sleep REM
    bevel = [("rem" if s == "light" and (i // 40) % 2 == 0 and rnd.random() < bevel_rem_bias * 2
              else s) for i, s in enumerate(t)]
    ours = [s if rnd.random() > ours_noise else rnd.choice(wc.CLASSES) for s in t]
    _write_source(repo, apple, "wc", "Dean’s Apple Watch")
    _write_source(repo, bevel, "wc", "Bevel")
    _write_ours(repo, ours)
    return t


def test_source_kinds():
    assert wc.source_kind("Dean’s Apple Watch") == "apple"
    assert wc.source_kind("Bevel") == "bevel"
    assert wc.source_kind("") == "apple"
    assert wc.source_kind("AutoSleep") is None


def test_consensus_recovers_the_night_and_learns_bevels_habit(repo):
    t = _night(repo)
    rep = wc.build(repo.conn)
    assert rep["n_nights"] == 1
    eps = wc.consensus_epochs(repo.conn, NIGHT)
    assert len(eps) >= N - 2
    lab = [(e[2], truth(int(round((e[0] - wc._to_unix(START.isoformat())) / 30)))) for e in eps]
    sure = [(a, b) for a, b in lab if a != "unknown"]
    assert len(sure) > 0.6 * len(lab)
    assert sum(a == b for a, b in sure) / len(sure) > 0.95
    light, rem = wc.CLASSES.index("light"), wc.CLASSES.index("rem")
    assert rep["confusion"]["bevel"][light][rem] > 0.25     # the reverse-engineered habit
    assert rep["confusion"]["apple"][light][light] > 0.85
    assert "apple~bevel" in rep["pooled_kappa"] and "ours~consensus" in rep["pooled_kappa"]


def test_our_own_vote_does_not_set_the_label(repo, tmp_path):
    _night(repo, ours_noise=0.3)
    wc.build(repo.conn)
    a = [e[2] for e in wc.consensus_epochs(repo.conn, NIGHT)]
    other = Repository(str(tmp_path / "d.db"))
    other.conn.executescript(repo.conn.execute(
        "SELECT sql FROM sqlite_master WHERE name='health_sleep'").fetchone()[0] + ";")
    other.conn.executescript("CREATE TABLE IF NOT EXISTS sensor_samples (id INTEGER PRIMARY KEY,"
                             " ts TEXT, hr REAL, hrv REAL, movement REAL, source TEXT);")
    _night(other, ours_noise=0.9, seed=1)    # same Apple/Bevel, much worse "ours"
    wc.build(other.conn)
    b = [e[2] for e in wc.consensus_epochs(other.conn, NIGHT)]
    assert a == b          # identical: our stager has no path into the labels at all


def test_a_lone_disputed_epoch_abstains(repo):
    _night(repo)
    wc.build(repo.conn)
    rows = repo.conn.execute("SELECT stage, apple, bevel FROM consensus_hypnogram "
                             "WHERE apple != bevel").fetchall()
    assert rows and any(r[0] == "unknown" for r in rows)


def test_calibration_uses_the_consensus_unless_eeg_nights_exist(repo, monkeypatch):
    _night(repo)
    wc.build(repo.conn)
    from sleepctl.learning import eeg_calibration as ec
    seen = {}

    def fake_build(r, **kw):
        seen.update(kw)
        return {"fitted_ts": "x", "enabled": False, "n_nights": 1, "reference": kw["reference"]}
    monkeypatch.setattr(ec, "build_calibration", fake_build)
    out = wc.calibrate(repo, stager=object())
    assert seen["epochs_fn"] is wc.consensus_epochs and seen["nights"] == [NIGHT]
    assert out["reference"] == "Apple/Bevel consensus"
    monkeypatch.setattr(wc, "imported_nights", lambda conn: [{}] * ec.MIN_NIGHTS)
    assert "skipped" in wc.calibrate(repo, stager=object())


def test_nothing_to_compare_is_said_plainly(repo):
    _write_ours(repo, ["light"] * 200)
    rep = wc.build(repo.conn)
    assert rep["nights"] == [] and "no Apple/Bevel" in rep["rationale"]


def test_snapshot_carries_agreement_not_minutes(repo):
    _night(repo)
    wc.build(repo.conn)
    snap = wc.snapshot(repo.conn)
    assert snap["nights"] == 1 and set(snap["sources"]) == {"apple", "bevel", "ours"}
    assert "minutes" not in str(snap) and "deep" not in str(snap)


def _apple_hr_table(repo):
    repo.conn.executescript("CREATE TABLE IF NOT EXISTS apple_health (ts TEXT NOT NULL, metric TEXT"
                            " NOT NULL, value REAL, extra TEXT, received TEXT NOT NULL,"
                            " PRIMARY KEY (ts, metric));")


def _nights_without_live_feed(repo, rnd):
    t = [truth(i) for i in range(N)]
    _write_source(repo, t, "wc", "Apple Watch")
    _write_source(repo, [s if rnd.random() > 0.1 else "light" for s in t], "wc", "Bevel")
    _write_ours(repo, [None] * N)            # the controller ran, but staged nothing
    return t


def test_a_night_without_a_live_feed_is_replayed_from_health_heart_rate(repo):
    _nights_without_live_feed(repo, random.Random(3))
    seen = {}

    def fake_replay(conn, times):
        seen["n"] = len(times)
        return ["light"] * len(times)
    rep = wc.build(repo.conn, replay=fake_replay)
    assert seen["n"] >= N - 2
    assert "ours_replay" in rep["nights"][0]["sources"] and "ours" not in rep["nights"][0]["sources"]
    assert "ours_replay" in rep["confusion"]
    # the replay is a comparison only: its night is not offered to the calibration as live data
    marked = repo.conn.execute("SELECT COUNT(*) FROM consensus_hypnogram WHERE ours LIKE "
                               "'replay:%'").fetchone()[0]
    assert marked >= N - 2


def test_the_health_replay_runs_the_real_stager(repo):
    from sleepctl.ml.sleep_staging.infer import SleepStager
    st = SleepStager.load()
    if not getattr(st, "available", False):
        pytest.skip("stager weights not available")
    _apple_hr_table(repo)
    rnd = random.Random(5)
    rows = []
    for k in range(0, N * 30, 300):          # a reading every 5 minutes, like Apple overnight
        i = k // 30
        hr = 64 if truth(i) == "awake" else 52 if truth(i) == "deep" else 57
        rows.append((_utc(i), "heart_rate", hr + rnd.uniform(-2, 2), "{}", "now"))
    repo.conn.executemany("INSERT INTO apple_health VALUES (?,?,?,?,?)", rows)
    repo.conn.commit()
    times = [wc._iso_unix(_utc(i)) for i in range(N)]
    out = wc.make_health_replay(st)(repo.conn, times)
    assert out is not None and sum(1 for x in out if x) > N / 2
    assert set(x for x in out if x) <= set(wc.CLASSES)


def test_the_openvitals_rater_votes_and_ours_does_not(repo):
    t = [truth(i) for i in range(N)]
    _write_source(repo, t, "wc", "Apple Watch")
    _write_ours(repo, ["rem"] * N)                 # a useless stager
    seen = {}

    def fake_ov(conn, times):
        seen["n"] = len(times)
        return [truth(i) for i in range(len(times))]
    rep = wc.build(repo.conn, replay=None, openvitals=fake_ov)
    assert seen["n"] >= N - 2 and "openvitals" in rep["nights"][0]["sources"]
    assert rep["pooled_kappa"]["apple~openvitals"] > 0.9
    assert rep["pooled_kappa"]["ours~consensus"] < 0.1


def test_apples_core_alone_is_not_taken_for_certain_light(repo):
    """The PSG prior: Apple labels much true deep as core, so a lone Apple "core" is weaker
    evidence of light than a lone Apple "REM" is of REM."""
    _write_source(repo, [truth(i) for i in range(N)], "wc", "Apple Watch")
    _write_ours(repo, [None] * N)
    wc.build(repo.conn, replay=None, openvitals=None)
    conf = dict(repo.conn.execute("SELECT apple, AVG(confidence) FROM consensus_hypnogram "
                                  "GROUP BY apple").fetchall())
    assert conf["light"] < conf["rem"]


def test_the_vendored_openvitals_model_runs():
    from sleepctl.ml import openvitals as ov
    if not ov.available():
        pytest.skip("onnxruntime not installed")
    import numpy as np
    t = 1_763_700_000.0 + np.arange(0, 8 * 3600, 5.0)
    hr = 58 + 4 * np.sin(np.arange(len(t)) / 300.0)
    out = ov.stage(list(zip(t, hr)))
    assert out and len(out["labels"]) > 600
    assert set(x for x in out["labels"] if x) <= set(wc.CLASSES)
