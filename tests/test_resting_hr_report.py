"""Resting-HR-from-real-data report: the three-gate filter, the CI math, and the coverage
estimate, exercised without a live box.

The property worth protecting is that this never reports a confident number it doesn't have --
a moving sample must never count as "at rest", an asleep sample must never count as "awake", and
a tiny n must visibly say so rather than printing a narrow-looking CI.
"""

from __future__ import annotations

import os
import sqlite3
import sys
from datetime import datetime, timedelta

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import resting_hr_report as rhr  # noqa: E402

T0 = datetime(2026, 8, 1, 7, 0, 0)


# ------------------------------------------------------------------ t_crit
def test_t_crit_matches_known_table_values():
    assert rhr.t_crit(1) == 12.706
    assert rhr.t_crit(30) == 2.042


def test_t_crit_interpolates_between_table_entries():
    v = rhr.t_crit(12)   # between df=10 (2.228) and df=15 (2.131)
    assert 2.131 < v < 2.228


def test_t_crit_converges_to_the_normal_limit_past_the_table():
    assert rhr.t_crit(500) == 1.960


def test_t_crit_rejects_nonpositive_df():
    with pytest.raises(ValueError):
        rhr.t_crit(0)


# ------------------------------------------------------------------ the fetch query
def _conn(rows):
    """rows: list of (ts, stage, presence, movement, heart_rate) -> an in-memory raw_samples db."""
    conn = sqlite3.connect(":memory:")
    conn.execute("""CREATE TABLE raw_samples (
        ts TEXT, stage TEXT, presence INTEGER, movement REAL, heart_rate REAL)""")
    conn.executemany(
        "INSERT INTO raw_samples (ts, stage, presence, movement, heart_rate) VALUES (?,?,?,?,?)",
        rows)
    conn.commit()
    return conn


def test_query_excludes_a_moving_sample():
    conn = _conn([(T0.isoformat(), "awake", 1, 0.30, 60.0),   # moving -- not at rest
                  ((T0 + timedelta(minutes=1)).isoformat(), "awake", 1, 0.02, 58.0)])
    rows = rhr.fetch_resting_hr_rows(conn, T0.isoformat())
    assert [r["heart_rate"] for r in rows] == [58.0]


def test_query_excludes_an_asleep_sample():
    conn = _conn([(T0.isoformat(), "light", 1, 0.02, 55.0),   # asleep -- not "awake"
                  ((T0 + timedelta(minutes=1)).isoformat(), "awake", 1, 0.02, 58.0)])
    rows = rhr.fetch_resting_hr_rows(conn, T0.isoformat())
    assert [r["heart_rate"] for r in rows] == [58.0]


def test_query_excludes_when_not_in_bed():
    conn = _conn([(T0.isoformat(), "awake", 0, 0.02, 55.0),   # out of bed
                  ((T0 + timedelta(minutes=1)).isoformat(), "awake", 1, 0.02, 58.0)])
    rows = rhr.fetch_resting_hr_rows(conn, T0.isoformat())
    assert [r["heart_rate"] for r in rows] == [58.0]


def test_query_excludes_a_null_movement_reading():
    """No movement reading means we cannot confirm stillness -- must not default to "at rest"."""
    conn = _conn([(T0.isoformat(), "awake", 1, None, 55.0),
                  ((T0 + timedelta(minutes=1)).isoformat(), "awake", 1, 0.02, 58.0)])
    rows = rhr.fetch_resting_hr_rows(conn, T0.isoformat())
    assert [r["heart_rate"] for r in rows] == [58.0]


def test_query_excludes_a_missing_heart_rate():
    conn = _conn([(T0.isoformat(), "awake", 1, 0.02, None),
                  ((T0 + timedelta(minutes=1)).isoformat(), "awake", 1, 0.02, 58.0)])
    rows = rhr.fetch_resting_hr_rows(conn, T0.isoformat())
    assert [r["heart_rate"] for r in rows] == [58.0]


def test_query_respects_the_lookback_cutoff():
    old = (T0 - timedelta(days=10)).isoformat()
    conn = _conn([(old, "awake", 1, 0.02, 70.0),
                  (T0.isoformat(), "awake", 1, 0.02, 58.0)])
    rows = rhr.fetch_resting_hr_rows(conn, (T0 - timedelta(hours=1)).isoformat())
    assert [r["heart_rate"] for r in rows] == [58.0]


def test_movement_exactly_at_the_threshold_counts_as_at_rest():
    conn = _conn([(T0.isoformat(), "awake", 1, rhr.REST_MOVEMENT_MAX, 58.0)])
    rows = rhr.fetch_resting_hr_rows(conn, T0.isoformat())
    assert len(rows) == 1


# ------------------------------------------------------------------ summarize()
def _rows(hrs, gap_min=1.0):
    return [{"ts": (T0 + timedelta(minutes=i * gap_min)).isoformat(), "heart_rate": h}
            for i, h in enumerate(hrs)]


def test_summarize_reports_the_sample_mean():
    s = rhr.summarize(_rows([56.0, 58.0, 60.0]))
    assert s["mean"] == pytest.approx(58.0)
    assert s["n"] == 3


def test_summarize_ci_widens_for_small_n():
    """Same spread, fewer points -> a wider interval. If this ever narrows with less data,
    the small-sample t-correction has been lost and the CI is silently overclaiming."""
    wide = rhr.summarize(_rows([50.0, 60.0, 70.0]))          # n=3
    narrow = rhr.summarize(_rows([50.0, 55.0, 60.0, 65.0, 70.0] * 6))  # n=30, same range
    assert (wide["ci_hi"] - wide["ci_lo"]) > (narrow["ci_hi"] - narrow["ci_lo"])


def test_summarize_a_single_sample_has_a_degenerate_ci():
    s = rhr.summarize(_rows([60.0]))
    assert s["ci_lo"] == s["ci_hi"] == 60.0
    assert s["sd"] == 0.0


def test_summarize_coverage_never_exceeds_the_span():
    """A huge inter-sample gap must not inflate 'time actually measured' past the span it
    was measured over."""
    rows = [{"ts": T0.isoformat(), "heart_rate": 58.0},
            {"ts": (T0 + timedelta(minutes=5)).isoformat(), "heart_rate": 60.0}]
    s = rhr.summarize(rows)
    assert s["covered_min"] <= s["span_min"] + 1e-9


def test_summarize_span_and_gap_are_measured_correctly():
    s = rhr.summarize(_rows([55.0, 56.0, 57.0], gap_min=2.0))
    assert s["span_min"] == pytest.approx(4.0)
    assert s["median_gap_s"] == pytest.approx(120.0)


# ------------------------------------------------------------------ main() end to end
def _db_file(tmp_path, rows):
    path = str(tmp_path / "t.db")
    conn = _conn(rows)
    with sqlite3.connect(path) as out:
        out.execute("""CREATE TABLE raw_samples (
            ts TEXT, stage TEXT, presence INTEGER, movement REAL, heart_rate REAL)""")
        out.executemany(
            "INSERT INTO raw_samples (ts, stage, presence, movement, heart_rate) VALUES (?,?,?,?,?)",
            rows)
    conn.close()
    return path


def test_main_reports_no_data_and_exits_nonzero_when_nothing_qualifies(tmp_path, capsys):
    path = _db_file(tmp_path, [(T0.isoformat(), "light", 1, 0.02, 55.0)])  # asleep only
    rc = rhr.main(["--db", path])
    assert rc == 1
    out = capsys.readouterr().out
    assert "No qualifying samples" in out


def test_main_warns_below_the_reliability_floor(tmp_path, capsys):
    now = datetime.now()
    rows = [((now - timedelta(minutes=i)).isoformat(), "awake", 1, 0.02, 58.0)
            for i in range(3)]
    path = _db_file(tmp_path, rows)
    rc = rhr.main(["--db", path, "--hours", "24"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "n = 3" in out
    assert "CAUTION" in out


def test_main_prints_a_clean_result_with_enough_data(tmp_path, capsys):
    now = datetime.now()
    rows = [((now - timedelta(minutes=i)).isoformat(), "awake", 1, 0.02, 58.0 + (i % 3))
            for i in range(20)]
    path = _db_file(tmp_path, rows)
    rc = rhr.main(["--db", path, "--hours", "24"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "n = 20" in out
    assert "CAUTION" not in out
