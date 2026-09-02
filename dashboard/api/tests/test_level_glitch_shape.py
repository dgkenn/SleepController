"""A one-sample spike and a sustained step are different problems with different remedies.

Measured on 2026-08-29/30, all three excursions are spikes to exactly -100 (the device floor)
reverting within one sample, twice while IDLE, and two share a timestamp with their neighbour --
the signature of a duplicated bad read, not of the Eight Sleep app running a schedule. The check
used to offer both explanations at once and warn either way.
"""

import sqlite3

import app.diagnostics as diag


class _Repo:
    def __init__(self, levels):
        self.conn = sqlite3.connect(":memory:")
        self.conn.execute("CREATE TABLE raw_samples (id INTEGER PRIMARY KEY, ts TEXT, "
                          "commanded_level INTEGER)")
        from datetime import datetime, timedelta
        t0 = datetime.now()
        for i, lv in enumerate(levels):
            self.conn.execute("INSERT INTO raw_samples (ts, commanded_level) VALUES (?,?)",
                              ((t0 + timedelta(minutes=i)).isoformat(" ", "seconds"), lv))
        self.conn.commit()


def test_a_reverting_spike_is_reported_as_a_bad_read():
    r = diag._check_device_level_glitches(_Repo([-54, -55, -100, -54, -55, -54]))
    assert r["status"] == "info"
    assert "spike" in r["detail"] and "bad cloud read" in r["detail"]


def test_a_sustained_step_still_warns():
    """A jump that STAYS is a real setpoint change the bed could not have slewed to."""
    r = diag._check_device_level_glitches(_Repo([-54, -55, -54, -10, -11, -10, -11]))
    assert r["status"] == "warn"
    assert "sustained step" in r["detail"]
    assert "another controller" in r["remedy"]


def test_a_clean_series_is_ok():
    r = diag._check_device_level_glitches(_Repo([-54, -55, -56, -55, -54, -53]))
    assert r["status"] == "ok"


def test_too_few_samples_is_not_judged():
    r = diag._check_device_level_glitches(_Repo([-54, -100]))
    assert r["status"] == "info"
    assert "not enough" in r["detail"]
