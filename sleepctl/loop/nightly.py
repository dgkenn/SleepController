"""Nightly update cycle: close out the night, score it, then Learn.

Runs once when the user leaves the bed (or at a fixed morning time): scores the night with
the reward, refreshes baselines, then chooses the next setpoint via the ML action-value
recommender when it is confident and has enough clean data, otherwise the conservative
rule-based policy ("do no harm"). Every action is logged for attribution.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sleepctl.config import AppConfig
from sleepctl.learning.baselines import BaselineEngine
from sleepctl.learning.policy import TieredPolicy
from sleepctl.learning.response import ResponseEstimator
from sleepctl.learning.setpoints import apply_recommendation
from sleepctl.ml.preference import revealed_preference
from sleepctl.ml.recommend import recommend_action
from sleepctl.ml.reward import night_outcome_score
from sleepctl.models import ActionRecord, NightSummary
from sleepctl.storage.repository import Repository


class NightlyUpdater:
    def __init__(
        self,
        cfg: AppConfig,
        repo: Repository,
        policy: TieredPolicy | None = None,
        use_ml: bool = True,
    ) -> None:
        self.cfg = cfg
        self.repo = repo
        self.baselines = BaselineEngine()
        self.response = ResponseEstimator()
        self.policy = policy or TieredPolicy(cfg)
        self.use_ml = use_ml

    def run(self, night: NightSummary, dry_run: bool = False) -> dict:
        cfg = self.cfg
        active = self.repo.latest_setpoints() or cfg.default_setpoints()
        self.repo.save_setpoints(active)
        night.setpoint_version = active.version

        # --- score the night (reward) and persist --------------------------------
        ctx = self.repo.get_context(night.date)
        churn = self._intervention_churn(night.date)
        # Score against the night's situation-specific benchmark (work/short vs off-day).
        mode = None
        nt = (getattr(ctx, "night_type", None) or "").lower()
        if nt in ("recovery", "off", "off_day", "rest"):
            from sleepctl.benchmarks import NightMode
            mode = NightMode.RECOVERY
        elif nt in ("work", "constrained", "short") or getattr(ctx, "is_short_sleep_day", None):
            from sleepctl.benchmarks import NightMode
            mode = NightMode.CONSTRAINED
        night.outcome_score = night_outcome_score(
            night, cfg, churn=churn,
            subjective_quality=getattr(ctx, "subjective_quality", None),
            grogginess=getattr(ctx, "grogginess", None),
            mode=mode,
        )
        self.repo.save_night_summary(night)
        self.repo.backfill_action_rewards()  # attribute rewards to the actions that earned them

        # --- baselines / response / rule policy ----------------------------------
        history = self.repo.recent_nights(14)
        baselines = self.baselines.update(history)
        self.repo.save_baselines(baselines)
        deltas = self.baselines.nightly_delta(night, baselines)
        response = self.response.estimate(history, self.repo.recent_interventions(200))

        self.policy.register_outcome(night)

        # --- choose the next setpoint: ML when confident, else rule policy --------
        ml_choice = recommend_action(self.repo, active, cfg) if self.use_ml else None
        if ml_choice is not None:
            next_profile = ml_choice.profile
            chosen = {
                "action": ml_choice.name, "source": "ml",
                "confidence": ml_choice.confidence, "reason": ml_choice.reason,
                "predicted": ml_choice.predicted, "params": ml_choice.action.deltas,
            }
            recommendation = {"action": ml_choice.name, "target": "ml",
                              "reason": ml_choice.reason}
        else:
            recommendation = self.policy.recommend(baselines, deltas, response, cfg)
            next_profile = apply_recommendation(active, recommendation, cfg)
            chosen = {
                "action": recommendation["action"], "source": "fallback",
                "confidence": 0.0, "reason": recommendation["reason"],
                "predicted": {}, "params": {"target": recommendation.get("target")},
            }

        # Anchor toward the user's repeated MANUAL temperature choices (revealed preference),
        # so constant manual tweaks pull the optimum instead of being fought each night.
        anchored = revealed_preference(self.repo, next_profile, cfg)
        if anchored is not None:
            next_profile = anchored
            chosen["reason"] += " | anchored toward manual preference"

        changed = next_profile.version != active.version
        if changed and not dry_run:
            self.repo.save_setpoints(next_profile)

        # --- log the chosen action (attributed to the version it creates) --------
        self.repo.log_action(ActionRecord(
            date=night.date,
            action_name=chosen["action"],
            params=chosen["params"],
            predicted=chosen["predicted"],
            confidence=chosen["confidence"],
            applied=changed and not dry_run,
            source=chosen["source"],
            creates_version=next_profile.version if changed else active.version,
        ))

        return {
            "date": night.date,
            "outcome_score": night.outcome_score,
            "baselines": baselines.metrics,
            "deltas": deltas,
            "response": response,
            "recommendation": recommendation,
            "chosen": chosen,
            "setpoint_version": active.version,
            "next_setpoint_version": next_profile.version if changed else active.version,
        }

    def _intervention_churn(self, night_date: str) -> float:
        """Number of distinct level changes logged for the night (oscillation proxy)."""
        row = self.repo.conn.execute(
            "SELECT COUNT(*) AS c FROM interventions WHERE night_date = ?", (night_date,)
        ).fetchone()
        return float(row["c"]) if row else 0.0
