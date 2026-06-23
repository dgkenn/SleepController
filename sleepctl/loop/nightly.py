"""Nightly update cycle: close out the night, then Learn.

Run once when the user leaves the bed (or at a fixed morning time): persist the night
summary, refresh rolling baselines, feed the outcome to the tiered policy, estimate
response curves, and store a baselines snapshot. Conservative by construction — the
policy itself enforces min-hold-nights and single-bad-night resistance.
"""

from __future__ import annotations

from sleepctl.config import AppConfig
from sleepctl.learning.baselines import BaselineEngine
from sleepctl.learning.policy import TieredPolicy
from sleepctl.learning.response import ResponseEstimator
from sleepctl.learning.setpoints import apply_recommendation
from sleepctl.models import NightSummary
from sleepctl.storage.repository import Repository


class NightlyUpdater:
    def __init__(self, cfg: AppConfig, repo: Repository, policy: TieredPolicy | None = None) -> None:
        self.cfg = cfg
        self.repo = repo
        self.baselines = BaselineEngine()
        self.response = ResponseEstimator()
        self.policy = policy or TieredPolicy(cfg)

    def run(self, night: NightSummary) -> dict:
        """Persist + learn from a completed night; evolve the setpoint; return the result."""
        # The setpoint that was ACTIVE during the night gets credit/blame for its outcome.
        active = self.repo.latest_setpoints() or self.cfg.default_setpoints()
        self.repo.save_setpoints(active)  # ensure this version is recorded for ML
        night.setpoint_version = active.version
        self.repo.save_night_summary(night)

        history = self.repo.recent_nights(14)
        baselines = self.baselines.update(history)
        self.repo.save_baselines(baselines)

        deltas = self.baselines.nightly_delta(night, baselines)
        interventions = self.repo.recent_interventions(200)
        response = self.response.estimate(history, interventions)

        # Feed last night's outcome into the held candidate, then get a recommendation.
        self.policy.register_outcome(night)
        recommendation = self.policy.recommend(baselines, deltas, response, self.cfg)

        # Evolve the learnable setpoint for the NEXT night and persist the new version.
        next_profile = apply_recommendation(active, recommendation, self.cfg)
        if next_profile.version != active.version:
            self.repo.save_setpoints(next_profile)

        return {
            "date": night.date,
            "baselines": baselines.metrics,
            "deltas": deltas,
            "response": response,
            "recommendation": recommendation,
            "setpoint_version": active.version,
            "next_setpoint_version": next_profile.version,
        }
