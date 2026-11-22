"""The usual wake time, learned from Apple's record of your recent mornings."""
from __future__ import annotations

import time
from datetime import date, datetime, timedelta, timezone

import pytest

from sleepctl.learning.habitual_wake import usual_wake
from sleepctl.storage.repository import Repository


@pytest.fixture()
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("TZ", "America/New_York")
    time.tzset()
    r = Repository(str(tmp_path / "w.db"))
    r.conn.executescript(
        "CREATE TABLE IF NOT EXISTS health_sleep (start TEXT NOT NULL, end TEXT NOT NULL,"
        " stage TEXT NOT NULL, source TEXT NOT NULL DEFAULT '', received TEXT NOT NULL,"
        " PRIMARY KEY (start, end, stage, source));")
    yield r
    monkeypatch.undo()
    time.tzset()


def _wake(repo, local_wake: datetime, source="Apple Watch"):
    start = local_wake - timedelta(hours=1)
    repo.conn.execute("INSERT INTO health_sleep VALUES (?,?,?,?,?)",
                      (start.astimezone(timezone.utc).isoformat(),
                       local_wake.astimezone(timezone.utc).isoformat(), "core", source, "x"))
    repo.conn.commit()


def test_weekdays_and_weekends_are_learned_apart(repo):
    # Mon 2026-11-16 .. Fri 11-20 around 06:45; Sat/Sun 11-14, 11-15, 11-21 around 09:00
    for d, hh, mm in ((16, 6, 40), (17, 6, 50), (18, 6, 45), (19, 6, 44), (20, 6, 47),
                      (14, 9, 5), (15, 8, 55), (21, 9, 0)):
        _wake(repo, datetime(2026, 11, d, hh, mm))
    now = datetime(2026, 11, 22, 21, 0)
    assert usual_wake(repo.conn, date(2026, 11, 23), now) == "06:45"     # Monday
    assert usual_wake(repo.conn, date(2026, 11, 22), now) == "09:00"     # Sunday


def test_too_few_nights_gives_no_guess(repo):
    _wake(repo, datetime(2026, 11, 20, 6, 45))
    _wake(repo, datetime(2026, 11, 19, 6, 45))
    assert usual_wake(repo.conn, date(2026, 11, 23), datetime(2026, 11, 22, 21, 0)) is None


def test_naps_and_other_apps_do_not_count(repo):
    for d in (16, 17, 18):
        _wake(repo, datetime(2026, 11, d, 6, 30))
        _wake(repo, datetime(2026, 11, d, 15, 0))                      # an afternoon nap
        _wake(repo, datetime(2026, 11, d, 7, 30), source="AutoSleep")  # another app's record
    assert usual_wake(repo.conn, date(2026, 11, 23), datetime(2026, 11, 22, 21, 0)) == "06:30"
