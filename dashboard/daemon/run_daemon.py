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
        self.session_mode = "night"   # night | induce | nap
        self.nap_plan = None          # NapPlan.to_dict() when a nap session is active
        self.nap_deadline = None      # datetime the nap should end
        self.simulate = simulate
        if simulate:
            self.source = SimulatorSource("normal", seed=7,
                                          start=datetime.now() - timedelta(minutes=1))
            self.actuator = SimulatorActuator(self.source)
        # Phone/independent-sensor fusion (same as the live daemon): the API writes the latest
        # iPhone-accelerometer sample to the bridge; overlay its sub-minute movement here too, so
        # the simulator path can demonstrate the phone feed end-to-end.
        self.wearable = None
        self._phone_fused = False
        self.hue_driver = None             # Philips Hue dawn-light driver (best-effort)
        self._pending_wake = None          # captured wake conditions, flushed to wake_log at close
        self._wake_last_stage = None
        self._wake_base_window = self.cfg.tunables.wake_window_min  # learned per-user window base
        self._wake_thermal_f = self.cfg.tunables.wake_ramp_temp_f   # tonight's wake-ramp temp
        if os.environ.get("SLEEPCTL_PHONE_SENSOR", "1") not in ("0", "false", "off"):
            try:
                from sleepctl.adapters.bcg import BridgeWearableSource
                self.wearable = BridgeWearableSource(self.repo)
            except Exception as exc:
                print(f"phone-sensor fusion disabled: {exc}", flush=True)
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
                # Gym advisor wires into the alarm: a GO call moves the deadline earlier.
                normal_wake = wake
                try:
                    from app import services
                    wake = services.gym_effective_wake(self.repo, wake)
                except Exception as exc:
                    print(f"gym wake adjust skipped: {exc}", flush=True)
                self.context.required_wake_time = wake
                # Drive the controller objective from the night mode (work/recovery/auto).
                self._apply_night_type(p.get("night_type") or "auto")
                # Choose an appropriate smart-wake window for this night (wide when rested,
                # narrow when sleep is scarce) and feed it to the orchestrator.
                try:
                    from sleepctl.controller.wake_orchestrator import choose_wake_window
                    explicit = p.get("window_min")
                    if explicit and int(explicit) > 0:       # user override from the picker
                        win = int(explicit)
                    else:                                      # Auto: choose for this night
                        win = choose_wake_window(self.context.night_type,
                                                 self.cycle.controller.wake_debt_min,
                                                 gym_go=wake < normal_wake,
                                                 base=self._wake_base_window)
                    self.cycle.controller.set_wake_window(win)
                    self.wake["window_min"] = win
                except Exception as exc:
                    print(f"wake window selection skipped: {exc}", flush=True)
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
            bridge.mark_applied(self.repo.conn, cmd["id"])
        return changed

    # ---------------------------------------------------------- onset / nap sessions
    def _start_induce(self) -> None:
        """'Make me tired': run the onset-induction (warm->cool) cascade now."""
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
        self.nap_plan = plan.to_dict()
        self.nap_deadline = deadline
        # wake-by the deadline; smart wake catches a light-sleep moment inside the window.
        self.context.required_wake_time = deadline
        self.cycle.controller.set_session(ctrl_mode, keep_light=plan.keep_light)

    def _end_session(self) -> None:
        self.session_mode = "night"
        self.nap_plan, self.nap_deadline = None, None
        self.context.required_wake_time = None
        self.cycle.controller.set_session("night", keep_light=False)

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
                "bed_presence": frame.presence if frame else None,
                "phone_fused": self._phone_fused,
                "wake_action": (decision.log_payload or {}).get("wake_action") if decision else None,
                "wake": self.wake,
                "session_mode": self.session_mode,
                "nap": self.nap_plan,
                "nap_deadline": self.nap_deadline.isoformat() if self.nap_deadline else None,
                # device health + the high-leverage feature state (simulator values here;
                # the live daemon supplies real device readings).
                "device": {"online": True, "has_water": True, "priming": False,
                           "needs_priming": False, "temp_available": True, "simulated": True},
                "thermal_health": {"state": "ok", "responding": True,
                                   "reason": "simulator", "device_level": None,
                                   "target_level": None, "gap": None},
                "preemption": self.cycle.controller.preemption_summary(),
            },
        }

    # ---------------------------------------------------------------- cycles
    def _read(self):
        frame = self.source.read_frame()
        now = self.source.now()
        # Presence-gated phone fusion: overlay the iPhone's sub-minute movement while in bed.
        self._phone_fused = False
        if self.wearable is not None and frame.presence is not False:
            try:
                from sleepctl.adapters.wearable import fuse_sample
                self._phone_fused = fuse_sample(frame, self.wearable.read_sample())
            except Exception as exc:
                print(f"wearable fusion skipped: {exc}", flush=True)
        return frame, now

    def control_tick(self) -> None:
        """Full sense->decide->act cycle plus a fresh snapshot."""
        self._apply_commands()
        self._refresh_hue()
        # A nap ends once its deadline has passed (the smart wake has fired by then).
        if self.nap_deadline is not None and datetime.now() >= self.nap_deadline:
            self._end_session()
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
            self._capture_wake(decision, frame, now)
            if decision is not None and decision.state.value.lower() == "idle":
                self._refresh_profiles()
        self._drive_dawn(decision)        # push the dawn light level to Hue (best-effort)
        bridge.write_runtime_state(self.repo.conn, self._snapshot(decision, frame))

    def _refresh_profiles(self) -> None:
        self._flush_wake_log()            # persist last night's wake conditions first
        try:
            from sleepctl.learning.lead_time import build_lead_time_profile
            from sleepctl.ml.wake_profile import build_wake_profile
            self.cycle.controller.set_wake_profile(
                build_wake_profile(self.repo),
                lead_profile=build_lead_time_profile(self.repo))
            from sleepctl.benchmarks import sleep_debt_min
            self.cycle.controller.wake_debt_min = sleep_debt_min(self.repo.recent_nights(14))
            # Learned maintenance settle-nudge direction (closes the maintenance loop).
            from sleepctl.learning.settle import learn_settle_nudge
            self.cycle.controller.set_settle_nudge(learn_settle_nudge(self.repo, self.cfg))
            mode = self._learn_mode()        # constraint-aware: learn for tonight's night-type
            # Personalize the alarm to YOUR grogginess curve (window + lift bar), per night-type.
            from sleepctl.learning.wake_tuning import learn_wake_tuning, wake_tuning_records
            tuning = learn_wake_tuning(wake_tuning_records(self.repo),
                                       base_window=self.cfg.tunables.wake_window_min, mode=mode)
            self.cycle.controller.wake_orch.cfg.p_wake_liftable = tuning.p_wake_liftable
            self._wake_base_window = tuning.window_min
            # Personalized THERMAL wake maneuver (warm vs cool) + tonight's exploration jitter.
            from sleepctl.learning.thermal_wake import (
                learn_thermal_wake, next_wake_f, thermal_wake_records)
            tw = learn_thermal_wake(thermal_wake_records(self.repo),
                                    base_f=self.cfg.tunables.wake_ramp_temp_f, mode=mode)
            self._wake_thermal_f = next_wake_f(tw.wake_f, datetime.now().timetuple().tm_yday)
            self.cycle.controller.set_wake_ramp_f(self._wake_thermal_f)
            # Personalized ONSET maneuver: learn the warm nudge that gets YOU to sleep fastest
            # (per night-type), with exploration. Closes the going-to-sleep loop.
            from sleepctl.learning.onset_tuning import (
                learn_onset, next_onset_warm_f, onset_records)
            ons = learn_onset(onset_records(self.repo),
                              base_f=self.cfg.tunables.onset_warm_nudge_f, mode=mode)
            self._onset_warm_f = next_onset_warm_f(ons.onset_warm_f,
                                                   datetime.now().timetuple().tm_yday)
            self.cycle.controller.set_onset_warm(self._onset_warm_f)
        except Exception as exc:
            print(f"profile refresh skipped: {exc}", flush=True)

    def _learn_mode(self):
        """Tonight's night-mode for constraint-aware learning ('constrained'|'recovery'|'normal'),
        or None to pool across modes when the mode isn't set yet."""
        nt = (getattr(self.context, "night_type", None) or "").lower()
        return nt if nt in ("constrained", "recovery", "normal") else None

    def _refresh_hue(self) -> None:
        """(Re)build the Hue dawn driver from the stored config and toggle the orchestrator's
        light ramp accordingly. Cheap; rebuilds only when the config actually changes."""
        try:
            from app import services
            c = services._get_hue_config(self.repo)
            sig = (c["enabled"], c["bridge_ip"], c["token"], tuple(c["target_ids"]),
                   tuple(c["therapy_ids"]), c["kind"])
            if sig == getattr(self, "_hue_sig", None):
                return
            self._hue_sig = sig
            ready = bool(c["enabled"] and c["bridge_ip"] and c["token"]
                         and (c["target_ids"] or c["therapy_ids"]))
            if ready:
                from sleepctl.adapters.hue import HueDawnDriver
                self.hue_driver = HueDawnDriver(c["bridge_ip"], c["token"], c["target_ids"],
                                                c["kind"], therapy_ids=c["therapy_ids"])
            else:
                self.hue_driver = None
            # Sunrise ramp only matters with actual dawn bulbs; the therapy plug fires off
            # should_wake regardless. Either way the lights now ride the orchestrator's wake logic.
            self.cycle.controller.set_dawn_light(bool(ready and c["target_ids"]))
        except Exception as exc:
            print(f"hue refresh skipped: {exc}", flush=True)

    def _drive_dawn(self, decision) -> None:
        if not self.hue_driver:
            return
        la = (decision.log_payload or {}).get("wake_action") if decision else None
        try:
            if la is None:                       # outside the wake window -> everything off
                self.hue_driver.set_level(0.0)
                self.hue_driver.set_therapy(False)
            else:
                self.hue_driver.set_level(float(la.get("light_level", 0.0)))   # sunrise ramp
                self.hue_driver.set_therapy(bool(la.get("should_wake")))       # therapy at wake
        except Exception as exc:
            print(f"hue drive skipped: {exc}", flush=True)

    def _capture_wake(self, decision, frame, now) -> None:
        """Record how the user was woken (stage, how early, forced) for the grogginess learner."""
        if decision is None or frame is None:
            return
        la = (decision.log_payload or {}).get("wake_action")
        if not la:
            return
        st = frame.stage.value if getattr(frame, "stage", None) else None
        if st and st.lower() not in ("awake", "unknown"):
            self._wake_last_stage = st                         # last sleep stage before surfacing
        # Capture at the moment of confirmation — the first "post_wake" (light dose held) or
        # "done" tick — NOT after the post-wake hold, so minutes_early/forced reflect the real
        # wake instant.
        if la.get("phase") in ("post_wake", "done") and self._pending_wake is None:
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
                "wake_thermal_f": self._wake_thermal_f,
                "onset_warm_f": getattr(self, "_onset_warm_f", None),
                "night_type": getattr(self.context, "night_type", None)}

    def _flush_wake_log(self) -> None:
        if not self._pending_wake:
            return
        try:
            nights = self.repo.recent_nights(1)
            date = nights[-1].date if nights else datetime.now().date().isoformat()
            bridge.write_wake_log(self.repo.conn, {"date": date, **self._pending_wake})
        except Exception as exc:
            print(f"wake log skipped: {exc}", flush=True)
        finally:
            self._pending_wake, self._wake_last_stage = None, None

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


def _env_truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true",
                    help="drive the REAL Eight Sleep Pod (default: simulator)")
    ap.add_argument("--dry-run", action="store_true",
                    help="live mode but read-only: log decisions, send no device commands")
    ap.add_argument("--poll-seconds", type=float, default=30.0,
                    help="control-cycle cadence (manual overrides apply faster)")
    ap.add_argument("--command-poll-seconds", type=float, default=1.0,
                    help="override-queue poll cadence (realtime temp control)")
    ap.add_argument("--telemetry-seconds", type=float, default=15.0,
                    help="fast decoupled sensor-telemetry refresh cadence (live mode)")
    ap.add_argument("--max-ticks", type=int, default=None)
    args = ap.parse_args()

    # Env toggles let docker-compose enable live mode without changing the command.
    live = args.live or _env_truthy("SLEEPCTL_LIVE")
    dry_run = args.dry_run or _env_truthy("SLEEPCTL_DRY_RUN")

    if not live:
        DashboardDaemon(simulate=True, poll_seconds=args.poll_seconds,
                        command_poll_seconds=args.command_poll_seconds).run(args.max_ticks)
        return

    # --- live: drive the real Pod via the async client ----------------------------
    import asyncio

    from sleepctl.adapters.credentials import load_credentials
    from sleepctl.adapters.eightsleep_cloud import EightSleepClient
    from sleepctl.config import AppConfig
    from app.db import get_repo
    from live_daemon import LiveDashboardDaemon

    creds = load_credentials(os.environ.get("EIGHTSLEEP_CREDENTIALS") or None)
    if not creds.is_complete():
        print("[daemon] live mode requires Eight Sleep credentials "
              "(EIGHTSLEEP_EMAIL / EIGHTSLEEP_PASSWORD). Falling back to simulator.",
              flush=True)
        DashboardDaemon(simulate=True, poll_seconds=args.poll_seconds,
                        command_poll_seconds=args.command_poll_seconds).run(args.max_ticks)
        return

    client = EightSleepClient(
        email=creds.email, password=creds.password, timezone=creds.timezone,
        side=os.environ.get("EIGHTSLEEP_SIDE") or creds.side,
        client_id=creds.client_id, client_secret=creds.client_secret,
    )
    # Environmental pre-compensation: enable the weather feed unless explicitly disabled.
    weather = None
    if os.environ.get("SLEEPCTL_WEATHER", "1") not in ("0", "false", "off"):
        try:
            from sleepctl.adapters.weather import OpenMeteoWeather
            lat = float(os.environ.get("SLEEPCTL_LAT", "42.3601"))
            lon = float(os.environ.get("SLEEPCTL_LON", "-71.0589"))
            weather = OpenMeteoWeather(latitude=lat, longitude=lon)
        except Exception as exc:
            print(f"[daemon] weather pre-compensation disabled: {exc}", flush=True)
    # Phone/independent-sensor fusion: the API writes the latest iPhone-accelerometer-derived
    # sample to the bridge; the daemon overlays its sub-minute movement onto the Pod frame.
    repo = get_repo()
    wearable = None
    if os.environ.get("SLEEPCTL_PHONE_SENSOR", "1") not in ("0", "false", "off"):
        try:
            from sleepctl.adapters.bcg import BridgeWearableSource
            wearable = BridgeWearableSource(repo)
        except Exception as exc:
            print(f"[daemon] phone-sensor fusion disabled: {exc}", flush=True)
    daemon = LiveDashboardDaemon(AppConfig.default(), client, repo, dry_run=dry_run,
                                 weather=weather, wearable=wearable)
    asyncio.run(daemon.run(poll_seconds=args.poll_seconds,
                           command_poll_seconds=args.command_poll_seconds,
                           telemetry_seconds=args.telemetry_seconds,
                           max_ticks=args.max_ticks))


if __name__ == "__main__":
    main()
