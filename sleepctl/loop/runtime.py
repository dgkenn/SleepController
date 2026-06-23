"""The closed-loop runtime: Sense -> Decide -> Act -> (nightly) Learn.

One ``tick()`` reads the freshest frame, asks the controller to decide, acts on the
device (slew/variability already enforced inside the controller), and logs all three
dataset layers. ``replay()`` drives the loop offline from the simulator; a live daemon
would call ``tick()`` on a timer (~1 min) instead.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sleepctl.adapters.base import CalendarSource, PodSensorSource, ThermalActuator
from sleepctl.config import AppConfig
from sleepctl.controller.controller import SleepController
from sleepctl.models import ContextRecord, ControllerState, Decision, Intervention, SensorFrame
from sleepctl.storage.repository import Repository


class Runtime:
    def __init__(
        self,
        cfg: AppConfig,
        source: PodSensorSource,
        actuator: ThermalActuator,
        repo: Repository,
        calendar: Optional[CalendarSource] = None,
        controller: Optional[SleepController] = None,
    ) -> None:
        self.cfg = cfg
        self.source = source
        self.actuator = actuator
        self.repo = repo
        self.calendar = calendar
        self.controller = controller or SleepController(cfg)
        self.recent: list[SensorFrame] = []
        self._last_action_level: Optional[int] = None

    def _night_date(self, now: datetime) -> str:
        # Group a night under the date it started (evening -> that calendar day).
        return now.date().isoformat()

    def tick(self, frame: SensorFrame, context: Optional[ContextRecord], now: datetime) -> Decision:
        decision = self.controller.decide(frame, context, self.recent, now, self.repo.latest_baselines())
        night_date = self._night_date(now)

        # --- Act -------------------------------------------------------------
        if self._last_action_level != decision.target_level:
            self.actuator.set_level(decision.target_level)
            magnitude_f = abs(
                decision.target_temp_f
                - (frame.bed_temp_f if frame.bed_temp_f is not None else decision.target_temp_f)
            )
            iv = Intervention(
                timestamp=now,
                state=decision.state,
                action=decision.action,
                magnitude_f=round(magnitude_f, 2),
                reason=decision.reason,
            )
            self.repo.log_intervention(iv, night_date)
            self._last_action_level = decision.target_level

        # --- Log (all three dataset layers) ----------------------------------
        wake = bool(self.controller.last_wake_event)
        self.repo.log_sample(frame, decision.state.value, wake, night_date)
        self.repo.log_decision(decision, night_date)

        self.recent.append(frame)
        if len(self.recent) > 60:
            self.recent = self.recent[-60:]
        return decision

    def replay(self, context: Optional[ContextRecord] = None) -> list[Decision]:
        """Drive the whole loop from a SimulatorSource until its night is exhausted."""
        from sleepctl.adapters.simulator import SimulatorSource

        if not isinstance(self.source, SimulatorSource):
            raise TypeError("replay() requires a SimulatorSource")

        decisions: list[Decision] = []
        while not self.source.exhausted:
            frame = self.source.read_frame()
            now = self.source.now()
            decisions.append(self.tick(frame, context, now))
        return decisions
