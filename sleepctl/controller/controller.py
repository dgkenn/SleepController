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
from sleepctl.controller.induction import InductionRoutine
from sleepctl.controller.maintenance import MaintenanceRoutine, WakeRecoveryRoutine
from sleepctl.controller.sleep_onset import SleepOnsetDetector
from sleepctl.controller.smart_wake import SmartWakeRoutine
from sleepctl.controller.state_machine import SleepStateMachine
from sleepctl.controller.thermal import ThermalController
from sleepctl.controller.wake_detection import WakeDetector
from sleepctl.models import (
    ContextRecord,
    ControllerState,
    CorrectionAction,
    Decision,
    NightObjective,
    SensorFrame,
    ThermalIntent,
)


class SleepController:
    def __init__(self, cfg: AppConfig, setpoints=None) -> None:
        self.cfg = cfg
        self.sm = SleepStateMachine(cfg)
        self.wake_detector = WakeDetector()
        self.onset_detector = SleepOnsetDetector(cfg)
        self.induction = InductionRoutine(cfg)
        self.maintenance = MaintenanceRoutine(cfg)
        self.wake_recovery = WakeRecoveryRoutine(cfg)
        self.smart_wake = SmartWakeRoutine(cfg)
        # The learnable setpoint profile (updated nightly by the learning loop / ML).
        self.thermal = ThermalController(cfg, profile=setpoints)

        self._bed_entry_time: Optional[datetime] = None
        self._sleep_onset_time: Optional[datetime] = None  # accurate fall-asleep time
        self._last_target_f: float = cfg.tunables.neutral_temp_f
        self.last_wake_event = None
        self.last_onset_event = None
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

        # --- wake detection -----------------------------------------------------
        wake_event = None
        if self.sm.state in (ControllerState.MAINTENANCE, ControllerState.WAKE_RECOVERY):
            wake_event = self.wake_detector.evaluate(frame, recent, now)
        self.last_wake_event = wake_event
        wake_detected = wake_event is not None

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

        # --- pick thermal intent per state -------------------------------------
        self.should_wake = False
        if state in (ControllerState.IDLE, ControllerState.CALIBRATION):
            # Night ended / out of bed: reset onset tracking for the next night.
            if frame.presence is False:
                self._bed_entry_time = None
                self._sleep_onset_time = None
                self.onset_detector.reset()
            intent = ThermalIntent.NEUTRAL
        elif state is ControllerState.INDUCTION:
            intent = self.induction.step(frame, objective, minutes_in_bed)
        elif state is ControllerState.MAINTENANCE:
            intent = self.maintenance.step(frame, objective)
        elif state is ControllerState.WAKE_RECOVERY:
            intent = self.wake_recovery.step(frame)
        elif state is ControllerState.WAKE_WINDOW:
            intent, self.should_wake = self.smart_wake.step(frame, now, required_wake)
            # Program a heat + gentle-vibration smart alarm for the optimal light-sleep wake.
            self.pending_wake_alarm = self.smart_wake.alarm_spec(now, required_wake)
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
            bed_temp_f, ambient_temp_f,
        )

        # --- correction action vs current bed temp -----------------------------
        action = self._action_for(current_f, target_f)
        reason = self._reason(state, intent, wake_event)
        confidence = 0.9 if not wake_detected else min(0.9, wake_event.confidence + 0.3)
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
            "stage": frame.stage.value,
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
            "should_wake": self.should_wake,
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
