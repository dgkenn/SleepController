"""The synchronous closed-loop runtime: Sense -> Decide -> Act -> (nightly) Learn.

One ``tick()`` reads the freshest frame, decides, acts on the device, and logs all three
dataset layers via the shared ``ControlCycle``. ``replay()`` drives the loop offline from
the simulator. The live (async) daemon lives in ``loop/live.py`` and shares the same
``ControlCycle`` so the decide/log logic is identical.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sleepctl.adapters.base import CalendarSource, PodSensorSource, ThermalActuator
from sleepctl.config import AppConfig
from sleepctl.controller.controller import SleepController
from sleepctl.loop.cycle import ControlCycle
from sleepctl.models import ContextRecord, Decision, SensorFrame
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
        self.cycle = ControlCycle(cfg, repo, controller)

    # Backwards-compatible accessors (some callers/tests inspect these).
    @property
    def controller(self) -> SleepController:
        return self.cycle.controller

    @property
    def recent(self) -> list[SensorFrame]:
        return self.cycle.recent

    def tick(self, frame: SensorFrame, context: Optional[ContextRecord], now: datetime) -> Decision:
        decision = self.cycle.decide(frame, context, now)
        level = self.cycle.pending_level(decision, frame, now)
        if level is not None:
            self.actuator.set_level(level)
        alarm = self.cycle.pending_alarm()
        if alarm is not None:
            self.actuator.set_alarm(alarm.time, alarm.vibration_power, alarm.thermal_level)
        self.cycle.log(frame, decision, now)
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
