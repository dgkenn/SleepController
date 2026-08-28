"""`trial.py` has had the whole randomization apparatus since it was written -- permuted blocks
stratified by night type, idempotent assignment, outcome-locked-before-reveal blinding -- and
`assign_night` was called from nowhere. The trial existed as dead code while roughly a dozen
behavioural changes went in unvalidated on n=1 confounded nights. This is the missing half.
"""
from sleepctl.config import AppConfig
from sleepctl.controller.controller import SleepController
from sleepctl.eval.trial import (POLICY_A_STATIC, POLICY_B_REACTIVE, POLICY_C_STABILIZED,
                                 get_assignment, get_assignment_for_display)
from sleepctl.eval.trial_apply import apply_trial_arm
from sleepctl.storage.repository import Repository

_PREEMPT_DISABLED = 9.9


def _setup(enabled=True):
    cfg = AppConfig.default()
    cfg.tunables.controller_trial_enabled = enabled
    return cfg, Repository(":memory:"), SleepController(cfg)


def _force(repo, night, policy):
    """Pin a night to a specific arm so each arm's effects can be asserted directly."""
    repo.conn.execute(
        "INSERT INTO trial_assignments (night_date, policy, block_id, block_index, night_type,"
        " seed, assigned_ts) VALUES (?,?,?,?,?,?,?)",
        (night, policy, "work:0", 0, "work", "s", "2026-08-28T21:00:00"))
    repo.conn.commit()


def test_the_trial_is_off_by_default():
    """It changes what the bed does on a schedule the user did not choose."""
    assert AppConfig.default().tunables.controller_trial_enabled is False
    cfg, repo, c = _setup(enabled=False)
    assert apply_trial_arm(repo, cfg, c, "2026-08-28", "work") is None


def test_enabling_it_assigns_and_persists_a_night():
    cfg, repo, c = _setup()
    info = apply_trial_arm(repo, cfg, c, "2026-08-28", "work")
    assert info is not None and info["policy"] in (
        POLICY_A_STATIC, POLICY_B_REACTIVE, POLICY_C_STABILIZED)
    assert get_assignment(repo.conn, "2026-08-28")["policy"] == info["policy"]


def test_a_restart_cannot_re_roll_the_arm():
    """Idempotence is the property that makes a mid-night restart safe."""
    cfg, repo, c = _setup()
    first = apply_trial_arm(repo, cfg, c, "2026-08-28", "work")
    again = apply_trial_arm(repo, cfg, SleepController(cfg), "2026-08-28", "work")
    assert first["policy"] == again["policy"]


def test_the_static_arm_stands_down_the_experimental_levers():
    cfg, repo, c = _setup()
    _force(repo, "2026-08-28", POLICY_A_STATIC)
    info = apply_trial_arm(repo, cfg, c, "2026-08-28", "work")
    assert info["static"] is True
    assert c.wake_risk_assessor.preempt_threshold == _PREEMPT_DISABLED
    assert c.precursor_detector.preempt_threshold == _PREEMPT_DISABLED
    assert c.steer_actuate is False


def test_the_reactive_arm_runs_with_the_stabilizer_off():
    cfg, repo, c = _setup()
    _force(repo, "2026-08-28", POLICY_B_REACTIVE)
    apply_trial_arm(repo, cfg, c, "2026-08-28", "work")
    assert c.trial_stabilizer_enabled is False
    assert c.steer_actuate is True
    assert c.wake_risk_assessor.preempt_threshold < 1.0


def test_the_stabilized_arm_runs_with_it_on():
    cfg, repo, c = _setup()
    _force(repo, "2026-08-28", POLICY_C_STABILIZED)
    apply_trial_arm(repo, cfg, c, "2026-08-28", "work")
    assert c.trial_stabilizer_enabled is True


def test_arm_b_actually_bypasses_the_stabilizer_in_the_decision_path():
    """The arm has to change behaviour, not just a flag."""
    from datetime import datetime
    cfg, repo, c = _setup()
    _force(repo, "2026-08-28", POLICY_B_REACTIVE)
    apply_trial_arm(repo, cfg, c, "2026-08-28", "work")
    c._last_target_f = 66.0
    c._stab_last_dir = -1
    c._stab_last_move_at = datetime(2026, 8, 27, 22, 50)
    held, _ = c._stabilize_target(68.0, datetime(2026, 8, 27, 23, 0), cfg)
    assert held is None, "arm B must command the reversal the stabilizer would have held"


def test_every_lever_is_set_on_every_arm_so_nothing_leaks_between_nights():
    """The detectors are built once at start-up. An arm that only DISABLED things would leave a
    later arm silently running the previous night's settings."""
    cfg, repo, c = _setup()
    _force(repo, "2026-08-28", POLICY_A_STATIC)
    apply_trial_arm(repo, cfg, c, "2026-08-28", "work")
    _force(repo, "2026-08-29", POLICY_C_STABILIZED)
    apply_trial_arm(repo, cfg, c, "2026-08-29", "work")
    assert c.wake_risk_assessor.preempt_threshold < 1.0, "arm A's disabled threshold leaked"
    assert c.steer_actuate is True
    assert c.trial_stabilizer_enabled is True


def test_the_policy_is_withheld_from_display_until_the_outcome_is_locked():
    """The blinding gate: knowing the arm colours the morning rating, which is the endpoint."""
    cfg, repo, c = _setup()
    apply_trial_arm(repo, cfg, c, "2026-08-28", "work")
    shown = get_assignment_for_display(repo.conn, "2026-08-28")
    assert "policy" not in shown or shown.get("policy") is None


def test_a_broken_repo_never_takes_the_daemon_down():
    cfg, _repo, c = _setup()

    class Broken:
        class conn:
            @staticmethod
            def execute(*a, **k):
                raise RuntimeError("no table")
    assert apply_trial_arm(Broken(), cfg, c, "2026-08-28", "work") is None
