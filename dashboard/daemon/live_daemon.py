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
import os
import threading
from datetime import datetime, timedelta
from typing import Optional


def _write_daemon_heartbeat() -> None:
    """Touch .run/daemon.heartbeat so the watchdog can detect daemon liveness by a FILE it
    reads directly (mtime), instead of an unreliable process/command-line query that flaps in the
    scheduled-task context and spuriously restarts a healthy daemon."""
    try:
        db = os.environ.get("SLEEPCTL_DB", "")
        root = os.path.dirname(db) if db else os.getcwd()
        run = os.path.join(root, ".run")
        os.makedirs(run, exist_ok=True)
        with open(os.path.join(run, "daemon.heartbeat"), "w") as fh:
            fh.write(datetime.now().isoformat())
    except Exception:
        pass

from sleepctl.config import AppConfig
from sleepctl.controller.controller import SleepController
from sleepctl.controller.thermal_health import ThermalResponseMonitor
from sleepctl.diagnostics_blackbox import BlackBoxRecorder
from sleepctl.loop.cycle import ControlCycle
from sleepctl.precompensation import compute_precompensation
from sleepctl.loop.nightly import NightlyUpdater
from sleepctl.models import ContextRecord, ControllerState
from sleepctl.storage.backup import maybe_run_backup

import command_spec as cs
from app import bridge

TEMP_MIN_F, TEMP_MAX_F = cs.TEMP_MIN_F, cs.TEMP_MAX_F


def _classify_tick_error(exc: BaseException) -> tuple[str, str]:
    """(category, severity) for a tick exception: cloud-flavored errors (RequestError/504/
    timeout — the common transient Eight Sleep API hiccups) are downgraded to a 'cloud'/'warn'
    event; everything else is a plain 'error'/'error' event."""
    msg = repr(exc)
    if any(s in msg for s in ("RequestError", "504", "timeout", "Timeout")):
        return "cloud", "warn"
    return "error", "error"


def _parse_wake_dt(wake_time):
    """'HH:MM' -> the next datetime it occurs, or None if malformed (so a bad UI command degrades
    gracefully instead of crashing the command loop)."""
    try:
        hh, mm = (int(x) for x in str(wake_time).split(":"))
        if not (0 <= hh < 24 and 0 <= mm < 60):
            return None
        now = datetime.now()
        wake = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        return wake + timedelta(days=1) if wake <= now else wake
    except Exception:
        return None


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
        self.efficacy_arm = None  # tonight's standing efficacy-trial arm, if the trial is enabled
        self.efficacy_trial_arm = None  # tonight's randomized efficacy MICRO-trial arm, if any
        self._phone_fused = False  # was the phone sample fused on the last frame (presence-gated)
        self.hue_driver = None     # Philips Hue dawn-light driver (best-effort)
        self._pending_wake = None  # captured wake conditions, flushed to wake_log at close-out
        self._wake_last_stage = None
        self._wake_base_window = cfg.tunables.wake_window_min  # learned per-user window base
        self._wake_thermal_f = cfg.tunables.wake_ramp_temp_f   # tonight's wake-ramp temperature
        self._onset_warm_f = cfg.tunables.onset_warm_nudge_f   # tonight's learned onset warmth
        self._deepen_policy = None     # learned deepening-response policy (do-no-harm gate)
        self._precursor_profile = None  # learned personalized awakening-precursor trajectory
        self._last_history_ts = 0.0    # monotonic clock: throttles state_history writes
        self._last_applied_commands: list = []
        self.blackbox = BlackBoxRecorder(bridge.run_dir())   # crash pre-history ring buffer
        # Load the learned profiles onto the controller AFTER all the state above exists (the
        # attach path flushes the wake log + applies every per-phase learner). Doing this last
        # fixes a startup ordering bug where the whole load was silently skipped.
        self._attach_profiles(controller)

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
        if not self.verbose:
            return
        # Belt-and-suspenders: main() forces UTF-8 stdout, but a log line must NEVER be able to
        # raise (a UnicodeEncodeError here previously killed the control loop AND its crash
        # handler, crash-looping the daemon). Fall back to an ASCII-safe render if anything goes
        # wrong, and swallow even that as a last resort.
        try:
            print(msg, flush=True)
        except Exception:
            try:
                print(msg.encode("ascii", "replace").decode("ascii"), flush=True)
            except Exception:
                pass

    def _emit_event(self, category: str, severity: str, code: str, message: str,
                    data: Optional[dict] = None) -> None:
        """Best-effort structured event log entry (see ``sleepctl.storage.repository.
        Repository.log_event``). Never allowed to break the control loop."""
        try:
            self.repo.log_event(category, severity, code, message, data)
        except Exception:
            pass

    @staticmethod
    def _clamp_temp(f) -> float:
        return cs.clamp_temp(f)

    def _learn_mode(self):
        """Tonight's night-mode for constraint-aware learning ('constrained'|'recovery'|'normal'),
        or None to pool across modes when the mode isn't set yet."""
        nt = (getattr(self.context, "night_type", None) or "").lower()
        return nt if nt in ("constrained", "recovery", "normal") else None

    def _attach_profiles(self, controller: SleepController) -> None:
        try:
            from sleepctl.learning.lead_time import build_lead_time_profile
            from sleepctl.learning.settle import learn_settle_nudge
            from sleepctl.ml.wake_profile import build_wake_profile
            controller.set_wake_profile(build_wake_profile(self.repo),
                                        lead_profile=build_lead_time_profile(self.repo))
            # Feed the in-bed self-test's measured ramp rates to the stall detector + wake warm-up
            # so both reason about YOUR bed's real cool/heat speed.
            cal = self.repo.get_thermal_calibration()
            if cal:
                thermal = getattr(self, "thermal", None)
                if thermal is not None:
                    thermal.set_measured_rates(cal.get("cool_levels_per_min"),
                                               cal.get("heat_levels_per_min"))
                controller.set_measured_thermal(cal.get("cool_lag_min"), cal.get("heat_lag_min"))
            # In-bed resting-physiology baseline → arousal/wake-risk anchor.
            controller.set_resting_baseline(self.repo.get_resting_baseline())
            # Personal comfort mapping → the controller's neutral is what YOU feel neutral, not the
            # device's water-scale default.
            comfort = self.repo.get_comfort_profile()
            if comfort and comfort.get("neutral_f") is not None:
                controller.thermal.profile.neutral_f = float(comfort["neutral_f"])
            controller.set_settle_nudge(learn_settle_nudge(self.repo, self.cfg))
            from sleepctl.benchmarks import sleep_debt_min
            controller.wake_debt_min = sleep_debt_min(self.repo.recent_nights(14))
            self._flush_wake_log()        # persist last night's wake conditions
            mode = self._learn_mode()     # constraint-aware: learn for tonight's night-type
            # Personalize the alarm to YOUR grogginess curve (window + lift bar), per night-type.
            from sleepctl.learning.wake_tuning import learn_wake_tuning, wake_tuning_records
            tuning = learn_wake_tuning(wake_tuning_records(self.repo),
                                       base_window=self.cfg.tunables.wake_window_min, mode=mode)
            controller.wake_orch.cfg.p_wake_liftable = tuning.p_wake_liftable
            self._wake_base_window = tuning.window_min
            # Personalized ONSET maneuver (warm nudge for fastest onset, per night-type) + explore.
            from sleepctl.learning.onset_tuning import (
                learn_onset, next_onset_warm_f, onset_records)
            ons = learn_onset(onset_records(self.repo),
                              base_f=self.cfg.tunables.onset_warm_nudge_f, mode=mode)
            self._onset_warm_f = next_onset_warm_f(ons.onset_warm_f,
                                                   datetime.now().timetuple().tm_yday)
            controller.set_onset_warm(self._onset_warm_f)
            # Deepening-response: gate tonight's deepen actuation on the learned do-no-harm policy
            # and the n-of-1 control schedule (does cooling actually deepen you, without waking you?).
            from sleepctl.learning.deepening import (
                deepening_records, learn_deepening, next_steer_mode)
            self._deepen_policy = learn_deepening(deepening_records(self.repo), mode=mode)
            steer_mode = next_steer_mode(self._deepen_policy,
                                         datetime.now().timetuple().tm_yday)
            controller.set_steer_policy(
                actuate=self._deepen_policy.enabled and steer_mode == "act")
            # Personalized awakening prediction: tune the precursor detector to the trajectory that
            # precedes YOUR awakenings (earlier, more accurate pre-emption).
            from sleepctl.learning.wake_causation import awakening_precursor_profile
            self._precursor_profile = awakening_precursor_profile(self.repo)
            controller.set_precursor_profile(self._precursor_profile)
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
        # Standing "does the controller help?" efficacy trial (opt-in, default OFF): assign
        # tonight CONTROLLED vs a do-no-harm HELD baseline and, on a HELD night, force a neutral
        # setpoint + disable experimental steering/preemption via EXISTING controller setters.
        # Applied AFTER the n-of-1 experiment arm above so a HELD night always wins (the stricter,
        # do-no-harm baseline) if the two features are ever enabled at once.
        try:
            from sleepctl.eval.efficacy import apply_efficacy_arm
            base_eff = controller.thermal.profile
            eff_prof, eff_info = apply_efficacy_arm(
                self.repo, self.cfg, controller, datetime.now().date().isoformat(), base_eff)
            controller.set_setpoints(eff_prof)
            self.efficacy_arm = eff_info
            if eff_info:
                self._log(f"efficacy trial: tonight is {eff_info['arm']}")
        except Exception as exc:
            self._log(f"efficacy-trial apply skipped: {exc}")

    def _apply_night_type(self, hint: str) -> None:
        try:
            from sleepctl.benchmarks import NightMode
            from sleepctl.controller.sleep_plan import plan_night
            plan = plan_night(datetime.now(), self.context.required_wake_time,
                              self.repo.recent_nights(14), hint=hint, repo=self.repo)
            self.context.night_type = plan.mode.value
            self.context.is_short_sleep_day = plan.mode == NightMode.CONSTRAINED
            self.context.sleep_opportunity_min = plan.sleep_opportunity_min
            # Hand tonight's PERSONALIZED ideal architecture to the in-night steerer.
            self.cycle.controller.set_night_targets(plan.targets, plan.est_sleep_min)
        except Exception as exc:
            self._log(f"night-type planning skipped: {exc}")
        # Night TYPE is only known now (plan_night just classified it) -- the randomized efficacy
        # micro-trial's eligibility gate needs that, so it's applied here, not at daemon start-up.
        self._apply_efficacy_micro_trial()

    def _apply_efficacy_micro_trial(self) -> None:
        """Randomized efficacy MICRO-trial (on by default, conservative): assign 'active' vs
        'sham' -- eligibility-gated so short/recovery/nap nights ALWAYS run active -- and on a
        sham night force a neutral hold via the EXISTING controller setters (do-no-harm, same
        pattern as the standing trial in ``_attach_profiles``). Applied AFTER the standing trial
        so a HELD night from that (older, coarser) system still wins if both are ever enabled at
        once; a SHAM night here is equally conservative either way."""
        try:
            from sleepctl.ml.efficacy_trial import apply_trial_arm
            base = self.cycle.controller.thermal.profile
            context = {"night_type": self.context.night_type, "session_mode": self.session_mode}
            prof, info = apply_trial_arm(
                self.repo, self.cfg, self.cycle.controller,
                datetime.now().date().isoformat(), context, base)
            self.cycle.controller.set_setpoints(prof)
            self.efficacy_trial_arm = info
            self._log(f"efficacy micro-trial: tonight is {info['arm']} "
                     f"(eligible={info['eligible']})")
        except Exception as exc:
            self._log(f"efficacy micro-trial apply skipped: {exc}")

    # ------------------------------------------------------------------ device
    async def _set_level(self, level: int) -> None:
        if not self.dry_run:
            await self.client.set_heating_level(level)

    async def _apply_commands(self) -> bool:
        """Drain the dashboard command queue, applying each to the REAL device. Returns
        True if any device-affecting change occurred."""
        changed = False
        self._last_applied_commands = []   # reset each call; read by the blackbox recorder
        while True:
            cmd = bridge.next_pending_command(self.repo.conn)
            if cmd is None:
                break
            t, p = cmd["type"], cmd["payload"]
            changed = True
            self._last_applied_commands.append(t)
            try:
                if t == "stop":
                    # EMERGENCY STOP is a safety override: hard-off the side ALWAYS, even in
                    # dry-run. A silent no-op emergency stop is exactly what you don't want.
                    cs.apply_stop_state(self)
                    try:
                        await self.client.turn_off_side()
                        self._log("EMERGENCY STOP: side turned off")
                    except Exception as exc:
                        self._log(f"EMERGENCY STOP turn_off_side failed: {exc}")
                elif t == "power_off":
                    cs.apply_power_off_state(self)
                    if not self.dry_run:
                        await self.client.turn_off_side()
                elif t == "pause":
                    cs.apply_pause(self)
                elif t in ("start", "resume"):
                    cs.apply_start_or_resume(self)
                elif t == "power_on":
                    cs.apply_power_on_state(self)
                    if not self.dry_run:
                        await self.client.turn_on_side()
                elif t == "away_on":
                    cs.apply_away_on_state(self)
                    if not self.dry_run:
                        await self.client.set_away_mode(True)
                elif t == "away_off":
                    cs.apply_away_off_state(self)
                    if not self.dry_run:
                        await self.client.set_away_mode(False)
                        await self.client.turn_on_side()
                elif t == "prime":
                    if not self.dry_run:
                        await self.client.prime_pod()
                elif t == "safe_default":
                    cs.apply_safe_default_state(self)
                    self.repo.save_setpoints(self.cfg.default_setpoints())
                elif t == "set_mode":
                    cs.apply_set_mode(self, p)
                elif t == "set_temp":
                    cs.apply_set_temp(self, p)
                    await self._set_level(self.cycle.controller.thermal.to_level(self.manual_target_f))
                elif t == "nudge_temp":
                    cs.apply_nudge_temp(self, p)
                    await self._set_level(self.cycle.controller.thermal.to_level(self.manual_target_f))
                elif t == "set_wake":
                    self.wake = cs.build_wake_dict(self.cfg, p)
                    wk = _parse_wake_dt(p.get("wake_time"))
                    if wk is None:
                        self._log(f"set_wake ignored: bad wake_time {p.get('wake_time')!r}")
                        self.wake = None
                        self.context.required_wake_time = None
                    else:
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
                    cs.apply_clear_wake(self)
                elif t == "induce_sleep":
                    self._start_induce()
                elif t == "start_nap":
                    self._start_nap(p.get("duration_min"), p.get("wake_time"))
                elif t == "end_session":
                    self._end_session()
                elif t == "self_test":
                    await self._run_self_test(p.get("mode", "full"))
                elif t == "self_test_cancel":
                    pass  # handled live by the running battery's cancel poll; no-op here
                elif t == "comfort_cal_start":
                    await self._comfort_start(p)
                elif t == "comfort_cal_rate":
                    await self._comfort_rate(p.get("rating"))
                elif t == "comfort_cal_cancel":
                    await self._comfort_cancel()
            except Exception as exc:  # never let a device hiccup wedge the queue
                # repr + type + the underlying cause: many cloud errors (e.g. RequestError) have an
                # empty str(), which made the log useless ("command prime failed:").
                cause = getattr(exc, "__cause__", None)
                self._log(f"command {t} failed: {type(exc).__name__}: {exc!r}"
                          + (f" <- {cause!r}" if cause is not None else ""))
            else:
                # A device command actually applied: log it to the structured event log (the
                # "what happened and when" query surface). Best-effort, never raises.
                if t in ("prime", "power_on", "power_off", "away_on", "away_off",
                        "set_temp", "stop", "self_test"):
                    self._emit_event("device", "info", t, f"device command applied: {t}", p)
            bridge.mark_applied(self.repo.conn, cmd["id"])
        return changed

    async def _run_self_test(self, mode: str) -> None:
        """Run the on-bed self-test / thermal-calibration battery. Pauses normal control (the
        battery drives the device directly), streams progress into runtime_state so the phone
        shows live PASS/FAIL, persists the measured cool/heat calibration for the timing modules,
        and leaves the side OFF (the user presses Power On to resume)."""
        from sleepctl.loop.self_test import run_self_test

        self._log(f"self-test starting (mode={mode})")
        self._emit_event("self_test", "info", "self_test_start",
                         f"self-test starting (mode={mode})", {"mode": mode})
        # Pause the closed loop so we're the only thing driving the device.
        self.paused = True

        def _on_progress(report) -> None:
            self._self_test_report = report.to_dict()
            try:
                bridge.write_self_test(self.repo.conn, self._self_test_report)
            except Exception:
                pass

        def _cancelled() -> bool:
            # Peek the queue WITHOUT consuming it: an emergency stop or an explicit cancel aborts
            # the battery promptly (it then SAFE-OFFs). Non-destructive read (status stays pending).
            try:
                row = self.repo.conn.execute(
                    "SELECT type FROM commands WHERE status='pending' "
                    "AND type IN ('stop','self_test_cancel') LIMIT 1").fetchone()
                return row is not None
            except Exception:
                return False

        try:
            report = await run_self_test(self.client, mode=mode, dry_run=self.dry_run,
                                         on_progress=_on_progress, cancelled=_cancelled)
            self._self_test_report = report.to_dict()
            try:
                if report.calibration:
                    self.repo.save_thermal_calibration(report.calibration)
                if report.resting_baseline:
                    self.repo.save_resting_baseline(report.resting_baseline)
                if report.calibration or report.resting_baseline:
                    self._attach_profiles(self.cycle.controller)  # apply the new anchors now
                    self._log(f"self-test saved: cal={report.calibration} "
                              f"rest={report.resting_baseline}")
            except Exception as exc:
                self._log(f"self-test persistence skipped: {exc}")
            self._log(f"self-test done (overall_passed={report.overall_passed}, "
                      f"aborted={report.aborted})")
            self._emit_event("self_test", "info", "self_test_end",
                             f"self-test done (overall_passed={report.overall_passed})",
                             {"mode": mode, "overall_passed": report.overall_passed,
                              "aborted": report.aborted})
        finally:
            # The battery already powered the side OFF; reflect that and hold so the loop doesn't
            # immediately re-drive. The user presses Power On to resume normal control.
            self.power_on, self.paused = False, True

    # ----------------------------------------------------- comfort calibration
    async def _comfort_set_level(self) -> None:
        """Hold the bed at the current comfort step so you can rate a settled temperature."""
        c = getattr(self, "comfort", None)
        if c is None:
            return
        target = c.current_target_f()
        if target is None:
            return
        self.power_on, self.paused, self.away = True, False, False
        if not self.dry_run:
            await self.client.set_heating_level(
                self.cycle.controller.thermal.to_level(float(target)))

    async def _comfort_start(self, p: dict) -> None:
        from sleepctl.controller.comfort import ComfortCalibration, steps_around
        steps = p.get("steps_f")
        if not steps:
            neutral = self.cycle.controller.thermal.profile.neutral_f
            steps = steps_around(neutral)
        self.comfort = ComfortCalibration(steps_f=[float(s) for s in steps])
        self._comfort_result = None
        self._log(f"comfort calibration started (steps={self.comfort.steps_f})")
        await self._comfort_set_level()

    async def _comfort_rate(self, rating) -> None:
        c = getattr(self, "comfort", None)
        if c is None or rating is None:
            return
        c.rate(int(rating))
        if c.done:
            await self._comfort_finalize()
        else:
            await self._comfort_set_level()

    async def _comfort_finalize(self) -> None:
        c = self.comfort
        prof = c.finalize()
        self._comfort_result = prof.to_dict()
        try:
            self.repo.save_comfort_profile(self._comfort_result)
            self._attach_profiles(self.cycle.controller)  # apply the new neutral now
            self._log(f"comfort calibration saved: {self._comfort_result}")
        except Exception as exc:
            self._log(f"comfort save skipped: {exc}")
        self.comfort = None
        # Leave the bed at the learned neutral, powered + on auto for the night.
        self.power_on, self.paused, self.mode = True, False, "auto"

    async def _comfort_cancel(self) -> None:
        if getattr(self, "comfort", None) is not None:
            self.comfort.cancel()
        self.comfort = None
        self.power_on, self.paused = False, True
        self._log("comfort calibration cancelled")

    def _comfort_snapshot(self) -> Optional[dict]:
        c = getattr(self, "comfort", None)
        if c is not None:
            return c.progress()
        return getattr(self, "_comfort_result", None) and {"running": False, "cancelled": False,
                                                           "result": self._comfort_result}

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
        """Advisory cross-shift sleep-debt plan: debt + strategy from recent nights, plus banking /
        prophylactic-nap logic from the next shift (auto-synced from the work calendar when
        connected, else the manual next-shift hint — see ``services.sync_calendar_to_shift``)."""
        try:
            from app import services
            self.shift_plan = services.shift_plan_view(self.repo)
        except Exception as exc:
            self._log(f"shift plan skipped: {exc}")
            return
        # Calendar-driven auto-wake (mirrors the gym advisor's effective-wake pattern above in
        # `set_wake`): only when the user has NOT set tonight's wake by hand (self.wake is None
        # exactly when no "set_wake" command has been applied / it was cleared) do we let the
        # next calendar shift arm a morning alarm. A manual wake pick ALWAYS wins — this branch
        # never runs once self.wake is set, and never touches self.context.required_wake_time
        # in that case. Night shifts intentionally get no morning alarm here (calendar_effective_
        # wake returns None) — the banking/anchor-sleep plan above already covers those.
        if self.wake is None:
            try:
                from app import services as _svc
                auto_wake = _svc.calendar_effective_wake(self.repo)
                if auto_wake is not None:
                    self.context.required_wake_time = auto_wake
            except Exception as exc:
                self._log(f"calendar auto-wake skipped: {exc}")

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
                self._log(f"WARNING: thermal: {th.reason}")
                self._emit_event("thermal", "warn", "thermal_stalled",
                                 th.reason or "thermal response stalled",
                                 {"device_level": frame.device_level,
                                  "target_level": frame.target_level})
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
            # DEVICE-REPORTED truth read back from the bed (vs the commanded target_level above):
            # device_level = what the Pod says it's actually doing, device_target_level = the level
            # the Pod accepted. Round-trip verification compares these against the command.
            "extra": {"manual_target_f": self.manual_target_f, "power_on": self.power_on,
                      "away": self.away, "wake": self.wake, "live": True,
                      "device_level": frame.device_level if frame else None,
                      "device_target_level": frame.target_level if frame else None,
                      "bed_presence": frame.presence if frame else None,
                      "dry_run": self.dry_run, "session_mode": self.session_mode,
                      "nap": self.nap_plan,
                      "nap_deadline": self.nap_deadline.isoformat() if self.nap_deadline else None,
                      "thermal_health": self.thermal.status().to_dict(),
                      "preemption": self.cycle.controller.preemption_summary(),
                      "steering": self.cycle.controller.steering_summary(),
                      "data_quality": self.cycle.controller.data_quality_summary(),
                      "guardrail": self.cycle.controller.guardrail_summary(),
                      "precompensation": self.precomp,
                      "device": self._safe_device_status(),
                      "experiment": self.active_experiment,
                      "efficacy_arm": self.efficacy_arm,
                      "shift_plan": self.shift_plan,
                      "self_test": getattr(self, "_self_test_report", None),
                      "comfort_cal": self._comfort_snapshot(),
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

    # ---- diagnostics: 48h state-history trend + black-box crash pre-history --------------
    def _record_state_history(self, snapshot: dict) -> None:
        """Append a throttled (~60s) copy of ``snapshot`` to ``state_history`` (see
        ``Repository.record_state_snapshot``) so /diag/history has a real trend, not just the
        latest instant. Best-effort: a DB hiccup here must never affect the control loop."""
        now = asyncio.get_event_loop().time()
        if now - self._last_history_ts < 60.0:
            return
        self._last_history_ts = now
        try:
            self.repo.record_state_snapshot(snapshot)
        except Exception:
            pass

    def _blackbox_entry(self, decision, frame) -> dict:
        """One tick's black-box summary: state/decision + key frame fields + any command
        applied this tick (see ``sleepctl.diagnostics_blackbox.BlackBoxRecorder``)."""
        return {
            "state": decision.state.value if decision else None,
            "intent": decision.thermal_intent.value if decision else None,
            "target_temp_f": decision.target_temp_f if decision else None,
            "reason": decision.reason if decision else None,
            "hr": frame.heart_rate if frame else None,
            "hrv": frame.hrv if frame else None,
            "rr": frame.respiratory_rate if frame else None,
            "stage": frame.stage.value if frame and frame.stage else None,
            "bed_temp_f": frame.bed_temp_f if frame else None,
            "presence": frame.presence if frame else None,
            "data_age_s": frame.data_age_seconds if frame else None,
            "commands": list(self._last_applied_commands),
        }

    def _maybe_backup(self) -> None:
        """Once-a-day rotating DB backup (see ``sleepctl.storage.backup``), called from the
        nightly close-out seam (``_maybe_close_out``). Filename-timestamp-gated so it's safe to call
        more than once/night and survives daemon restarts. Best-effort: never allowed to break
        the control loop."""
        try:
            path = maybe_run_backup(self.repo.path)
            if path:
                self._emit_event("backup", "info", "db_backup", "rotating DB backup written",
                                 {"path": path})
        except Exception as exc:
            self._log(f"db backup skipped: {exc}")

    def _check_failure_alerts(self) -> None:
        """Nighttime failure push: an offline bed, empty reservoir, wedged command queue, or a
        stalled control loop should page the phone before the user finds out by being
        uncomfortable at 3am -- see ``app.services.check_and_alert_failures`` for the detection +
        live/night gating + per-condition hourly rate limit. Called once per control tick
        (~poll_seconds, default 60s -- fine cadence for a per-condition-throttled push).
        Best-effort: a push/DB hiccup here must never affect the control loop."""
        try:
            from app import services
            services.check_and_alert_failures(self.repo)
        except Exception as exc:
            self._log(f"failure-alert check skipped: {exc}")

    def _refresh_hue(self) -> None:
        """(Re)build the Hue dawn driver from the stored config; toggle the orchestrator's light
        ramp accordingly. Rebuilds only when the config changes."""
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
            self._log(f"hue refresh skipped: {exc}")

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
        # Capture at confirmation — first "post_wake" (light dose held) or "done" — not after the
        # post-wake hold, so minutes_early/forced reflect the real wake instant.
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
            self._log(f"wake log skipped: {exc}")
        finally:
            self._pending_wake, self._wake_last_stage = None, None

    # ------------------------------------------------------------------ cycles
    async def control_tick(self) -> None:
        await self._apply_commands()
        # Comfort calibration owns the bed while active: hold the current step and publish state,
        # bypassing the normal control decision (you're rating settled temperatures).
        if getattr(self, "comfort", None) is not None and not self.comfort.done:
            await self._comfort_set_level()
            await self.client.update()
            frame = self._read_frame()
            self._record_thermal(frame, self.client.now())
            bridge.write_runtime_state(self.repo.conn, self._snapshot(self._last_decision, frame))
            return
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
            if decision.state != self._prev_state:
                self._emit_event("state", "info", "state_transition",
                                 f"{self._prev_state.value} -> {decision.state.value}",
                                 {"from": self._prev_state.value, "to": decision.state.value})
            self._prev_state = decision.state
        self._last_decision = decision
        self._drive_dawn(decision)        # push the dawn light level to Hue (best-effort)
        snapshot = self._snapshot(decision, frame)
        bridge.write_runtime_state(self.repo.conn, snapshot)
        self._record_state_history(snapshot)
        self.blackbox.record(self._blackbox_entry(decision, frame))
        self._check_failure_alerts()

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
        comfort_active = getattr(self, "comfort", None) is not None and not self.comfort.done
        if comfort_active:
            await self._comfort_set_level()
        elif self.power_on and not self.paused and not self.away:
            decision = self.cycle.decide(frame, self.context, now)
            if self.mode == "manual" and self.manual_target_f is not None:
                await self._set_level(self.cycle.controller.thermal.to_level(self.manual_target_f))
        self._last_decision = decision
        snapshot = self._snapshot(decision, frame)
        bridge.write_runtime_state(self.repo.conn, snapshot)
        self._record_state_history(snapshot)
        self.blackbox.record(self._blackbox_entry(decision, frame))
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
        snapshot = self._snapshot(self._last_decision, frame)
        bridge.write_runtime_state(self.repo.conn, snapshot)
        self._record_state_history(snapshot)
        self.blackbox.record(self._blackbox_entry(self._last_decision, frame))

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
                # Record tonight's outcome against whichever arm the standing efficacy trial
                # assigned (no-op if the trial is off / this night was never assigned an arm).
                from sleepctl.eval.efficacy import record_efficacy_outcome
                total = night.total_sleep_min
                deep_pct = (night.deep_min / total) if (night.deep_min is not None and total) \
                    else None
                record_efficacy_outcome(
                    self.repo, night_date, wake_events=night.wake_events, deep_pct=deep_pct,
                    efficiency=night.sleep_efficiency, outcome_score=night.outcome_score)
                # Record tonight's outcome against whichever arm the randomized efficacy
                # MICRO-trial assigned (no-op if this night was never assigned one -- e.g. the
                # daemon restarted mid-night before `_apply_night_type` ran).
                from sleepctl.ml.efficacy_trial import record_trial_outcome
                record_trial_outcome(
                    self.repo, night_date, wake_events=night.wake_events, deep_pct=deep_pct,
                    hrv=night.avg_hrv, efficiency=night.sleep_efficiency,
                    outcome_score=night.outcome_score)
                self._emit_event("nightly", "info", "nightly_close_out",
                                 f"nightly close-out ran for {night_date}",
                                 {"night_date": night_date})
                self.repo.prune_events()  # housekeeping: cap event-log growth, once/night
                self._maybe_backup()      # rotating DB backup: once/day, gated on-disk
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
        # Heartbeat from a real OS THREAD, started BEFORE anything else. Unlike an asyncio task, a
        # thread keeps writing the liveness file even when the event loop is blocked by a synchronous
        # call (a slow/hung pyEight request, or the ~10-min on-bed self-test) — so the watchdog can
        # never false-restart a busy-but-healthy daemon. Beats through connect() too.
        self._hb_stop = threading.Event()
        self._hb_thread = threading.Thread(target=self._heartbeat_thread, name="daemon-heartbeat",
                                           daemon=True)
        self._hb_thread.start()
        await self.client.connect()
        # Take exclusive thermal control: disable Eight Sleep's Autopilot so its dynamic
        # bedtime engine stops overriding our commands (verified live -- Autopilot re-writes
        # currentLevel to its own escalating targets within ~45s, and away mode idles the pod).
        # smart.enabled=false keeps the pod actuating under our currentLevel. Skipped in dry-run
        # (read-only) and when the client doesn't support it (e.g. the simulator).
        if not self.dry_run and hasattr(self.client, "set_autopilot"):
            try:
                await self.client.set_autopilot(False)
                self._log("Eight Sleep Autopilot DISABLED (exclusive control; no schedule override).")
                self._emit_event("lifecycle", "info", "autopilot_disabled",
                                 "Autopilot disabled for exclusive control", {})
            except Exception as exc:  # pragma: no cover - network dependent
                self._log(f"WARNING: could not disable Autopilot: {exc!r}")
        self._log(f"sleepctl dashboard LIVE daemon started (dry_run={self.dry_run}, "
                  f"control={poll_seconds:g}s, telemetry={telemetry_seconds:g}s)."
                  + ("  [READ-ONLY: no device commands]" if self.dry_run else ""))
        self._emit_event("lifecycle", "info", "daemon_started",
                         f"daemon started (dry_run={self.dry_run})",
                         {"dry_run": self.dry_run, "poll_seconds": poll_seconds,
                          "telemetry_seconds": telemetry_seconds})
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
                    cat, sev = _classify_tick_error(exc)
                    self._emit_event(cat, sev, "tick_error", repr(exc),
                                     {"consec_errors": self._consec_errors})
                    try:
                        bridge.write_runtime_state(
                            self.repo.conn, self._snapshot(None, None, error=repr(exc)))
                    except Exception:
                        pass
                    try:
                        self.blackbox.dump_crash()   # preserve the ~200 ticks before this error
                    except Exception:
                        pass
                    await asyncio.sleep(min(30.0, command_poll_seconds * min(self._consec_errors, 8)))
                finally:
                    if due:
                        last_control = loop_now
                # Liveness heartbeat for the self-diagnosis battery (see run_daemon.py's sync
                # loop for the same touch — kept independent of the runtime_state DB write so a
                # DB hiccup can't also blind the "is the daemon alive" check).
                bridge.write_heartbeat("daemon")
                if max_ticks is not None and ticks >= max_ticks:
                    break
                if shutdown_event is not None and shutdown_event.is_set():
                    break
                await asyncio.sleep(command_poll_seconds)
        finally:
            self._hb_stop.set()
            self._emit_event("lifecycle", "info", "daemon_stopping",
                             "daemon stopping; device client closing")
            try:
                self.blackbox.dump_latest()   # clean-shutdown pre-history snapshot
            except Exception:
                pass
            await self.client.close()
            self._log("sleepctl dashboard LIVE daemon stopped; device client closed.")

    def _heartbeat_thread(self) -> None:
        """Write .run/daemon.heartbeat every ~5s from a dedicated OS thread — immune to the asyncio
        event loop being blocked by a synchronous call, so the watchdog's liveness signal stays
        fresh through any long/blocking operation (self-test, hung cloud request)."""
        _write_daemon_heartbeat()   # beat once immediately (covers a slow connect())
        while not self._hb_stop.wait(5.0):
            _write_daemon_heartbeat()
