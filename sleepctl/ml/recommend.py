"""ML recommender — the nightly action-value tailoring step.

Pipeline: build feature rows -> drop confounded nights -> gate on data sufficiency ->
fit per-outcome ridge models -> score the candidate actions at the recent context ->
select the smallest effective one (uncertainty-gated). Returns the chosen ``ActionScore``
(its ``profile`` is the next ``SetpointProfile(source="ml")``) or ``None`` to defer to the
conservative rule-based policy ("do no harm").
"""

from __future__ import annotations

from statistics import median
from typing import Optional

from sleepctl.config import AppConfig
from sleepctl.ml.actions import ActionScore
from sleepctl.ml.confounders import clean_rows
from sleepctl.ml.dataset import CONTEXT_FEATURES, build_feature_rows
from sleepctl.ml.model import SetpointModel
from sleepctl.ml.select import score_actions, select_action
from sleepctl.models import SetpointProfile


def _recent_context(rows) -> dict:
    """Most recent non-null context per feature, else median across rows, else 0."""
    ctx = {}
    for f in CONTEXT_FEATURES:
        latest = next((getattr(r, f) for r in reversed(rows) if getattr(r, f) is not None), None)
        if latest is None:
            vals = [getattr(r, f) for r in rows if getattr(r, f) is not None]
            latest = median(vals) if vals else 0.0
        ctx[f] = latest
    return ctx


def recommend_action(
    repo,
    current: SetpointProfile,
    cfg: AppConfig,
) -> Optional[ActionScore]:
    rows = build_feature_rows(repo)
    usable = clean_rows(rows)  # exclude confounded nights from training
    if len(usable) < cfg.ml.min_nights:
        return None  # not enough clean data -> defer to the rule-based policy

    model = SetpointModel(lam=cfg.ml.ridge_lambda).fit(usable)
    if not ({"wake_events", "deep_pct"} & set(model.trained_outcomes())):
        return None  # no maintenance/deep signal yet

    ctx = _recent_context(rows)
    scores = score_actions(model, current, ctx, cfg)
    chosen = select_action(scores, cfg)
    return chosen


def recommend_setpoint(repo, current: SetpointProfile, cfg: AppConfig) -> Optional[SetpointProfile]:
    """Convenience: return just the next profile (or None), for callers that want it."""
    chosen = recommend_action(repo, current, cfg)
    if chosen is None or chosen.name == "no_change":
        return None
    return chosen.profile
