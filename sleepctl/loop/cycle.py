"""Shared control cycle: decide + log + level-change tracking.

Extracted from ``Runtime.tick`` so the synchronous offline runtime and the asynchronous
live daemon share IDENTICAL decide/log/intervention logic — only the *act* step differs
(sync ``actuator.set_level`` vs. ``await client.set_heating_level``).

Usage per tick:
    decision = cycle.decide(frame, context, now)
    level = cycle.pending_level(decision, frame, now)   # None unless it changed
    if level is not None: <act: sync or async>
    cycle.log(frame, decision, now)
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sleepctl.config import AppConfig
from sleepctl.controller.controller import SleepController
from sleepctl.models import ContextRecord, Decision, Intervention, SensorFrame
from sleepctl.storage.repository import Repository


class ControlCycle:
    def __init__(self, cfg: AppConfig, repo: Repository, controller: Optional[SleepController] = None) -> None:
        self.cfg = cfg
        self.repo = repo
        self.controller = controller or SleepController(cfg)
        self.recent: list[SensorFrame] = []
        self._last_action_level: Optional[int] = None
        self._wake_alarm_sent = False

    def pending_alarm(self):
        """Return the controller's wake alarm spec once (heat+vibration), else None."""
        spec = getattr(self.controller, "pending_wake_alarm", None)
        if spec is not None and not self._wake_alarm_sent:
            self._wake_alarm_sent = True
            return spec
        return None

    @staticmethod
    def night_date(now: datetime) -> str:
        """Group a night under the date it STARTED, using a noon cutoff.

        Everything from noon on day D through noon on D+1 (so the whole overnight,
        including post-midnight and a 2am bedtime for a late-night worker) is labeled D.
        """
        from datetime import timedelta

        return (now - timedelta(hours=12)).date().isoformat()

    def decide(self, frame: SensorFrame, context: Optional[ContextRecord], now: datetime) -> Decision:
        return self.controller.decide(frame, context, self.recent, now, self.repo.latest_baselines())

    def pending_level(self, decision: Decision, frame: SensorFrame, now: datetime) -> Optional[int]:
        """If the commanded level changed, log the Intervention and return the new level.

        The Intervention is logged here (intent to act). Callers then perform the actual
        device write (sync or async); in dry-run they skip the write but the intent is
        still recorded.
        """
        if self._last_action_level == decision.target_level:
            return None
        magnitude_f = abs(
            decision.target_temp_f
            - (frame.bed_temp_f if frame.bed_temp_f is not None else decision.target_temp_f)
        )
        self.repo.log_intervention(
            Intervention(
                timestamp=now,
                state=decision.state,
                action=decision.action,
                magnitude_f=round(magnitude_f, 2),
                reason=decision.reason,
            ),
            self.night_date(now),
        )
        self._last_action_level = decision.target_level
        return decision.target_level

    def log(self, frame: SensorFrame, decision: Decision, now: datetime) -> None:
        night_date = self.night_date(now)
        wake = bool(self.controller.last_wake_event)
        self.repo.log_sample(frame, decision.state.value, wake, night_date)
        self.repo.log_decision(decision, night_date)
        # Record an anticipatory pre-cool event (edge-triggered) for efficacy learning.
        evt = getattr(self.controller, "pending_precool_event", None)
        if evt is not None:
            try:
                self.repo.log_precool_event(
                    night_date, evt["ts"], evt["window_type"],
                    evt["lead_used_min"], evt["eta_min"])
            except Exception:
                pass
            self.controller.pending_precool_event = None
        # Record an in-night "nudge deeper" steer event (edge-triggered) for efficacy learning.
        steer = getattr(self.controller, "pending_steer_event", None)
        if steer is not None:
            try:
                self.repo.log_steer_event(
                    night_date, steer["ts"], steer["maneuver"], steer["stage_before"],
                    steer["deep_deficit_min"], steer["frac_of_night"], steer["horizon_min"])
            except Exception:
                pass
            self.controller.pending_steer_event = None
        self.recent.append(frame)
        if len(self.recent) > 60:
            self.recent = self.recent[-60:]
