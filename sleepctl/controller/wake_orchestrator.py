"""The wake orchestrator — multi-signal, anticipatory, escalating, inertia-minimizing smart wake.

Goal: wake you at the BEST moment in a window, as gently as possible, confirm you're actually up,
and NEVER let you oversleep the hard deadline. It improves on a stage-only alarm on every axis
available without rooting the Pod:

  • Multi-signal liftability — fuses the calibrated sleep/wake classifier's P(wake)
    (HR/HRV/respiration/movement, INCLUDING the sub-minute iPhone movement on the frame) with the
    stage, so a light/surfacing moment is detected earlier and more reliably than one 60 s label.
  • Anticipatory, not reactive — an ultradian cycle predictor estimates when the NEXT light-sleep
    ascent will arrive; if a near-zero-inertia moment is coming before the deadline, it WAITS for
    it instead of forcing a wake out of deep sleep (the worst case for inertia; Brooks & Lack 2006,
    doi:10.1093/sleep/29.6.831).
  • Sleep-debt adaptive — deep in debt, protecting total sleep matters more than shaving inertia
    (Van Dongen 2003, doi:10.1093/sleep/26.2.117): the early-wake window narrows and a stronger
    surfacing signal is required, so you squeeze the sleep. Well-rested, it widens the window to
    prioritize a zero-inertia wake.
  • Silent, escalating ladder + dawn — thermal "dawn" ramp and an optional light ramp first, then
    gentle vibration, stronger, then full at the deadline. Audio stays OFF (tactile, not noise).
  • Wake CONFIRMATION + anti-relapse — it doesn't assume one nudge means awake: it keeps going (and
    re-escalates) until you've truly surfaced (bed-exit or sustained wakefulness), then stands down.
  • Hard-deadline guarantee + stale fallback — no light moment / stale data → fire at the deadline
    at full strength. A bad read can never cost you a wake.

Stateful across ticks; the controller holds one instance per night.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import List, Optional

from sleepctl.controller.sleep_cycle import SleepCyclePredictor
from sleepctl.models import SensorFrame, SleepStage, ThermalIntent


def choose_wake_window(night_type: Optional[str], debt_min: float = 0.0,
                       gym_go: bool = False, base: int = 30) -> int:
    """Pick an appropriate smart-wake window for the night the picker just set.

    Wide when sleep is plentiful (chase a zero-inertia light-sleep wake); narrow when sleep is
    scarce — a short/work night, an early gym wake, or accumulated debt — so we don't trade away
    needed sleep just to shave grogginess. The orchestrator narrows further, continuously, from
    live debt; this sets the per-night ceiling the time selector hands it."""
    nt = (night_type or "").lower()
    if nt in ("constrained", "short", "work", "damage_control"):
        w = 15
    elif nt == "recovery":
        w = 22
    else:
        w = base
    if gym_go:
        w = min(w, 20)
    if debt_min >= 300:
        w = min(w, 18)
    return max(10, min(base, w))


@dataclass
class WakeConfig:
    window_min: int = 30            # smart window opens this many min before the deadline
    thermal_dawn_min: int = 20      # warm "dawn" ramp (and light) begin this many min before
    p_wake_liftable: float = 0.45   # classifier P(wake) at/above which a moment is "liftable"
    p_wake_up: float = 0.85         # at/above this you're treated as surfacing
    last_resort_min: int = 6        # if still deep this close to the deadline, wake anyway...
    hard_buffer_s: int = 120        # ...unless within this buffer we MUST engage no matter what
    confirm_ticks: int = 2          # consecutive surfacing ticks needed to confirm you're up
    gentle_vibration: int = 30
    strong_vibration: int = 70
    max_vibration: int = 100
    escalate_gentle_s: int = 120
    escalate_strong_s: int = 240
    silent_only: bool = True
    light_enabled: bool = False     # drive a smart-bulb dawn via the daemon webhook
    post_wake_light_min: int = 20   # hold the bright light dose this long AFTER you've surfaced
    debt_window_shrink: float = 0.4  # high debt shrinks the early window by up to this fraction
    debt_threshold_raise: float = 0.15  # ...and raises the liftable bar by up to this

    @staticmethod
    def from_tunables(t) -> "WakeConfig":
        c = WakeConfig()
        c.window_min = int(getattr(t, "wake_window_min", c.window_min))
        c.post_wake_light_min = int(getattr(t, "post_wake_light_min", c.post_wake_light_min))
        if getattr(t, "wake_vibration_enabled", True):
            c.gentle_vibration = int(getattr(t, "wake_vibration_power", c.gentle_vibration))
        else:
            c.gentle_vibration = c.strong_vibration = c.max_vibration = 0
        return c


@dataclass
class WakeAction:
    phase: str                      # idle | hold | dawn | wait_cycle | gentle | escalate | fire | post_wake | done
    should_wake: bool
    vibration_power: int            # 0..100 (0 = none; audio always off)
    thermal_intent: ThermalIntent
    target_time: Optional[datetime]
    p_wake: Optional[float] = None
    light_level: float = 0.0        # 0..1 dawn-light ramp for a smart bulb (if enabled)
    cycle: Optional[dict] = None     # cycle predictor state, for telemetry
    reason: str = ""
    vibration_pulse: str = "off"    # off | slow | medium | continuous — rhythmic, not a flat buzz

    def to_dict(self) -> dict:
        return {"phase": self.phase, "should_wake": self.should_wake,
                "vibration_power": self.vibration_power,
                "thermal_intent": self.thermal_intent.value if self.thermal_intent else None,
                "target_time": self.target_time.isoformat() if self.target_time else None,
                "p_wake": round(self.p_wake, 3) if self.p_wake is not None else None,
                "light_level": round(self.light_level, 2), "cycle": self.cycle,
                "reason": self.reason, "vibration_pulse": self.vibration_pulse}


class WakeOrchestrator:
    def __init__(self, cfg: WakeConfig | None = None, classifier=None) -> None:
        self.cfg = cfg or WakeConfig()
        self.classifier = classifier
        self.predictor = SleepCyclePredictor()
        self._engaged_at: Optional[datetime] = None
        self._up_streak = 0
        self._confirmed = False
        self._confirmed_at: Optional[datetime] = None

    def reset(self) -> None:
        self._engaged_at = None
        self._up_streak = 0
        self._confirmed = False
        self._confirmed_at = None
        self.predictor.reset()

    # -------------------------------------------------------------- signals
    def _p_wake(self, frame, recent, hr_base, hrv_base) -> Optional[float]:
        if self.classifier is None:
            return None
        try:
            return self.classifier.probability(frame, recent, hr_base, hrv_base).p
        except Exception:
            return None

    def _signs_up(self, frame: SensorFrame, p_wake: Optional[float]) -> bool:
        if frame.presence is False:
            return True
        if frame.stage is SleepStage.AWAKE and (frame.movement or 0) >= 0.4:
            return True
        return p_wake is not None and p_wake >= self.cfg.p_wake_up

    def _is_liftable(self, frame, p_wake, stale, p_liftable) -> bool:
        if frame.stage is SleepStage.DEEP:
            return False
        if stale or p_wake is None:
            return frame.stage in (SleepStage.LIGHT, SleepStage.AWAKE)
        return frame.stage in (SleepStage.LIGHT, SleepStage.AWAKE) or p_wake >= p_liftable

    def _vibration_for(self, now, deadline) -> int:
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

    @staticmethod
    def _pulse_for_phase(phase: str) -> str:
        """Express vibration as a building rhythm, not a flat buzz. A melodic/rhythmic waking
        signal eases sleep inertia where a neutral constant one worsens it (McFarlane 2020,
        doi:10.1371/journal.pone.0215788); the same principle applied to silent haptics."""
        return {"gentle": "slow", "escalate": "medium", "fire": "continuous"}.get(phase, "off")

    def _light(self, now, dawn_start, deadline) -> float:
        if not self.cfg.light_enabled or now < dawn_start:
            return 0.0
        span = (deadline - dawn_start).total_seconds()
        if span <= 0:
            return 1.0
        return max(0.0, min(1.0, (now - dawn_start).total_seconds() / span))

    # -------------------------------------------------------------- main
    def evaluate(self, now: datetime, frame: SensorFrame, recent: List[SensorFrame],
                 required_wake: Optional[datetime], *, hr_base=None, hrv_base=None,
                 data_stale: bool = False, debt_min: float = 0.0) -> WakeAction:
        c = self.cfg
        if required_wake is None:
            self.reset()
            return WakeAction("idle", False, 0, ThermalIntent.NEUTRAL, None, reason="no wake time")

        self.predictor.observe(now, frame.stage)
        p_wake = self._p_wake(frame, recent, hr_base, hrv_base)

        # Sleep-debt adaptation: in debt, protect total sleep — narrow the early window and demand
        # a stronger surfacing signal; well-rested, keep the full window for a zero-inertia wake.
        debt_factor = max(0.0, min(1.0, debt_min / 360.0))
        eff_window = c.window_min * (1.0 - c.debt_window_shrink * debt_factor)
        p_liftable = min(0.95, c.p_wake_liftable + c.debt_threshold_raise * debt_factor)
        window_start = required_wake - timedelta(minutes=eff_window)
        dawn_start = required_wake - timedelta(minutes=c.thermal_dawn_min)
        light = self._light(now, dawn_start, required_wake)
        cyc = self.predictor.predict(now, frame.stage).to_dict()

        # Before the window and before dawn: nothing yet.
        if now < window_start and now < dawn_start:
            return WakeAction("idle", False, 0, ThermalIntent.NEUTRAL, required_wake, p_wake,
                              0.0, cyc, "before wake window")

        # Wake confirmation / anti-relapse: only stand down once truly up.
        if not self._confirmed:
            if frame.presence is False:
                self._confirmed, self._confirmed_at = True, now
            elif self._signs_up(frame, p_wake):
                self._up_streak += 1
                if self._up_streak >= c.confirm_ticks:
                    self._confirmed, self._confirmed_at = True, now
            else:
                self._up_streak = 0
        if self._confirmed:
            # Post-wake circadian light dose: keep the bulbs bright + the therapy lamp ON for a
            # short window after you surface to lock in alertness and shift the clock (dawn-sim
            # trials hold light ~20 min past wake — Gabel 2014, doi:10.1016/j.bbr.2014.12.043),
            # while the bed stops warming (warm skin is sleep-permissive — Te Lindert & Van
            # Someren 2018, doi:10.1016/B978-0-444-63912-7.00021-7). In bed only; bed-exit ends it.
            held = (self._confirmed_at is not None
                    and (now - self._confirmed_at).total_seconds() < c.post_wake_light_min * 60)
            if held and frame.presence is not False and c.light_enabled:
                return WakeAction("post_wake", True, 0, ThermalIntent.NEUTRAL, required_wake,
                                  p_wake, 1.0, cyc,
                                  f"awake — bright light dose ({c.post_wake_light_min} min) to "
                                  "lock in alertness")
            return WakeAction("done", True, 0, ThermalIntent.WAKE_RAMP, required_wake, p_wake,
                              0.0, cyc, "confirmed up — alarm stood down")

        # Hard deadline (or past it): guaranteed full wake.
        if now >= required_wake:
            if self._engaged_at is None:
                self._engaged_at = now
            return WakeAction("fire", True, c.max_vibration, ThermalIntent.WAKE_RAMP,
                              required_wake, p_wake, max(light, 1.0 if c.light_enabled else 0.0),
                              cyc, "deadline reached — guaranteed wake",
                              vibration_pulse=self._pulse_for_phase("fire"))

        secs_to_deadline = (required_wake - now).total_seconds()
        liftable = self._is_liftable(frame, p_wake, data_stale, p_liftable)

        if self._engaged_at is None:
            if now >= window_start and liftable:
                self._engaged_at = now
                reason = "light/surfacing moment in-window — waking gently now"
            else:
                # Deep / not liftable. Decide WAIT vs ENGAGE using the cycle prediction.
                must_engage = secs_to_deadline <= c.hard_buffer_s
                next_light_s = cyc["minutes_to_next_light"] * 60.0
                light_imminent = next_light_s <= (secs_to_deadline - c.hard_buffer_s)
                last_resort = secs_to_deadline <= c.last_resort_min * 60
                if must_engage or (last_resort and not light_imminent):
                    self._engaged_at = now
                    reason = ("deadline buffer — waking despite deep sleep" if must_engage
                              else "no light window will arrive in time — waking now")
                else:
                    intent = ThermalIntent.WAKE_RAMP if now >= dawn_start else ThermalIntent.NEUTRAL
                    if light_imminent and last_resort:
                        phase, why = "wait_cycle", (
                            f"deep now, but a light ascent is ~{cyc['minutes_to_next_light']:.0f} min "
                            "out — waiting for the gentler wake")
                    elif now >= dawn_start:
                        phase, why = "dawn", "holding through deep — dawn ramp on"
                    else:
                        phase, why = "hold", "waiting for a light-sleep moment"
                    return WakeAction(phase, False, 0, intent, required_wake, p_wake, light, cyc, why)
        else:
            reason = "waking in progress"

        v = self._vibration_for(now, required_wake)
        phase = self._phase_for_vibration(v)
        return WakeAction(phase, True, v, ThermalIntent.WAKE_RAMP, required_wake, p_wake,
                          max(light, 0.5 if c.light_enabled else 0.0), cyc, reason,
                          vibration_pulse=self._pulse_for_phase(phase))
