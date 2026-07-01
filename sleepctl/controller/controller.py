"""SleepController — the Decide step of Sense/Decide/Act/Learn.

Given the freshest sensor frame + schedule context + recent history, it advances the
state machine, picks a thermal intent via the matching routine, resolves a safe target
temperature/level, and returns a fully-explained ``Decision``. It never performs device
I/O or persistence — the runtime loop acts on the returned Decision.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sleepctl.config import AppConfig
from sleepctl.controller.architecture import ArchitectureSteering
from sleepctl.controller.induction import InductionRoutine
from sleepctl.controller.maintenance import MaintenanceRoutine, WakeRecoveryRoutine
from sleepctl.controller.arousal import ArousalDetector, ArousalLevel
from sleepctl.controller.precursor import PrecursorDetector
from sleepctl.controller.sleep_onset import SleepOnsetDetector
from sleepctl.controller.smart_wake import SmartWakeRoutine
from sleepctl.controller.state_machine import SleepStateMachine
from sleepctl.controller.wake_risk import WakeRiskAssessor
from sleepctl.controller.thermal import ThermalController
from sleepctl.controller.wake_detection import WakeDetector
from sleepctl.models import (
    ContextRecord,
    ControllerState,
    CorrectionAction,
    Decision,
    NightObjective,
    SensorFrame,
    SleepStage,
    ThermalIntent,
)


class SleepController:
    def __init__(self, cfg: AppConfig, setpoints=None) -> None:
        self.cfg = cfg
        self.sm = SleepStateMachine(cfg)
        self.wake_detector = WakeDetector()
        self.onset_detector = SleepOnsetDetector(cfg)
        self.arousal_detector = ArousalDetector(cfg)
        self.precursor_detector = PrecursorDetector(cfg)
        # Proactive sleep-maintenance: a learned WakeProfile can be attached by the loop.
        self.wake_risk_assessor = WakeRiskAssessor(cfg)
        self.induction = InductionRoutine(cfg)
        self.maintenance = MaintenanceRoutine(cfg)
        self.wake_recovery = WakeRecoveryRoutine(cfg)
        self.smart_wake = SmartWakeRoutine(cfg)
        # In-night architecture steering ("nudge me deeper"): compares the realized deep/REM curve
        # to tonight's personalized ideal and biases the bed deeper when behind + risk is low.
        self.steering = ArchitectureSteering(cfg)
        # Tonight's personalized ideal architecture (set by the daemon from the SleepPlan); the
        # steerer targets these. None -> steering holds (no target to chase).
        self.night_targets = None
        self.est_sleep_min: Optional[float] = None
        # Accrued time-in-stage since onset (the realized architecture so far).
        self._arch_deep_min = 0.0
        self._arch_rem_min = 0.0
        self._arch_light_min = 0.0
        self._arch_last_ts: Optional[datetime] = None
        self._deepen_active = False         # edge-trigger for steer-event logging
        self.last_steer = None              # last SteerDecision (telemetry)
        self.pending_steer_event = None     # consumed + logged by the cycle
        # Deepening-response policy: whether to ACTUATE the deepen nudge tonight. On control
        # ('observe') nights this is False — the steerer still judges + logs a SHADOW event (the
        # n-of-1 control arm) but doesn't cool. Set nightly by the daemon from the learner.
        self.steer_actuate = True
        # Measured thermal effect-latency from the in-bed self-test (minutes for a cool/heat
        # command to fully land). None until the self-test runs. Floors the deepening-response
        # horizon (don't judge "did it deepen?" before the cool has taken effect) and lengthens the
        # wake warm-up runway so the bed is actually warm by the wake time.
        self.measured_cool_lag_min: Optional[float] = None
        self.measured_heat_lag_min: Optional[float] = None
        # Multi-signal, escalating, inertia-minimizing wake orchestrator (uses the calibrated
        # sleep/wake classifier + the fused fast movement; never oversleeps the deadline).
        from sleepctl.controller.sleep_wake import SleepWakeClassifier
        from sleepctl.controller.wake_orchestrator import WakeConfig, WakeOrchestrator
        self.wake_orch = WakeOrchestrator(WakeConfig.from_tunables(cfg.tunables),
                                          classifier=SleepWakeClassifier(cfg))
        self.last_wake_action = None        # exposed for telemetry/dashboard
        self.wake_debt_min = 0.0            # cumulative sleep debt -> debt-adaptive wake strategy
        # The learnable setpoint profile (updated nightly by the learning loop / ML).
        self.thermal = ThermalController(cfg, profile=setpoints)

        self._bed_entry_time: Optional[datetime] = None
        self._sleep_onset_time: Optional[datetime] = None  # accurate fall-asleep time
        self._last_target_f: float = cfg.tunables.neutral_temp_f
        # Session mode: "night" | "induce" | "nap_power" | "nap_cycle". Power naps keep the bed
        # light so slow-wave sleep (and its grogginess on waking) doesn't set in.
        self.session_mode = "night"
        self.session_keep_light = False
        self.last_wake_event = None
        self.last_onset_event = None
        self.last_arousal = None          # last ArousalAssessment
        self.last_wake_risk = None        # last WakeRisk
        self.last_precursor = None        # last PrecursorAssessment (leading-edge drift)
        self.last_precursor_profile = None  # learned personalized awakening-precursor trajectory
        self._arousal_started: Optional[datetime] = None  # for re-settling latency
        self.last_resettle_latency_min: Optional[float] = None
        self._anticipatory_active = False
        self.pending_precool_event = None  # consumed + logged by the cycle
        self.should_wake = False
        self.pending_wake_alarm = None  # WakeAlarmSpec to program (vibration + heat), once

    def _objective(self, context: Optional[ContextRecord]) -> NightObjective:
        if context is None:
            return NightObjective.OPTIMIZE
        nt = (context.night_type or "").lower()
        if nt in ("recovery", "off", "off_day", "rest"):
            return NightObjective.RECOVERY
        if nt in ("work", "constrained", "short") or context.is_short_sleep_day:
            return NightObjective.DAMAGE_CONTROL
        return NightObjective.OPTIMIZE

    def decide(
        self,
        frame: SensorFrame,
        context: Optional[ContextRecord],
        recent: list[SensorFrame],
        now: datetime,
        baselines=None,
    ) -> Decision:
        cfg = self.cfg
        objective = self._objective(context)
        required_wake = context.required_wake_time if context else None
        current_f = frame.bed_temp_f if frame.bed_temp_f is not None else self._last_target_f

        # --- stale-data guard: never act on stale/low-confidence data -----------
        if frame.is_stale(cfg.tunables.stale_data_seconds):
            level = self.thermal.to_level(self._last_target_f)
            return self._build(
                now, self.sm.state, objective, ThermalIntent.STABILIZE,
                self._last_target_f, level, CorrectionAction.HOLD,
                "data stale; holding last command", 0.3, frame,
                wake_signals=[],
            )

        # --- graded arousal detection (maintenance: detect + grade disturbances) -
        sleep_hr_base, sleep_hrv_base = self._sleep_baseline(recent)
        arousal = None
        wake_detected = False
        self._preempt_cool = False
        if self.sm.state in (ControllerState.MAINTENANCE, ControllerState.WAKE_RECOVERY):
            arousal = self.arousal_detector.assess(
                frame, recent, now, sleep_hr_base, sleep_hrv_base)
            self.last_arousal = arousal
            wake_detected = arousal.is_awakening
            wake_event = arousal.wake_event
            # Proactive prevention: in maintenance, watch for wake PRECURSORS and pre-empt
            # with a gentle cooling assist before a disturbance becomes an awakening.
            if self.sm.state is ControllerState.MAINTENANCE and not wake_detected:
                mins_since_onset = (
                    (now - self._sleep_onset_time).total_seconds() / 60.0
                    if self._sleep_onset_time is not None else None)
                risk = self.wake_risk_assessor.assess(
                    frame, recent, now, target_temp_f=self._last_target_f,
                    sleep_hr_baseline=sleep_hr_base,
                    minutes_since_onset=mins_since_onset)
                self.last_wake_risk = risk
                # Leading-edge: detect the slow pre-arousal drift (HR creep, HRV decay,
                # building restlessness, bed warming) over a short window — earlier than the
                # point-in-time wake-risk score.
                precursor = self.precursor_detector.detect(
                    frame, recent, now, sleep_hr_base, sleep_hrv_base)
                self.last_precursor = precursor
                # Pre-empt on rising risk OR a leading-edge precursor OR a micro-arousal.
                self._preempt_cool = risk.preempt or precursor.should_preempt or (
                    arousal.level is ArousalLevel.MICRO and frame.stage is not SleepStage.DEEP)
                # Edge-trigger a pre-cool efficacy event when anticipatory cooling first
                # fires for a window (so the lead-time learner can later score prevention).
                anticip = next((r for r in risk.reasons if r.startswith("anticipatory_")), None)
                if anticip and not self._anticipatory_active:
                    wtype = anticip[len("anticipatory_"):]
                    eta, _ = self.wake_risk_assessor.profile.next_window_eta(
                        now, mins_since_onset)
                    lead = (self.wake_risk_assessor.lead_profile.lead_for(wtype)
                            if self.wake_risk_assessor.lead_profile else None)
                    self.pending_precool_event = {
                        "ts": now, "window_type": wtype,
                        "lead_used_min": lead if lead is not None else 0.0,
                        "eta_min": eta if eta is not None else 0.0,
                    }
                self._anticipatory_active = bool(anticip)
        else:
            wake_event = None
        self.last_wake_event = wake_event

        # Re-settling latency: time from an awakening to physiology re-stabilising.
        if wake_detected and self._arousal_started is None:
            self._arousal_started = now
        elif (not wake_detected and self._arousal_started is not None
              and self.sm.state is ControllerState.MAINTENANCE):
            self.last_resettle_latency_min = (
                now - self._arousal_started).total_seconds() / 60.0
            self._arousal_started = None

        # --- accurate sleep-onset detection (asleep vs lying in bed awake) -------
        if self._bed_entry_time is None and frame.presence:
            self._bed_entry_time = now
            self.onset_detector.reset()
            self._sleep_onset_time = None
        onset_confirmed = None
        if self._sleep_onset_time is None and self.sm.state in (
            ControllerState.INDUCTION, ControllerState.IDLE, ControllerState.CALIBRATION,
        ):
            onset_event = self.onset_detector.evaluate(
                frame, recent, now, bed_entry_time=self._bed_entry_time)
            if onset_event is not None:
                self._sleep_onset_time = onset_event.timestamp
                self.last_onset_event = onset_event
            onset_confirmed = onset_event is not None

        # --- advance state machine ---------------------------------------------
        state = self.sm.transition(frame, now, wake_detected, required_wake,
                                   onset_confirmed=onset_confirmed)

        minutes_in_bed = (
            (now - self._bed_entry_time).total_seconds() / 60.0
            if self._bed_entry_time
            else 0.0
        )

        # --- accrue the realized architecture (drives in-night steering) -------
        if self._sleep_onset_time is not None and frame.presence is not False:
            self._accrue_architecture(now, frame.stage)

        # --- pick thermal intent per state -------------------------------------
        self.should_wake = False
        self.last_wake_action = None      # only set inside WAKE_WINDOW (drives lights/therapy)
        if state in (ControllerState.IDLE, ControllerState.CALIBRATION):
            # Night ended / out of bed: reset onset tracking for the next night.
            if frame.presence is False:
                self._bed_entry_time = None
                self._sleep_onset_time = None
                self.onset_detector.reset()
                self.wake_orch.reset()
                self._reset_architecture()
            intent = ThermalIntent.NEUTRAL
        elif state is ControllerState.INDUCTION:
            intent = self.induction.step(frame, objective, minutes_in_bed)
        elif state is ControllerState.MAINTENANCE:
            # In-night architecture steering: compare the realized deep/REM curve to tonight's
            # personalized ideal and, when behind on deep + light + wake-risk LOW, nudge deeper —
            # reconciled with the wake-up trajectory (stands down near the deadline).
            deepen = self._evaluate_steering(now, frame, wake_detected, minutes_in_bed,
                                             required_wake)
            intent = self.maintenance.step(frame, objective,
                                           preempt_cool=getattr(self, "_preempt_cool", False),
                                           keep_light=self.session_keep_light, deepen=deepen)
        elif state is ControllerState.WAKE_RECOVERY:
            self._deepen_active = False     # an awakening breaks any active deepen maneuver
            intent = self.wake_recovery.step(frame)
        elif state is ControllerState.WAKE_WINDOW:
            # Multi-signal orchestrator: fuse the calibrated P(wake) with stage to catch a real
            # light-sleep moment early, run the thermal dawn, escalate vibration silently, and
            # guarantee the deadline. Falls back to stage-only when data is stale.
            stale = (frame.data_age_seconds is not None
                     and frame.data_age_seconds > self.cfg.tunables.telemetry_stale_seconds)
            action = self.wake_orch.evaluate(
                now, frame, recent, required_wake,
                hr_base=sleep_hr_base, hrv_base=sleep_hrv_base, data_stale=stale,
                debt_min=self.wake_debt_min)
            self.last_wake_action = action
            intent, self.should_wake = action.thermal_intent, action.should_wake
            # Program the device's native vibration+heat smart alarm as the hardware backstop.
            self.pending_wake_alarm = self.smart_wake.alarm_spec(now, required_wake)
            if self.pending_wake_alarm is not None and action.vibration_power:
                self.pending_wake_alarm.vibration_power = action.vibration_power
        else:
            intent = ThermalIntent.NEUTRAL

        # --- composite temperature inputs --------------------------------------
        # Exposed-skin ambient = bedroom air (preferred) or outdoor weather fallback.
        ambient_temp_f = frame.room_temp_f
        if ambient_temp_f is None and context is not None:
            ambient_temp_f = context.outdoor_temp_f
        # Covered-body signal = the Pod's measured bed-surface temperature.
        bed_temp_f = frame.bed_temp_f

        # --- resolve safe target + level (composite feedback) ------------------
        # The water command is nudged so the blended effective temperature hits target;
        # slew is anchored to the last command so the device never jumps > max_step_f.
        target_f, level = self.thermal.resolve(
            intent, objective, cfg.profile.hot_sleeper, self._last_target_f,
            bed_temp_f, ambient_temp_f, now=now,
        )

        # --- correction action vs current bed temp -----------------------------
        action = self._action_for(current_f, target_f)
        reason = self._reason(state, intent, wake_event)
        if not wake_detected:
            confidence = 0.9
        else:
            arousal_conf = arousal.confidence if arousal is not None else 0.6
            confidence = min(0.9, arousal_conf + 0.3)
        # The Pod senses HR/HRV/RR via ballistocardiography, which needs stillness, so
        # discount confidence when there is significant movement (biometrics less reliable).
        confidence *= self._biometric_reliability(frame)

        self._last_target_f = target_f
        return self._build(
            now, state, objective, intent, target_f, level, action, reason,
            confidence, frame,
            wake_signals=wake_event.signals if wake_event else [],
            minutes_in_bed=minutes_in_bed,
            ambient_temp_f=ambient_temp_f,
        )

    # -- helpers -----------------------------------------------------------------
    @staticmethod
    def _round_opt(value, ndigits: int = 2):
        return round(value, ndigits) if value is not None else None

    def set_session(self, mode: str, keep_light: Optional[bool] = None) -> None:
        """Select the session mode ('night' | 'induce' | 'nap_power' | 'nap_cycle'). Power
        naps keep the bed light so slow-wave sleep doesn't set in."""
        self.session_mode = mode or "night"
        if keep_light is None:
            keep_light = mode in ("nap_power",)
        self.session_keep_light = bool(keep_light)

    def preemption_summary(self) -> dict:
        """Live predictive-pre-emption state for the dashboard: is the controller actively
        heading off an awakening, and which signals (point-in-time risk + leading-edge drift)
        drove it."""
        risk = self.last_wake_risk
        pre = self.last_precursor
        preempting = bool(getattr(self, "_preempt_cool", False))
        return {
            "preempting": preempting,
            "intent": "settle_cool" if preempting else None,
            "wake_risk": round(risk.score, 3) if risk else None,
            "risk_reasons": list(risk.reasons) if risk else [],
            "precursor_score": round(pre.score, 3) if pre else None,
            "precursor_reasons": list(pre.reasons) if pre else [],
            "precursor_signals": pre.signals if pre else {},
        }

    def set_night_targets(self, targets, est_sleep_min: Optional[float] = None) -> None:
        """Hand the controller tonight's PERSONALIZED ideal architecture (from the SleepPlan) so the
        in-night steerer can chase the same deep/REM curve the dashboard shows and the policy
        learns. ``est_sleep_min`` is the expected sleep duration the trajectory is scaled to."""
        self.night_targets = targets
        if est_sleep_min is not None:
            self.est_sleep_min = float(est_sleep_min)

    def _evaluate_steering(self, now, frame, wake_detected, minutes_in_bed,
                           required_wake=None) -> bool:
        """Run the in-night steerer (MAINTENANCE only). Returns True to nudge deeper this tick.

        This is where the three in-night thermal maneuvers RECONCILE, by a strict precedence:
          1. wake-PREVENTION wins — ``risk_low`` requires no detected awakening AND no active
             pre-empt (which folds in rising wake-risk, the leading-edge precursor, and a
             micro-arousal), so the steerer never fights a brewing disturbance (maintenance first);
          2. wake-UP handoff — within the pre-wake standoff of the deadline the steerer stands
             down so the smart-wake ramp owns the bed (no deepening into sleep inertia);
          3. then the favorable-state controller acts: ACQUIRE deeper when behind, or DEFEND the
             deep/REM state you're already in.
        """
        cfg = self.cfg
        if self.night_targets is None or not cfg.tunables.inight_steering_enabled \
                or self.session_keep_light:
            self._deepen_active = False
            return False
        mso = ((now - self._sleep_onset_time).total_seconds() / 60.0
               if self._sleep_onset_time is not None else minutes_in_bed)
        est = self.est_sleep_min or getattr(self.night_targets, "total_sleep_target_min", 0) or 0.0
        risk_low = (not wake_detected) and (not getattr(self, "_preempt_cool", False))
        mins_to_wake = ((required_wake - now).total_seconds() / 60.0
                        if required_wake is not None else None)
        steer = self.steering.evaluate(
            minutes_since_onset=mso, est_sleep_min=est,
            deep_min_so_far=self._arch_deep_min, rem_min_so_far=self._arch_rem_min,
            current_stage=frame.stage, targets=self.night_targets, risk_low=risk_low,
            minutes_to_wake=mins_to_wake)
        self.last_steer = steer
        deepen = steer.deepen
        # n-of-1 control: ACTUATE only on 'act' nights; on 'observe'/disabled nights the steerer
        # still judges + logs a SHADOW event (applied=0) but does NOT cool — that's the control arm
        # the deepening-response learner compares against (does cooling beat the natural base rate?).
        actuate = deepen and self.steer_actuate
        # Edge-trigger the steer-event ledger when the deepen VERDICT first starts (either arm), so
        # the learner scores stage response + any awakening for both actuated and control nights.
        if deepen and not self._deepen_active:
            self.pending_steer_event = {
                "ts": now,
                "maneuver": steer.maneuver,
                "stage_before": frame.stage.value if frame.stage is not None else None,
                "deep_deficit_min": round(steer.deep_deficit_min, 2),
                "frac_of_night": round(steer.frac_of_night, 3),
                "horizon_min": self._steer_horizon_min(),
                "applied": 1 if actuate else 0,
            }
        self._deepen_active = deepen
        return actuate

    def set_precursor_profile(self, profile) -> None:
        """Apply the learned, personalized awakening-precursor trajectory to the precursor detector,
        so pre-emption triggers on the drift pattern that actually precedes YOUR awakenings."""
        self.last_precursor_profile = profile
        try:
            self.precursor_detector.personalize(profile)
        except Exception:
            pass

    def set_measured_thermal(self, cool_lag_min, heat_lag_min) -> None:
        """Apply the in-bed self-test's measured cool/heat effect-latency (minutes-to-settle):
        floors the deepening horizon (cool) and widens the wake warm-up runway (heat)."""
        self.measured_cool_lag_min = cool_lag_min
        self.measured_heat_lag_min = heat_lag_min
        self.wake_orch.set_warm_lead(self.warm_lead_min())

    def _steer_horizon_min(self) -> float:
        """Deepening-response scoring horizon, floored at the measured cool-lag (+2 min) so the
        learner never judges 'did cooling deepen me?' before the cool has actually landed."""
        base = self.cfg.tunables.steer_response_horizon_min
        if self.measured_cool_lag_min:
            return round(max(base, self.measured_cool_lag_min + 2.0), 1)
        return base

    def warm_lead_min(self) -> Optional[float]:
        """How many minutes before the wake deadline the warming ramp should begin so the bed is
        actually warm by then — the measured heat-lag (+2 min margin), or None if uncalibrated.
        Consumed by the wake orchestrator to widen a too-short warm-up runway."""
        if self.measured_heat_lag_min:
            return round(self.measured_heat_lag_min + 2.0, 1)
        return None

    def set_steer_policy(self, actuate: bool) -> None:
        """Set whether tonight ACTUATES the deepen nudge (learned do-no-harm gate + the n-of-1
        control schedule). False = a control/observe night: judge + shadow-log, but don't cool."""
        self.steer_actuate = bool(actuate)

    def _reset_architecture(self) -> None:
        self._arch_deep_min = self._arch_rem_min = self._arch_light_min = 0.0
        self._arch_last_ts = None
        self._deepen_active = False
        self.last_steer = None

    def _accrue_architecture(self, now: datetime, stage) -> None:
        """Accumulate realized time-in-stage since onset (the night's unfolding architecture).
        Ignores gaps/jumps so a stale tick can't inflate a bucket."""
        if self._arch_last_ts is not None and stage is not None:
            dt = (now - self._arch_last_ts).total_seconds() / 60.0
            if 0.0 < dt <= 10.0:
                if stage is SleepStage.DEEP:
                    self._arch_deep_min += dt
                elif stage is SleepStage.REM:
                    self._arch_rem_min += dt
                elif stage is SleepStage.LIGHT:
                    self._arch_light_min += dt
        self._arch_last_ts = now

    def steering_summary(self) -> dict:
        """Live in-night steering state for the dashboard: are we actively nudging deeper, and how
        far off the ideal deep/REM curve are we right now."""
        s = self.last_steer
        maneuver = s.maneuver if s else "hold"
        verdict_deepen = bool(self._deepen_active)
        return {
            "active": verdict_deepen and self.steer_actuate,     # actually nudging deeper (acquire)
            "observing": verdict_deepen and not self.steer_actuate,  # control night: judging, not cooling
            "defending": maneuver in ("defend_deep", "defend_rem"),  # holding a favorable state
            "maneuver": maneuver,
            "deep_deficit_min": round(s.deep_deficit_min, 1) if s else None,
            "rem_deficit_min": round(s.rem_deficit_min, 1) if s else None,
            "frac_of_night": round(s.frac_of_night, 3) if s else None,
            "deep_min_so_far": round(self._arch_deep_min, 1),
            "rem_min_so_far": round(self._arch_rem_min, 1),
            "reason": s.reason if s else None,
        }

    def set_settle_nudge(self, nudge_f: float) -> None:
        """Apply the learned signed maintenance settle nudge to the thermal controller."""
        self.thermal.set_settle_nudge(nudge_f)

    def set_onset_warm(self, warm_f: float) -> None:
        """Apply tonight's learned (per-mode, explored) onset warm nudge to the induction phase."""
        self.thermal.set_onset_warm(warm_f)

    def set_wake_window(self, minutes: int) -> None:
        """The time selector sets the per-night smart-wake window ceiling (choose_wake_window)."""
        self.wake_orch.cfg.window_min = max(1, int(minutes))

    def set_dawn_light(self, enabled: bool) -> None:
        """Tell the orchestrator a smart-bulb sunrise is wired up, so it actually computes a
        ramping ``light_level`` through the dawn window (otherwise it stays 0 and only the
        therapy plug — which keys off ``should_wake`` — would fire). The daemon calls this when a
        Hue dawn driver with sunrise targets is configured."""
        self.wake_orch.cfg.light_enabled = bool(enabled)

    def set_wake_ramp_f(self, wake_f: float) -> None:
        """Apply the learned per-person thermal wake maneuver (the WAKE_RAMP target temperature)."""
        from dataclasses import replace
        self.thermal.profile = replace(self.thermal.profile, wake_ramp_f=float(wake_f))

    def set_setpoints(self, profile) -> None:
        """Swap the active SetpointProfile for the night (e.g. an experiment arm applied on top
        of the learned setpoint). No-op on None so callers can pass through safely."""
        if profile is not None:
            self.thermal.profile = profile

    def set_wake_profile(self, profile=None, lead_profile=None) -> None:
        """Attach the learned per-user awakening phenotype + cooling lead-times to the
        wake-risk assessor (proactive sleep maintenance)."""
        if profile is not None:
            self.wake_risk_assessor.profile = profile
        if lead_profile is not None:
            self.wake_risk_assessor.lead_profile = lead_profile
            # Make the whole thermal loop latency-aware with the learned actuation lag.
            self.thermal.set_response_lag(lead_profile.response_lag_min)

    @staticmethod
    def _sleep_baseline(recent: list) -> tuple:
        """Recent settled-sleep HR/HRV baselines (from asleep, low-motion frames) so the
        arousal + wake-risk detectors measure surges/creep against the right reference."""
        asleep = [f for f in (recent or [])
                  if f.stage in (SleepStage.LIGHT, SleepStage.DEEP, SleepStage.REM)
                  and (f.movement is None or f.movement < 0.2)]
        pool = asleep[-15:] if asleep else (recent[-10:] if recent else [])
        hrs = [f.heart_rate for f in pool if f.heart_rate is not None]
        hrvs = [f.hrv for f in pool if f.hrv is not None]
        import statistics as _st
        hr = _st.fmean(hrs) if hrs else None
        hrv = _st.fmean(hrvs) if hrvs else None
        return hr, hrv

    @staticmethod
    def _biometric_reliability(frame: SensorFrame) -> float:
        """1.0 when still; lower when moving (ballistocardiography needs stillness)."""
        if frame.movement is None:
            return 1.0
        # movement ~0 -> 1.0; movement >= 0.5 -> ~0.6 floor. Linear in between.
        return max(0.6, 1.0 - 0.8 * min(frame.movement, 0.5))

    def _action_for(self, current_f: float, target_f: float) -> CorrectionAction:
        delta = target_f - current_f
        if abs(delta) < 0.5:
            return CorrectionAction.HOLD
        return CorrectionAction.WARMER if delta > 0 else CorrectionAction.COOLER

    def _reason(self, state, intent, wake_event) -> str:
        if wake_event is not None:
            return f"{state.value}: awakening ({','.join(wake_event.signals)}); stabilizing"
        base = f"{state.value} -> {intent.value}"
        if self.sm.reason:
            base += f" ({self.sm.reason})"
        return base

    def _build(
        self, now, state, objective, intent, target_f, level, action, reason,
        confidence, frame, wake_signals, minutes_in_bed: float = 0.0,
        ambient_temp_f=None,
    ) -> Decision:
        log_payload = {
            "stage": frame.stage.value if frame.stage is not None else None,
            "stage_confidence": frame.stage_confidence,
            "heart_rate": frame.heart_rate,
            "hrv": frame.hrv,
            "respiratory_rate": frame.respiratory_rate,
            "movement": frame.movement,
            "bed_temp_f": frame.bed_temp_f,
            "room_temp_f": frame.room_temp_f,
            "ambient_temp_f": ambient_temp_f,
            "composite_temp_f": self._round_opt(
                self.thermal.composite_temp(frame.bed_temp_f, ambient_temp_f)
            ),
            "effective_target_f": round(
                self.thermal.target_for(intent, objective, self.cfg.profile.hot_sleeper,
                                        self._last_target_f), 2
            ),
            "data_age_seconds": frame.data_age_seconds,
            "wake_signals": wake_signals,
            "steering": self.steering_summary(),
            "should_wake": self.should_wake,
            "wake_action": self.last_wake_action.to_dict() if self.last_wake_action else None,
            "minutes_in_bed": round(minutes_in_bed, 1),
        }
        return Decision(
            timestamp=now,
            state=state,
            objective=objective,
            thermal_intent=intent,
            target_temp_f=round(target_f, 2),
            target_level=level,
            action=action,
            reason=reason,
            confidence=round(confidence, 2),
            log_payload=log_payload,
        )
