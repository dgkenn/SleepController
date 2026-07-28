"""Action selection — the do-no-harm gate on every learned setpoint change.

``sleepctl/ml/select.py`` decides what the nightly learner actually does to the bed, and it had
no tests at any level. Its three safety properties are the reason the ML path is allowed to touch
a live setpoint at all, and each fails silently if broken:

  1. low confidence  -> hold. A thin-data model must never move the bed.
  2. small margin    -> hold. A predicted improvement inside the noise is not a reason to act.
  3. smallest effective. Given several actions that clear the bar, take the gentlest one.

``score_actions`` is tested against a stub model rather than a fitted one, so these pin the
SELECTION logic rather than the quality of whatever model is passed in.
"""

from __future__ import annotations

import pytest

from sleepctl.config import AppConfig
from sleepctl.ml.actions import ACTIONS, ActionScore
from sleepctl.ml.select import score_actions, select_action


def _score(name, reward, confidence, magnitude=None):
    action = next(a for a in ACTIONS if a.name == name)
    if magnitude is not None:
        action = type(action)(action.name, action.deltas, magnitude, action.kind)
    return ActionScore(action, None, {}, reward, confidence, reason=name)


def _cfg(conf_min=0.35, base_margin=0.5):
    cfg = AppConfig.default()
    cfg.ml.conf_min = conf_min
    cfg.ml.base_margin = base_margin
    return cfg


# ------------------------------------------------------------------ property 1: confidence gate
def test_low_confidence_always_holds_however_good_the_prediction_looks():
    cfg = _cfg(conf_min=0.35)
    scores = [_score("no_change", 0.0, 0.10), _score("strong_cool", 99.0, 0.10)]
    chosen = select_action(scores, cfg)
    assert chosen.name == "no_change"
    assert "do no harm" in chosen.reason


def test_confidence_exactly_at_the_floor_is_allowed_to_act():
    """A strict `<` at the boundary — pinned so it can't silently become `<=`."""
    cfg = _cfg(conf_min=0.35, base_margin=0.1)
    scores = [_score("no_change", 0.0, 0.35), _score("slight_cool", 5.0, 0.35)]
    assert select_action(scores, cfg).name == "slight_cool"


# ------------------------------------------------------------------ property 2: margin gate
def test_a_marginal_improvement_does_not_justify_moving_the_bed():
    cfg = _cfg(base_margin=0.5)
    # confidence 1.0 -> required margin = 0.5; a +0.4 gain must not clear it
    scores = [_score("no_change", 0.0, 1.0), _score("slight_cool", 0.4, 1.0)]
    chosen = select_action(scores, cfg)
    assert chosen.name == "no_change"
    assert "beats hold" in chosen.reason


def test_the_required_margin_grows_as_confidence_falls():
    """The uncertainty-aware part: the same predicted gain is accepted when the model is
    confident and refused when it isn't."""
    cfg = _cfg(conf_min=0.2, base_margin=0.5)
    gain = 0.8
    confident = [_score("no_change", 0.0, 1.0), _score("slight_cool", gain, 1.0)]
    unsure = [_score("no_change", 0.0, 0.4), _score("slight_cool", gain, 0.4)]
    assert select_action(confident, cfg).name == "slight_cool"   # required 0.5
    assert select_action(unsure, cfg).name == "no_change"        # required 1.25


def test_a_worse_prediction_never_wins():
    cfg = _cfg()
    scores = [_score("no_change", 1.0, 0.9), _score("strong_cool", -5.0, 0.9)]
    assert select_action(scores, cfg).name == "no_change"


# ------------------------------------------------------------------ property 3: smallest effective
def test_the_gentlest_qualifying_action_wins_over_a_bigger_one():
    cfg = _cfg(base_margin=0.1)
    scores = [_score("no_change", 0.0, 1.0),
              _score("slight_cool", 2.0, 1.0),      # magnitude 1
              _score("strong_cool", 3.0, 1.0)]      # magnitude 2, higher reward
    chosen = select_action(scores, cfg)
    assert chosen.name == "slight_cool", "a larger intervention must not win on reward alone"
    assert "smallest effective" in chosen.reason


def test_reward_breaks_ties_within_the_same_magnitude():
    cfg = _cfg(base_margin=0.1)
    scores = [_score("no_change", 0.0, 1.0),
              _score("slight_cool", 2.0, 1.0),
              _score("slight_warm", 3.0, 1.0)]      # same magnitude, better reward
    assert select_action(scores, cfg).name == "slight_warm"


def test_the_chosen_action_reports_its_gain_over_holding():
    cfg = _cfg(base_margin=0.1)
    scores = [_score("no_change", 1.0, 1.0), _score("slight_cool", 3.5, 1.0)]
    chosen = select_action(scores, cfg)
    assert "+2.50" in chosen.reason


# ------------------------------------------------------------------ scoring
def test_score_actions_scores_every_candidate_once():
    cfg = AppConfig.default()

    class _StubModel:
        def confidence(self):
            return 0.8

        def predict_outcomes(self, x):
            return {"deep_min": 90.0, "rem_min": 100.0, "wake_events": 1.0,
                    "sleep_efficiency": 0.9, "avg_hrv": 60.0, "total_sleep_min": 420.0}

    scores = score_actions(_StubModel(), cfg.default_setpoints(), {}, cfg)
    assert len(scores) == len(ACTIONS)
    assert {s.name for s in scores} == {a.name for a in ACTIONS}
    assert all(s.confidence == 0.8 for s in scores)


def test_score_actions_applies_each_action_to_a_distinct_candidate_profile():
    """Each score must carry the profile that action WOULD produce — if they all shared one
    profile the selector would be choosing between identical states."""
    cfg = AppConfig.default()

    class _StubModel:
        def confidence(self):
            return 0.9

        def predict_outcomes(self, x):
            return {"deep_min": 90.0, "rem_min": 100.0, "wake_events": 1.0,
                    "sleep_efficiency": 0.9, "avg_hrv": 60.0, "total_sleep_min": 420.0}

    scores = score_actions(_StubModel(), cfg.default_setpoints(), {}, cfg)
    by_name = {s.name: s for s in scores}
    hold = by_name["no_change"].profile
    cooled = by_name["slight_cool"].profile
    assert cooled.deep_bias_f < hold.deep_bias_f
    assert cooled is not hold


def test_no_change_is_always_present_for_the_selector_to_fall_back_to():
    """select_action does an unguarded `next(...)` for it; its absence would be a crash."""
    assert any(a.name == "no_change" and a.magnitude == 0 for a in ACTIONS)
