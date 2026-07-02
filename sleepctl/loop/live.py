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
from dataclasses import replace
from datetime import datetime
from typing import Optional, Protocol

from sleepctl.adapters import thermal_sim
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
    async def update(self, user: bool = True, device: bool = True) -> None: ...
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


# Per-`pod_scenario` thermal-model parameters (levels/min ramp, capacity-band fraction).
# ``None``/"normal" never reaches this table -- the legacy idealized path is untouched.
# See ``sleepctl.adapters.thermal_sim`` for what these numbers are matched against.
_POD_SCENARIOS = (
    "realistic", "air_bound", "stuck_prime", "competing_controller",
    "frozen_telemetry", "rate_limited",
)
_POD_THERMAL_PARAMS = {
    "realistic": dict(ramp=thermal_sim.DEFAULT_RAMP_PER_MIN, capacity=thermal_sim.DEFAULT_CAPACITY),
    "air_bound": dict(ramp=thermal_sim.AIR_BOUND_RAMP_PER_MIN, capacity=thermal_sim.AIR_BOUND_CAPACITY),
    "stuck_prime": dict(ramp=thermal_sim.DEFAULT_RAMP_PER_MIN, capacity=thermal_sim.DEFAULT_CAPACITY),
    "competing_controller": dict(ramp=thermal_sim.DEFAULT_RAMP_PER_MIN, capacity=thermal_sim.DEFAULT_CAPACITY),
    "frozen_telemetry": dict(ramp=thermal_sim.DEFAULT_RAMP_PER_MIN, capacity=thermal_sim.DEFAULT_CAPACITY),
    "rate_limited": dict(ramp=thermal_sim.DEFAULT_RAMP_PER_MIN, capacity=thermal_sim.DEFAULT_CAPACITY),
}
# air_bound and stuck_prime both model "a prime that never completes": the element cannot
# actually move regardless of what target it's told to chase.
_STUCK_PRIME_SCENARIOS = ("stuck_prime", "air_bound")


class SimulatedLiveClient:
    """Async LiveClient backed by the deterministic simulator (offline testing/demo).

    After the scripted night is exhausted it emits a few "out of bed" frames (presence
    False) so the daemon's end-of-night close-out path is exercised.

    By default this is the original idealized model (bed_temp_f jitters near 70F regardless
    of the commanded level) -- unchanged, so existing callers/tests see identical behavior.
    Passing ``pod_scenario`` opts into the realistic Pod 2 dynamics from
    ``sleepctl.adapters.thermal_sim`` (slow plate ramp + capacity/ambient-bounded bed temp)
    plus, for every value except ``"realistic"``, one of the adverse failure modes measured
    live:

      * ``"realistic"``            -- accurate dynamics only, no fault.
      * ``"air_bound"``             -- reduced heat-transfer capacity: narrow achievable band
                                       AND a slower ramp; a prime never completes.
      * ``"stuck_prime"``           -- ``priming`` stays True forever, ``last_prime`` never
                                       advances, and the plate cannot move.
      * ``"competing_controller"``  -- an external actor (the app / a zombie daemon) resets
                                       the device's own target register back toward
                                       ``competing_target_level`` every ``competing_period_ticks``
                                       ticks, regardless of our last command.
      * ``"frozen_telemetry"``      -- device readings (bed_temp, level) stop changing across
                                       ticks (and their reported age keeps growing) even
                                       though commands are still issued.
      * ``"rate_limited"``          -- every ``rate_limited_every``-th read comes back
                                       stale/None (models a cloud ``RequestError``).

    One simulated minute elapses per ``read_frame()`` call (``dt_min=1.0``), matching the
    scripted night's per-minute cadence.
    """

    def __init__(
        self,
        scenario: str = "normal",
        seed: int = 7,
        start: Optional[datetime] = None,
        trailing_out_of_bed: int = 3,
        pod_scenario: Optional[str] = None,
        ambient_f: float = 72.0,
        competing_period_ticks: int = 20,
        competing_target_level: int = -68,
        rate_limited_every: int = 5,
        freeze_after_ticks: int = 5,
    ) -> None:
        from datetime import timedelta

        self._timedelta = timedelta
        self.source = SimulatorSource(scenario, seed=seed, start=start)
        self.actuator = SimulatorActuator(self.source)
        self._trailing = trailing_out_of_bed
        self._extra = 0
        # device-parity state (so the dashboard live daemon is testable offline)
        self.powered = True
        self.away = False
        self.prime_count = 0
        self.off_count = 0
        self.on_count = 0
        self.last_alarm = None
        self.last_level = None
        self.level_set_count = 0

        # -- realistic thermal model / adverse-scenario state (opt-in) --------------------
        if pod_scenario is not None and pod_scenario not in _POD_SCENARIOS:
            raise ValueError(f"unknown pod_scenario {pod_scenario!r}; expected one of "
                              f"{_POD_SCENARIOS}")
        self.pod_scenario = pod_scenario
        self.ambient_f = ambient_f
        self.competing_period_ticks = max(1, int(competing_period_ticks))
        self.competing_target_level = competing_target_level
        self.rate_limited_every = max(1, int(rate_limited_every))
        self.freeze_after_ticks = max(1, int(freeze_after_ticks))
        self._tick = 0
        self._plate_level = 0.0          # actual plate level (ramps)
        self._plate_target = 0.0         # the device's own target register
        self._bed_temp_f = ambient_f
        self._our_last_commanded: Optional[int] = None
        self._priming = pod_scenario in _STUCK_PRIME_SCENARIOS
        self._last_prime: Optional[datetime] = None
        self._last_low_water: Optional[datetime] = None
        self._external_overrides = 0
        self._external_active = False
        self._frozen_frame: Optional[SensorFrame] = None
        self._frozen_bed_temp: Optional[float] = None
        self._frozen_device_level: Optional[int] = None
        self._frozen_target_level: Optional[int] = None
        self._frozen_since_tick: Optional[int] = None

    async def connect(self) -> None:
        return None

    async def update(self, user: bool = True, device: bool = True) -> None:
        return None

    def read_frame(self) -> SensorFrame:
        if not self.source.exhausted:
            frame = self.source.read_frame()
        else:
            # Night over: report the user out of bed so the daemon closes out the night.
            self._extra += 1
            frame = SensorFrame(
                timestamp=self.now(),
                stage=SleepStage.AWAKE,
                presence=False,
                bed_temp_f=70.0,
                room_temp_f=68.0,
                data_age_seconds=30.0,
            )
        if self.pod_scenario is not None:
            frame = self._apply_pod_model(frame)
        return frame

    def _apply_pod_model(self, frame: SensorFrame) -> SensorFrame:
        """Overlay the realistic plate/bed-temp dynamics + the selected fault onto ``frame``.

        Only reached when ``pod_scenario`` was set; the default path never calls this.
        """
        tick = self._tick
        self._tick += 1
        params = _POD_THERMAL_PARAMS[self.pod_scenario]

        # -- competing_controller: an external actor periodically resets the device's OWN
        # target register back toward its schedule, regardless of what we last commanded.
        self._external_active = False
        if (self.pod_scenario == "competing_controller" and tick > 0
                and tick % self.competing_period_ticks == 0):
            self._plate_target = float(self.competing_target_level)
            self._external_overrides += 1
            self._external_active = True

        # -- stuck_prime / air_bound: "a prime never completes" -> the element can't move.
        ramp = 0.0 if self.pod_scenario in _STUCK_PRIME_SCENARIOS else params["ramp"]
        self._plate_level = thermal_sim.step_plate_level(
            self._plate_level, self._plate_target, 1.0, ramp)
        self._bed_temp_f = thermal_sim.step_bed_temp(
            self._bed_temp_f, self._plate_level, self.ambient_f, 1.0, params["capacity"])

        device_level = int(round(self._plate_level))
        target_level = int(round(self._plate_target))
        bed_temp_f = self._bed_temp_f
        age = frame.data_age_seconds

        # -- frozen_telemetry: device readings stop changing across ticks despite commands
        # (a wedged/crash-looped daemon replaying its last-known-good snapshot). Its reported
        # age keeps growing, since the underlying poll never actually refreshes.
        if self.pod_scenario == "frozen_telemetry":
            if self._frozen_frame is None and tick >= self.freeze_after_ticks:
                self._frozen_frame = frame
                self._frozen_bed_temp = bed_temp_f
                self._frozen_device_level = device_level
                self._frozen_target_level = target_level
                self._frozen_since_tick = tick
            if self._frozen_frame is not None:
                frame = self._frozen_frame
                bed_temp_f = self._frozen_bed_temp
                device_level = self._frozen_device_level
                target_level = self._frozen_target_level
                age = (tick - self._frozen_since_tick) * 60.0 + 30.0

        # -- rate_limited: occasional reads come back stale/None (a cloud RequestError) --
        if (self.pod_scenario == "rate_limited" and tick > 0
                and tick % self.rate_limited_every == 0):
            age = None
            frame = replace(frame, heart_rate=None, hrv=None)

        return replace(
            frame,
            bed_temp_f=bed_temp_f,
            commanded_level=(self._our_last_commanded if self._our_last_commanded is not None
                              else frame.commanded_level),
            device_level=device_level,
            target_level=target_level,
            data_age_seconds=age,
        )

    def now(self) -> datetime:
        base = self.source.now()
        if self.source.exhausted and self._extra:
            return base + self._timedelta(minutes=self._extra)
        return base

    def device_status(self) -> dict:
        # Simulator is always "healthy" — flagged so the UI can label it as simulated. The
        # extra fields below are always present (neutral defaults when no pod_scenario is
        # active) so diagnostics detectors can rely on the richer shape unconditionally.
        priming = bool(self._priming) if self.pod_scenario is not None else False
        target_level = int(round(self._plate_target)) if self.pod_scenario is not None else \
            self.actuator.get_current_level()
        device_level = int(round(self._plate_level)) if self.pod_scenario is not None else \
            self.actuator.get_current_level()
        return {
            "online": True,
            "has_water": True,
            "priming": priming,
            "needs_priming": priming,
            "temp_available": True,
            "simulated": True,
            "pod_scenario": self.pod_scenario,
            "last_prime": self._last_prime.isoformat() if self._last_prime else None,
            "last_low_water": self._last_low_water.isoformat() if self._last_low_water else None,
            "device_level": device_level,
            "device_target_level": target_level,
            "now_heating": target_level > 0,
            "now_cooling": target_level < 0,
            "external_schedule": {
                "active": self._external_active,
                "resets_to": (self.competing_target_level
                              if self.pod_scenario == "competing_controller" else None),
                "override_count": self._external_overrides,
            },
        }

    @property
    def finished(self) -> bool:
        return self.source.exhausted and self._extra >= self._trailing

    async def set_heating_level(self, level: int, duration_s: int = 0) -> None:
        self.last_level = level
        self.level_set_count += 1
        self.actuator.set_level(level, duration_s)
        self._our_last_commanded = level
        if self.pod_scenario is not None:
            self._plate_target = float(level)

    async def set_wake_alarm(self, spec) -> None:
        self.last_alarm = spec
        self.actuator.set_alarm(spec.time, spec.vibration_power, spec.thermal_level)

    # ---- Eight Sleep app-parity controls (no-ops over the simulator) -------------
    async def turn_on_side(self) -> None:
        self.powered = True
        self.on_count += 1

    async def turn_off_side(self) -> None:
        self.powered = False
        self.off_count += 1
        self.actuator.set_level(0)

    async def set_away_mode(self, enabled: bool) -> None:
        self.away = bool(enabled)
        if enabled:
            self.actuator.set_level(0)

    async def prime_pod(self) -> None:
        self.prime_count += 1
        if self.pod_scenario in _STUCK_PRIME_SCENARIOS:
            # A prime that can't finish: priming stays True, last_prime never advances.
            self._priming = True
            return
        self._priming = False
        self._last_prime = self.now()

    async def increment_level(self, offset: int) -> None:
        self._level = int(getattr(self, "_level", 0) + offset)
        self.actuator.set_level(self._level)

    async def fetch_night_summary(self, date: str) -> NightSummary:
        return self.source.fetch_night_summary(date)

    async def close(self) -> None:
        return None
