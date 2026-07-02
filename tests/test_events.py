"""Structured, queryable event log: log_event / recent_events / prune_events on Repository."""

from __future__ import annotations

from datetime import datetime, timedelta

from sleepctl.storage.repository import Repository


def test_log_event_and_recent_events_roundtrip():
    repo = Repository(":memory:")
    repo.log_event("device", "info", "set_temp", "target set to 68", {"target_f": 68})
    rows = repo.recent_events()
    assert len(rows) == 1
    r = rows[0]
    assert r["category"] == "device"
    assert r["severity"] == "info"
    assert r["code"] == "set_temp"
    assert r["message"] == "target set to 68"
    assert r["data"] == {"target_f": 68}
    assert r["ts"]  # timestamp populated


def test_recent_events_newest_first_and_limit():
    repo = Repository(":memory:")
    for i in range(5):
        repo.log_event("state", "info", f"code{i}", f"msg{i}")
    rows = repo.recent_events(limit=3)
    assert len(rows) == 3
    # newest first
    assert [r["code"] for r in rows] == ["code4", "code3", "code2"]


def test_recent_events_filters_by_category_and_severity():
    repo = Repository(":memory:")
    repo.log_event("device", "info", "prime", "prime applied")
    repo.log_event("thermal", "warn", "thermal_stalled", "stalled")
    repo.log_event("cloud", "warn", "tick_error", "RequestError 504")
    repo.log_event("error", "error", "tick_error", "boom")

    by_cat = repo.recent_events(category="thermal")
    assert len(by_cat) == 1 and by_cat[0]["code"] == "thermal_stalled"

    by_sev = repo.recent_events(severity="warn")
    assert {r["code"] for r in by_sev} == {"thermal_stalled", "tick_error"}
    assert all(r["severity"] == "warn" for r in by_sev)

    by_both = repo.recent_events(category="cloud", severity="warn")
    assert len(by_both) == 1 and by_both[0]["code"] == "tick_error"
    assert by_both[0]["category"] == "cloud"


def test_recent_events_filters_by_since():
    repo = Repository(":memory:")
    old_ts = (datetime.now() - timedelta(days=2)).isoformat()
    repo.conn.execute(
        "INSERT INTO events (ts, category, severity, code, message, data) VALUES (?,?,?,?,?,?)",
        (old_ts, "lifecycle", "info", "daemon_started", "old event", "{}"),
    )
    repo.conn.commit()
    repo.log_event("lifecycle", "info", "daemon_started", "new event")

    since = (datetime.now() - timedelta(hours=1)).isoformat()
    rows = repo.recent_events(since_iso=since)
    assert len(rows) == 1 and rows[0]["message"] == "new event"


def test_log_event_never_raises_on_bad_connection():
    repo = Repository(":memory:")
    repo.conn.close()  # force every subsequent op to fail
    repo.log_event("device", "info", "x", "y")  # must not raise
    assert repo.recent_events() == []  # must not raise, returns empty on error
    assert repo.prune_events() == 0  # must not raise


def test_prune_events_by_age_and_row_cap():
    repo = Repository(":memory:")
    old_ts = (datetime.now() - timedelta(days=30)).isoformat()
    for i in range(3):
        repo.conn.execute(
            "INSERT INTO events (ts, category, severity, code, message, data) VALUES (?,?,?,?,?,?)",
            (old_ts, "lifecycle", "info", f"old{i}", "old", "{}"),
        )
    repo.conn.commit()
    for i in range(5):
        repo.log_event("lifecycle", "info", f"new{i}", "new")

    deleted = repo.prune_events(keep_days=14, max_rows=20000)
    assert deleted == 3  # only the 3 old rows removed by age
    remaining = repo.recent_events(limit=100)
    assert len(remaining) == 5
    assert all(r["code"].startswith("new") for r in remaining)

    # now cap by row count regardless of age
    deleted2 = repo.prune_events(keep_days=14, max_rows=2)
    assert deleted2 == 3
    assert len(repo.recent_events(limit=100)) == 2
