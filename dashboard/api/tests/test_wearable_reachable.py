"""The reachability check, and the stale counter that made it lie.

A successful reconnect prints "connected", not a new "consecutive failures: N" line, so the last
count in the log outlives the outage it described. Read naively the check reported "the band is
not merely unworn, it is unreachable" at 2026-09-01 21:36 -- while cardiac_sensor showed a
sample one second old and actigraphy 147 batches.
"""

import os

import app.diagnostics as diag


def _log(tmp_path, lines):
    with open(os.path.join(tmp_path, "verity.log"), "w") as fh:
        fh.write("\n".join(lines) + "\n")
    return str(tmp_path)


_FAILING = ["connecting to 24:AC:AC:16:96:1D ...",
            "session error (...); reconnecting in 25s (consecutive failures: 916)"]
_RECOVERED = _FAILING + ["scanning for a Polar/BLE heart-rate sensor (20s)...",
                         "connecting to 24:AC:AC:16:96:1D ...",
                         "connected",
                         "PMD: start PPI ok"]


def test_the_counter_is_cleared_by_a_later_success(tmp_path):
    assert diag._verity_consecutive_failures(_log(tmp_path, _RECOVERED)) == 0


def test_the_counter_survives_while_it_is_still_failing(tmp_path):
    assert diag._verity_consecutive_failures(_log(tmp_path, _FAILING)) == 916


def test_a_connecting_line_is_not_a_success(tmp_path):
    """"connecting to ..." contains "connect"; only a completed "connected" counts."""
    lines = _FAILING + ["scanning...", "connecting to 24:AC:AC:16:96:1D ..."]
    assert diag._verity_consecutive_failures(_log(tmp_path, lines)) == 916


def test_no_log_is_unknown_not_zero(tmp_path):
    assert diag._verity_consecutive_failures(str(tmp_path)) is None


class _Repo:
    conn = None


def _reach(monkeypatch, age_s, run_dir):
    monkeypatch.setattr("app.bridge.read_cardiac_sample",
                        lambda conn: {"age_seconds": age_s})
    return diag._check_wearable_reachable(_Repo(), run_dir)


def test_a_fresh_sample_settles_it_whatever_the_log_says(monkeypatch, tmp_path):
    """Data arriving a second ago is data arriving."""
    r = _reach(monkeypatch, 1.0, _log(tmp_path, _FAILING))
    assert r["status"] == "ok"


def test_a_gap_that_swallowed_a_night_still_warns(monkeypatch, tmp_path):
    r = _reach(monkeypatch, 20 * 3600, _log(tmp_path, _FAILING))
    assert r["status"] == "warn"
    assert "night" in r["detail"]


def test_two_nights_gone_is_a_failure(monkeypatch, tmp_path):
    r = _reach(monkeypatch, 45 * 3600, _log(tmp_path, _FAILING))
    assert r["status"] == "fail"


def test_an_ordinary_daytime_gap_is_quiet(monkeypatch, tmp_path):
    r = _reach(monkeypatch, 3 * 3600, _log(tmp_path, _RECOVERED))
    assert r["status"] == "ok"
