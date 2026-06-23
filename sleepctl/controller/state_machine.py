"""Guarded controller state machine.

States: IDLE -> INDUCTION -> MAINTENANCE <-> WAKE_RECOVERY -> WAKE_WINDOW -> IDLE.
Transitions are conservative and explainable; the caller supplies the derived facts
(asleep, wake_detected, required_wake_time) rather than the machine reaching into other
subpackages.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

from sleepctl.config import AppConfig
from sleepctl.models import ControllerState, SensorFrame


class SleepStateMachine:
    def __init__(self, cfg: AppConfig, state: ControllerState = ControllerState.IDLE) -> None:
        self.cfg = cfg
        self.state = state
        self.reason = "init"
        self._asleep_streak = 0
        self._stable_streak = 0  # consecutive stable samples during wake-recovery
        self._recovery_started: Optional[datetime] = None

    def _is_asleep(self, frame: SensorFrame) -> bool:
        from sleepctl.models import SleepStage

        return frame.stage in (SleepStage.LIGHT, SleepStage.DEEP, SleepStage.REM)

    def transition(
        self,
        frame: SensorFrame,
        now: datetime,
        wake_detected: bool,
        required_wake_time: Optional[datetime],
        onset_confirmed: Optional[bool] = None,
    ) -> ControllerState:
        prev = self.state
        s = self.state
        wake_window = timedelta(minutes=self.cfg.tunables.wake_window_min)
        in_wake_window = (
            required_wake_time is not None
            and now >= (required_wake_time - wake_window)
        )
        past_wake = required_wake_time is not None and now >= required_wake_time

        # Left the bed (after wake time) -> IDLE.
        if frame.presence is False and (past_wake or s is ControllerState.WAKE_WINDOW):
            self.state, self.reason = ControllerState.IDLE, "left bed after wake time"
            return self.state

        if s in (ControllerState.IDLE, ControllerState.CALIBRATION):
            if frame.presence is True:
                self.state, self.reason = ControllerState.INDUCTION, "got into bed"

        elif s is ControllerState.INDUCTION:
            if self._is_asleep(frame):
                self._asleep_streak += 1
            else:
                self._asleep_streak = 0
            # Prefer the accurate multi-signal + persistence onset detector when wired; fall
            # back to the simple asleep-streak heuristic otherwise. This is what keeps lying
            # in bed awake from being mistaken for sleep.
            if onset_confirmed is None:
                onset_ok = self._asleep_streak >= 2
            else:
                onset_ok = bool(onset_confirmed)
            if in_wake_window:
                self.state, self.reason = ControllerState.WAKE_WINDOW, "entered wake window"
            elif onset_ok:
                self.state, self.reason = ControllerState.MAINTENANCE, "sleep onset confirmed"

        elif s is ControllerState.MAINTENANCE:
            if in_wake_window:
                self.state, self.reason = ControllerState.WAKE_WINDOW, "entered wake window"
            elif wake_detected:
                self._recovery_started = now
                self._stable_streak = 0
                self.state, self.reason = ControllerState.WAKE_RECOVERY, "awakening detected"

        elif s is ControllerState.WAKE_RECOVERY:
            if in_wake_window:
                self.state, self.reason = ControllerState.WAKE_WINDOW, "entered wake window"
            else:
                if self._is_asleep(frame) and not wake_detected:
                    self._stable_streak += 1
                else:
                    self._stable_streak = 0
                recovered = (
                    self._recovery_started is not None
                    and now - self._recovery_started
                    >= timedelta(minutes=self.cfg.tunables.wake_recovery_minutes)
                    and self._stable_streak >= 2
                )
                if recovered:
                    self.state, self.reason = (
                        ControllerState.MAINTENANCE,
                        "physiology re-stabilized",
                    )

        elif s is ControllerState.WAKE_WINDOW:
            pass  # remain until the user leaves the bed

        if self.state is prev and self.reason == "init":
            self.reason = "hold state"
        return self.state
