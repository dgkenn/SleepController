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
        """The wake alarm spec while it still needs programming onto the device, else None.

        Does NOT mark it sent — the caller must call :meth:`mark_alarm_sent` once the device has
        actually ACCEPTED it. This used to flip the flag on the way out, which meant a single
        failure in the send (a cloud 5xx, a token refresh, a network blip, or the Pod having no
        alarm slot to drive) silently discarded the alarm for the WHOLE NIGHT: the spec was
        recorded as delivered by the act of handing it over. For a user whose only wake mechanism
        is vibration — audio is deliberately off — that is the difference between waking up and
        not. Retrying on the next tick costs nothing; the device accepts the same PUT idempotently.
        """
        spec = getattr(self.controller, "pending_wake_alarm", None)
        if spec is not None and not self._wake_alarm_sent:
            return spec
        return None

    def mark_alarm_sent(self) -> None:
        """Confirm the device accepted the alarm; stops ``pending_alarm`` re-offering it."""
        self._wake_alarm_sent = True

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
        # RE-ASSERT on drift. Returning None whenever the commanded level is unchanged means the
        # controller speaks to the device exactly ONCE per target change and then goes silent --
        # so anything else driving the Pod wins permanently, because we only ever fight once.
        #
        # That is not hypothetical here: this account's Eight Sleep bedtime schedule cannot be
        # disabled through the API (the server refuses the write with "Subscription required"),
        # and it walks the device away from us at ~1 level/min. Measured 2026-08-05 with the user
        # in bed: target -72 held constant while the device drifted -56 -> -48 over ~8 minutes,
        # with NO command sent in between. The thermal-response check then reported it as stalled
        # hardware and advised power-cycling the Hub, which would have found nothing wrong.
        #
        # So when the device has drifted materially off target we re-send, even though our own
        # decision has not changed. Re-assertion does NOT log a fresh Intervention -- the intent
        # to act has not changed, only the device's compliance with it, and logging every refresh
        # would swamp the ledger the learners read.
        dev = getattr(frame, "device_level", None)
        tol = getattr(getattr(self.cfg, "tunables", None), "level_reassert_tolerance", 5)
        if self._last_action_level == decision.target_level:
            if dev is None or abs(int(dev) - int(decision.target_level)) <= int(tol):
                return None
            return decision.target_level
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
                    steer["deep_deficit_min"], steer["frac_of_night"], steer["horizon_min"],
                    applied=steer.get("applied", 1))
            except Exception:
                pass
            self.controller.pending_steer_event = None
        self.recent.append(frame)
        if len(self.recent) > 60:
            self.recent = self.recent[-60:]
