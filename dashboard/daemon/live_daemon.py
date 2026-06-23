"""Async dashboard control daemon for the REAL Eight Sleep Pod (or the offline simulator).

This is the live counterpart of the synchronous, simulator-only ``DashboardDaemon``. It is
client-agnostic: it drives either the real async ``EightSleepClient`` (pyEight) or the
``SimulatedLiveClient`` (offline testing), bridging the async device I/O to the sync
``ControlCycle``. It owns the device, applies the dashboard's command queue to it, and writes
the ``runtime_state`` snapshot the API/SSE reads — so the iPhone app controls and observes the
actual bed.

Safety: ``dry_run=True`` makes it read-only (decisions logged, **zero** device writes). The
controller's slew / variability / 55–110 °F clamps still bound every command, and Emergency
Stop (the ``stop`` command) hard-offs the side via ``turn_off_side()``.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Optional

from sleepctl.config import AppConfig
from sleepctl.controller.controller import SleepController
from sleepctl.loop.cycle import ControlCycle
from sleepctl.loop.nightly import NightlyUpdater
from sleepctl.models import ContextRecord, ControllerState

from app import bridge

TEMP_MIN_F, TEMP_MAX_F = 55.0, 110.0


class LiveDashboardDaemon:
    def __init__(self, cfg: AppConfig, client, repo, dry_run: bool = False,
                 verbose: bool = True) -> None:
        self.cfg = cfg
        self.client = client
        self.repo = repo
        self.dry_run = dry_run
        self.verbose = verbose
        controller = SleepController(cfg, setpoints=repo.latest_setpoints())
        self._attach_profiles(controller)
        self.cycle = ControlCycle(cfg, repo, controller)
        self.nightly = NightlyUpdater(cfg, repo)
        self.context = ContextRecord(date=datetime.now().date().isoformat())
        # control state (mirrors the simulator daemon)
        self.mode = "auto"
        self.paused = False
        self.power_on = True
        self.away = False
        self.manual_target_f: Optional[float] = None
        self.last_target_f: Optional[float] = None
        self.wake = None
        self._prev_state = ControllerState.IDLE
        self._saw_sleep = False

    # ------------------------------------------------------------------ helpers
    def _log(self, msg: str) -> None:
        if self.verbose:
            print(msg, flush=True)

    @staticmethod
    def _clamp_temp(f) -> float:
        return max(TEMP_MIN_F, min(TEMP_MAX_F, float(f)))

    def _attach_profiles(self, controller: SleepController) -> None:
        try:
            from sleepctl.learning.lead_time import build_lead_time_profile
            from sleepctl.ml.wake_profile import build_wake_profile
            controller.set_wake_profile(build_wake_profile(self.repo),
                                        lead_profile=build_lead_time_profile(self.repo))
        except Exception as exc:
            self._log(f"profile load skipped: {exc}")

    def _apply_night_type(self, hint: str) -> None:
        try:
            from sleepctl.benchmarks import NightMode
            from sleepctl.controller.sleep_plan import plan_night
            plan = plan_night(datetime.now(), self.context.required_wake_time,
                              self.repo.recent_nights(14), hint=hint)
            self.context.night_type = plan.mode.value
            self.context.is_short_sleep_day = plan.mode == NightMode.CONSTRAINED
            self.context.sleep_opportunity_min = plan.sleep_opportunity_min
        except Exception as exc:
            self._log(f"night-type planning skipped: {exc}")

    # ------------------------------------------------------------------ device
    async def _set_level(self, level: int) -> None:
        if not self.dry_run:
            await self.client.set_heating_level(level)

    async def _apply_commands(self) -> bool:
        """Drain the dashboard command queue, applying each to the REAL device. Returns
        True if any device-affecting change occurred."""
        changed = False
        while True:
            cmd = bridge.next_pending_command(self.repo.conn)
            if cmd is None:
                break
            t, p = cmd["type"], cmd["payload"]
            changed = True
            try:
                if t in ("stop", "power_off"):
                    self.power_on = False
                    self.paused = True
                    if not self.dry_run:
                        await self.client.turn_off_side()
                elif t == "pause":
                    self.paused = True
                elif t in ("start", "resume"):
                    self.paused = False
                elif t == "power_on":
                    self.power_on, self.paused, self.away = True, False, False
                    if not self.dry_run:
                        await self.client.turn_on_side()
                elif t == "away_on":
                    self.away, self.power_on = True, False
                    if not self.dry_run:
                        await self.client.set_away_mode(True)
                elif t == "away_off":
                    self.away, self.power_on = False, True
                    if not self.dry_run:
                        await self.client.set_away_mode(False)
                        await self.client.turn_on_side()
                elif t == "prime":
                    if not self.dry_run:
                        await self.client.prime_pod()
                elif t == "safe_default":
                    self.paused, self.power_on, self.away = False, True, False
                    self.manual_target_f, self.mode = None, "auto"
                    self.repo.save_setpoints(self.cfg.default_setpoints())
                elif t == "set_mode":
                    self.mode = p.get("mode", "auto")
                elif t == "set_temp":
                    self.manual_target_f = self._clamp_temp(p.get("target_f"))
                    self.mode, self.power_on, self.paused = "manual", True, False
                    await self._set_level(self.cycle.controller.thermal.to_level(self.manual_target_f))
                elif t == "nudge_temp":
                    base = self.manual_target_f if self.manual_target_f is not None \
                        else (self.last_target_f if self.last_target_f is not None else 70.0)
                    self.manual_target_f = self._clamp_temp(base + float(p.get("delta_f", 0)))
                    self.mode, self.power_on, self.paused = "manual", True, False
                    await self._set_level(self.cycle.controller.thermal.to_level(self.manual_target_f))
                elif t == "set_wake":
                    self.wake = {
                        "wake_time": p.get("wake_time"),
                        "window_min": p.get("window_min") or self.cfg.tunables.wake_window_min,
                        "vibration_power": p.get("vibration_power")
                        if p.get("vibration_power") is not None
                        else self.cfg.tunables.wake_vibration_power,
                        "thermal_level": p.get("thermal_level"),
                        "night_type": p.get("night_type") or "auto",
                    }
                    hh, mm = (int(x) for x in p["wake_time"].split(":"))
                    wk = datetime.now().replace(hour=hh, minute=mm, second=0, microsecond=0)
                    if wk <= datetime.now():
                        wk += timedelta(days=1)
                    self.context.required_wake_time = wk
                    self._apply_night_type(p.get("night_type") or "auto")
                elif t == "clear_wake":
                    self.wake = None
                    self.context.required_wake_time = None
                    self.context.night_type = None
                    self.context.is_short_sleep_day = None
            except Exception as exc:  # never let a device hiccup wedge the queue
                self._log(f"command {t} failed: {exc}")
            bridge.mark_applied(self.repo.conn, cmd["id"])
        return changed

    # ------------------------------------------------------------------ snapshot
    def _snapshot(self, decision, frame) -> dict:
        target = decision.target_temp_f if decision else None
        if self.mode == "manual" and self.manual_target_f is not None:
            target = self.manual_target_f
        if target is not None:
            self.last_target_f = target
        mode = "away" if self.away else ("paused" if self.paused else self.mode)
        return {
            "state": "OFF" if not self.power_on else (decision.state.value if decision else "IDLE"),
            "objective": decision.objective.value if decision else None,
            "mode": mode,
            "target_temp_f": target if self.power_on else None,
            "bed_temp_f": frame.bed_temp_f if frame else None,
            "room_temp_f": frame.room_temp_f if frame else None,
            "stage": frame.stage.value if frame else None,
            "confidence": decision.confidence if decision else None,
            "target_level": decision.target_level if decision else None,
            "daemon_alive": True,
            "extra": {"manual_target_f": self.manual_target_f, "power_on": self.power_on,
                      "away": self.away, "wake": self.wake, "live": True,
                      "dry_run": self.dry_run},
        }

    # ------------------------------------------------------------------ cycles
    async def control_tick(self) -> None:
        await self._apply_commands()
        await self.client.update()
        frame = self.client.read_frame()
        now = self.client.now()
        decision = None
        if self.power_on and not self.paused and not self.away:
            decision = self.cycle.decide(frame, self.context, now)
            if self.mode == "manual" and self.manual_target_f is not None:
                await self._set_level(self.cycle.controller.thermal.to_level(self.manual_target_f))
            elif self.mode == "auto":
                level = self.cycle.pending_level(decision, frame, now)
                if level is not None:
                    await self._set_level(level)
                alarm = self.cycle.pending_alarm()
                if alarm is not None and not self.dry_run:
                    await self.client.set_wake_alarm(alarm)
            self.cycle.log(frame, decision, now)
            await self._maybe_close_out(decision, now)
            self._prev_state = decision.state
        bridge.write_runtime_state(self.repo.conn, self._snapshot(decision, frame))

    async def command_tick(self) -> None:
        """Fast path for realtime control: apply queued overrides and snapshot now."""
        if not await self._apply_commands():
            return
        await self.client.update()
        frame = self.client.read_frame()
        now = self.client.now()
        decision = None
        if self.power_on and not self.paused and not self.away:
            decision = self.cycle.decide(frame, self.context, now)
            if self.mode == "manual" and self.manual_target_f is not None:
                await self._set_level(self.cycle.controller.thermal.to_level(self.manual_target_f))
        bridge.write_runtime_state(self.repo.conn, self._snapshot(decision, frame))

    async def _maybe_close_out(self, decision, now) -> None:
        if decision.state in (ControllerState.MAINTENANCE, ControllerState.WAKE_RECOVERY,
                              ControllerState.WAKE_WINDOW):
            self._saw_sleep = True
        left_bed = (decision.state is ControllerState.IDLE
                    and self._prev_state is not ControllerState.IDLE)
        if left_bed and self._saw_sleep:
            night_date = self.cycle.night_date(now)
            self.context.date = night_date
            self.repo.save_context(self.context)
            try:
                night = await self.client.fetch_night_summary(night_date)
                self.nightly.run(night)
            except Exception as exc:
                self._log(f"nightly close-out skipped: {exc}")
            self._attach_profiles(self.cycle.controller)  # learn from the night just ended
            self._saw_sleep = False

    async def run(self, poll_seconds: float = 60.0, command_poll_seconds: float = 2.0,
                  dry_run: Optional[bool] = None, max_ticks: Optional[int] = None,
                  shutdown_event: Optional[asyncio.Event] = None) -> None:
        if dry_run is not None:
            self.dry_run = dry_run
        await self.client.connect()
        self._log(f"sleepctl dashboard LIVE daemon started (dry_run={self.dry_run})."
                  + ("  [READ-ONLY: no device commands]" if self.dry_run else ""))
        ticks = 0
        last_control = 0.0
        try:
            while True:
                loop_now = asyncio.get_event_loop().time()
                if loop_now - last_control >= poll_seconds:
                    await self.control_tick()
                    last_control = loop_now
                    ticks += 1
                else:
                    await self.command_tick()
                if max_ticks is not None and ticks >= max_ticks:
                    break
                if shutdown_event is not None and shutdown_event.is_set():
                    break
                await asyncio.sleep(command_poll_seconds)
        finally:
            await self.client.close()
            self._log("sleepctl dashboard LIVE daemon stopped; device client closed.")
