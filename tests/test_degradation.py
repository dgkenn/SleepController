"""The silent-failure ledger.

A subsystem that throws every tick is the worst failure this system has: the loop stays healthy,
the verdict stays green, and the feature simply isn't running. These tests pin the properties that
make that visible -- a true occurrence count despite rate-limited writes, and never raising out of
the ``except`` blocks it is called from.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from sleepctl import degradation
from sleepctl.degradation import (
    NOTABLE_COUNT,
    DegradationLedger,
    recent,
    summarize,
)


@pytest.fixture()
def repo(tmp_path):
    from sleepctl.storage.repository import Repository

    r = Repository(str(tmp_path / "deg.db"))
    yield r
    r.close()


@pytest.fixture(autouse=True)
def _clean_ledger():
    degradation.LEDGER.reset()
    yield
    degradation.LEDGER.reset()


# ------------------------------------------------------------------ counting
def test_records_count_first_and_last():
    led = DegradationLedger()
    led.record("stager", ValueError("boom"))
    led.record("stager", ValueError("boom again"))
    snap = led.snapshot()
    assert snap["stager"]["count"] == 2
    assert snap["stager"]["error"] == "ValueError: boom again"
    assert snap["stager"]["first"] is not None and snap["stager"]["last"] is not None


def test_subsystems_are_tracked_independently():
    led = DegradationLedger()
    led.record("stager", ValueError("a"))
    led.record("wearable fusion", KeyError("b"))
    led.record("stager", ValueError("c"))
    snap = led.snapshot()
    assert snap["stager"]["count"] == 2
    assert snap["wearable fusion"]["count"] == 1


def test_accepts_a_plain_string_reason():
    led = DegradationLedger()
    led.record("thing", "no weather provider configured")
    assert led.snapshot()["thing"]["error"] == "no weather provider configured"


def test_record_never_raises_and_keeps_working_afterwards():
    """It runs inside the except blocks that keep the night alive, so it must not raise -- but
    swallowing everything would also pass that. It must still be FUNCTIONAL after a bad input."""
    class Nasty:
        def __str__(self):
            raise RuntimeError("even str() explodes")

    led = DegradationLedger()
    led.record("x", Nasty())           # must not raise
    led.record(None, ValueError("v"))  # must not raise
    led.record("stager", ValueError("a real one"))
    assert led.snapshot()["stager"]["count"] == 1, "the ledger must survive bad input intact"


def test_record_survives_a_broken_repo():
    class Boom:
        def log_event(self, *a, **k):
            raise RuntimeError("db gone")

    led = DegradationLedger()
    led.record("x", ValueError("v"), repo=Boom())
    assert led.snapshot()["x"]["count"] == 1


# ------------------------------------------------------------------ persistence
def test_first_failure_is_persisted_immediately(repo):
    led = DegradationLedger()
    led.record("stager", ValueError("boom"), repo=repo)
    rows = repo.recent_events(category=degradation.EVENT_CATEGORY)
    assert len(rows) == 1
    assert "stager" in rows[0]["message"]


def test_writes_are_rate_limited_but_the_count_is_true(repo):
    """A per-tick failure must not write thousands of rows -- nor under-report itself."""
    led = DegradationLedger()
    for _ in range(200):
        led.record("stager", ValueError("boom"), repo=repo)

    rows = repo.recent_events(category=degradation.EVENT_CATEGORY)
    assert len(rows) <= 5, f"should write a handful of rows, not 200 (got {len(rows)})"
    assert led.snapshot()["stager"]["count"] == 200

    # ...and the persisted count must stay within an order of magnitude of the truth, so a remote
    # reader sees "100x", not "1x", for a subsystem that failed on every tick.
    agg = recent(repo)
    assert agg["stager"]["count"] == 100


def test_recent_reads_the_highest_count_per_subsystem(repo):
    repo.log_event(degradation.EVENT_CATEGORY, "warn", "stager", "stager failed (1x)",
                   {"subsystem": "stager", "count": 1, "error": "ValueError: old"})
    repo.log_event(degradation.EVENT_CATEGORY, "warn", "stager", "stager failed (57x)",
                   {"subsystem": "stager", "count": 57, "error": "ValueError: new"})
    agg = recent(repo)
    assert agg["stager"]["count"] == 57
    assert "new" in agg["stager"]["last_error"]


def test_recent_ignores_other_event_categories(repo):
    repo.log_event("something_else", "warn", "x", "unrelated", {"subsystem": "x", "count": 9})
    assert recent(repo) == {}


def test_recent_survives_a_broken_repo():
    class Boom:
        def recent_events(self, *a, **k):
            raise RuntimeError("db gone")

    assert recent(Boom()) == {}


# ------------------------------------------------------------------ summarizing
def test_nothing_recorded_is_ok():
    status, detail = summarize({})
    assert status == "ok"


def test_isolated_skips_are_info_not_a_warning():
    status, detail = summarize({"stager": {"count": 1, "last_error": "x"}})
    assert status == "info"
    assert "stager" in detail


def test_a_repeatedly_failing_subsystem_warns_and_names_it():
    agg = {"stager": {"count": NOTABLE_COUNT + 40, "last_error": "ValueError: boom"},
           "hue": {"count": 1, "last_error": "timeout"}}
    status, detail = summarize(agg)
    assert status == "warn"
    assert "stager" in detail and "NOT running" in detail
    assert "ValueError: boom" in detail


def test_worst_offender_is_reported_first():
    agg = {"minor": {"count": NOTABLE_COUNT, "last_error": "a"},
           "major": {"count": 500, "last_error": "b"}}
    _, detail = summarize(agg)
    assert detail.index("major") < detail.index("minor")


# ------------------------------------------------------------------ the diagnostics surface
def test_diagnostics_check_reports_a_failing_subsystem(repo):
    import sys
    import os

    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "dashboard", "api"))
    from app import diagnostics

    repo.log_event(degradation.EVENT_CATEGORY, "warn", "wearable fusion",
                   "wearable fusion failed (99x)",
                   {"subsystem": "wearable fusion", "count": 99, "error": "KeyError: hr"})
    c = diagnostics._check_degraded(repo)
    assert c["status"] == "warn"
    assert "wearable fusion" in c["detail"]
    assert c["remedy"] and "WITHOUT them" in c["remedy"]


def test_diagnostics_check_is_ok_on_a_clean_night(repo):
    import sys
    import os

    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "dashboard", "api"))
    from app import diagnostics

    assert diagnostics._check_degraded(repo)["status"] == "ok"


def test_a_non_string_subsystem_name_cannot_empty_the_whole_ledger():
    """Regression: snapshot() sorts its entries, and a None key mixed with strings raised
    TypeError -- swallowed by its own defensive except into an EMPTY dict, discarding every
    failure recorded that night. A ledger that loses its contents is worse than none at all."""
    led = DegradationLedger()
    led.record(None, ValueError("v"))
    led.record(42, ValueError("v"))
    led.record("stager", ValueError("real one"))
    snap = led.snapshot()
    assert snap["stager"]["count"] == 1
    assert len(snap) == 3, snap
    assert all(isinstance(k, str) for k in snap)
