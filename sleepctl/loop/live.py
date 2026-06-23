"""Asynchronous live daemon: drives the real Pod via the async pyEight client.

Bridges pyEight's async sense/act to the sync ``ControlCycle`` (shared with the offline
``Runtime``). Each iteration: update -> read frame -> decide -> (act unless dry-run) ->
log -> on return-to-IDLE run the nightly close-out -> sleep. Honors a shutdown event and
always closes the client.

``LiveClient`` is the minimal async protocol the daemon needs; ``EightSleepClient``
implements it for the real device, and ``SimulatedLiveClient`` implements it over the
deterministic simulator so the daemon is fully testable offline (no pyEight required).
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Optional, Protocol

from sleepctl.adapters.simulator import SimulatorActuator, SimulatorSource
from sleepctl.config import AppConfig
from sleepctl.controller.controller import SleepController
from sleepctl.loop.cycle import ControlCycle
from sleepctl.loop.nightly import NightlyUpdater
from sleepctl.models import (
    ContextRecord,
    ControllerState,
    Decision,
    NightSummary,
    SensorFrame,
    SleepStage,
)
from sleepctl.storage.repository import Repository


class LiveClient(Protocol):
    """Async device protocol the daemon depends on."""

    async def connect(self) -> None: ...
    async def update(self) -> None: ...
    def read_frame(self) -> SensorFrame: ...
    def now(self) -> datetime: ...
    async def set_heating_level(self, level: int, duration_s: int = 0) -> None: ...
    async def set_wake_alarm(self, spec) -> None: ...
    async def fetch_night_summary(self, date: str) -> NightSummary: ...
    async def close(self) -> None: ...


class LiveDaemon:
    def __init__(
        self,
        cfg: AppConfig,
        client: LiveClient,
        repo: Repository,
        context: Optional[ContextRecord] = None,
        controller: Optional[SleepController] = None,
        weather=None,
        verbose: bool = True,
    ) -> None:
        self.cfg = cfg
        self.client = client
        self.repo = repo
        self.context = context or ContextRecord(date=datetime.now().date().isoformat())
        self.weather = weather  # optional WeatherSource for ambient awareness
        self.cycle = ControlCycle(cfg, repo, controller)
        self.nightly = NightlyUpdater(cfg, repo)
        self.verbose = verbose
        self._prev_state: ControllerState = ControllerState.IDLE
        self._saw_sleep = False
        self.decisions: list[Decision] = []

    def _log(self, msg: str) -> None:
        if self.verbose:
            print(msg, flush=True)

    def _refresh_weather(self) -> None:
        """Update the context's outdoor temp from the weather source (fails soft)."""
        if self.weather is None:
            return
        temp = self.weather.current_temp_f()  # cached internally; safe to call each tick
        if temp is not None:
            self.context.outdoor_temp_f = temp

    async def run(
        self,
        poll_seconds: float = 60.0,
        dry_run: bool = False,
        max_ticks: Optional[int] = None,
        shutdown_event: Optional[asyncio.Event] = None,
    ) -> list[Decision]:
        await self.client.connect()
        self._log(
            f"sleepctl live daemon started (dry_run={dry_run}, poll={poll_seconds}s)."
            + ("  [READ-ONLY: no temperature commands will be sent]" if dry_run else "")
        )
        ticks = 0
        try:
            while True:
                await self.client.update()
                self._refresh_weather()
                frame = self.client.read_frame()
                now = self.client.now()

                decision = self.cycle.decide(frame, self.context, now)
                self.decisions.append(decision)

                level = self.cycle.pending_level(decision, frame, now)
                if level is not None and not dry_run:
                    await self.client.set_heating_level(level)
                alarm = self.cycle.pending_alarm()
                if alarm is not None and not dry_run:
                    await self.client.set_wake_alarm(alarm)
                self.cycle.log(frame, decision, now)

                self._log(
                    f"[{now:%H:%M}] {decision.state.value:<13} {decision.thermal_intent.value:<14} "
                    f"target={decision.target_temp_f:>5.1f}F level={decision.target_level:>4} "
                    f"{decision.action.value:<6} {'(dry)' if dry_run and level is not None else ''} "
                    f"| {decision.reason}"
                )

                await self._maybe_close_out_night(decision, now)
                self._prev_state = decision.state

                ticks += 1
                if max_ticks is not None and ticks >= max_ticks:
                    break
                if shutdown_event is not None and shutdown_event.is_set():
                    break
                if not await self._sleep(poll_seconds, shutdown_event):
                    break
        finally:
            await self.client.close()
            self._log("sleepctl live daemon stopped; device client closed.")
        return self.decisions

    async def _sleep(self, seconds: float, shutdown_event: Optional[asyncio.Event]) -> bool:
        """Sleep, returning False if a shutdown was requested during the wait."""
        if shutdown_event is None:
            await asyncio.sleep(seconds)
            return True
        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=seconds)
            return False  # event fired -> stop
        except asyncio.TimeoutError:
            return True

    async def _maybe_close_out_night(self, decision: Decision, now: datetime) -> None:
        """When the user leaves the bed after a night of sleep, run the Learn phase."""
        if decision.state in (
            ControllerState.MAINTENANCE,
            ControllerState.WAKE_RECOVERY,
            ControllerState.WAKE_WINDOW,
        ):
            self._saw_sleep = True
        left_bed = (
            decision.state is ControllerState.IDLE
            and self._prev_state is not ControllerState.IDLE
        )
        if left_bed and self._saw_sleep:
            night_date = self.cycle.night_date(now)
            # Persist the night's ambient/schedule context (layer 3) before learning.
            self.context.date = night_date
            self.repo.save_context(self.context)
            night = await self.client.fetch_night_summary(night_date)
            result = self.nightly.run(night)
            rec = result["recommendation"]
            self._log(f"  nightly learn: {rec['action']} -> {rec['reason']}")
            self._saw_sleep = False


class SimulatedLiveClient:
    """Async LiveClient backed by the deterministic simulator (offline testing/demo).

    After the scripted night is exhausted it emits a few "out of bed" frames (presence
    False) so the daemon's end-of-night close-out path is exercised.
    """

    def __init__(
        self,
        scenario: str = "normal",
        seed: int = 7,
        start: Optional[datetime] = None,
        trailing_out_of_bed: int = 3,
    ) -> None:
        from datetime import timedelta

        self._timedelta = timedelta
        self.source = SimulatorSource(scenario, seed=seed, start=start)
        self.actuator = SimulatorActuator(self.source)
        self._trailing = trailing_out_of_bed
        self._extra = 0

    async def connect(self) -> None:
        return None

    async def update(self) -> None:
        return None

    def read_frame(self) -> SensorFrame:
        if not self.source.exhausted:
            return self.source.read_frame()
        # Night over: report the user out of bed so the daemon closes out the night.
        self._extra += 1
        return SensorFrame(
            timestamp=self.now(),
            stage=SleepStage.AWAKE,
            presence=False,
            bed_temp_f=70.0,
            room_temp_f=68.0,
            data_age_seconds=30.0,
        )

    def now(self) -> datetime:
        base = self.source.now()
        if self.source.exhausted and self._extra:
            return base + self._timedelta(minutes=self._extra)
        return base

    @property
    def finished(self) -> bool:
        return self.source.exhausted and self._extra >= self._trailing

    async def set_heating_level(self, level: int, duration_s: int = 0) -> None:
        self.actuator.set_level(level, duration_s)

    async def set_wake_alarm(self, spec) -> None:
        self.actuator.set_alarm(spec.time, spec.vibration_power, spec.thermal_level)

    async def fetch_night_summary(self, date: str) -> NightSummary:
        return self.source.fetch_night_summary(date)

    async def close(self) -> None:
        return None
