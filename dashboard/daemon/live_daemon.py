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
from sleepctl.controller.thermal_health import ThermalResponseMonitor
from sleepctl.loop.cycle import ControlCycle
from sleepctl.precompensation import compute_precompensation
from sleepctl.loop.nightly import NightlyUpdater
from sleepctl.models import ContextRecord, ControllerState

from app import bridge

TEMP_MIN_F, TEMP_MAX_F = 55.0, 110.0


class LiveDashboardDaemon:
    def __init__(self, cfg: AppConfig, client, repo, dry_run: bool = False,
                 verbose: bool = True, weather=None, wearable=None) -> None:
        self.cfg = cfg
        self.client = client
        self.repo = repo
        self.dry_run = dry_run
        self.verbose = verbose
        # Optional separate fast sensor (BLE strap / bedside radar). When present, its sub-minute
        # HR/movement is fused onto every Pod frame (zero device risk; controller unchanged).
        self.wearable = wearable
        self.shift_plan = None  # advisory cross-shift sleep plan, refreshed on the control tick
        # Optional WeatherSource for environmental pre-compensation (None -> feature off,
        # keeps the simulator/test path network-free).
        self.weather = weather
        self.precomp = compute_precompensation(None, cfg)
        self._precomp_checked = 0.0
        controller = SleepController(cfg, setpoints=repo.latest_setpoints())
        self._attach_profiles(controller)
        self.cycle = ControlCycle(cfg, repo, controller)
        self.nightly = NightlyUpdater(cfg, repo)
        # Confirms the bed is actually heating/cooling from the Hub's water-side device level
        # (not the cover-side bed temp, which can be an ambient artifact).
        self.thermal = ThermalResponseMonitor(cfg)
        self._thermal_state = "unknown"
        self.context = ContextRecord(date=datetime.now().date().isoformat())
        # control state (mirrors the simulator daemon)
        self.mode = "auto"
        self.paused = False
        self.power_on = True
        self.away = False
        self.manual_target_f: Optional[float] = None
        self.last_target_f: Optional[float] = None
        self.wake = None
        self.session_mode = "night"
        self.nap_plan = None
        self.nap_deadline = None
        self._prev_state = ControllerState.IDLE
        self._saw_sleep = False
        self._consec_errors = 0
        self._last_decision = None  # reused by the fast telemetry tick between control ticks
        self.active_experiment = None  # tonight's applied n-of-1 arm, if any
        self._phone_fused = False  # was the phone sample fused on the last frame (presence-gated)
        self.hue_driver = None     # Philips Hue dawn-light driver (best-effort)
        self._pending_wake = None  # captured wake conditions, flushed to wake_log at close-out
        self._wake_last_stage = None
        self._wake_base_window = cfg.tunables.wake_window_min  # learned per-user window base
        self._wake_thermal_f = cfg.tunables.wake_ramp_temp_f   # tonight's wake-ramp temperature

    # ------------------------------------------------------ onset / nap sessions
    def _start_induce(self) -> None:
        self.session_mode = "induce"
        self.mode, self.power_on, self.paused, self.away = "auto", True, False, False
        self.nap_plan, self.nap_deadline = None, None
        self.cycle.controller.set_session("induce", keep_light=False)

    def _start_nap(self, duration_min=None, wake_time=None) -> None:
        from sleepctl.controller.nap import NapStrategy, nap_strategy
        now = datetime.now()
        if wake_time:
            hh, mm = (int(x) for x in str(wake_time).split(":"))
            deadline = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
            if deadline <= now:
                deadline += timedelta(days=1)
        else:
            deadline = now + timedelta(minutes=int(duration_min or 20))
        window = max(5, int((deadline - now).total_seconds() // 60))
        plan = nap_strategy(window, now_hour=now.hour, cfg=self.cfg)
        ctrl_mode = "nap_power" if plan.strategy in (NapStrategy.POWER, NapStrategy.TRAP) \
            else "nap_cycle"
        self.session_mode = "nap"
        self.mode, self.power_on, self.paused, self.away = "auto", True, False, False
        self.nap_plan, self.nap_deadline = plan.to_dict(), deadline
        self.context.required_wake_time = deadline
        self.cycle.controller.set_session(ctrl_mode, keep_light=plan.keep_light)

    def _end_session(self) -> None:
        self.session_mode = "night"
        self.nap_plan, self.nap_deadline = None, None
        self.context.required_wake_time = None
        self.cycle.controller.set_session("night", keep_light=False)

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
            from sleepctl.learning.settle import learn_settle_nudge
            from sleepctl.ml.wake_profile import build_wake_profile
            controller.set_wake_profile(build_wake_profile(self.repo),
                                        lead_profile=build_lead_time_profile(self.repo))
            controller.set_settle_nudge(learn_settle_nudge(self.repo, self.cfg))
            from sleepctl.benchmarks import sleep_debt_min
            controller.wake_debt_min = sleep_debt_min(self.repo.recent_nights(14))
            self._flush_wake_log()        # persist last night's wake conditions
            # Personalize the alarm to YOUR grogginess curve (window + lift bar).
            from sleepctl.learning.wake_tuning import learn_wake_tuning, wake_tuning_records
            tuning = learn_wake_tuning(wake_tuning_records(self.repo),
                                       base_window=self.cfg.tunables.wake_window_min)
            controller.wake_orch.cfg.p_wake_liftable = tuning.p_wake_liftable
            self._wake_base_window = tuning.window_min
        except Exception as exc:
            self._log(f"profile load skipped: {exc}")
        # Apply tonight's active experiment arm on top of the learned setpoint (closes the
        # n-of-1 loop: the assigned arm now actually drives the controller).
        try:
            from dataclasses import replace

            from sleepctl.experiments import apply_experiment_arm
            from sleepctl.learning.thermal_wake import (
                learn_thermal_wake, next_wake_f, thermal_wake_records)
            base = self.repo.latest_setpoints() or self.cfg.default_setpoints()
            # Learn the per-person THERMAL wake maneuver (warm vs cool, magnitude) from grogginess,
            # with active exploration so the curve gets sampled. Sets tonight's wake-ramp temp.
            tw = learn_thermal_wake(thermal_wake_records(self.repo),
                                    base_f=self.cfg.tunables.wake_ramp_temp_f)
            self._wake_thermal_f = next_wake_f(tw.wake_f, datetime.now().timetuple().tm_yday)
            base = replace(base, wake_ramp_f=self._wake_thermal_f)
            prof, arm = apply_experiment_arm(self.repo, datetime.now().date().isoformat(), base)
            controller.set_setpoints(prof)
            self.active_experiment = arm
            if arm and arm.get("applied"):
                self._log(f"experiment '{arm.get('name')}' arm {arm.get('arm')} applied tonight")
        except Exception as exc:
            self._log(f"experiment-arm apply skipped: {exc}")

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
                if t == "stop":
                    # EMERGENCY STOP is a safety override: hard-off the side ALWAYS, even in
                    # dry-run. A silent no-op emergency stop is exactly what you don't want.
                    self.power_on = False
                    self.paused = True
                    try:
                        await self.client.turn_off_side()
                        self._log("EMERGENCY STOP: side turned off")
                    except Exception as exc:
                        self._log(f"EMERGENCY STOP turn_off_side failed: {exc}")
                elif t == "power_off":
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
                    # Gym advisor wires into the alarm: a GO call moves the deadline earlier.
                    normal_wk = wk
                    try:
                        from app import services
                        wk = services.gym_effective_wake(self.repo, wk)
                    except Exception as exc:
                        self._log(f"gym wake adjust skipped: {exc}")
                    self.context.required_wake_time = wk
                    self._apply_night_type(p.get("night_type") or "auto")
                    # Choose an appropriate smart-wake window for THIS night and feed it to the
                    # orchestrator (wide when rested, narrow when sleep is scarce).
                    try:
                        from sleepctl.controller.wake_orchestrator import choose_wake_window
                        explicit = p.get("window_min")
                        if explicit and int(explicit) > 0:   # user override from the picker
                            win = int(explicit)
                        else:                                  # Auto: choose for this night
                            win = choose_wake_window(self.context.night_type,
                                                     self.cycle.controller.wake_debt_min,
                                                     gym_go=wk < normal_wk,
                                                     base=self._wake_base_window)
                        self.cycle.controller.set_wake_window(win)
                        self.wake["window_min"] = win
                    except Exception as exc:
                        self._log(f"wake window selection skipped: {exc}")
                elif t == "clear_wake":
                    self.wake = None
                    self.context.required_wake_time = None
                    self.context.night_type = None
                    self.context.is_short_sleep_day = None
                elif t == "induce_sleep":
                    self._start_induce()
                elif t == "start_nap":
                    self._start_nap(p.get("duration_min"), p.get("wake_time"))
                elif t == "end_session":
                    self._end_session()
            except Exception as exc:  # never let a device hiccup wedge the queue
                self._log(f"command {t} failed: {exc}")
            bridge.mark_applied(self.repo.conn, cmd["id"])
        return changed

    def _refresh_precomp(self, now) -> None:
        """Refresh the forecast-driven feed-forward bias (~every 30 min). No-op without a
        weather source. The bias is applied to the thermal controller and surfaced."""
        if self.weather is None:
            return
        loop_now = asyncio.get_event_loop().time()
        if self.precomp.get("trend") is not None and (loop_now - self._precomp_checked) < 1800:
            return
        self._precomp_checked = loop_now
        try:
            fc = self.weather.overnight_forecast(from_dt=now)
            self.precomp = compute_precompensation(fc, self.cfg)
            self.cycle.controller.thermal.set_ambient_bias(self.precomp.get("bias_f", 0.0))
        except Exception as exc:
            self._log(f"precompensation refresh skipped: {exc}")

    def _read_frame(self):
        """Read the Pod frame and fuse a fresh wearable sample over it (if a wearable is
        attached) — sub-minute HR/movement onto the ~60s Pod data, controller-transparent.

        Presence-gated: the phone is only fused while the Pod senses you in bed. The moment
        bed presence drops (you got up), the phone feed is ignored — so it auto-engages on
        bed-in and disengages on bed-out with no phone-side action. (Unknown presence still
        fuses, so we never lose data to a missing reading.)"""
        frame = self.client.read_frame()
        self._phone_fused = False
        if self.wearable is not None and frame.presence is not False:
            try:
                from sleepctl.adapters.wearable import fuse_sample
                self._phone_fused = fuse_sample(frame, self.wearable.read_sample())
            except Exception as exc:
                self._log(f"wearable fusion skipped: {exc}")
        return frame

    def _refresh_shift_plan(self) -> None:
        """Advisory cross-shift sleep-debt plan (calendar-fed shifts are a follow-up; debt +
        strategy come from recent nights now)."""
        try:
            from sleepctl.shift_manager import plan_shift_sleep
            self.shift_plan = plan_shift_sleep(self.repo.recent_nights(14), [],
                                               datetime.now()).to_dict()
        except Exception as exc:
            self._log(f"shift plan skipped: {exc}")

    def _safe_device_status(self) -> dict:
        fn = getattr(self.client, "device_status", None)
        try:
            return fn() if fn else {}
        except Exception:
            return {}

    def _record_thermal(self, frame, now) -> None:
        """Track the Hub's water-side device level vs target; warn when it stalls."""
        self.thermal.record(now, frame.target_level, frame.device_level)
        th = self.thermal.status(now)
        if th.state != self._thermal_state:
            if th.state == "stalled":
                self._log(f"⚠ thermal: {th.reason}")
            self._thermal_state = th.state

    # ------------------------------------------------------------------ snapshot
    def _snapshot(self, decision, frame, error: Optional[str] = None) -> dict:
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
                      "dry_run": self.dry_run, "session_mode": self.session_mode,
                      "nap": self.nap_plan,
                      "nap_deadline": self.nap_deadline.isoformat() if self.nap_deadline else None,
                      "thermal_health": self.thermal.status().to_dict(),
                      "preemption": self.cycle.controller.preemption_summary(),
                      "precompensation": self.precomp,
                      "device": self._safe_device_status(),
                      "experiment": self.active_experiment,
                      "shift_plan": self.shift_plan,
                      "device_error": error,
                      "data_age_s": round(frame.data_age_seconds, 1)
                      if frame is not None and frame.data_age_seconds is not None else None,
                      "telemetry_stale": bool(
                          frame is not None and frame.data_age_seconds is not None
                          and frame.data_age_seconds > self.cfg.tunables.telemetry_stale_seconds),
                      # Bed presence drives the phone supplement: in_bed -> the phone feed is
                      # fused; out of bed -> it's ignored automatically.
                      "bed_presence": frame.presence if frame is not None else None,
                      "phone_fused": self._phone_fused,
                      "wake_action": (decision.log_payload or {}).get("wake_action")
                      if decision else None},
        }

    def _refresh_hue(self) -> None:
        """(Re)build the Hue dawn driver from the stored config; toggle the orchestrator's light
        ramp accordingly. Rebuilds only when the config changes."""
        try:
            from app import services
            c = services._get_hue_config(self.repo)
            sig = (c["enabled"], c["bridge_ip"], c["token"], tuple(c["target_ids"]), c["kind"])
            if sig == getattr(self, "_hue_sig", None):
                return
            self._hue_sig = sig
            ready = c["enabled"] and c["bridge_ip"] and c["token"] and c["target_ids"]
            if ready:
                from sleepctl.adapters.hue import HueDawnDriver
                self.hue_driver = HueDawnDriver(c["bridge_ip"], c["token"], c["target_ids"], c["kind"])
            else:
                self.hue_driver = None
            self.cycle.controller.wake_orch.cfg.light_enabled = bool(ready)
        except Exception as exc:
            self._log(f"hue refresh skipped: {exc}")

    def _drive_dawn(self, decision) -> None:
        if not self.hue_driver or decision is None:
            return
        la = (decision.log_payload or {}).get("wake_action")
        if la is not None:
            try:
                self.hue_driver.set_level(float(la.get("light_level", 0.0)))
            except Exception as exc:
                self._log(f"hue drive skipped: {exc}")

    def _capture_wake(self, decision, frame, now) -> None:
        """Record how the user was woken (stage, how early, forced) for the grogginess learner."""
        if decision is None or frame is None:
            return
        la = (decision.log_payload or {}).get("wake_action")
        if not la:
            return
        st = frame.stage.value if getattr(frame, "stage", None) else None
        if st and st.lower() not in ("awake", "unknown"):
            self._wake_last_stage = st
        if la.get("phase") == "done" and self._pending_wake is None:
            mins_early, forced = None, False
            dl = la.get("target_time")
            if dl:
                try:
                    deadline = datetime.fromisoformat(dl)
                    mins_early = max(0.0, (deadline - now).total_seconds() / 60.0)
                    forced = now >= deadline
                except Exception:
                    pass
            if (self._wake_last_stage or "").lower() == "deep":
                forced = True
            self._pending_wake = {
                "woke_from_stage": self._wake_last_stage,
                "minutes_early": round(mins_early, 1) if mins_early is not None else None,
                "window_min": (self.wake or {}).get("window_min"),
                "forced": forced, "p_wake": la.get("p_wake"),
                "wake_thermal_f": self._wake_thermal_f}

    def _flush_wake_log(self) -> None:
        if not self._pending_wake:
            return
        try:
            nights = self.repo.recent_nights(1)
            date = nights[-1].date if nights else datetime.now().date().isoformat()
            bridge.write_wake_log(self.repo.conn, {"date": date, **self._pending_wake})
        except Exception as exc:
            self._log(f"wake log skipped: {exc}")
        finally:
            self._pending_wake, self._wake_last_stage = None, None

    # ------------------------------------------------------------------ cycles
    async def control_tick(self) -> None:
        await self._apply_commands()
        self._refresh_hue()
        if self.nap_deadline is not None and datetime.now() >= self.nap_deadline:
            self._end_session()
        await self.client.update()
        frame = self._read_frame()
        now = self.client.now()
        self._record_thermal(frame, now)
        self._refresh_precomp(now)
        self._refresh_shift_plan()
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
            self._capture_wake(decision, frame, now)
            await self._maybe_close_out(decision, now)
            self._prev_state = decision.state
        self._last_decision = decision
        self._drive_dawn(decision)        # push the dawn light level to Hue (best-effort)
        bridge.write_runtime_state(self.repo.conn, self._snapshot(decision, frame))

    async def command_tick(self) -> bool:
        """Fast path for realtime control: apply queued overrides and snapshot now.
        Returns True if a command was applied (the loop then resets its telemetry timer)."""
        if not await self._apply_commands():
            return False
        await self.client.update()
        frame = self._read_frame()
        now = self.client.now()
        self._record_thermal(frame, now)
        decision = None
        if self.power_on and not self.paused and not self.away:
            decision = self.cycle.decide(frame, self.context, now)
            if self.mode == "manual" and self.manual_target_f is not None:
                await self._set_level(self.cycle.controller.thermal.to_level(self.manual_target_f))
        self._last_decision = decision
        bridge.write_runtime_state(self.repo.conn, self._snapshot(decision, frame))
        return True

    async def telemetry_tick(self) -> None:
        """Fast, read-only telemetry refresh decoupled from control decisions: pulls fresh
        user data (HR/HRV/stage/level — the cloud's ~30s floor) WITHOUT the heavier device
        poll or any actuation, and re-publishes the snapshot reusing the last control
        decision. Keeps the dashboard's sensor data under ``live_telemetry_seconds`` old
        without changing control cadence or sending any device command."""
        await self.client.update(device=False)
        frame = self._read_frame()
        now = self.client.now()
        self._record_thermal(frame, now)
        bridge.write_runtime_state(self.repo.conn, self._snapshot(self._last_decision, frame))

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
                  telemetry_seconds: Optional[float] = None,
                  dry_run: Optional[bool] = None, max_ticks: Optional[int] = None,
                  shutdown_event: Optional[asyncio.Event] = None) -> None:
        if dry_run is not None:
            self.dry_run = dry_run
        if telemetry_seconds is None:
            telemetry_seconds = self.cfg.tunables.live_telemetry_seconds
        await self.client.connect()
        self._log(f"sleepctl dashboard LIVE daemon started (dry_run={self.dry_run}, "
                  f"control={poll_seconds:g}s, telemetry={telemetry_seconds:g}s)."
                  + ("  [READ-ONLY: no device commands]" if self.dry_run else ""))
        ticks = 0
        last_control = 0.0
        last_telem = 0.0
        try:
            while True:
                loop_now = asyncio.get_event_loop().time()
                due = loop_now - last_control >= poll_seconds
                telem_due = loop_now - last_telem >= telemetry_seconds
                try:
                    if due:
                        await self.control_tick()
                        ticks += 1
                        last_telem = loop_now
                    elif await self.command_tick():
                        last_telem = loop_now
                    elif telem_due:
                        # fast, decoupled telemetry refresh so the dashboard never shows
                        # sensor data older than telemetry_seconds
                        await self.telemetry_tick()
                        last_telem = loop_now
                    self._consec_errors = 0
                except Exception as exc:
                    # A transient device/cloud error (timeout, token refresh, 5xx) must NOT
                    # kill the 24/7 loop. Log, surface a degraded snapshot so the dashboard
                    # shows the problem, hold (the device keeps its last safe command), and
                    # back off so we don't hammer a failing API.
                    self._consec_errors += 1
                    self._log(f"tick error #{self._consec_errors}: {exc!r}; holding")
                    try:
                        bridge.write_runtime_state(
                            self.repo.conn, self._snapshot(None, None, error=repr(exc)))
                    except Exception:
                        pass
                    await asyncio.sleep(min(30.0, command_poll_seconds * min(self._consec_errors, 8)))
                finally:
                    if due:
                        last_control = loop_now
                if max_ticks is not None and ticks >= max_ticks:
                    break
                if shutdown_event is not None and shutdown_event.is_set():
                    break
                await asyncio.sleep(command_poll_seconds)
        finally:
            await self.client.close()
            self._log("sleepctl dashboard LIVE daemon stopped; device client closed.")
