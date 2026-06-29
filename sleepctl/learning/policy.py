"""Tiered, conservative cross-night policy: try -> hold -> escalate/revert.

Priorities mirror CONTROL_PRIORITY: sleep maintenance (wake events) is the dominant
error signal, then stage confidence/HRV, then deep/efficiency. The policy starts with
minimal changes, holds them for ``min_hold_nights`` before judging, escalates only if
no improvement, and reverts if the priority metrics clearly worsen. It will NOT change
course on a single bad night.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from sleepctl.config import AppConfig
from sleepctl.models import Baselines, NightSummary


@dataclass
class _Candidate:
    target: str  # which knob, e.g. "deep_bias_cooling"
    magnitude_f: float
    nights_held: int = 0
    outcomes: list[float] = field(default_factory=list)  # priority-metric scores


def _priority_score(night: NightSummary) -> float:
    """Higher is better. Wake events dominate (maintenance is the top priority)."""
    score = 0.0
    if night.wake_events is not None:
        score -= 10.0 * night.wake_events  # fewer wake-ups strongly preferred
    if night.sleep_efficiency is not None:
        score += 5.0 * night.sleep_efficiency
    if night.deep_min is not None:
        score += 0.02 * night.deep_min
    if night.avg_hrv is not None:
        score += 0.05 * night.avg_hrv
    return score


class TieredPolicy:
    def __init__(self, cfg: AppConfig) -> None:
        self.cfg = cfg
        self.candidate: Optional[_Candidate] = None
        self._last_night: Optional[NightSummary] = None

    def register_outcome(self, night: NightSummary) -> None:
        """Feed last night's result into the held candidate's trail."""
        self._last_night = night
        if self.candidate is not None:
            self.candidate.outcomes.append(_priority_score(night))
            self.candidate.nights_held += 1

    @staticmethod
    def _stage_fraction(night: Optional[NightSummary], stage_min: str) -> Optional[float]:
        if night is None or night.total_sleep_min in (None, 0):
            return None
        val = getattr(night, stage_min, None)
        if val is None:
            return None
        return float(val) / float(night.total_sleep_min)

    def recommend(
        self,
        baselines: Baselines,
        deltas: dict,
        response: dict,
        cfg: Optional[AppConfig] = None,
        targets=None,
    ) -> dict:
        cfg = cfg or self.cfg
        min_hold = cfg.tunables.min_hold_nights
        max_step = cfg.tunables.max_step_f
        # Drive toward the user's LEARNED ideal floors when they're given (from
        # personalized_targets — felt-recovery + stress, bounded near evidence); otherwise FALL
        # BACK to the evidence floor under uncertainty. This is how the controller chases YOUR ideal
        # without ever straying from the literature when it doesn't yet know you.
        deep_floor = getattr(targets, "deep_pct_min", None)
        rem_floor = getattr(targets, "rem_pct_min", None)
        if deep_floor is None:
            deep_floor = cfg.benchmarks.deep_pct_floor
        if rem_floor is None:
            rem_floor = cfg.benchmarks.rem_pct_floor

        # No active candidate -> start a minimal trial aimed at the top priority.
        if self.candidate is None:
            wake_delta = deltas.get("wake_events_delta", 0.0)
            deep_pct = self._stage_fraction(self._last_night, "deep_min")
            rem_pct = self._stage_fraction(self._last_night, "rem_min")
            reason = "start minimal {} trial (Tier 1)"
            # Priority order: maintenance (wake events) first, then the Autopilot-style
            # low-deep / low-REM stage triggers.
            if wake_delta and wake_delta > 0:
                target, reason = "thermal_stability", "wake events up vs baseline -> " + reason
            elif deep_pct is not None and deep_pct < deep_floor:
                target = "deep_bias_cooling"
                reason = f"deep {deep_pct:.0%} < {deep_floor:.0%} -> " + reason
            elif rem_pct is not None and rem_pct < rem_floor:
                target = "rem_warming"
                reason = f"REM {rem_pct:.0%} < {rem_floor:.0%} -> " + reason
            else:
                target = "deep_bias_cooling"
            self.candidate = _Candidate(target=target, magnitude_f=min(1.0, max_step))
            return {
                "action": "try",
                "target": target,
                "magnitude_f": self.candidate.magnitude_f,
                "reason": reason.format(target),
            }

        c = self.candidate
        # Still inside the hold window -> hold; do not judge on a single bad night.
        if c.nights_held < min_hold:
            return {
                "action": "hold",
                "target": c.target,
                "magnitude_f": c.magnitude_f,
                "reason": f"holding {c.target} ({c.nights_held}/{min_hold} nights before judging)",
            }

        # Judge on a ROBUST aggregate of the trail, never a single night. The first
        # night is the reference; post-change nights are compared by their median and
        # by how MANY of them moved, so one outlier bad night cannot flip the policy.
        import statistics

        baseline = c.outcomes[0] if c.outcomes else 0.0
        post = c.outcomes[1:]
        post_median = statistics.median(post) if post else baseline
        improved = bool(post) and post_median > baseline + 1e-9

        margin = 5.0  # priority-score drop equivalent to ~half a wake event
        worse_nights = sum(1 for o in post if o < baseline - margin)
        # Revert only when a MAJORITY of post-change nights are clearly worse AND the
        # median is worse — robust to a single bad night.
        worsened = (
            len(post) >= 2
            and worse_nights > len(post) / 2
            and post_median < baseline - margin
        )

        if worsened:
            target = c.target
            self.candidate = None
            return {
                "action": "revert",
                "target": target,
                "magnitude_f": 0.0,
                "reason": f"{target} worsened priority metrics; reverting toward baseline",
            }
        if improved:
            target = c.target
            self.candidate = None  # lock in; re-baseline before the next trial
            return {
                "action": "hold",
                "target": target,
                "magnitude_f": c.magnitude_f,
                "reason": f"{target} improved outcomes; keep and re-baseline",
            }
        # No improvement after the hold window -> escalate one small step.
        c.magnitude_f = min(max_step, c.magnitude_f + 0.5)
        c.nights_held = 0
        c.outcomes = []
        return {
            "action": "escalate",
            "target": c.target,
            "magnitude_f": c.magnitude_f,
            "reason": f"no improvement; escalate {c.target} to {c.magnitude_f}°F (still <= max_step)",
        }
