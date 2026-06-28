"""The wake orchestrator — multi-signal, escalating, inertia-minimizing smart wake.

Goal: wake you at the BEST moment in a window, as gently as possible, while NEVER letting you
oversleep the hard deadline (a missed shift is unacceptable). It improves on a stage-only alarm
on every axis available without rooting the Pod:

  • Multi-signal liftability — instead of trusting one 60 s stage label, it fuses the calibrated
    sleep/wake classifier's P(wake) (HR/HRV/respiration/movement, INCLUDING the sub-minute iPhone
    movement already fused onto the frame) with the stage. A light/surfacing moment is detected
    earlier and more reliably.
  • Inertia-aware timing — waking out of slow-wave (deep) sleep causes the worst sleep inertia, so
    the orchestrator will wake you a few minutes EARLY at a genuine light-sleep moment rather than
    later from deep (Brooks & Lack 2006, doi:10.1093/sleep/29.6.831; the same SWS-inertia
    principle behind smart alarms).
  • Silent, escalating ladder — thermal "dawn" ramp first, then gentle vibration, then stronger,
    then full at the deadline. Audio stays OFF (tactile, not noise). It backs off the instant
    you're actually up.
  • Hard-deadline guarantee — if no light moment comes, or the data is stale/low-confidence, it
    falls back to firing at the deadline at full strength. It never trusts a bad read into a
    missed wake.

Stateful across ticks (tracks when waking began so the ladder escalates); the controller holds
one instance per night.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import List, Optional

from sleepctl.models import SensorFrame, SleepStage, ThermalIntent


@dataclass
class WakeConfig:
    window_min: int = 30            # smart window opens this many min before the deadline
    thermal_dawn_min: int = 20      # warm "dawn" ramp begins this many min before the deadline
    p_wake_liftable: float = 0.45   # classifier P(wake) at/above which a moment is "liftable"
    p_wake_up: float = 0.85         # at/above this you're treated as already awake
    last_resort_min: int = 6        # if still deep this close to the deadline, start waking anyway
    gentle_vibration: int = 30
    strong_vibration: int = 70
    max_vibration: int = 100
    escalate_gentle_s: int = 120    # gentle for this long after engaging, then strong
    escalate_strong_s: int = 240    # strong until this long, then max
    silent_only: bool = True        # vibration + thermal only; audio never used

    @staticmethod
    def from_tunables(t) -> "WakeConfig":
        c = WakeConfig()
        c.window_min = int(getattr(t, "wake_window_min", c.window_min))
        if getattr(t, "wake_vibration_enabled", True):
            c.gentle_vibration = int(getattr(t, "wake_vibration_power", c.gentle_vibration))
        else:
            c.gentle_vibration = c.strong_vibration = c.max_vibration = 0
        return c


@dataclass
class WakeAction:
    phase: str                      # idle | hold | dawn | gentle | escalate | fire | done
    should_wake: bool               # the controller's wake flag
    vibration_power: int            # 0..100 (0 = none; audio always off)
    thermal_intent: ThermalIntent
    target_time: Optional[datetime]
    p_wake: Optional[float] = None
    reason: str = ""

    def to_dict(self) -> dict:
        return {"phase": self.phase, "should_wake": self.should_wake,
                "vibration_power": self.vibration_power,
                "thermal_intent": self.thermal_intent.value if self.thermal_intent else None,
                "target_time": self.target_time.isoformat() if self.target_time else None,
                "p_wake": round(self.p_wake, 3) if self.p_wake is not None else None,
                "reason": self.reason}


class WakeOrchestrator:
    """Stateful per-night wake decision. Call ``evaluate`` each control tick in the wake window."""

    def __init__(self, cfg: WakeConfig | None = None, classifier=None) -> None:
        self.cfg = cfg or WakeConfig()
        self.classifier = classifier
        self._engaged_at: Optional[datetime] = None   # when the wake ladder began
        self._done = False                            # user confirmed up

    def reset(self) -> None:
        self._engaged_at = None
        self._done = False

    # -------------------------------------------------------------- liftability
    def _p_wake(self, frame: SensorFrame, recent: List[SensorFrame],
                hr_base: Optional[float], hrv_base: Optional[float]) -> Optional[float]:
        if self.classifier is None:
            return None
        try:
            return self.classifier.probability(frame, recent, hr_base, hrv_base).p
        except Exception:
            return None

    def _is_up(self, frame: SensorFrame, p_wake: Optional[float]) -> bool:
        if frame.presence is False:
            return True
        if frame.stage is SleepStage.AWAKE and (frame.movement or 0) >= 0.4:
            return True
        return p_wake is not None and p_wake >= self.cfg.p_wake_up

    def _is_liftable(self, frame: SensorFrame, p_wake: Optional[float], stale: bool) -> bool:
        """A good (light/surfacing) moment to wake gently — never out of deep/SWS."""
        if frame.stage in (SleepStage.DEEP, SleepStage.REM):
            # Deep especially: hold. (REM is light-ish but waking mid-REM can be groggy too; only
            # lift on REM if the classifier says they're clearly surfacing.)
            if frame.stage is SleepStage.DEEP:
                return False
        if stale or p_wake is None:
            return frame.stage in (SleepStage.LIGHT, SleepStage.AWAKE)
        return (frame.stage in (SleepStage.LIGHT, SleepStage.AWAKE)
                or p_wake >= self.cfg.p_wake_liftable)

    # -------------------------------------------------------------- ladder
    def _vibration_for(self, now: datetime, deadline: datetime) -> int:
        c = self.cfg
        if now >= deadline:
            return c.max_vibration
        if self._engaged_at is None:
            return 0
        elapsed = (now - self._engaged_at).total_seconds()
        if elapsed < c.escalate_gentle_s:
            return c.gentle_vibration
        if elapsed < c.escalate_strong_s:
            return c.strong_vibration
        return c.max_vibration

    def _phase_for_vibration(self, v: int) -> str:
        c = self.cfg
        if v >= c.max_vibration and c.max_vibration > 0:
            return "fire"
        if v >= c.strong_vibration and c.strong_vibration > 0:
            return "escalate"
        return "gentle"

    # -------------------------------------------------------------- main
    def evaluate(self, now: datetime, frame: SensorFrame, recent: List[SensorFrame],
                 required_wake: Optional[datetime], *, hr_base: Optional[float] = None,
                 hrv_base: Optional[float] = None, data_stale: bool = False) -> WakeAction:
        c = self.cfg
        if required_wake is None:
            self.reset()
            return WakeAction("idle", False, 0, ThermalIntent.NEUTRAL, None, reason="no wake time")

        window_start = required_wake - timedelta(minutes=c.window_min)
        dawn_start = required_wake - timedelta(minutes=c.thermal_dawn_min)
        p_wake = self._p_wake(frame, recent, hr_base, hrv_base)

        # Already up — back off entirely.
        if self._is_up(frame, p_wake):
            self._done = True
            return WakeAction("done", True, 0, ThermalIntent.WAKE_RAMP, required_wake, p_wake,
                              "you're up — alarm stood down")

        # Before the window: nothing yet (a pre-dawn neutral hold).
        if now < window_start and now < dawn_start:
            return WakeAction("idle", False, 0, ThermalIntent.NEUTRAL, required_wake, p_wake,
                              "before wake window")

        # Hard deadline (or past it): guaranteed full wake.
        if now >= required_wake:
            if self._engaged_at is None:
                self._engaged_at = now
            v = c.max_vibration
            return WakeAction("fire", True, v, ThermalIntent.WAKE_RAMP, required_wake, p_wake,
                              "deadline reached — guaranteed wake")

        secs_to_deadline = (required_wake - now).total_seconds()
        liftable = self._is_liftable(frame, p_wake, data_stale)
        last_resort = secs_to_deadline <= c.last_resort_min * 60

        # Decide whether to ENGAGE the wake ladder.
        if self._engaged_at is None:
            if now >= window_start and liftable:
                self._engaged_at = now
                reason = ("light/surfacing moment in-window — waking gently now to avoid a "
                          "deep-sleep wake later")
            elif last_resort:
                self._engaged_at = now
                reason = "deadline near — starting the wake even though sleep is still deep"
            else:
                # Hold: run the thermal dawn ramp if we're inside it, else just wait.
                intent = ThermalIntent.WAKE_RAMP if now >= dawn_start else ThermalIntent.NEUTRAL
                phase = "dawn" if now >= dawn_start else "hold"
                return WakeAction(phase, False, 0, intent, required_wake, p_wake,
                                  "deep/not-liftable — holding, dawn ramp on"
                                  if phase == "dawn" else "waiting for a light-sleep moment")
        else:
            reason = "waking in progress"

        v = self._vibration_for(now, required_wake)
        phase = self._phase_for_vibration(v)
        return WakeAction(phase, True, v, ThermalIntent.WAKE_RAMP, required_wake, p_wake, reason)
