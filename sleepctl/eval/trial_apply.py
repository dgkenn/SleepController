"""Apply tonight's randomized CONTROLLER-POLICY arm (see ``trial.py`` for the design).

``trial.py`` has had the whole apparatus -- permuted-block randomization stratified by night
type, idempotent assignment, outcome-locked-before-reveal blinding -- since it was written, and
``assign_night`` was called from nowhere. The trial existed as dead code while roughly a dozen
behavioural changes went in unvalidated on n=1 confounded nights. This is the missing half.

The three arms, mapped onto levers the controller already exposes (no new decision logic):

  * ``A_static``    -- a credible static comparator. Holds the learned neutral setpoint and
    disables the experimental levers: in-night steering and predictive pre-emption. Deliberately
    NOT "thermal control off", which would unblind itself instantly (see the trial docstring).
  * ``B_reactive``  -- full reactive control with the target stabilizer OFF: every decision the
    controller reaches is commanded, including small and reversing moves.
  * ``C_stabilized`` -- the same, with the stabilizer ON (deadband + reversal dwell), which is
    what production has been running.

Nothing here touches a safety clamp, the comfort band, or smart-wake. The worst an arm can do is
give a night equivalent to an earlier version of this system, never one worse than not having it.
"""

from __future__ import annotations

from typing import Optional

from sleepctl.eval.trial import (POLICY_A_STATIC, POLICY_B_REACTIVE, POLICY_C_STABILIZED,
                                 assign_night, get_assignment)

#: Above the maximum achievable score (1.0), so ``score >= threshold`` is always False. Same
#: mechanism the efficacy trial uses to disable pre-emption without editing decision logic.
_PREEMPT_DISABLED = 9.9


def _seed(cfg) -> str:
    return str(getattr(getattr(cfg, "tunables", None), "trial_seed", "sleepctl-n-of-1"))


def trial_enabled(cfg) -> bool:
    return bool(getattr(getattr(cfg, "tunables", None), "controller_trial_enabled", False))


def apply_trial_arm(repo, cfg, controller, night_date: str,
                    night_type: Optional[str] = None) -> Optional[dict]:
    """Assign tonight (idempotently) and apply the arm. Returns the arm info, or None when the
    trial is off.

    Every lever is set EXPLICITLY on every arm, never merely left alone. The detectors are built
    once at controller start-up, so an arm that only disabled things would leak its settings into
    every later night -- silently turning the next arm into a copy of this one.
    """
    if not trial_enabled(cfg):
        return None
    try:
        row = assign_night(repo.conn, night_date, night_type, seed=_seed(cfg))
    except Exception:
        return None
    if not row:
        return None
    policy = row.get("policy")
    t = cfg.tunables
    wra = getattr(controller, "wake_risk_assessor", None)
    pd = getattr(controller, "precursor_detector", None)

    static = policy == POLICY_A_STATIC
    # Pre-emption: off on the static arm, at its configured threshold otherwise.
    if wra is not None:
        wra.preempt_threshold = (_PREEMPT_DISABLED if static
                                 else getattr(t, "wake_risk_preempt_threshold", 0.5))
    if pd is not None:
        pd.preempt_threshold = (_PREEMPT_DISABLED if static
                                else getattr(t, "precursor_preempt_threshold", 0.40))
    # In-night steering: only the static arm stands it down.
    try:
        controller.set_steer_policy(actuate=not static)
    except Exception:
        pass
    # The stabilizer separates B from C. Setting it on EVERY arm (not just C) is what stops a
    # previous night's value carrying forward.
    controller.trial_stabilizer_enabled = policy == POLICY_C_STABILIZED

    return {"policy": policy, "block_id": row.get("block_id"),
            "block_index": row.get("block_index"), "stratum": row.get("stratum"),
            "static": static}


def arm_for_night(repo, night_date: str) -> Optional[dict]:
    """Internal read of tonight's assignment. Anything user-facing must instead go through
    ``trial.get_assignment_for_display``, which withholds the policy until the outcome is locked."""
    try:
        return get_assignment(repo.conn, night_date)
    except Exception:
        return None
