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
import json
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


# Cap on the rolling recent-tick-error window persisted into runtime_state.extra (see
# LiveDashboardDaemon._recent_errors) -- generous relative to health_monitor's 3-error
# "repeated_cloud_errors" threshold, just bounding memory/row size during a very long outage.
_MAX_RECENT_ERRORS = 20


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
        self._away_heal_mono = 0.0  # throttle for the away self-heal read (see _heal_away)
        self.manual_target_f: Optional[float] = None
        self.last_target_f: Optional[float] = None
        self.wake = None
        self.session_mode = "night"
        self.nap_plan = None          # NapPlan.to_dict() (dashboard-facing) when a nap is active
        self.nap_deadline = None      # CURRENT operative deadline fed to required_wake_time
        # Time-anchoring bookkeeping for the active nap (see sleepctl.controller.nap): the actual
        # NapPlan object (kept internally so it can be re-planned once onset is known), when the
        # button was pressed (duration-nap fallback math), the immutable hard wake-by deadline
        # (wake-by requests only; None for a duration nap, whose deadline is onset-derived) and
        # whether the one-time re-plan-on-onset has already run this session.
        self._nap_plan_obj = None
        self.nap_start = None
        self.nap_hard_deadline = None
        self._nap_replanned = False
        # "Help me fall asleep" surfaced constraint when a wake deadline leaves little sleep
        # opportunity remaining (see ``_apply_induce_deadline_awareness``); None otherwise.
        self._induce_note = None
        # Which onset timestamp (if any) has already been logged to `events` this bed session --
        # see _maybe_log_onset. Keyed by the onset event's own timestamp rather than a bare bool
        # so a fresh onset after a reset (out of bed and back in) logs again.
        self._onset_logged_ts = None
        self._prev_state = ControllerState.IDLE
        self._saw_sleep = False
        self._consec_errors = 0
        # Rolling window of recent tick-error reprs, persisted into runtime_state.extra each tick
        # (see _snapshot) so app.services.evaluate_and_sync_health_alerts can see a sustained
        # cloud/device outage (e.g. daytime Eight Sleep API down) and push a critical alert via
        # health_monitor.evaluate_health's recent_errors path -- capped well above the 3-error
        # alert threshold so the count stays accurate for any realistic outage.
        self._recent_errors: list[str] = []
        self._last_decision = None  # reused by the fast telemetry tick between control ticks
        self.active_experiment = None  # tonight's applied n-of-1 arm, if any
        self.efficacy_arm = None  # tonight's standing efficacy-trial arm, if the trial is enabled
        self.efficacy_trial_arm = None  # tonight's randomized efficacy MICRO-trial arm, if any
        self.thermal_trial_arm = None   # tonight's n-of-1 thermal DOSE-RESPONSE arm, if any
        self._phone_fused = False  # was the phone sample fused on the last frame (presence-gated)
        self.hue_driver = None     # Philips Hue dawn-light driver (best-effort)
        self.plug_driver = None    # non-Hue Wi-Fi wake-therapy plug driver (best-effort)
        # True once the Pod has refused an alarm WRITE with 402/403 (subscription-gated).
        # Latched so we stop retrying a refusal no client can talk its way past, and so
        # the snapshot can say plainly that vibration is unavailable this night.
        self._alarm_write_denied = False
        self._pending_wake = None  # captured wake conditions, flushed to wake_log at close-out
        self._wake_last_stage = None
        self._wake_base_window = cfg.tunables.wake_window_min  # learned per-user window base
        self._wake_thermal_f = cfg.tunables.wake_ramp_temp_f   # tonight's wake-ramp temperature
        self._onset_warm_f = cfg.tunables.onset_warm_nudge_f   # tonight's learned onset warmth
        self._onset_cold_settle_f = cfg.tunables.onset_cold_settle_temp_f  # tonight's cold opener
        self._warm_pulse_on = True     # tonight's A/B: run the brief warm pulse (user opted in)
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
        self._nap_plan_obj, self.nap_start = None, None
        self.nap_hard_deadline, self._nap_replanned = None, False
        self._induce_note = None
        self._onset_logged_ts = None
        self._apply_induce_deadline_awareness()
        self.cycle.controller.set_session("induce", keep_light=False)
        self._persist_session()

    def _apply_induce_deadline_awareness(self) -> None:
        """'Help me fall asleep' must know how much sleep opportunity is actually left. If a wake
        deadline is set and the opportunity remaining RIGHT NOW (not whatever ``plan_night``
        computed back when the alarm was set — the user may have been awake a while since) is
        short, route tonight onto the EXISTING short-night/DAMAGE_CONTROL compression path
        (``InductionRoutine`` halves the warm-opener phase — see induction.py) so a slow,
        luxurious cascade doesn't eat the little time left. This reuses the same classification
        the controller already understands (``context.is_short_sleep_day`` ->
        ``NightObjective.DAMAGE_CONTROL``); it does not invent any new behaviour. Best-effort:
        never raises."""
        try:
            ctx = self.context
            wake = ctx.required_wake_time
            if wake is None:
                return
            remaining_min = (wake - datetime.now()).total_seconds() / 60.0
            if remaining_min <= 0:
                return
            from sleepctl.benchmarks import CONSTRAINED_OPPORTUNITY_MIN
            if remaining_min < CONSTRAINED_OPPORTUNITY_MIN:
                ctx.is_short_sleep_day = True
                self._induce_note = (
                    f"only ~{remaining_min / 60.0:.1f}h left before your wake time — "
                    "keeping the onset cascade short so it doesn't eat the time you have")
                self._log(f"induce: {self._induce_note}")
                self._emit_event(
                    "induction", "info", "induce_deadline_constrained", self._induce_note,
                    {"remaining_min": round(remaining_min, 1)})
        except Exception as exc:
            self._skip("induction deadline-awareness", exc)

    def _start_nap(self, duration_min=None, wake_time=None) -> None:
        from sleepctl.controller.nap import NapRequestKind, NapStrategy, fallback_deadline, nap_strategy
        now = datetime.now()
        if wake_time:
            hh, mm = (int(x) for x in str(wake_time).split(":"))
            hard_deadline = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
            if hard_deadline <= now:
                hard_deadline += timedelta(days=1)
            requested_window_min = max(5, int((hard_deadline - now).total_seconds() // 60))
            plan = nap_strategy(requested_window_min, now_hour=now.hour, cfg=self.cfg,
                                request_kind=NapRequestKind.WAKE_BY.value,
                                requested_window_min=requested_window_min)
            deadline = hard_deadline
        else:
            requested_window_min = max(1, int(duration_min or 20))
            plan = nap_strategy(requested_window_min, now_hour=now.hour, cfg=self.cfg,
                                request_kind=NapRequestKind.DURATION.value,
                                requested_window_min=requested_window_min)
            hard_deadline = None  # no externally-fixed wall for a pure duration nap
            # Anchor the wake to onset once it's known (see _maybe_replan_nap); until then this
            # fallback cap guards the "never actually falls asleep" case (see fallback_deadline).
            deadline = fallback_deadline(now, plan)
        ctrl_mode = "nap_power" if plan.strategy in (NapStrategy.POWER, NapStrategy.TRAP) \
            else "nap_cycle"
        self.session_mode = "nap"
        self.mode, self.power_on, self.paused, self.away = "auto", True, False, False
        self._nap_plan_obj = plan
        self.nap_plan = plan.to_dict()
        self.nap_deadline = deadline
        self.nap_start = now
        self.nap_hard_deadline = hard_deadline
        self._nap_replanned = False
        self._induce_note = None
        self.context.required_wake_time = deadline
        self.cycle.controller.set_session(ctrl_mode, keep_light=plan.keep_light)
        self._persist_session()

    def _maybe_replan_nap(self) -> None:
        """Once sleep onset is CONFIRMED (see ``SleepController.sleep_onset_time``), re-plan the
        active nap against the TRUE sleep window instead of the press-time estimate — the fix for
        the historical bug where a nap's deadline was anchored to button-press time (time in bed)
        rather than onset (time asleep). Runs once per nap session, right when onset first
        confirms. Best-effort: a failure here must never break the control loop; the fallback
        cap / hard wake-by deadline already set by ``_start_nap`` remains the safety backstop."""
        if self.session_mode != "nap" or self._nap_replanned or self._nap_plan_obj is None:
            return
        onset = self.cycle.controller.sleep_onset_time
        if onset is None:
            return
        try:
            from sleepctl.controller.nap import NapRequestKind, replan_on_onset
            deadline_for_replan = self.nap_hard_deadline or self.nap_deadline
            new_plan = replan_on_onset(self._nap_plan_obj, onset, deadline_for_replan, cfg=self.cfg)
            self._nap_plan_obj = new_plan
            self.nap_plan = new_plan.to_dict()
            self._nap_replanned = True
            planned_wake = onset + timedelta(minutes=new_plan.target_sleep_min)
            if new_plan.request_kind == NapRequestKind.WAKE_BY.value and self.nap_hard_deadline:
                # The hard wake-by wall is NEVER exceeded; a trap-zone "cap short" resolution
                # (see nap.py) brings the effective planned wake EARLIER than the wall the user
                # asked for -- min() enforces that (planned_wake is always <= the wall by
                # construction of replan_on_onset, so this is a no-op outside the trap case).
                self.nap_deadline = min(self.nap_hard_deadline, planned_wake)
            else:
                self.nap_deadline = planned_wake
            self.context.required_wake_time = self.nap_deadline
            self.cycle.controller.update_nap_keep_light(new_plan.keep_light)
            self._log(f"nap replanned on confirmed onset: {new_plan.headline} "
                      f"(realized_sleep_min={new_plan.realized_sleep_min}, "
                      f"deadline={self.nap_deadline.isoformat()})")
            self._emit_event("nap", "info", "nap_replanned", new_plan.headline, new_plan.to_dict())
        except Exception as exc:
            self._skip("nap replan", exc)

    def _maybe_log_onset(self) -> None:
        """Persist the confirmed SleepOnsetEvent -- WHICH signals actually fired, not just that
        onset happened -- the first time it appears each bed session.

        Before this, ``SleepOnsetDetector.evaluate()``'s result was kept on the controller as
        ``last_onset_event`` and then never read by anything: no log line, no DB row, nothing a
        later audit (or a person asking "did fall-asleep detection use the accelerometer last
        night") could query. The timestamp survived (into ``sleep_onset_latency_min`` via
        ``_bed_entry_time``), but the EVIDENCE -- which of stillness/hr_drop/hrv_rise/
        respiration_regular actually voted -- was computed live and discarded every single
        night. Runs alongside ``_maybe_replan_nap`` at both tick call sites."""
        try:
            event = self.cycle.controller.last_onset_event
            if event is None or event.timestamp == self._onset_logged_ts:
                return
            self._onset_logged_ts = event.timestamp
            self._log(f"sleep onset confirmed at {event.timestamp.isoformat()} "
                      f"(confidence={event.confidence:.2f}, signals={event.signals})")
            self._emit_event("sleep", "info", "onset_confirmed",
                             f"onset confirmed on {len(event.signals)} signal(s): "
                             f"{', '.join(event.signals)}",
                             {"timestamp": event.timestamp.isoformat(),
                              "confidence": event.confidence, "signals": event.signals,
                              "latency_min": event.latency_min})
        except Exception as exc:
            self._skip("onset event log", exc)

    def _end_session(self) -> None:
        self.session_mode = "night"
        self.nap_plan, self.nap_deadline = None, None
        self._nap_plan_obj, self.nap_start = None, None
        self.nap_hard_deadline, self._nap_replanned = None, False
        self._induce_note = None
        self._onset_logged_ts = None
        self.context.required_wake_time = None
        self.cycle.controller.set_session("night", keep_light=False)
        self._persist_session_clear()

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

    def _skip(self, subsystem: str, exc, note: str = "") -> None:
        """Record a SWALLOWED subsystem failure, then log it.

        The bare ``_log`` this replaces was invisible twice over: it no-ops entirely when
        ``verbose`` is off, and even when on it lands in daemon.log, which the watchdog overwrites
        on the next restart. A subsystem could therefore fail on every tick all night while /diag,
        the published health snapshot and the verdict all stayed green — the loop is fine, the
        feature just isn't running. ``sleepctl.degradation`` persists the tally so that shows up.
        See the ``degraded`` diagnostics check."""
        try:
            from sleepctl import degradation

            degradation.record(subsystem, exc, repo=self.repo)
        except Exception:
            pass
        self._log(f"{subsystem} skipped: {exc}" + (f" ({note})" if note else ""))

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
            # Reach-time (traverse) model: prefers the measured self-test rates and the continuous
            # thermal_samples dataset. Makes the onset cascade's warm-pulse phase long enough to be
            # felt (warming-from-cold is slow) and widens — never shortens — the wake warm-up runway.
            try:
                from sleepctl.controller.thermal_latency import ThermalLatencyModel
                controller.set_induction_latency(ThermalLatencyModel.from_repo(self.repo))
            except Exception as exc:
                self._skip("induction latency", exc)
            # In-bed resting-physiology baseline → arousal/wake-risk anchor.
            controller.set_resting_baseline(self.repo.get_resting_baseline())
            # Personal comfort mapping → the controller's neutral is what YOU feel neutral, not the
            # device's water-scale default.
            comfort = self.repo.get_comfort_profile()
            if comfort and comfort.get("neutral_f") is not None:
                # set_measured_neutral (not a bare assignment) so the controller KNOWS this
                # neutral came from the user rather than the population default. Without that
                # flag the hot-sleeper cool bias is stacked on top of a neutral already measured
                # from a hot sleeper -- double-counting that resolved to 62.5 F on a night whose
                # own data showed 63-64 F waking them.
                controller.thermal.set_measured_neutral(float(comfort["neutral_f"]))
            # ATTACH the whole profile, not just the neutral. Without this
            # ``controller.comfort_profile`` stays None forever, and BOTH consumers of the
            # personal comfort BAND silently no-op: the guardrail's out-of-band check
            # (guardrail.py's documented reason (b)) and the hard comfort clamp in decide().
            # set_comfort_profile existed but was called from nowhere in production -- so the
            # cool/warm edges the comfort sweep exists to produce were being computed, stored,
            # and then ignored.
            controller.set_comfort_profile(comfort)
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
                decide_warm_pulse, learn_cold_settle, learn_onset, next_cold_settle_f,
                next_onset_warm_f, onset_records)
            yday = datetime.now().timetuple().tm_yday
            ons = learn_onset(onset_records(self.repo),
                              base_f=self.cfg.tunables.onset_warm_nudge_f, mode=mode)
            self._onset_warm_f = next_onset_warm_f(ons.onset_warm_f, yday)
            controller.set_onset_warm(self._onset_warm_f)
            # 3-phase onset opener: learn the really-cold depth (per night-type) + explore, and
            # A/B whether tonight runs the brief warm pulse (both arms keep getting sampled).
            cs = learn_cold_settle(onset_records(self.repo),
                                   base_f=self.cfg.tunables.onset_cold_settle_temp_f, mode=mode)
            self._onset_cold_settle_f = next_cold_settle_f(cs.onset_cold_settle_f, yday)
            controller.set_onset_cold_settle(self._onset_cold_settle_f)
            self._warm_pulse_on, _ = decide_warm_pulse(onset_records(self.repo), yday)
            controller.set_warm_pulse_arm(self._warm_pulse_on)
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
            self._skip("learned profile load", exc)
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
            self._skip("night-type planning", exc)
        # Night TYPE is only known now (plan_night just classified it) -- the randomized efficacy
        # micro-trial's eligibility gate needs that, so it's applied here, not at daemon start-up.
        self._apply_efficacy_micro_trial()
        self._apply_thermal_dose_trial()

    def _apply_thermal_dose_trial(self) -> None:
        """n-of-1 thermal DOSE-RESPONSE trial: randomize tonight's maintenance temperature OFFSET
        across a comfort-clamped ladder, so we can measure THIS user's personal response curve
        (primary outcome: wake events) instead of shipping population defaults. Off unless
        ``cfg.thermal_trial.enabled`` -- it changes what the bed does overnight, so it is opt-in.

        Applied AFTER the efficacy micro-trial, and DELIBERATELY SKIPPED on a sham night: a sham
        night is defined as a neutral do-no-harm hold, so layering an experimental offset on top
        would both corrupt that arm and confound the two experiments with each other. Same for a
        HELD night from the older standing trial. When we skip, we say so, so the audit trail
        shows why a night carries no thermal arm."""
        try:
            trial_cfg = getattr(self.cfg, "thermal_trial", None)
            if trial_cfg is None or not getattr(trial_cfg, "enabled", False):
                return
            # Never stack this on top of another experiment's arm (validity + do-no-harm).
            eff = (self.efficacy_trial_arm or {}).get("arm")
            if eff == "sham":
                self._log("thermal dose-trial: skipped (efficacy micro-trial assigned SHAM tonight)")
                return

            from sleepctl.ml.thermal_trial import apply_trial_arm
            base = self.cycle.controller.thermal.profile
            context = {"night_type": self.context.night_type, "session_mode": self.session_mode}
            prof, info = apply_trial_arm(
                self.repo, self.cfg, datetime.now().date().isoformat(), context, base)
            self.cycle.controller.set_setpoints(prof)
            self.thermal_trial_arm = info
            self._log(f"thermal dose-trial: offset {info.get('offset_f'):+.2f}F "
                      f"(eligible={info.get('eligible')})")
        except Exception as exc:
            self._skip("thermal dose-trial apply", exc)

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
                            self._skip("wake window selection", exc)
                    self._persist_wake()
                elif t == "clear_wake":
                    cs.apply_clear_wake(self)
                    self._persist_wake()
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
            self._skip("ambient precompensation", exc)

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
                self._skip("wearable fusion", exc)
            # Attach the DENSE trailing HR/movement series (~1 sample/2 s from the Verity) for the
            # wearable sleep-stager. The frame fields alone are ~1 sample/minute, which washes out
            # the short-timescale HR variability staging relies on. Purely additive and
            # best-effort: on any failure the stager falls back to the frame buffer.
            try:
                reader = getattr(self.wearable, "read_history", None)
                if reader is not None:
                    hist = reader(minutes=45.0) or {}
                    if hist.get("hr"):
                        frame.hr_history = hist["hr"]
                    if hist.get("activity"):
                        frame.activity_history = hist["activity"]
                        # Which SCALE that series is on. bridge.sensor_history_series returns
                        # "counts" for the wearable's own PIM and "phone_index" for the iPhone's
                        # 0..1 index -- a ~17x difference measured on real data. Any absolute
                        # motion threshold is meaningless without it, so thread it through rather
                        # than letting the controller guess.
                        frame.activity_units = hist.get("activity_units")
            except Exception as exc:
                self._skip("dense sensor history", exc)
        return frame

    def _refresh_shift_plan(self) -> None:
        """Advisory cross-shift sleep-debt plan: debt + strategy from recent nights, plus banking /
        prophylactic-nap logic from the next shift (auto-synced from the work calendar when
        connected, else the manual next-shift hint — see ``services.sync_calendar_to_shift``)."""
        try:
            from app import services
            self.shift_plan = services.shift_plan_view(self.repo)
        except Exception as exc:
            self._skip("shift plan", exc)
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
                self._skip("calendar auto-wake", exc)

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

        # Persist a timestamped thermal-response sample ONLY while the bed is really actuating
        # toward a target (a genuine heat/cool event). Skips OFF/paused/away and skips ticks
        # parked at setpoint (|delta| <= 3) to keep the dataset lean. Best-effort: wrapped so a
        # logging failure never disrupts control. Called from both the control (~60s) and
        # command ticks — a few samples/min during a move is exactly the resolution we want.
        try:
            if (self.power_on and not self.paused and not self.away and frame is not None
                    and frame.target_level is not None and frame.device_level is not None):
                delta = frame.target_level - frame.device_level
                direction = "heating" if delta > 3 else ("cooling" if delta < -3 else "hold")
                if direction != "hold":
                    state = self._prev_state.value if self._prev_state is not None else None
                    bridge.record_thermal_sample(self.repo.conn, {
                        "ts": now.isoformat(),
                        "device_level": frame.device_level,
                        "target_level": frame.target_level,
                        "delta_level": delta,
                        "direction": direction,
                        "bed_temp_f": frame.bed_temp_f,
                        "room_temp_f": frame.room_temp_f,
                        "state": state,
                        "session_mode": self.session_mode,
                    })
        except Exception as exc:
            self._skip("thermal sampling", exc)

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
                      # True once the Pod refused an alarm WRITE (subscription-gated). Published
                      # so /diag, the health branch and the preflight can all say plainly that
                      # vibration is unavailable and the wake is running on light + warmth.
                      "alarm_write_denied": bool(getattr(self, "_alarm_write_denied", False)),
                      # WHY the sensed bed temperature was unavailable on the last read (None =
                      # it was available). bed_temp_f has been None on every sample of every
                      # captured night, holding the thermal loop permanently open, and the cause
                      # was not knowable from outside the box. Published so /diag and the health
                      # branch can name it instead of leaving it to be inferred.
                      "bed_temp_reason": getattr(self.client, "last_bed_temp_reason", None),
                      "nap": self.nap_plan,
                      "nap_deadline": self.nap_deadline.isoformat() if self.nap_deadline else None,
                      # Surfaced constraint when "help me fall asleep" is pressed with little
                      # sleep opportunity left before a wake deadline (see
                      # _apply_induce_deadline_awareness); None outside that situation.
                      "induce_note": self._induce_note,
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
                      # Tonight's induction program (cold opener depth, warm-nudge magnitude, and
                      # whether the brief warm pulse is armed) so the dashboard can show it.
                      "onset_warm_f": getattr(self, "_onset_warm_f", None),
                      "onset_cold_settle_f": getattr(self, "_onset_cold_settle_f", None),
                      "warm_pulse_on": getattr(self, "_warm_pulse_on", None),
                      "device_error": error,
                      # Consecutive tick-error count + a rolling window of their reprs -- read by
                      # app.services.evaluate_and_sync_health_alerts (via runtime_state.extra) so a
                      # sustained daytime cloud outage crosses health_monitor's repeated-error
                      # threshold and pushes a critical alert, instead of silently retrying forever.
                      "consec_errors": self._consec_errors,
                      "recent_errors": list(self._recent_errors),
                      "data_age_s": round(frame.data_age_seconds, 1)
                      if frame is not None and frame.data_age_seconds is not None else None,
                      "telemetry_stale": bool(
                          frame is not None and frame.data_age_seconds is not None
                          and frame.data_age_seconds > self.cfg.tunables.telemetry_stale_seconds),
                      # Age of the WEARABLE cardiac feed specifically. `data_age_s` above tracks
                      # the Pod's telemetry, which keeps flowing happily while the band is dead
                      # -- so it cannot detect the failure that actually matters here, where the
                      # Verity is the only source of stage/HR/movement. Feeds health_monitor's
                      # `cardiac_sensor_lost` alert.
                      "cardiac_age_s": self._cardiac_age_s(),
                      # Bed presence drives the phone supplement: in_bed -> the phone feed is
                      # fused; out of bed -> it's ignored automatically.
                      "bed_presence": frame.presence if frame is not None else None,
                      "phone_fused": self._phone_fused,
                      # "estimated" => the sleep stage steering the controller was DERIVED from an
                      # external HR sensor's vitals (no Pod stage); "sensor" => a real device stage.
                      "stage_source": (decision.log_payload or {}).get("stage_source")
                      if decision else None,
                      # tonight's n-of-1 thermal dose-response arm (None when the trial is off,
                      # the night is ineligible, or it was skipped to avoid confounding a sham night)
                      "thermal_trial": self.thermal_trial_arm,
                      "wake_action": (decision.log_payload or {}).get("wake_action")
                      if decision else None},
        }


    # ---- wake persistence across daemon restarts ------------------------------------------
    # `self.wake` used to live only in memory, so ANY daemon restart silently dropped the
    # alarm -- and the watchdog restarts the daemon on its own (stale heartbeat, deploy,
    # self-update). Observed live: a wake set at bedtime was gone twice before morning, each
    # time with no error and no user-visible signal. The wake deadline is the one piece of
    # state the whole night is planned around, so it is persisted here and restored on boot.
    _WAKE_KV_KEY = "daemon_wake_state"

    def _persist_wake(self) -> None:
        """Best-effort: never let a persistence failure break the control loop."""
        try:
            payload = None
            if self.wake and self.context.required_wake_time is not None:
                payload = {"wake": self.wake,
                           "required_wake_time": self.context.required_wake_time.isoformat()}
            self.repo.conn.execute(
                "INSERT INTO settings_kv (key, value) VALUES (?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (self._WAKE_KV_KEY, json.dumps(payload) if payload else ""))
            self.repo.conn.commit()
        except Exception as exc:
            self._skip("wake persistence", exc)

    _SESSION_KV_KEY = "daemon_session_state"

    #: A restored session older than this is treated as stale and dropped, so a session left
    #: open days ago cannot resurrect itself. Comfortably longer than any real night or nap.
    _SESSION_MAX_AGE_H = 16.0

    def _cardiac_age_s(self):
        """Seconds since the last wearable HR sample, or None when there has never been one.

        Read-only and best-effort: this feeds an alert, and an alert must never be able to break
        the control loop it is watching.
        """
        try:
            s = bridge.read_cardiac_sample(self.repo.conn)
            age = (s or {}).get("age_seconds")
            return round(float(age), 1) if age is not None else None
        except Exception:
            return None

    def _persist_session(self) -> None:
        """Persist the ACTIVE session (induce / nap) across a daemon restart.

        The wake time was already persisted; the session was not -- so any restart dropped the
        controller straight back to IDLE. That is unrecoverable on this deployment, because the
        state machine only leaves IDLE on ``presence is True`` and this Pod has NEVER once
        reported presence True (checked over every sample ever recorded). The session is started
        by an explicit "Help me fall asleep" command, so once it is lost nothing re-arms it and
        the rest of the night silently goes uncontrolled -- including the morning wake, which can
        only fire from inside a session.

        Observed 2026-08-05: a restart at 23:30:17 took a live MAINTENANCE session to IDLE two
        seconds later while the user was asleep, and it stayed there until the command was
        re-issued by hand.
        """
        try:
            payload = None
            # "night" is the RESTING default (_end_session sets it), not an active session --
            # only "induce" and "nap" are things a restart could lose.
            if self.session_mode in ("induce", "nap"):
                payload = {
                    "session_mode": self.session_mode,
                    "started": datetime.now().isoformat(),
                    "nap_plan": self.nap_plan,
                    "nap_deadline": (self.nap_deadline.isoformat()
                                     if self.nap_deadline else None),
                    "nap_hard_deadline": (self.nap_hard_deadline.isoformat()
                                          if self.nap_hard_deadline else None),
                    "nap_start": self.nap_start.isoformat() if self.nap_start else None,
                }
            self.repo.conn.execute(
                "INSERT INTO settings_kv (key, value) VALUES (?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (self._SESSION_KV_KEY, json.dumps(payload) if payload else ""))
            self.repo.conn.commit()
        except Exception as exc:
            self._skip("session persistence", exc)

    def _restore_session(self) -> None:
        """Re-arm a persisted session on startup so a restart no longer ends the night."""
        try:
            row = self.repo.conn.execute(
                "SELECT value FROM settings_kv WHERE key=?", (self._SESSION_KV_KEY,)).fetchone()
            if not row or not row[0]:
                return
            data = json.loads(row[0])
            mode = data.get("session_mode")
            if not mode:
                return
            started = data.get("started")
            if started:
                age_h = (datetime.now() - datetime.fromisoformat(started)).total_seconds() / 3600.0
                if age_h > self._SESSION_MAX_AGE_H:
                    self._log(f"persisted {mode} session is {age_h:.1f}h old; not restoring")
                    self._persist_session_clear()
                    return
            if mode == "nap":
                # A nap is anchored to a deadline; without one there is nothing to restore to.
                dl = data.get("nap_deadline")
                if not dl or datetime.fromisoformat(dl) <= datetime.now():
                    self._log("persisted nap deadline has passed; not restoring")
                    self._persist_session_clear()
                    return
                self.session_mode = "nap"
                self.nap_plan = data.get("nap_plan")
                self.nap_deadline = datetime.fromisoformat(dl)
                hd = data.get("nap_hard_deadline")
                self.nap_hard_deadline = datetime.fromisoformat(hd) if hd else None
                ns = data.get("nap_start")
                self.nap_start = datetime.fromisoformat(ns) if ns else None
                self.mode, self.power_on, self.paused, self.away = "auto", True, False, False
                self.context.required_wake_time = self.nap_deadline
                keep_light = bool((self.nap_plan or {}).get("keep_light"))
                ctrl_mode = "nap_power" if keep_light else "nap_cycle"
                self.cycle.controller.set_session(ctrl_mode, keep_light=keep_light)
            else:
                self._start_induce()
            self._log(f"restored {mode} session after daemon restart")
        except Exception as exc:
            self._skip("session restore", exc)

    def _persist_session_clear(self) -> None:
        try:
            self.repo.conn.execute(
                "INSERT INTO settings_kv (key, value) VALUES (?,'') "
                "ON CONFLICT(key) DO UPDATE SET value=''", (self._SESSION_KV_KEY,))
            self.repo.conn.commit()
        except Exception:
            pass

    def _restore_wake(self) -> None:
        """Reload a persisted wake on startup. Ignores one already in the past -- a stale
        deadline from a previous night must not arm a phantom alarm."""
        try:
            row = self.repo.conn.execute(
                "SELECT value FROM settings_kv WHERE key=?", (self._WAKE_KV_KEY,)).fetchone()
            if not row or not row[0]:
                return
            data = json.loads(row[0])
            wk = datetime.fromisoformat(data["required_wake_time"])
            if wk <= datetime.now():
                self._log("persisted wake time is in the past; not restoring")
                return
            self.wake = data.get("wake")
            self.context.required_wake_time = wk
            win = (self.wake or {}).get("window_min")
            if win:
                self.cycle.controller.set_wake_window(int(win))
            self._log(f"restored wake {wk.strftime('%H:%M')} "
                      f"(window {win} min) after daemon restart")
        except Exception as exc:
            self._skip("wake restore", exc)

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
            self._skip("failure-alert check", exc)
        # Separately: the once-an-evening readiness check. The detector above is a NIGHTTIME
        # pager — right for "the reservoir just ran dry", useless for "the daemon died this
        # afternoon", which you can only act on BEFORE you're in bed. Self-gated to the pre-bed
        # window and once per calendar day, so calling it every tick costs nothing.
        try:
            from app import services
            services.check_pre_bed_readiness(self.repo)
        except Exception as exc:
            self._skip("pre-bed readiness check", exc)

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
        self._refresh_wake_plug()

    def _refresh_wake_plug(self) -> None:
        """(Re)build the NON-Hue wake-therapy plug driver from its stored config.

        Separate from the Hue driver because it is a different transport, but it is driven from
        the SAME wake decision in _drive_dawn -- the orchestrator stays the only thing that
        decides WHEN the lamp fires. Rebuilt only when the config changes."""
        try:
            from app import services
            c = services._get_plug_config(self.repo)
            sig = (c["enabled"], c["backend"], c["max_on_min"],
                   tuple(sorted((c["config"] or {}).items())))
            if sig == getattr(self, "_plug_sig", None):
                return
            self._plug_sig = sig
            if c["enabled"] and c["config"]:
                from sleepctl.adapters.smart_plug import SmartPlugTherapyDriver
                self.plug_driver = SmartPlugTherapyDriver(
                    c["backend"], c["config"], max_on_min=c["max_on_min"])
                self._log(f"wake therapy plug enabled (backend={c['backend']}, "
                          f"max_on={c['max_on_min']:.0f} min)")
            else:
                # Turn it OFF on the way out -- disabling the feature must not strand an
                # energised lamp.
                old = getattr(self, "plug_driver", None)
                if old is not None:
                    try:
                        old.off()
                    except Exception:
                        pass
                self.plug_driver = None
        except Exception as exc:
            self._log(f"wake plug refresh skipped: {exc}")

    def _drive_dawn(self, decision) -> None:
        plug = getattr(self, "plug_driver", None)
        if not self.hue_driver and not plug:
            return
        la = (decision.log_payload or {}).get("wake_action") if decision else None
        # The SAME wake decision drives both transports, so a Hue lamp and a generic Wi-Fi plug
        # can never disagree about whether it is time to get up.
        should = bool(la.get("should_wake")) if la else False
        try:
            if self.hue_driver:
                if la is None:                   # outside the wake window -> everything off
                    self.hue_driver.set_level(0.0)
                    self.hue_driver.set_therapy(False)
                else:
                    self.hue_driver.set_level(float(la.get("light_level", 0.0)))  # sunrise ramp
                    self.hue_driver.set_therapy(should)                           # therapy at wake
        except Exception as exc:
            self._log(f"hue drive skipped: {exc}")
        try:
            if plug:
                plug.set_therapy(should)
        except Exception as exc:
            self._log(f"wake plug drive skipped: {exc}")

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

        # Push the wake to the phone the moment the orchestrator commits to it. The Pod's
        # vibration alarm is subscription-gated (403), so without this the wake is the thermal
        # ramp alone -- silent, and easily slept through. Best-effort and idempotent per night:
        # a push failure must never disturb the control loop that owns the ramp itself.
        if la.get("should_wake"):
            try:
                from app import services as _svc
                mins_early = None
                dl = la.get("target_time")
                if dl:
                    try:
                        mins_early = max(0.0, (datetime.fromisoformat(dl)
                                               - now).total_seconds() / 60.0)
                    except Exception:
                        pass
                res = _svc.deliver_wake_push(
                    self.repo, stage=(self._wake_last_stage or st),
                    minutes_early=mins_early,
                    night_date=self.cycle.night_date(now), now=now)
                if res.get("sent"):
                    self._emit_event("wake", "info", "wake_push_sent",
                                     "Wake pushed to your phone.", res)
            except Exception as exc:
                self._skip("wake push", exc)
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
                "onset_cold_settle_f": getattr(self, "_onset_cold_settle_f", None),
                "warm_pulse_on": getattr(self, "_warm_pulse_on", None),
                "night_type": getattr(self.context, "night_type", None)}

    def _flush_wake_log(self) -> None:
        if not self._pending_wake:
            return
        try:
            nights = self.repo.recent_nights(1)
            date = nights[-1].date if nights else datetime.now().date().isoformat()
            bridge.write_wake_log(self.repo.conn, {"date": date, **self._pending_wake})
        except Exception as exc:
            self._skip("wake log", exc)
        finally:
            self._pending_wake, self._wake_last_stage = None, None

    async def _heal_away(self) -> None:
        """Keep away mode OFF unless the *user* commanded it. Away idles the pod to
        target 0 and blinds side resolution; Eight Sleep's own app/Autopilot can turn
        it on out from under us. Throttled to one authoritative read every ~5 min.
        No-op in dry-run and for clients without away introspection (e.g. simulator)."""
        if self.dry_run or self.away:
            return  # user-commanded away is honored; dry-run never writes
        if not hasattr(self.client, "is_away"):
            return
        mono = asyncio.get_event_loop().time()
        if mono - getattr(self, "_away_heal_mono", 0.0) < 300.0:
            return
        self._away_heal_mono = mono
        try:
            if await self.client.is_away():
                await self.client.set_away_mode(False)
                if hasattr(self.client, "turn_on_side"):
                    await self.client.turn_on_side()
                self._log("away mode was ON without a user command -> cleared (pod re-enabled).")
                self._emit_event("device", "warn", "away_auto_cleared",
                                 "away mode was enabled externally; auto-cleared", {})
        except Exception as exc:  # never let self-heal wedge the loop
            self._log(f"away self-heal skipped: {exc!r}")

    # ------------------------------------------------------------------ cycles
    async def control_tick(self) -> None:
        await self._apply_commands()
        await self._heal_away()
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
        await self.client.update()
        frame = self._read_frame()
        now = self.client.now()
        self._record_thermal(frame, now)
        self._refresh_precomp(now)
        self._refresh_shift_plan()
        decision = None
        if self.power_on and not self.paused and not self.away:
            decision = self.cycle.decide(frame, self.context, now)
            self._maybe_replan_nap()
            self._maybe_log_onset()
            if self.mode == "manual" and self.manual_target_f is not None:
                await self._set_level(self.cycle.controller.thermal.to_level(self.manual_target_f))
            elif self.mode == "auto":
                level = self.cycle.pending_level(decision, frame, now)
                if level is not None:
                    await self._set_level(level)
                alarm = self.cycle.pending_alarm()
                if alarm is not None and not self.dry_run:
                    # Confirm-on-success: if this raises (cloud 5xx, token refresh, or the Pod
                    # having no alarm slot), the spec stays pending and the next tick retries it,
                    # rather than the night silently losing its only wake mechanism.
                    #
                    # Contained, too. Unhandled, the failure propagated to the tick handler, which
                    # HOLDS the whole control loop for that tick -- so a missing alarm slot didn't
                    # just cost the alarm, it stopped thermal steering on every tick through the
                    # wake window. The alarm is a backstop for the in-loop escalation ladder;
                    # losing it must never take maintenance down with it.
                    try:
                        await self.client.set_wake_alarm(alarm)
                        self.cycle.mark_alarm_sent()
                        self._alarm_write_denied = False
                    except Exception as exc:
                        # 402/403 is a SERVER-side refusal (Eight Sleep gating the alarm behind a
                        # subscription). No client can work around that, so retrying it every tick
                        # is pointless noise -- record it once, loudly, and stand down. The thermal
                        # wake ramp and the Hue sunrise are driven by us through the ordinary
                        # setpoint/light paths and keep working, so the wake degrades to
                        # light + warmth rather than disappearing. That is worth PAGING about:
                        # for a user who needs silence, vibration was the only tactile cue.
                        msg = str(exc)
                        permanent = "402" in msg or "403" in msg
                        if permanent and not getattr(self, "_alarm_write_denied", False):
                            self._alarm_write_denied = True
                            self.cycle.mark_alarm_sent()   # stop re-offering a refused write
                            self._emit_event(
                                "alert", "warn", "alarm_write_denied",
                                "The Pod refused the alarm write (subscription-gated). Vibration "
                                "is unavailable; waking via the thermal ramp + dawn light only.",
                                {"error": msg[:300]})
                        self._skip("wake alarm programming", exc,
                                   note=("server refused (subscription) — falling back to thermal "
                                         "+ light wake" if permanent else
                                         "retrying next tick; if this says 'no alarm slot', "
                                         "create one wake alarm in the Eight Sleep app once"))
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
        # A nap's deadline is checked LAST, after this tick's decide()/device-actuation already
        # ran with the deadline still armed — so the tick that first crosses it still gets a full
        # wake_orch pass (guaranteed "fire" action, should_wake=True, native alarm) instead of
        # losing it to an end-of-session reset that would wipe required_wake_time out from under
        # this same tick (which would previously drop the deadline-crossing wake action).
        if self.nap_deadline is not None and datetime.now() >= self.nap_deadline:
            self._end_session()

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
            self._maybe_replan_nap()
            self._maybe_log_onset()
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
                # Reconstruct the night from OUR OWN persisted frames, then let any richer
                # upstream field win. The adapters' fetch_night_summary is a stub returning an
                # all-None NightSummary (the Eight Sleep nightly metrics are membership-gated),
                # which made nightly.run() throw here every single night -- silently, via the
                # except below -- leaving `nightly_summaries` permanently EMPTY and starving
                # every learner, efficacy trial and report downstream of it.
                from sleepctl.loop.night_rollup import (merge_night_summary,
                                                        reconstruct_night_summary)
                night = reconstruct_night_summary(self.repo, night_date)
                try:
                    night = merge_night_summary(
                        night, await self.client.fetch_night_summary(night_date))
                except Exception as exc:
                    self._skip("upstream night summary", exc)
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
                # Same for the thermal dose-response arm (no-op when the night was never assigned
                # one -- trial disabled, ineligible night, or skipped as a sham night).
                try:
                    from sleepctl.ml.thermal_trial import record_trial_outcome as record_thermal
                    record_thermal(
                        self.repo, night_date, wake_events=night.wake_events,
                        deep_min=night.deep_min, sleep_efficiency=night.sleep_efficiency,
                        hrv=night.avg_hrv)
                except Exception as exc:
                    self._skip("thermal dose-trial outcome", exc)
                self._emit_event("nightly", "info", "nightly_close_out",
                                 f"nightly close-out ran for {night_date}",
                                 {"night_date": night_date})
                try:
                    # Refresh the dashboard's cached ML recommendation now that a new setpoint
                    # version may exist -- the ONLY place build_status's shown recommendation
                    # should change, since build_status itself never recomputes (see
                    # app.services.cached_ml_recommendation).
                    from app import services as dashboard_services
                    dashboard_services.refresh_ml_recommendation_cache(self.repo)
                except Exception:
                    pass
            except Exception as exc:
                self._skip("nightly close-out", exc)
            # Housekeeping runs on its OWN try: it is the only retention and backup path in the
            # system, and it used to share the block above -- so the learning step failing (which
            # it did every night on the stub summary) silently took pruning and the rotating DB
            # backup down with it.
            try:
                self.repo.prune_events()  # housekeeping: cap event-log growth, once/night
                # High-write tables with no prior retention (raw_samples/decisions/interventions/
                # thermal_samples) -- prune here (once/night), never on the per-tick hot path.
                self.repo.prune_raw_samples()
                self.repo.prune_decisions()
                self.repo.prune_interventions()
                bridge.prune_thermal_samples(self.repo.conn)
                self._maybe_backup()      # rotating DB backup: once/day, gated on-disk
            except Exception as exc:
                self._skip("nightly housekeeping", exc)
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
        # The recurring bedtime SCHEDULE is a SEPARATE mechanism from Autopilot and can hold a
        # target that fights our commanded level even when Autopilot is off/unavailable (seen
        # live via diagnostics_thermal's external_setpoint_conflict on a non-Autopilot account).
        # Attempt to disable it too. On a free account Eight Sleep's own server paywalls that
        # write (verified live: 403 "Subscription required" on PUT .../bedtime), so here this can
        # usually only DISCOVER the conflict, not fix it -- say so distinctly rather than
        # emitting a generic warning that reads like a bug in this code.
        if not self.dry_run and hasattr(self.client, "set_schedule_enabled"):
            try:
                result = await self.client.set_schedule_enabled(False)
                if result.get("ok") and result.get("changed"):
                    self._log("Eight Sleep bedtime SCHEDULE disabled (exclusive control).")
                    self._emit_event("lifecycle", "info", "schedule_disabled",
                                     "Bedtime schedule disabled for exclusive control", {})
                elif result.get("ok"):
                    self._log(f"Eight Sleep bedtime schedule: {result.get('detail')} "
                              "(nothing to change).")
                elif result.get("paywalled"):
                    self._log("Eight Sleep bedtime schedule is still ACTIVE and CANNOT be "
                              "disabled via the API on this account -- Eight Sleep's own server "
                              "refuses the write with 'Subscription required'. It may keep "
                              "fighting our commanded level; the only known fix is disabling "
                              "the schedule in the Eight Sleep app itself.")
                    self._emit_event("lifecycle", "warn", "schedule_disable_paywalled",
                                     "Bedtime schedule disable blocked by Eight Sleep's "
                                     "subscription paywall", {})
                else:
                    self._log("WARNING: could not disable the Eight Sleep bedtime schedule "
                              f"({result.get('detail')}) -- it may still fight commanded levels.")
            except Exception as exc:  # pragma: no cover - network dependent
                self._log(f"WARNING: could not disable bedtime schedule: {exc!r}")
        # Reload a wake deadline persisted by a previous daemon instance (see _restore_wake):
        # the watchdog restarts this process on its own, and losing the alarm silently is the
        # worst possible failure for the one thing the night is planned around.
        self._restore_wake()
        # ...and the SESSION itself. Without this a restart dropped a live night to IDLE, which
        # this Pod can never leave on its own (presence has never once read True), so the rest of
        # the night ran uncontrolled and the morning wake -- which only fires from inside a
        # session -- never came.
        self._restore_session()
        # Away mode idles the pod to target 0 (bed does nothing) and poisons side
        # resolution. Something outside our control (Eight Sleep's own app/Autopilot)
        # can enable it -- so the daemon owns this flag: unless the *user* commanded
        # away, ensure it is OFF at startup and keep it off each tick (see _heal_away).
        await self._heal_away()
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
                    self._recent_errors = []
                except Exception as exc:
                    # A transient device/cloud error (timeout, token refresh, 5xx) must NOT
                    # kill the 24/7 loop. Log, surface a degraded snapshot so the dashboard
                    # shows the problem, hold (the device keeps its last safe command), and
                    # back off so we don't hammer a failing API.
                    self._consec_errors += 1
                    self._recent_errors = (self._recent_errors + [repr(exc)])[-_MAX_RECENT_ERRORS:]
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
