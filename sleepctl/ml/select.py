"""Action scoring + smallest-effective selection (with uncertainty gating).

For each candidate action we predict the resulting outcomes (at the action's setpoint +
recent context), score them with the reward, and attach the model's confidence. Selection
prefers **no change**, then the **smallest** action whose predicted benefit clears an
uncertainty-aware margin; low confidence -> no change ("do no harm").
"""

from __future__ import annotations

from typing import Optional

from sleepctl.benchmarks import NightMode
from sleepctl.config import AppConfig
from sleepctl.ml.actions import ACTIONS, ActionScore, apply_action
from sleepctl.ml.dataset import SETPOINT_FEATURES
from sleepctl.ml.model import SetpointModel
from sleepctl.ml.reward import reward_from_outcomes


def score_actions(model: SetpointModel, profile, ctx: dict, cfg: AppConfig,
                  mode: Optional[NightMode] = None) -> list[ActionScore]:
    conf = model.confidence()
    scores: list[ActionScore] = []
    for action in ACTIONS:
        cand = apply_action(profile, action)
        x = {**{k: getattr(cand, k) for k in SETPOINT_FEATURES}, **ctx}
        predicted = model.predict_outcomes(x)
        reward = reward_from_outcomes(predicted, cfg, mode=mode)
        scores.append(ActionScore(action, cand, predicted, reward, conf,
                                  reason=f"{action.name} (mag {action.magnitude})"))
    return scores


def select_action(scores: list[ActionScore], cfg: AppConfig) -> ActionScore:
    no_change = next(s for s in scores if s.name == "no_change")
    conf = no_change.confidence
    if conf < cfg.ml.conf_min:
        no_change.reason = f"confidence {conf:.2f} < {cfg.ml.conf_min} -> hold (do no harm)"
        return no_change

    # Uncertainty-aware threshold: require a bigger improvement when less confident.
    required = cfg.ml.base_margin / max(conf, 0.2)
    improving = [s for s in scores
                 if s.name != "no_change" and s.reward > no_change.reward + required]
    if not improving:
        no_change.reason = f"no action beats hold by >{required:.2f} reward -> hold"
        return no_change

    # Smallest effective: lowest magnitude first, then highest reward.
    improving.sort(key=lambda s: (s.action.magnitude, -s.reward))
    best = improving[0]
    gain = best.reward - no_change.reward
    best.reason = (f"{best.name}: +{gain:.2f} reward over hold "
                   f"(smallest effective, conf {conf:.2f})")
    return best
