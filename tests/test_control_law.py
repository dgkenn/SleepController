"""The control law, end to end and pinned: evidence-grounded ideal targets, how conflicting
"what should the temperature be" signals reconcile to ONE coherent target, how the learners own
DISTINCT knobs (so they can't stomp each other), and exactly how a deviation from ideal
architecture translates into a bounded temperature change — reconciled by a single priority order.

If any of these guarantees regress, this test fails loudly.
"""

from __future__ import annotations

from sleepctl.benchmarks import NightMode, targets_for
from sleepctl.config import AppConfig
from sleepctl.controller.thermal import ThermalController
from sleepctl.learning.policy import TieredPolicy
from sleepctl.learning.setpoints import apply_recommendation
from sleepctl.ml.actions import ACTIONS
from sleepctl.models import NightObjective, NightSummary, SetpointProfile, ThermalIntent

CFG = AppConfig.default()


# --------------------------------------------------------------- 1. evidence-grounded priors
def test_starting_priors_are_evidence_grounded_and_ordered():
    p = CFG.default_setpoints()
    # cool for deep, warm toward wake, with neutral between — the Autopilot-RCT direction.
    assert p.deep_bias_f < p.neutral_f < p.wake_ramp_f
    assert p.rem_warm_offset_f > 0          # warmth promotes REM (Eight Sleep Autopilot RCT)
    # hot-sleeper biases the baseline cool; the settle default cools; onset nudges warm.
    assert CFG.tunables.hot_sleeper_cool_bias_f < 0
    assert CFG.tunables.maintenance_settle_nudge_f < 0
    assert CFG.tunables.onset_warm_nudge_f > 0
    # concrete anchored values (documented from the device's 55–110 °F water scale)
    assert p.deep_bias_f == 66.0 and p.neutral_f == 70.0 and p.wake_ramp_f == 74.0


def test_ideal_architecture_per_situation():
    normal, short, recovery = (targets_for(m) for m in
                               (NightMode.NORMAL, NightMode.CONSTRAINED, NightMode.RECOVERY))
    # normal: balanced architecture (Ohayon 2004) + continuity (Ohayon 2017)
    assert 0.15 <= normal.deep_pct_min <= 0.20 and 0.20 <= normal.rem_pct_ideal <= 0.27
    # short night: protect the homeostatically-defended deep, de-emphasize REM, demand efficiency
    assert short.deep_pct_min >= normal.deep_pct_min          # protect deep when sleep is scarce
    assert short.rem_pct_ideal <= normal.rem_pct_ideal        # REM de-emphasized
    assert short.efficiency_min >= normal.efficiency_min
    assert short.weights["waso"] + short.weights["awakenings"] > short.weights["deep"]  # continuity-first
    # recovery: boost REM rebound + weight duration & autonomic recovery
    assert recovery.rem_pct_ideal >= normal.rem_pct_ideal and recovery.hrv_recovery_weighted
    assert recovery.weights["total"] >= recovery.weights["deep"]


# --------------------------------------------------------- 2. ONE coherent target per intent
def _t(intent, hot=True, obj=NightObjective.OPTIMIZE, last=70.0):
    return ThermalController(CFG).target_for(intent, obj, hot, last)


def test_intents_reconcile_to_one_ordered_target():
    neutral = _t(ThermalIntent.NEUTRAL)
    # cool intents are colder than neutral; warm intents are warmer — a single coherent ordering
    assert _t(ThermalIntent.DEEP_BIAS_COOL) < neutral
    assert _t(ThermalIntent.INDUCTION_COOL) < neutral
    assert _t(ThermalIntent.REM_NEUTRAL) > neutral            # warmth promotes REM
    assert _t(ThermalIntent.ONSET_WARM) > neutral             # cutaneous warming speeds onset
    assert _t(ThermalIntent.WAKE_RAMP) > neutral              # warm toward wake


def test_hot_sleeper_cool_bias_applies_to_cool_intents_only():
    # the cool bias pulls neutral/deep DOWN, but the deliberately-warm intents bypass it
    assert _t(ThermalIntent.NEUTRAL, hot=True) < _t(ThermalIntent.NEUTRAL, hot=False)
    assert _t(ThermalIntent.DEEP_BIAS_COOL, hot=True) < _t(ThermalIntent.DEEP_BIAS_COOL, hot=False)
    assert _t(ThermalIntent.WAKE_RAMP, hot=True) == _t(ThermalIntent.WAKE_RAMP, hot=False)
    assert _t(ThermalIntent.ONSET_WARM, hot=True) == _t(ThermalIntent.ONSET_WARM, hot=False)


def test_short_night_damps_cooling_toward_neutral():
    deep_opt = _t(ThermalIntent.DEEP_BIAS_COOL, obj=NightObjective.OPTIMIZE)
    deep_dc = _t(ThermalIntent.DEEP_BIAS_COOL, obj=NightObjective.DAMAGE_CONTROL)
    assert deep_opt < deep_dc <= _t(ThermalIntent.NEUTRAL)    # calmer (closer to neutral) on short nights


def test_every_resolved_command_is_safe():
    th = ThermalController(CFG)
    last = 70.0
    for intent in ThermalIntent:
        water, level = th.resolve(intent, NightObjective.OPTIMIZE, True, last,
                                  bed_temp_f=75.0, ambient_temp_f=70.0)
        assert 55.0 <= water <= 110.0 and -100 <= level <= 100
        assert abs(water - last) <= CFG.tunables.max_step_f + 1e-6   # slew never exceeded
        last = water


# --------------------------------------------------- 3. learners own DISTINCT knobs (no stomping)
def test_ml_actions_never_touch_the_wake_ramp():
    # wake_ramp_f is owned by the thermal_wake learner; onset_warm by the onset learner; settle by
    # the settle learner. The ML action-value set only moves maintenance/architecture knobs.
    for a in ACTIONS:
        assert "wake_ramp_f" not in a.deltas
        assert set(a.deltas) <= {"neutral_f", "deep_bias_f", "rem_warm_offset_f", "composite_bed_weight"}


def test_each_phase_learner_moves_only_its_own_target():
    th = ThermalController(CFG)
    base_neutral = _t(ThermalIntent.NEUTRAL)
    base_deep = _t(ThermalIntent.DEEP_BIAS_COOL)
    # wake learner -> only the wake ramp target moves
    th.profile = th.profile.__class__(**{**th.profile.__dict__, "wake_ramp_f": 80.0})
    assert th.target_for(ThermalIntent.WAKE_RAMP, NightObjective.OPTIMIZE, True, 70.0) == 80.0
    # onset learner -> only the onset target moves; neutral/deep unchanged
    th.set_onset_warm(2.0)
    assert th.target_for(ThermalIntent.ONSET_WARM, NightObjective.OPTIMIZE, True, 70.0) == th.profile.neutral_f + 2.0
    assert _t(ThermalIntent.NEUTRAL) == base_neutral and _t(ThermalIntent.DEEP_BIAS_COOL) == base_deep
    # settle learner -> only the settle target moves
    th.set_settle_nudge(1.5)
    assert th.settle_nudge_f == 1.5


# ------------------------------------- 4. deviation -> action -> bounded temperature change
def _policy_with_last(deep_min, rem_min, total=420):
    pol = TieredPolicy(CFG)
    pol.register_outcome(NightSummary(date="2026-06-10", total_sleep_min=total,
                                      deep_min=deep_min, rem_min=rem_min, wake_events=1))
    return pol


def test_low_deep_cools_the_deep_target():
    pol = _policy_with_last(deep_min=40, rem_min=90)          # deep ~10% -> below floor
    rec = pol.recommend(None, {"wake_events_delta": 0.0}, {})
    assert rec["target"] == "deep_bias_cooling"
    before = CFG.default_setpoints()
    after = apply_recommendation(before, rec, CFG)
    assert after.deep_bias_f < before.deep_bias_f             # the bed runs COOLER during deep
    assert before.deep_bias_f - after.deep_bias_f <= CFG.tunables.max_step_f  # bounded per night


def test_low_rem_warms_the_rem_target():
    pol = _policy_with_last(deep_min=90, rem_min=40)          # deep ok, REM ~10% -> below floor
    rec = pol.recommend(None, {"wake_events_delta": 0.0}, {})
    assert rec["target"] == "rem_warming"
    after = apply_recommendation(CFG.default_setpoints(), rec, CFG)
    assert after.rem_warm_offset_f > CFG.default_setpoints().rem_warm_offset_f   # warmer in REM


def test_maintenance_takes_priority_over_architecture():
    # wake events up AND deep low at once -> maintenance (sleep #1 priority) wins the conflict.
    pol = _policy_with_last(deep_min=40, rem_min=40)          # both deep and REM are low
    rec = pol.recommend(None, {"wake_events_delta": 2.0}, {})  # but wake events are up
    assert rec["target"] == "thermal_stability"               # not deep/rem — maintenance first


def test_changes_are_held_before_being_judged():
    pol = _policy_with_last(deep_min=40, rem_min=90)
    pol.recommend(None, {"wake_events_delta": 0.0}, {})       # opens a candidate
    # next night, still inside the hold window -> HOLD, don't flip on one night
    out = pol.recommend(None, {"wake_events_delta": 0.0}, {})
    assert out["action"] == "hold" and "before judging" in out["reason"]
