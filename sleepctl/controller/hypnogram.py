"""Refuse hypnograms that sleep physiology does not allow.

WHAT THE PUBLISHED NIGHTS LOOK LIKE
-----------------------------------
    2026-08-29    0.0% REM
    2026-08-30   69.0% REM,  0.4% deep   -- REM/AWAKE flipping every 1-2 minutes for hours
    2026-08-31   16.3% REM, 14.2% deep, with DEEP scored 2 minutes after bed entry

A healthy adult runs roughly 20-25% REM and 13-23% deep. Two of those three nights are not
hypnograms at all, and the third opens with a stage that cannot occur when it occurs. The
2026-08-30 architecture accrual reports 336 minutes of REM against 2 minutes of deep, which then
tells in-night steering it is 216 minutes AHEAD on REM -- so the steerer defends a surplus that
does not exist while the real deep deficit goes unaddressed.

WHY HYSTERESIS DID NOT CATCH IT
-------------------------------
``SleepController._hold_stage`` damps sleep-stage flapping but exempts every transition
involving AWAKE, in both directions, and that exemption is correct: delaying a wake label to
smooth a chart would trade away the one thing this system exists to catch. The consequence is
that a stage which oscillates THROUGH awake bypasses the damping entirely, which is exactly the
2026-08-30 pattern: R A R A R A, every switch legal, the whole night nonsense.

WHAT THIS ADDS INSTEAD
----------------------
Constraints on WHEN a stage is possible, not on how fast it may change. Each one only ever
reclassifies REM or DEEP down to LIGHT; none of them can delay or suppress an AWAKE label, so
wake responsiveness is untouched.

  1. **Not before sleep.** REM and deep require sleep onset to have been confirmed. You cannot be
     in REM before you are asleep -- and on 2026-08-30, REM was scored from 22:13 against a
     sleep-onset latency of 77.7 minutes from a 21:39 bed entry.
  2. **Not too early after onset.** The first REM period follows onset by roughly 70-110 minutes
     in a normal adult; slow-wave sleep begins earlier but still takes minutes to build. The
     defaults here sit well below both so that a genuinely short-latency night is not rewritten.
  3. **Re-entry runs through light sleep.** After an awakening, sleep resumes in light sleep and
     descends from there -- it does not resume in REM. This is the rule that ends the
     REM/AWAKE oscillation, and it is a fact about sleep architecture rather than a smoothing
     trick.

Reclassified epochs keep their timing and are labelled LIGHT with reduced confidence, so a night
that trips these rules reads as "light sleep we are unsure about" rather than as a confident
REM night that never happened.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Tuple

from sleepctl.models import SleepStage

#: Confidence multiplier applied to a reclassified epoch. The model said REM; we are overruling
#: it on structural grounds and should not then present LIGHT as though it were observed.
RECLASSIFIED_CONFIDENCE = 0.6

_DEEPER_THAN_LIGHT = (SleepStage.REM, SleepStage.DEEP)


@dataclass
class HypnogramVerdict:
    stage: SleepStage
    confidence: Optional[float]
    reason: Optional[str] = None       # None when the stage was left alone

    @property
    def changed(self) -> bool:
        return self.reason is not None


class HypnogramConstraint:
    """Applies the timing rules above. One instance per controller; reset between nights."""

    def __init__(self) -> None:
        self._last_awake_at: Optional[datetime] = None
        self._seen_light_since_awake_at: Optional[datetime] = None
        self.reclassified: dict = {}
        self.last_reason: Optional[str] = None

    def reset(self) -> None:
        self._last_awake_at = None
        self._seen_light_since_awake_at = None
        self.reclassified = {}
        self.last_reason = None

    def observe(self, stage: SleepStage, now: datetime) -> None:
        """Record the ADOPTED stage, so re-entry is judged on what the night actually shows."""
        if stage is SleepStage.AWAKE:
            self._last_awake_at = now
            self._seen_light_since_awake_at = None
        elif stage is SleepStage.LIGHT and self._seen_light_since_awake_at is None:
            self._seen_light_since_awake_at = now

    def apply(self, stage: SleepStage, confidence: Optional[float], now: datetime, cfg,
              sleep_onset_time: Optional[datetime] = None) -> HypnogramVerdict:
        """Return the stage this moment can physiologically hold."""
        if stage not in _DEEPER_THAN_LIGHT:
            # AWAKE, LIGHT and UNKNOWN pass through untouched. In particular AWAKE is never
            # delayed, suppressed or downgraded here.
            self.last_reason = None
            return HypnogramVerdict(stage, confidence)
        t = getattr(cfg, "tunables", cfg)
        if not bool(getattr(t, "hypnogram_constraints", True)):
            return HypnogramVerdict(stage, confidence)

        reason = None
        if sleep_onset_time is None:
            reason = "before_sleep_onset"
        else:
            since_onset = (now - sleep_onset_time).total_seconds() / 60.0
            floor = float(getattr(t, "rem_earliest_min", 35.0) if stage is SleepStage.REM
                          else getattr(t, "deep_earliest_min", 8.0))
            if since_onset < floor:
                reason = ("rem_too_early_after_onset" if stage is SleepStage.REM
                          else "deep_too_early_after_onset")
        if reason is None and self._last_awake_at is not None:
            # Sleep resumes through light sleep. Either we have not seen light since the
            # awakening at all, or we have not been back in it long enough to have descended.
            need = float(getattr(t, "reentry_light_min", 5.0))
            if self._seen_light_since_awake_at is None:
                reason = "no_light_sleep_since_awakening"
            elif (now - self._seen_light_since_awake_at).total_seconds() / 60.0 < need:
                reason = "too_soon_after_awakening"

        self.last_reason = reason
        if reason is None:
            return HypnogramVerdict(stage, confidence)
        self.reclassified[reason] = self.reclassified.get(reason, 0) + 1
        conf = None if confidence is None else round(float(confidence) * RECLASSIFIED_CONFIDENCE, 4)
        return HypnogramVerdict(SleepStage.LIGHT, conf, reason)

    def summary(self) -> dict:
        return {"reclassified": dict(self.reclassified),
                "total": sum(self.reclassified.values()),
                "last_reason": self.last_reason}


def constrain(est: Tuple, now: datetime, cfg, constraint: HypnogramConstraint,
              sleep_onset_time: Optional[datetime] = None) -> Tuple:
    """Convenience wrapper for the controller's ``(stage, confidence, source)`` estimate tuple.

    ``source`` is passed through UNCHANGED. It names which estimator produced the label and is
    consumed as a fixed vocabulary elsewhere; appending a reason here silently broke that
    contract. The reason is recorded on the constraint instead (``last_reason`` / ``summary``),
    where it is published per tick without pretending to be a different estimator.
    """
    stage, conf, source = est
    v = constraint.apply(stage, conf, now, cfg, sleep_onset_time)
    return (v.stage, v.confidence, source)


#: Physiological bounds on a whole night's architecture, as fractions of realized sleep. Adult
#: norms are roughly 20-25% REM and 13-23% N3; these are deliberately wider than the norms --
#: the point is to catch a hypnogram that cannot be true, not to insist on an average night.
REM_FRACTION_BOUNDS = (0.05, 0.40)
DEEP_FRACTION_BOUNDS = (0.02, 0.40)

#: Below this much scored sleep the fractions are too noisy to judge.
MIN_MINUTES_TO_JUDGE = 90.0


def architecture_plausible(deep_min: float, rem_min: float, light_min: float
                           ) -> Tuple[bool, Optional[str]]:
    """Could this night's realized architecture be a real night's sleep?

    THE POINT IS TO STOP ACTING ON IT, not to correct it. On 2026-08-30 the accrual recorded 336
    minutes of REM against 2 minutes of deep -- 69% of scored sleep in REM -- and the in-night
    steerer read that as a 216-minute REM SURPLUS and spent the night defending it. Timing
    constraints tighten that hypnogram but cannot rescue it, because the error is the model
    mislabelling NREM as REM rather than mislabelling WHEN. So the honest handling is for the
    steerer to stand down on a night whose architecture is not believable, instead of steering
    confidently by a number that is wrong.
    """
    total = float(deep_min or 0.0) + float(rem_min or 0.0) + float(light_min or 0.0)
    if total < MIN_MINUTES_TO_JUDGE:
        return True, None                     # too little to judge; do not block on it
    rem_f = float(rem_min or 0.0) / total
    deep_f = float(deep_min or 0.0) / total
    if not (REM_FRACTION_BOUNDS[0] <= rem_f <= REM_FRACTION_BOUNDS[1]):
        return False, f"rem_fraction_{rem_f:.2f}"
    if not (DEEP_FRACTION_BOUNDS[0] <= deep_f <= DEEP_FRACTION_BOUNDS[1]):
        return False, f"deep_fraction_{deep_f:.2f}"
    return True, None
