"""Detect that the sleeper has GOT UP, using only the wearable.

WHY THIS EXISTS
---------------
``arousal.py`` grades one of its four levels ``OUT_OF_BED``, and the only thing that can ever
raise it is ``frame.presence is False``. On an Eight Sleep account without an Autopilot
membership presence is ``None`` forever -- the same fact that made ``_wearable_bed_entry``
necessary in the first place. So this system has had a bed-ENTRY substitute for months and no
bed-EXIT substitute at all, and the asymmetry is exactly as bad as it sounds: sessions could
start on wearable evidence but only ever end on a deadline.

WHAT THAT COST, MEASURED
------------------------
On the published nights:

    2026-08-25   the ENTIRE active span is daytime, last active tick 18:37
    2026-08-27   active until 11:21, with 276 ticks at HR > 95 bpm
    2026-08-24   active until 11:59

2026-08-27 is the clearest. The controller re-entered INDUCTION at about 06:00 and ran
induction/maintenance until 11:21 while the wearable reported a median heart rate of 102-122 bpm
by local hour -- someone up and moving through their morning. Read back through the export, that
night reported a mean SLEEPING heart rate of 104 bpm against 69 bpm awake.

Three separate harms, all from one gap:

  1. the Pod is heated or cooled for hours against an empty bed,
  2. every statistic computed from the night -- staging, WASO, sleep efficiency, the reference
     comparisons, the HRV calibration -- is contaminated with waking daytime physiology,
  3. ``_wearable_bed_entry`` re-opens a new "night" every morning, because its HR ceiling of
     120 bpm admits a person walking around their apartment.

WHAT COUNTS AS EVIDENCE
-----------------------
Three channels, none of which needs the Pod:

  * **orthostatic heart rate** -- standing up raises heart rate ~10-20 bpm and ambulation raises
    it further. Scored against THIS night's own lying baseline, not an absolute number, so it
    personalises and survives a night with a fever or a hard training day.
  * **sustained motion** -- lying in bed produces brief bursts separated by stillness; being up
    produces motion in most epochs. The discriminator is the FRACTION of a window that is
    active, not any single peak, which is what makes it different from every other movement
    signal in this codebase (they all read amplitude).
  * **an absolute lying ceiling** -- a sustained heart rate above roughly 95 bpm is not someone
    asleep, whatever their baseline.

No single channel ends a session on its own except the compound one (elevated AND moving).
Requiring agreement is deliberate: a false bed-exit drops the thermal command mid-night, which
is precisely the disturbance this controller exists to avoid.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional, Sequence

#: How many of the most recent HR observations taken while genuinely asleep define the lying
#: baseline. ~90 ticks is an hour and a half at one tick a minute -- long enough to be stable,
#: short enough to track a night that drifts.
BASELINE_WINDOW = 90

#: Below this many baseline observations the orthostatic rule abstains rather than guessing.
MIN_BASELINE_SAMPLES = 15

#: Heart rates at or above this never enter the lying baseline, whatever the state machine says
#: about the tick. See ``observe_sleeping``.
BASELINE_MAX_HR = 95.0

#: Where in the sorted sleeping-HR sample the baseline sits.
BASELINE_QUANTILE = 0.4


@dataclass
class BedExitAssessment:
    """One tick's verdict on whether the sleeper is out of bed, with its evidence."""

    out_of_bed: bool = False
    confidence: float = 0.0
    reasons: List[str] = field(default_factory=list)
    hr_excess: Optional[float] = None       # bpm above the night's lying baseline
    active_fraction: Optional[float] = None  # share of the window with real movement
    lying_baseline: Optional[float] = None
    n_ticks: int = 0

    def to_dict(self) -> dict:
        return {"out_of_bed": self.out_of_bed, "confidence": round(self.confidence, 3),
                "reasons": list(self.reasons),
                "hr_excess": (None if self.hr_excess is None else round(self.hr_excess, 1)),
                "active_fraction": (None if self.active_fraction is None
                                    else round(self.active_fraction, 3)),
                "lying_baseline": (None if self.lying_baseline is None
                                   else round(self.lying_baseline, 1)),
                "n_ticks": self.n_ticks}


class BedExitDetector:
    """Tracks the night's lying heart rate and reports when the evidence says "up"."""

    def __init__(self) -> None:
        self._lying_hr: List[float] = []
        self._out_since: Optional[datetime] = None
        self.last: BedExitAssessment = BedExitAssessment()

    # -- baseline ------------------------------------------------------------
    def observe_sleeping(self, heart_rate: Optional[float]) -> None:
        """Feed a heart rate sampled while the sleeper is believed to be ASLEEP.

        The caller decides what "asleep" means (the controller uses a non-AWAKE stage in
        MAINTENANCE). Keeping that judgement outside this class means the baseline can never be
        poisoned by the very ticks this detector is being asked to rule on.
        """
        if heart_rate is None:
            return
        try:
            hr = float(heart_rate)
        except (TypeError, ValueError):
            return
        if not (25.0 <= hr <= BASELINE_MAX_HR):
            # A rate this high is not a sleeping rate, whatever the state machine believes. The
            # ceiling is what stops the baseline being POISONED by the very situation this
            # detector exists to catch: replayed on 2026-08-27, where the controller sat in
            # MAINTENANCE through a walking-around morning, the unfiltered baseline climbed to
            # 108.5 bpm -- and an orthostatic rule measured against 108.5 can never fire again.
            return
        self._lying_hr.append(hr)
        if len(self._lying_hr) > BASELINE_WINDOW:
            del self._lying_hr[:-BASELINE_WINDOW]

    @property
    def lying_baseline(self) -> Optional[float]:
        """This night's sleeping heart rate, or None until there is enough of it to say.

        A low quantile rather than the median: the samples feeding this are labelled asleep by
        the state machine, and the state machine is exactly what goes wrong here. Sitting below
        the middle of the distribution means a stretch of mislabelled ticks has to be large
        before it moves the bar.
        """
        if len(self._lying_hr) < MIN_BASELINE_SAMPLES:
            return None
        vals = sorted(self._lying_hr)
        return vals[max(0, min(len(vals) - 1, int(BASELINE_QUANTILE * len(vals))))]

    def reset(self) -> None:
        self._lying_hr.clear()
        self._out_since = None
        self.last = BedExitAssessment()

    # -- the assessment ------------------------------------------------------
    def assess(self, frame, recent: Sequence, cfg, now: Optional[datetime] = None
               ) -> BedExitAssessment:
        """Grade the trailing window. ``recent`` is the controller's frame history."""
        t = getattr(cfg, "tunables", cfg)
        window_min = float(getattr(t, "bed_exit_window_min", 10.0))
        need = max(3, int(window_min))
        window = list(recent or [])[-(need - 1):] + [frame]
        hrs = [float(f.heart_rate) for f in window
               if getattr(f, "heart_rate", None) is not None]
        moves = [float(f.movement) for f in window
                 if getattr(f, "movement", None) is not None]

        assessment = BedExitAssessment(n_ticks=len(window), lying_baseline=self.lying_baseline)
        if len(window) < need or len(hrs) < max(3, need // 2):
            # Not enough of a window to judge. Abstaining is the safe answer: the cost of a
            # missed exit is a warm empty bed, and the cost of a false one is waking the sleeper.
            self.last = assessment
            self._out_since = None
            return assessment

        median_hr = statistics.median(hrs)
        base = self.lying_baseline
        reasons: List[str] = []
        if base is not None:
            assessment.hr_excess = median_hr - base
            if assessment.hr_excess >= float(getattr(t, "bed_exit_hr_excess_bpm", 18.0)):
                reasons.append("hr_orthostatic")
        if moves:
            thresh = float(getattr(t, "bed_exit_motion_threshold", 0.25))
            assessment.active_fraction = sum(1 for m in moves if m >= thresh) / len(moves)
            if assessment.active_fraction >= float(
                    getattr(t, "bed_exit_active_fraction", 0.6)):
                reasons.append("sustained_motion")
        if median_hr >= float(getattr(t, "bed_exit_hr_ceiling", 95.0)):
            reasons.append("hr_above_lying_ceiling")

        # WHAT COMBINATION IS ALLOWED TO END A SESSION
        #
        # Count CHANNELS, not reasons. `hr_orthostatic` and `hr_above_lying_ceiling` are two
        # readings of one signal, and an early version of this that counted reasons treated
        # their agreement as corroboration -- which it is not, and which is the same mistake as
        # scoring a stager against itself.
        #
        # Two real channels (heart rate AND motion) is the fast path, at
        # ``bed_exit_persist_min``. Heart rate ALONE is allowed to act, but only on the slow
        # path, because on this hardware it is often the only channel there is: replaying
        # 2026-08-27, the movement index read a flat 0.022 with ZERO samples above 0.05 through
        # the entire 07:00-11:00 walking-around morning -- lower than during actual sleep --
        # while heart rate sat at 102-122 bpm. A motion-corroboration requirement would have
        # detected nothing at all on the very night that motivated this module. So the slow path
        # asks for a longer hold instead of a second channel, and it demands BOTH heart-rate
        # readings whenever a baseline exists: above an absolute lying ceiling AND well above
        # this sleeper's own sleeping rate.
        hr_reasons = [r for r in reasons if r.startswith("hr_")]
        channels = (1 if hr_reasons else 0) + (1 if "sustained_motion" in reasons else 0)
        persist_min = float(getattr(t, "bed_exit_persist_min", 5.0))
        if channels >= 2:
            qualifies = True
        elif "hr_above_lying_ceiling" in reasons and (
                base is None or "hr_orthostatic" in reasons):
            qualifies = True
            persist_min = float(getattr(t, "bed_exit_hr_only_persist_min", 15.0))
        else:
            # Motion alone is a restless hour of turning over, not someone standing up.
            qualifies = False
        assessment.reasons = reasons
        stamp = now or getattr(frame, "timestamp", None)
        if qualifies:
            if self._out_since is None:
                self._out_since = stamp
            held = ((stamp - self._out_since).total_seconds() / 60.0
                    if stamp and self._out_since else 0.0)
            assessment.out_of_bed = held >= persist_min
            # Confidence grows with how long it has held and how many channels agree, and is
            # reported even before the persistence threshold is met so the telemetry shows the
            # detector working up to a decision rather than flipping out of nowhere.
            assessment.confidence = min(1.0, 0.4 + 0.2 * len(reasons)
                                        + 0.4 * min(1.0, held / max(persist_min, 1e-9)))
        else:
            self._out_since = None
            assessment.confidence = 0.0
        self.last = assessment
        return assessment

    # -- the entry side ------------------------------------------------------
    def blocks_entry(self, frame, recent: Sequence, cfg) -> Optional[str]:
        """Reason to REFUSE a wearable bed entry right now, or None to allow it.

        Entry is a different question from exit and needs different evidence. Someone who has
        just lain down still has a walked-up heart rate, so the orthostatic rule would block
        every genuine bed entry -- but they are, by definition, LYING STILL. Stillness is
        therefore the entry gate, alongside a heart-rate ceiling far below the 120 bpm that let
        a morning of walking around open a new night.
        """
        t = getattr(cfg, "tunables", cfg)
        need = max(3, int(float(getattr(t, "bed_exit_window_min", 10.0))))
        window = list(recent or [])[-(need - 1):] + [frame]
        if len(window) < need:
            return None
        moves = [float(f.movement) for f in window
                 if getattr(f, "movement", None) is not None]
        if moves:
            thresh = float(getattr(t, "bed_exit_motion_threshold", 0.25))
            active = sum(1 for m in moves if m >= thresh) / len(moves)
            if active >= float(getattr(t, "bed_entry_max_active_fraction", 0.4)):
                return f"still moving ({active:.0%} of window active)"
        hrs = [float(f.heart_rate) for f in window
               if getattr(f, "heart_rate", None) is not None]
        if hrs:
            median_hr = statistics.median(hrs)
            ceiling = float(getattr(t, "bed_entry_hr_ceiling", 95.0))
            if median_hr >= ceiling:
                return f"heart rate {median_hr:.0f} bpm is not someone lying down"
        return None
