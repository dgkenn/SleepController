"""Unattended-failure logging in the Verity forwarder.

The forwarder POSTs every ~2s all night with nobody watching. A PERSISTENT failure -- a missing
ingest token 401ing every batch is the likely one -- would otherwise write ~43k identical lines a
night into .run/verity.log, which nothing rotates. A full disk fails WRITES while deletes still
succeed, so it surfaces as the controller mysteriously losing data rather than as a disk problem.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import verity_forwarder as vf  # noqa: E402


@pytest.fixture(autouse=True)
def _reset():
    vf._reset_repeat_log()
    yield
    vf._reset_repeat_log()


def _drive(n, key="post:HTTPError", msg="POST failed (401)", capsys=None):
    for _ in range(n):
        vf._log_repeating(key, msg)
    return capsys.readouterr().out.strip().splitlines() if capsys else []


def test_first_failure_is_logged_immediately(capsys):
    lines = _drive(1, capsys=capsys)
    assert len(lines) == 1 and "POST failed" in lines[0]


def test_a_nights_worth_of_identical_failures_stays_tiny(capsys):
    """~43k occurrences is one night of 2s POSTs against a dead endpoint."""
    lines = _drive(43000, capsys=capsys)
    assert len(lines) <= 12, f"{len(lines)} lines for 43000 failures"


def test_the_true_count_is_always_reported(capsys):
    """Throttling must not hide severity -- the last line carries the real number."""
    lines = _drive(500, capsys=capsys)
    assert any("[x" in ln for ln in lines)
    last = [ln for ln in lines if "[x" in ln][-1]
    reported = int(last.split("[x")[1].split("]")[0])
    assert reported > 100, last


def test_a_success_resets_the_throttle(capsys):
    """After recovery, the NEXT failure must be reported immediately, not swallowed."""
    _drive(100, capsys=capsys)
    vf._reset_repeat_log()
    lines = _drive(1, capsys=capsys)
    assert len(lines) == 1 and "[x" not in lines[0]


def test_a_different_failure_is_reported_immediately(capsys):
    """A new failure mode is news even while an old one is being throttled."""
    _drive(100, capsys=capsys)
    vf._log_repeating("post:URLError", "POST failed (connection refused)")
    out = capsys.readouterr().out
    assert "connection refused" in out


def test_switching_conditions_reports_the_closing_count(capsys):
    _drive(50, capsys=capsys)
    vf._log_repeating("post:URLError", "POST failed (connection refused)")
    out = capsys.readouterr().out
    assert "ended after 50 occurrences" in out


def test_intermittent_blips_are_each_reported(capsys):
    """A genuine one-off network blip every few minutes must not be silently absorbed."""
    for _ in range(5):
        vf._log_repeating("post:URLError", "POST failed (blip)")
        vf._reset_repeat_log()
    lines = capsys.readouterr().out.strip().splitlines()
    assert len(lines) == 5
