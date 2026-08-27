"""Randomized, blinded n-of-1 trial of CONTROLLER POLICIES.

Why randomize the controller rather than validate the sleep labels: the question "was that
really REM?" is unanswerable at home without PSG, and it is not the question the system exists
to answer. An estimator can be a poor stager and still a useful control signal (if its latent
"REM-ish" state reliably marks moments when cooling helps, it is doing its job), and a good
stager can make a terrible controller (small errors that make the bed hunt all night). So
PHYSIOLOGICAL VALIDITY and CONTROL UTILITY are measured separately: validity by the sanity gate
in ``controller_sanity.py`` as a diagnostic, utility by this trial as the actual endpoint.

Design decisions, each fixing a specific way an n-of-1 trial goes wrong:

* **Blocked within night_type.** Rotating shift work is a large, non-random nuisance factor.
  Free randomization balances it only in expectation, which at ~10 nights per arm it will not
  do. Arms are balanced inside each stratum instead.
* **Multi-night blocks.** Sleep debt carries over: a bad night deepens the next one whatever the
  policy does. Switching every night maximises carryover contamination, so an arm holds for
  ``block_size`` nights (default 2) before switching.
* **Outcome locked before reveal.** The morning check-in must be recorded BEFORE the assignment
  can be shown. Otherwise knowing the arm colours the rating, which is the whole endpoint.
* **Arm A is not a sham.** "Thermal control off" is trivially identifiable and would unblind
  itself, so A holds a static setpoint -- a credible comparator, not an obvious placebo.
* **Seeded and reproducible.** The assignment for a night is a pure function of (seed, stratum,
  block index), so it can be audited after the fact and cannot be quietly re-rolled.

Blinding is still only partial and this is worth stating plainly: a bed that changes temperature
feels different from one that does not, so the intervention is perceptible in the sensory channel
no matter how the assignment is stored. ``record_blind_guess`` exists to MEASURE how much
blinding actually held rather than to assume it.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Optional

#: The arms. A is a credible static comparator, not "control off" -- see module docstring.
POLICY_A_STATIC = "A_static"
POLICY_B_REACTIVE = "B_reactive"
POLICY_C_STABILIZED = "C_stabilized"

DEFAULT_ARMS = (POLICY_A_STATIC, POLICY_B_REACTIVE, POLICY_C_STABILIZED)

#: Nights an arm holds before switching. Trades switch frequency against carryover contamination.
DEFAULT_BLOCK_SIZE = 2

#: Strata that get their own balanced randomization. Anything unrecognised falls to "other".
_KNOWN_STRATA = ("work", "off", "other")


def _stratum(night_type: Optional[str]) -> str:
    nt = (night_type or "").strip().lower()
    if nt in ("work", "constrained", "short"):
        return "work"
    if nt in ("off", "off_day", "recovery", "rest"):
        return "off"
    return "other"


def _block_permutation(seed: str, stratum: str, block_number: int, arms) -> list:
    """A deterministic, uniformly-chosen permutation of ``arms`` for one block.

    Pure function of its inputs: the same (seed, stratum, block) always yields the same order, so
    an assignment can be re-derived and audited later and cannot be silently re-rolled to get a
    preferred arm. Uses a hash rather than ``random`` so it needs no global state and is stable
    across processes and Python versions.
    """
    arms = list(arms)
    out = []
    pool = list(arms)
    for i in range(len(arms)):
        digest = hashlib.sha256(
            f"{seed}|{stratum}|{block_number}|{i}".encode("utf-8")).hexdigest()
        idx = int(digest[:8], 16) % len(pool)
        out.append(pool.pop(idx))
    return out


def plan_assignment(night_date: str, night_type: Optional[str], *, seed: str,
                    prior_assignments: Optional[list] = None,
                    arms=DEFAULT_ARMS, block_size: int = DEFAULT_BLOCK_SIZE) -> dict:
    """Decide which arm ``night_date`` belongs to, without touching the database.

    ``prior_assignments`` are the existing rows for the SAME stratum (dicts with at least
    ``night_date``, ``block_id``, ``block_index``), newest last. The next night either continues
    the current block or opens the next one.
    """
    stratum = _stratum(night_type)
    prior = [p for p in (prior_assignments or [])
             if _stratum(p.get("night_type")) == stratum
             and p.get("night_date") != night_date]
    prior.sort(key=lambda p: str(p.get("night_date")))

    if prior:
        last = prior[-1]
        last_block = int(str(last.get("block_id") or "0").rsplit(":", 1)[-1] or 0)
        last_index = int(last.get("block_index") or 0)
        if last_index + 1 < block_size:
            block_number, block_index = last_block, last_index + 1
        else:
            block_number, block_index = last_block + 1, 0
    else:
        block_number, block_index = 0, 0

    # A block is one arm held for block_size nights; consecutive blocks cycle through a
    # permutation so every arm appears equally often within the stratum.
    perm = _block_permutation(seed, stratum, block_number // len(arms), arms)
    policy = perm[block_number % len(arms)]

    return {
        "night_date": night_date,
        "policy": policy,
        "block_id": f"{stratum}:{block_number}",
        "block_index": block_index,
        "night_type": night_type,
        "stratum": stratum,
        "seed": seed,
    }


# --------------------------------------------------------------------------- persistence
def assign_night(conn, night_date: str, night_type: Optional[str], *, seed: str,
                 controller_version: Optional[str] = None,
                 arms=DEFAULT_ARMS, block_size: int = DEFAULT_BLOCK_SIZE,
                 now: Optional[datetime] = None) -> dict:
    """Assign ``night_date`` to an arm and persist it. Idempotent: an existing assignment is
    returned unchanged, so a daemon restart mid-night can never re-roll the arm."""
    existing = get_assignment(conn, night_date)
    if existing is not None:
        return existing

    rows = conn.execute(
        "SELECT night_date, block_id, block_index, night_type FROM trial_assignments"
    ).fetchall()
    prior = [dict(r) for r in rows]
    plan = plan_assignment(night_date, night_type, seed=seed, prior_assignments=prior,
                           arms=arms, block_size=block_size)
    ts = (now or datetime.now()).isoformat()
    conn.execute(
        "INSERT INTO trial_assignments (night_date, policy, block_id, block_index, night_type,"
        " controller_version, seed, assigned_ts) VALUES (?,?,?,?,?,?,?,?)",
        (night_date, plan["policy"], plan["block_id"], plan["block_index"],
         night_type, controller_version, seed, ts))
    conn.commit()
    return get_assignment(conn, night_date)


def get_assignment(conn, night_date: str) -> Optional[dict]:
    """The full row INCLUDING the policy. For internal/controller use only -- anything that can
    reach the user must go through ``get_assignment_for_display``."""
    row = conn.execute(
        "SELECT * FROM trial_assignments WHERE night_date = ?", (night_date,)).fetchone()
    return dict(row) if row is not None else None


def lock_outcome(conn, night_date: str, now: Optional[datetime] = None) -> bool:
    """Mark the morning outcome as recorded. Must be called by the check-in path BEFORE the arm
    is revealed. Idempotent; returns False if there is no assignment for that night."""
    row = get_assignment(conn, night_date)
    if row is None:
        return False
    if row.get("outcome_locked"):
        return True
    conn.execute(
        "UPDATE trial_assignments SET outcome_locked = 1, outcome_locked_ts = ? "
        "WHERE night_date = ?", ((now or datetime.now()).isoformat(), night_date))
    conn.commit()
    return True


def get_assignment_for_display(conn, night_date: str,
                               now: Optional[datetime] = None) -> dict:
    """THE BLINDING GATE. Everything user-facing -- dashboard, /diag, CLI, reports -- must read
    the arm through here, never through ``get_assignment``.

    Until the morning outcome is locked this withholds the policy entirely. It deliberately does
    not leak it through a side channel either: no arm name, no block id, no "same as last night".
    """
    row = get_assignment(conn, night_date)
    if row is None:
        return {"night_date": night_date, "assigned": False, "blinded": False, "policy": None}
    if not row.get("outcome_locked"):
        return {
            "night_date": night_date,
            "assigned": True,
            "blinded": True,
            "policy": None,
            "message": ("blinded until the morning check-in is recorded -- rating a night while "
                        "knowing its arm would bias the only endpoint this trial has"),
        }
    if not row.get("revealed"):
        conn.execute(
            "UPDATE trial_assignments SET revealed = 1, revealed_ts = ? WHERE night_date = ?",
            ((now or datetime.now()).isoformat(), night_date))
        conn.commit()
    return {
        "night_date": night_date,
        "assigned": True,
        "blinded": False,
        "policy": row["policy"],
        "block_id": row.get("block_id"),
        "night_type": row.get("night_type"),
        "controller_version": row.get("controller_version"),
    }


def record_blind_guess(conn, night_date: str, guess: str,
                       now: Optional[datetime] = None) -> bool:
    """Record which arm the user THOUGHT ran -- the manipulation check.

    Blinding here is inherently partial: a bed that changes temperature feels different from one
    that holds still, so the intervention is perceptible however the assignment is stored. This
    measures how much blinding actually held instead of assuming it, which is what lets the
    effect estimate be discounted honestly if guesses turn out to be accurate.
    """
    row = get_assignment(conn, night_date)
    if row is None:
        return False
    notes = {}
    if row.get("notes"):
        try:
            notes = json.loads(row["notes"])
        except Exception:
            notes = {}
    notes["blind_guess"] = guess
    notes["blind_guess_ts"] = (now or datetime.now()).isoformat()
    conn.execute("UPDATE trial_assignments SET notes = ? WHERE night_date = ?",
                 (json.dumps(notes), night_date))
    conn.commit()
    return True


#: Substrings in a decision ``reason`` that identify which arm ran. Arm C's stabilizer annotates
#: its holds, which would otherwise let the arm be read straight off the decision log.
_ARM_HINT_TOKENS = ("stabilizer",)


def scrub_arm_hints(text: Optional[str]) -> Optional[str]:
    """Remove arm-identifying phrases from a user-facing ``reason`` string.

    Defense in depth for the blinding contract, NOT a guarantee. A single-user trial cannot be
    blinded against someone with full access to their own logs and their own bed: the
    intervention is perceptible (a bed that changes temperature feels different from one that
    holds still), and any behavioural difference is in principle detectable in the raw data. What
    this does buy is that the arm is not sitting in plain text on a screen the user reads at the
    exact moment they are rating the night -- which is the one moment blinding has to survive.
    Real protection comes from locking the outcome before revealing (``lock_outcome``) and from
    measuring how much blinding held (``record_blind_guess``).
    """
    if not text:
        return text
    out = str(text)
    for tok in _ARM_HINT_TOKENS:
        if tok in out.lower():
            parts = [p for p in out.split(";")
                     if tok not in p.lower()]
            out = ";".join(parts).strip().strip(";").strip() or "held"
    return out
