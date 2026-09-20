"""A morning note like "awake 00:15-00:25, up 3:10" is ground truth, scored like a tap."""
from datetime import datetime, timedelta

from sleepctl.learning.declared_awakenings import (declared_instants, declared_intervals,
                                                    parse_declared, score_declared)
from sleepctl.learning.wake_truth import wake_truth_profile
from sleepctl.storage.repository import Repository

NOTES_DDL = "CREATE TABLE IF NOT EXISTS notes (id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT NOT NULL, text TEXT, created TEXT)"


def test_parse_ranges_singles_and_ampm():
    got = parse_declared("awake 00:15-00:25, up 3:10; woke 1am to 1:20am. bed at 22:00, up 3 times")
    assert got == [((0, 15), (0, 25)), ((3, 10), None), ((1, 0), (1, 20))]
    assert parse_declared("slept great") == []
    assert parse_declared("awakening 12:30am-12:45am") == [((0, 30), (0, 45))]


def test_intervals_land_on_the_right_side_of_midnight():
    ivs = declared_intervals("2026-09-19", ["awake 23:50-00:05, up 3:10"])
    assert ivs[0]["start"] == "2026-09-19T23:50:00" and ivs[0]["end"] == "2026-09-20T00:05:00"
    assert ivs[0]["minutes"] == 15.0
    assert ivs[1]["start"] == "2026-09-20T03:10:00" and ivs[1]["minutes"] == 5.0


def _repo_with_night(tmp_path, stage_at):
    repo = Repository(str(tmp_path / "d.db"), check_same_thread=False)
    repo.conn.execute(NOTES_DDL)
    t = datetime(2026, 9, 19, 23, 0)
    for i in range(0, 300):
        ts = t + timedelta(minutes=i)
        repo.conn.execute("INSERT INTO raw_samples (ts, night_date, stage) VALUES (?,?,?)",
                          (ts.isoformat(), "2026-09-19", stage_at(ts)))
    repo.conn.commit()
    return repo


def test_declared_awakenings_are_scored_against_the_stages_held_inside(tmp_path):
    def stage_at(ts):
        return "awake" if datetime(2026, 9, 20, 0, 15) <= ts <= datetime(2026, 9, 20, 0, 25) else "light"
    repo = _repo_with_night(tmp_path, stage_at)
    repo.conn.execute("INSERT INTO notes (date, text, created) VALUES (?,?,?)",
                      ("2026-09-20", "awake 00:15-00:25, up 3:10", "x"))
    repo.conn.commit()
    res = score_declared(repo, "2026-09-19")
    assert res["n"] == 2 and res["n_scored_awake"] == 1 and res["agreement"] == 0.5
    assert res["intervals"][0]["scored_awake"] is True and res["intervals"][1]["scored_awake"] is False


def test_declared_instants_feed_the_wake_truth_learner(tmp_path):
    repo = _repo_with_night(tmp_path, lambda ts: "light")
    for i in range(10):
        repo.conn.execute("INSERT INTO notes (date, text, created) VALUES (?,?,?)",
                          ("2026-09-20", f"awake 0{i % 4}:1{i}-0{i % 4}:2{i}", "x"))
    repo.conn.commit()
    inst = declared_instants(repo)
    assert len(inst) == 10 and all(st == "light" for _t, st in inst)
    p = wake_truth_profile(repo)
    assert p["personalized"] is True and p["n_notes"] == 10 and p["agreement"] == 0.0
    assert "morning notes" in p["rationale"]


def test_a_note_with_no_times_contributes_nothing(tmp_path):
    repo = _repo_with_night(tmp_path, lambda ts: "light")
    repo.conn.execute("INSERT INTO notes (date, text, created) VALUES (?,?,?)", ("2026-09-20", "rough night", "x"))
    repo.conn.commit()
    assert declared_instants(repo) == []
    assert wake_truth_profile(repo)["n"] == 0
