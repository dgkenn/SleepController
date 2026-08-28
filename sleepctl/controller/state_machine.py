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
        wearable_bed_entry: bool = False,
    ) -> ControllerState:
        prev = self.state
        s = self.state
        wake_window = timedelta(minutes=self.cfg.tunables.wake_window_min)
        # The window has to CLOSE. Without an upper bound `now >= required_wake - window` stays
        # true for the whole rest of the day, and WAKE_WINDOW below is written to "remain until
        # the user leaves the bed" -- where leaving the bed means `presence is False`, which on
        # an account with no Autopilot membership never happens. Between them that made
        # WAKE_WINDOW a terminal state escapable only by restarting the daemon, with the
        # stale-data guard, the data-quality gate and the abandoned-session timeout all
        # suppressed the entire time, because every one of them stands down inside the window.
        # Measured: 2026-08-25 sat in WAKE_RECOVERY from 12:00 to 18:37 -- 6.6 hours, 786 ticks,
        # zero heart rate, zero movement, commanding the bed throughout.
        close_after = timedelta(minutes=float(
            getattr(self.cfg.tunables, "wake_window_close_min", 60.0)))
        in_wake_window = (
            required_wake_time is not None
            and now >= (required_wake_time - wake_window)
            and now <= (required_wake_time + close_after)
        )
        past_wake = required_wake_time is not None and now >= required_wake_time
        window_closed = (required_wake_time is not None
                         and now > required_wake_time + close_after)

        # Left the bed (after wake time) -> IDLE.
        if frame.presence is False and (past_wake or s is ControllerState.WAKE_WINDOW):
            self.state, self.reason = ControllerState.IDLE, "left bed after wake time"
            return self.state

        if s in (ControllerState.IDLE, ControllerState.CALIBRATION):
            if frame.presence is True:
                self.state, self.reason = ControllerState.INDUCTION, "got into bed"
            elif wearable_bed_entry:
                # Pod presence is unavailable (None forever on an account with no Autopilot
                # membership), so `presence is True` can never fire and the controller could
                # never start a night by itself -- see AppConfig.wearable_bed_entry for the
                # measurement. The caller has confirmed sustained, live, plausible wearable
                # physiology; treat that as bed entry. Note `presence is False` (a POSITIVE
                # bed-exit report) is handled above and still wins, so this only ever fills in
                # for UNKNOWN presence, never contradicts the Pod.
                self.state, self.reason = (ControllerState.INDUCTION,
                                           "wearable bed entry (Pod presence unavailable)")

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
                elif window_closed:
                    # Recovery needs `_is_asleep`, which needs a stage, which needs a feed. With
                    # no feed the stable streak can never build and this state has no exit at
                    # all -- the 2026-08-25 failure exactly.
                    self.state, self.reason = (ControllerState.IDLE,
                                               "wake window closed while recovering")

        elif s is ControllerState.WAKE_WINDOW:
            # Remain until the user leaves the bed -- or until the window itself expires. The
            # bed-exit branch at the top of this method needs `presence is False` to fire, so on
            # this hardware the expiry is the ONLY exit that exists.
            if window_closed:
                self.state, self.reason = (ControllerState.IDLE,
                                           "wake window closed without a bed exit")

        if self.state is prev and self.reason == "init":
            self.reason = "hold state"
        return self.state
