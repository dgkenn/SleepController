"""Dashboard control daemon.

Owns the device. It (1) applies pending override commands from the API on a fast poll so
manual temperature changes feel realtime, (2) runs one sense->decide->act cycle via the
sleepctl ControlCycle on a slower cadence, and (3) writes a runtime_state snapshot the
API/SSE reads. Emergency stop / power / away / mode / manual-temp overrides are honored here
so the API never touches the device directly. Runs against the simulator by default (no Pod
needed) or the live Eight Sleep client.

Control surface (matches the official Eight Sleep app):
  power_on/power_off  - turn the side on/off
  away_on/away_off    - travel/away mode
  prime               - prime the Pod water
  set_temp/nudge_temp - manual temperature (absolute or +/- realtime adjust)
  set_mode            - auto | manual | view
  set_wake/clear_wake - smart wake alarm (time + window + vibration + thermal)
  start/pause/resume/stop/safe_default
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime, timedelta, timezone

# make sleepctl + dashboard.api importable when run as a script
sys.path.insert(0, "/app")
sys.path.insert(0, "/app/dashboard/api")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "api"))

from sleepctl.adapters.simulator import SimulatorActuator, SimulatorSource  # noqa: E402
from sleepctl.config import AppConfig  # noqa: E402
from sleepctl.controller.controller import SleepController  # noqa: E402
from sleepctl.loop.cycle import ControlCycle  # noqa: E402
from sleepctl.models import ContextRecord  # noqa: E402

from app import bridge  # noqa: E402
from app.db import get_repo  # noqa: E402

# How far a single +/- nudge moves the target, and the manual temp clamp (water °F).
NUDGE_STEP_DEFAULT_F = 1.0
TEMP_MIN_F, TEMP_MAX_F = 55.0, 110.0


class DashboardDaemon:
    def __init__(self, simulate: bool = True, poll_seconds: float = 30.0,
                 command_poll_seconds: float = 1.0) -> None:
        self.cfg = AppConfig.default()
        self.repo = get_repo()
        controller = SleepController(self.cfg, setpoints=self.repo.latest_setpoints())
        # Attach the learned awakening phenotype so proactive sleep-maintenance is personalised.
        try:
            from sleepctl.learning.lead_time import build_lead_time_profile
            from sleepctl.ml.wake_profile import build_wake_profile
            controller.set_wake_profile(
                build_wake_profile(self.repo),
                lead_profile=build_lead_time_profile(self.repo))
        except Exception as exc:
            print(f"wake-profile load skipped: {exc}", flush=True)
        self.cycle = ControlCycle(self.cfg, self.repo, controller)
        self.poll_seconds = poll_seconds
        self.command_poll_seconds = command_poll_seconds
        self.mode = "auto"
        self.paused = False
        self.power_on = True
        self.away = False
        self.wake = None  # {"wake_time","window_min","vibration_power","thermal_level"}
        self.manual_target_f = None
        self.last_target_f = None  # last effective target (for relative nudges)
        self.simulate = simulate
        if simulate:
            self.source = SimulatorSource("normal", seed=7,
                                          start=datetime.now() - timedelta(minutes=1))
            self.actuator = SimulatorActuator(self.source)
        self.context = ContextRecord(date=datetime.now().date().isoformat())

    # ---------------------------------------------------------------- commands
    def _clamp_temp(self, f: float) -> float:
        return max(TEMP_MIN_F, min(TEMP_MAX_F, float(f)))

    def _apply_night_type(self, hint: str) -> None:
        """Compute tonight's plan and push the night mode into the controller context so
        its objective (OPTIMIZE / DAMAGE_CONTROL / RECOVERY) follows the schedule."""
        try:
            from sleepctl.benchmarks import NightMode
            from sleepctl.controller.sleep_plan import plan_night
            recent = self.repo.recent_nights(14)
            plan = plan_night(datetime.now(), self.context.required_wake_time, recent,
                              hint=hint)
            self.context.night_type = plan.mode.value
            self.context.is_short_sleep_day = plan.mode == NightMode.CONSTRAINED
            self.context.sleep_opportunity_min = plan.sleep_opportunity_min
        except Exception as exc:
            print(f"night-type planning skipped: {exc}", flush=True)

    def _apply_commands(self) -> bool:
        """Apply all pending commands. Returns True if any device-affecting change occurred."""
        changed = False
        while True:
            cmd = bridge.next_pending_command(self.repo.conn)
            if cmd is None:
                break
            t, p = cmd["type"], cmd["payload"]
            changed = True
            if t == "stop":
                self.paused = True
                self.power_on = False
                if self.simulate:
                    self.actuator.set_level(0)
            elif t == "pause":
                self.paused = True
            elif t in ("start", "resume"):
                self.paused = False
            elif t == "power_off":
                self.power_on = False
                self.paused = True
                if self.simulate:
                    self.actuator.set_level(0)
            elif t == "power_on":
                self.power_on = True
                self.paused = False
                self.away = False
            elif t == "away_on":
                self.away = True
                self.power_on = False
                if self.simulate:
                    self.actuator.set_level(0)
            elif t == "away_off":
                self.away = False
                self.power_on = True
            elif t == "prime":
                # simulator: no-op (water priming is a device routine); live client primes.
                pass
            elif t == "safe_default":
                self.paused = False
                self.power_on = True
                self.away = False
                self.manual_target_f = None
                self.mode = "auto"
                self.repo.save_setpoints(self.cfg.default_setpoints())
            elif t == "set_mode":
                self.mode = p.get("mode", "auto")
            elif t == "set_temp":
                self.manual_target_f = self._clamp_temp(p.get("target_f"))
                self.mode = "manual"
                self.power_on = True
                self.paused = False
            elif t == "nudge_temp":
                base = self.manual_target_f if self.manual_target_f is not None \
                    else (self.last_target_f if self.last_target_f is not None else 70.0)
                self.manual_target_f = self._clamp_temp(base + float(p.get("delta_f", 0)))
                self.mode = "manual"
                self.power_on = True
                self.paused = False
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
                wake = datetime.now().replace(hour=hh, minute=mm, second=0, microsecond=0)
                if wake <= datetime.now():
                    wake += timedelta(days=1)
                self.context.required_wake_time = wake
                # Drive the controller objective from the night mode (work/recovery/auto).
                self._apply_night_type(p.get("night_type") or "auto")
            elif t == "clear_wake":
                self.wake = None
                self.context.required_wake_time = None
                self.context.night_type = None
                self.context.is_short_sleep_day = None
            bridge.mark_applied(self.repo.conn, cmd["id"])
        return changed

    # ---------------------------------------------------------------- snapshot
    def _snapshot(self, decision, frame) -> dict:
        target = decision.target_temp_f if decision else None
        if self.mode == "manual" and self.manual_target_f is not None:
            target = self.manual_target_f
        if target is not None:
            self.last_target_f = target
        mode = "paused" if self.paused else self.mode
        if self.away:
            mode = "away"
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
            "extra": {
                "manual_target_f": self.manual_target_f,
                "power_on": self.power_on,
                "away": self.away,
                "wake": self.wake,
            },
        }

    # ---------------------------------------------------------------- cycles
    def _read(self):
        frame = self.source.read_frame()
        now = self.source.now()
        return frame, now

    def control_tick(self) -> None:
        """Full sense->decide->act cycle plus a fresh snapshot."""
        self._apply_commands()
        frame, now = self._read()
        decision = None
        if self.power_on and not self.paused and not self.away:
            decision = self.cycle.decide(frame, self.context, now)
            if self.mode == "manual" and self.manual_target_f is not None:
                level = self.cycle.controller.thermal.to_level(self.manual_target_f)
                self.actuator.set_level(level)
            elif self.mode == "auto":
                level = self.cycle.pending_level(decision, frame, now)
                if level is not None:
                    self.actuator.set_level(level)
                alarm = self.cycle.pending_alarm()
                if alarm is not None:
                    self.actuator.set_alarm(alarm.time, alarm.vibration_power, alarm.thermal_level)
            self.cycle.log(frame, decision, now)
            # When a night ends (back to IDLE), resolve pre-cool efficacy and refresh the
            # learned wake + lead-time profiles so prevention improves night over night.
            if decision is not None and decision.state.value.lower() == "idle":
                self._refresh_profiles()
        bridge.write_runtime_state(self.repo.conn, self._snapshot(decision, frame))

    def _refresh_profiles(self) -> None:
        try:
            from sleepctl.learning.lead_time import build_lead_time_profile
            from sleepctl.ml.wake_profile import build_wake_profile
            self.cycle.controller.set_wake_profile(
                build_wake_profile(self.repo),
                lead_profile=build_lead_time_profile(self.repo))
        except Exception as exc:
            print(f"profile refresh skipped: {exc}", flush=True)

    def command_tick(self) -> None:
        """Fast path: apply queued overrides and, when one lands, actuate + snapshot now so
        manual temperature / power changes are reflected within ~1s (realtime feel)."""
        if not self._apply_commands():
            return
        frame, now = self._read()
        decision = None
        if self.power_on and not self.paused and not self.away:
            decision = self.cycle.decide(frame, self.context, now)
            if self.mode == "manual" and self.manual_target_f is not None:
                self.actuator.set_level(self.cycle.controller.thermal.to_level(self.manual_target_f))
        bridge.write_runtime_state(self.repo.conn, self._snapshot(decision, frame))

    # backward-compatible alias
    def tick(self) -> None:
        self.control_tick()

    def run(self, max_ticks=None) -> None:
        ticks = 0
        last_control = 0.0
        while True:
            now = time.monotonic()
            try:
                if now - last_control >= self.poll_seconds:
                    self.control_tick()
                    last_control = now
                    ticks += 1
                else:
                    self.command_tick()
            except Exception as exc:  # keep the daemon alive; surface via stale state
                print(f"daemon tick error: {exc}", flush=True)
            if max_ticks is not None and ticks >= max_ticks:
                break
            time.sleep(self.command_poll_seconds)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true", help="use the real Eight Sleep client")
    ap.add_argument("--poll-seconds", type=float, default=30.0,
                    help="control-cycle cadence (manual overrides apply faster)")
    ap.add_argument("--command-poll-seconds", type=float, default=1.0,
                    help="override-queue poll cadence (realtime temp control)")
    ap.add_argument("--max-ticks", type=int, default=None)
    args = ap.parse_args()
    DashboardDaemon(simulate=not args.live, poll_seconds=args.poll_seconds,
                    command_poll_seconds=args.command_poll_seconds).run(args.max_ticks)


if __name__ == "__main__":
    main()
