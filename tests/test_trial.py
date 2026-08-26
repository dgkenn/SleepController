"""Tests for the randomized, blinded controller-policy trial.

The properties that matter are the ones that make the result trustworthy: the blind must not
leak, an assignment must never be silently re-rolled, and arms must balance WITHIN each shift
stratum rather than only in expectation.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timedelta

from sleepctl.eval import trial
from sleepctl.storage.repository import Repository


@contextmanager
def _repo():
    d = tempfile.mkdtemp()
    try:
        r = Repository(os.path.join(d, "t.db"))
        try:
            yield r
        finally:
            r.close()
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _dates(n, start="2026-09-01"):
    d0 = datetime.fromisoformat(start)
    return [(d0 + timedelta(days=i)).date().isoformat() for i in range(n)]


# ------------------------------------------------------------------ blinding
def test_the_policy_is_withheld_until_the_outcome_is_locked():
    """THE core guarantee. Knowing the arm before rating the night biases the only endpoint the
    trial has, so the display path must withhold it until the check-in is recorded."""
    with _repo() as repo:
        trial.assign_night(repo.conn, "2026-09-01", "work", seed="s1")
        shown = trial.get_assignment_for_display(repo.conn, "2026-09-01")
        assert shown["blinded"] is True
        assert shown["policy"] is None

        trial.lock_outcome(repo.conn, "2026-09-01")
        shown = trial.get_assignment_for_display(repo.conn, "2026-09-01")
        assert shown["blinded"] is False
        assert shown["policy"] in trial.DEFAULT_ARMS


def test_blinded_display_leaks_no_side_channel():
    """Withholding the arm name is not enough: block id or night_type would let the arm be
    inferred by elimination across nights."""
    with _repo() as repo:
        trial.assign_night(repo.conn, "2026-09-01", "work", seed="s1")
        shown = trial.get_assignment_for_display(repo.conn, "2026-09-01")
        blob = repr(shown).lower()
        for arm in trial.DEFAULT_ARMS:
            assert arm.lower() not in blob
        assert "block_id" not in shown


def test_reveal_is_recorded_once_the_outcome_is_locked():
    with _repo() as repo:
        trial.assign_night(repo.conn, "2026-09-01", "work", seed="s1")
        trial.lock_outcome(repo.conn, "2026-09-01")
        trial.get_assignment_for_display(repo.conn, "2026-09-01")
        assert trial.get_assignment(repo.conn, "2026-09-01")["revealed"] == 1


# ------------------------------------------------------------------ no re-rolling
def test_an_assignment_is_never_re_rolled():
    """A daemon restart mid-night must not be able to change the arm -- that would silently
    break randomization and let a night be re-drawn until it gave a preferred answer."""
    with _repo() as repo:
        first = trial.assign_night(repo.conn, "2026-09-01", "work", seed="s1")
        for _ in range(5):
            again = trial.assign_night(repo.conn, "2026-09-01", "work", seed="different-seed")
            assert again["policy"] == first["policy"]


def test_assignment_is_reproducible_from_the_seed():
    """Pure function of (seed, stratum, block): auditable after the fact."""
    a = trial.plan_assignment("2026-09-01", "work", seed="s1")
    b = trial.plan_assignment("2026-09-01", "work", seed="s1")
    assert a["policy"] == b["policy"]
    c = trial.plan_assignment("2026-09-01", "work", seed="s2")
    assert isinstance(c["policy"], str)


# ------------------------------------------------------------------ balance + blocking
def test_arms_balance_within_each_shift_stratum():
    """Rotating shifts are a large nuisance factor; at n-of-1 sample sizes free randomization
    will NOT balance them. Arms must balance inside each stratum."""
    with _repo() as repo:
        n_per = 6 * len(trial.DEFAULT_ARMS) * trial.DEFAULT_BLOCK_SIZE
        for i, d in enumerate(_dates(n_per * 2)):
            nt = "work" if i % 2 == 0 else "off"
            trial.assign_night(repo.conn, d, nt, seed="balance")
        rows = [dict(r) for r in repo.conn.execute(
            "SELECT night_date, policy, night_type FROM trial_assignments").fetchall()]
        for stratum in ("work", "off"):
            counts = Counter(r["policy"] for r in rows if r["night_type"] == stratum)
            assert set(counts) == set(trial.DEFAULT_ARMS), counts
            # every arm within a few nights of every other inside this stratum
            assert max(counts.values()) - min(counts.values()) <= trial.DEFAULT_BLOCK_SIZE, counts


def test_an_arm_holds_for_a_whole_block_to_limit_carryover():
    """Sleep debt carries over, so switching every night maximises contamination. An arm holds
    for block_size nights before switching."""
    with _repo() as repo:
        for d in _dates(8):
            trial.assign_night(repo.conn, d, "work", seed="blocks")
        rows = [dict(r) for r in repo.conn.execute(
            "SELECT night_date, policy, block_id, block_index FROM trial_assignments "
            "ORDER BY night_date").fetchall()]
        by_block = {}
        for r in rows:
            by_block.setdefault(r["block_id"], set()).add(r["policy"])
        for block, policies in by_block.items():
            assert len(policies) == 1, f"block {block} mixed arms: {policies}"
        assert max(len(list(g)) for g in by_block.values()) == 1


# ------------------------------------------------------------------ manipulation check
def test_a_blind_guess_can_be_recorded():
    """Blinding is only partial -- a bed that changes temperature is perceptible. Recording what
    the user guessed measures how much blinding held instead of assuming it."""
    with _repo() as repo:
        trial.assign_night(repo.conn, "2026-09-01", "work", seed="s1")
        assert trial.record_blind_guess(repo.conn, "2026-09-01", trial.POLICY_A_STATIC) is True
        notes = trial.get_assignment(repo.conn, "2026-09-01")["notes"]
        assert trial.POLICY_A_STATIC in notes


def test_locking_or_guessing_an_unassigned_night_is_a_no_op():
    with _repo() as repo:
        assert trial.lock_outcome(repo.conn, "2099-01-01") is False
        assert trial.record_blind_guess(repo.conn, "2099-01-01", "x") is False
        shown = trial.get_assignment_for_display(repo.conn, "2099-01-01")
        assert shown["assigned"] is False and shown["policy"] is None
