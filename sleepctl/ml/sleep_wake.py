"""Dedicated SLEEP/WAKE detector using every channel the Verity actually gives us.

WHY A SEPARATE MODEL AT ALL
---------------------------
Two-stage and four-stage are different problems, and the literature is blunt about the gap.
Altini & Kinnunen (Sensors 2021;21:4302) report 94% for sleep/wake from an accelerometer alone
and 96% with autonomic features, against 57% and 79% for the same data at four stages. Our own
four-class stager sits at kappa 0.395-0.419 with wake_f1 0.450-0.493 -- and WAKE is the thing
this system exists to act on: awakening detection drives pre-emption, the wake ledger, WASO and
every learner downstream. Asking one four-class model to also be the wake detector taxes the
easy problem with the hard one's errors.

WHAT THIS USES THAT NOTHING ELSE DID
------------------------------------
``hrv_features`` -- the inter-beat-interval extractor in the Topalidis lineage -- was written and
then wired to nothing. Its own docstring records the situation: the Verity streams true
beat-to-beat PPI, we persist it in ``rr_intervals`` (28,960 intervals in one night, 0.0% outside
the 300-2000 ms physiological band), and it "never reaches the sleep stager". That is the single
richest unused signal we have, and it is the one Topalidis et al. (Sensors 2023;23:9077) rode to
kappa 0.75 on THIS EXACT SENSOR, from intervals alone, without an accelerometer.

So this detector fuses three INDEPENDENT channels:

  1. **autonomic** -- HRV features from raw IBIs (the channel that was going unused),
  2. **motion**    -- Cole-Kripke over the armband's own actigraphy counts (a PSG-validated
     algorithm, in the modality it was validated for),
  3. **cardiac**   -- heart rate against the night's own sleeping level.

MEASURED CALIBRATION (2026-08-28, nights 2026-08-23/24/27, 1604 epochs)
----------------------------------------------------------------------
The promise recorded below -- that this channel stays off "until it has been calibrated against
real nights" -- has now been kept. ``sleepctl.eval.autonomic_calibration`` scores each feature by
AUC against Cole-Kripke+Webster on the armband's own counts: a different MODALITY from the
inter-beat intervals these features come from, so a feature that separates is carrying real
information rather than echoing its own label.

Two corrections had to land before the numbers meant anything, and both changed the answer:

  * the published nights mix a naive-local clock with an absolute one, so read from anywhere but
    the box every epoch got someone else's heart rate. Device heart rate and ``hr_from_ibi``
    disagreed by a mean of 47 bpm; aligned, 3.6.
  * the nights contained hours of walking-around morning, because nothing could end a session
    (see ``sleepctl/controller/bed_exit.py``). Left in, ordinary daytime physiology stands in for
    this sleeper's wake distribution.

    feature        AUC    per-night range   direction held
    hr_from_ibi   0.688    0.629 - 0.745    3 of 3
    ibi_sd1_sd2   0.633    0.597 - 0.687    3 of 3
    ibi_hf_nu     0.579    0.486 - 0.663    2 of 3
    ibi_lf_hf     0.579    0.486 - 0.663    2 of 3
    ibi_lf_nu     0.579    0.486 - 0.663    2 of 3
    ibi_hf        0.429    0.325 - 0.522    1 of 3   (backwards)
    ibi_rmssd     0.357    0.248 - 0.472    0 of 3   (backwards)
    ibi_pnn50     0.356    0.251 - 0.453    0 of 3   (backwards)

The three backwards features are the RAW time-domain vagal indices, and the most likely cause is
the reference rather than the physiology: it scores wake from MOTION, and motion corrupts PPG
beat detection toward more interval variability -- so its wake epochs are exactly the epochs
where RMSSD and pNN50 are inflated by artifact. SD1/SD2 is a ratio and largely cancels that,
which is consistent with it being the one vagal feature that behaves. Three nights against a
confounded reference is not grounds to rewrite directions the physiology supports, so the weights
above are unchanged.

As one composite score the channel reaches AUC 0.535 -- one night below 0.5 -- and at its best
threshold (0.69, not the 0.5 shipped here) it manages kappa 0.066.

THE CONCLUSION, AND WHY THE DEFAULT DOES NOT CHANGE. The channel as configured separates almost
nothing. The one feature that clearly does is heart rate, and the CARDIAC channel already carries
heart rate; the features that would have made this INDEPENDENT evidence rather than a second
opinion on the first are either weak or confounded. Switching it on would add weight, not
information. Three nights is thin and the measurement re-runs in one command as more accumulate
(``scripts/calibrate_autonomic.py``); what it does not support is turning this on today.

WHAT THIS IS NOT
----------------
It is NOT a model fitted to polysomnography. Training an IBI-to-PSG classifier needs
IBI-with-PSG data, which we do not have -- the PhysioNet corpus behind the four-class stager
carries wrist HR, not beat-to-beat intervals. So the combination rule here is not learned; it
scores each channel against the NIGHT'S OWN distribution and sums with fixed weights, using only
directions that are established in the autonomic sleep literature (waking raises heart rate and
LF/HF, and suppresses vagally-mediated RMSSD/pNN50).

Being relative to the night rather than to absolute thresholds is deliberate: it removes the
unit-scale problem that has bitten this project repeatedly, and it personalises automatically.
The honest claim is "three independent channels agreeing", not "validated accuracy" -- and
``sleepctl.eval.reference_stagers`` is how that claim gets checked.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from sleepctl.ml.sleep_staging.hrv_features import hrv_features

#: Features whose INCREASE indicates wake, and whose decrease indicates sleep, with the weight
#: each carries. Directions are from the autonomic sleep literature, not fitted here.
_WAKE_UP_FEATURES = {
    "hr_from_ibi": 1.0,    # waking raises heart rate -- the most robust single indicator
    "ibi_lf_hf": 0.6,      # sympathovagal balance shifts sympathetic on waking
    "ibi_lf_nu": 0.3,      # normalised LF rises with that same shift
}
#: Features whose DECREASE indicates wake -- all vagally mediated, and all suppressed by arousal.
#: Note ``ibi_sd1_sd2`` is SD1/SD2: SD1 is the SHORT-term (vagal) axis of the Poincare ellipse,
#: so the ratio FALLS on waking. Getting that direction backwards would have scored every
#: awakening as evidence of sleep. MEASURED on this sleeper (see MEASURED CALIBRATION in the
#: module docstring) the direction holds on 3 nights of 3, at AUC 0.633 -- the second strongest
#: feature here, and the only vagal one whose direction survives contact with real data. Its
#: raw counterparts ``ibi_rmssd`` and ``ibi_pnn50`` come out backwards, which is most likely the
#: reference's fault rather than the physiology's: the reference scores wake FROM MOTION, and
#: motion corrupts PPG beat detection in the direction of more interval variability. SD1/SD2 is
#: a ratio, so that artifact largely cancels. The weights below are therefore left as the
#: literature sets them -- three nights against a motion-derived reference is not grounds to
#: rewrite established directions, and the channel is off regardless.
_WAKE_DOWN_FEATURES = {
    "ibi_rmssd": 0.8,
    "ibi_pnn50": 0.5,
    "ibi_hf": 0.4,
    "ibi_hf_nu": 0.3,
    "ibi_sd1_sd2": 0.3,
}

#: Every feature the autonomic channel reads. Exported per night so the channel can be
#: calibrated against real data before it is trusted with a control decision.
AUTONOMIC_FEATURE_NAMES = tuple(sorted(set(_WAKE_UP_FEATURES) | set(_WAKE_DOWN_FEATURES)))

#: Minimum windows before the night's own distribution means anything.
_MIN_WINDOWS = 8


def _median(xs: Sequence[float]) -> Optional[float]:
    xs = [x for x in xs if x is not None and not math.isnan(x)]
    return statistics.median(xs) if xs else None


def _mad(xs: Sequence[float], med: float) -> float:
    """Median absolute deviation -- robust scale. Outlier-heavy HRV features make the standard
    deviation a poor normaliser; a single artifact epoch would otherwise flatten every z-score."""
    devs = [abs(x - med) for x in xs if x is not None and not math.isnan(x)]
    m = statistics.median(devs) if devs else 0.0
    return m if m > 1e-9 else 1.0


@dataclass
class SleepWakeResult:
    """One epoch's verdict, with the per-channel evidence that produced it."""
    awake: bool
    score: float                       # 0 = deeply asleep, 1 = clearly awake
    channels: Dict[str, Optional[float]] = field(default_factory=dict)
    reasons: List[str] = field(default_factory=list)
    n_channels: int = 0

    def to_dict(self) -> dict:
        return {"awake": self.awake, "score": round(self.score, 3),
                "channels": {k: (round(v, 3) if v is not None else None)
                             for k, v in self.channels.items()},
                "reasons": self.reasons, "n_channels": self.n_channels}


class SleepWakeDetector:
    """Fuses autonomic, motion and cardiac evidence into a sleep/wake call per epoch.

    Stateless across nights: every call scores a whole night's windows together, because the
    normalisation is against that night's own distribution.
    """

    #: The autonomic channel is OFF by default and must be switched on deliberately.
    #:
    #: Its weights, threshold and squash steepness are hand-set from literature DIRECTIONS, with
    #: no labelled IBI data to fit them against -- the PhysioNet corpus behind the four-class
    #: stager carries wrist HR, not beat-to-beat intervals. Measured on synthetic data with a
    #: known 80/20 split it scored 47.6% accuracy with 22 false positives, i.e. worse than
    #: useless as a standalone detector. Cole-Kripke on the same epochs is PSG-validated at ~88%.
    #:
    #: So the features are EXTRACTED and EXPORTED (that is the point -- they were reaching
    #: nothing at all before), but they do not drive a control decision until they have been
    #: calibrated against real nights. Turning this on without that calibration would be
    #: substituting a plausible story for a measurement.
    AUTONOMIC_DEFAULT = False

    def __init__(self, threshold: float = 0.5, motion_weight: float = 1.0,
                 sleep_quantile: float = 0.4,
                 use_autonomic: Optional[bool] = None) -> None:
        self.use_autonomic = (self.AUTONOMIC_DEFAULT if use_autonomic is None
                              else bool(use_autonomic))
        self.threshold = float(threshold)
        self.motion_weight = float(motion_weight)
        # Where the "asleep" zero point sits in the night's own distribution -- see
        # _autonomic_scores. 0.4 assumes a night in bed is mostly asleep without assuming a
        # specific sleep efficiency.
        self.sleep_quantile = float(sleep_quantile)

    # -- channel 1: autonomic -------------------------------------------------
    def _autonomic_scores(self, windows: Sequence[Dict[str, float]]) -> List[Optional[float]]:
        """Per-window wake evidence in [0,1] from HRV, or None where features are missing."""
        usable = [w for w in windows if w]
        if len(usable) < _MIN_WINDOWS:
            return [None] * len(windows)
        # Anchor on the SLEEPING level, not the night's median. Normalising to the median makes
        # roughly half the night score above baseline by construction, which is wrong for a
        # minority class: measured on synthetic data with a clean 80/20 split, sleep epochs came
        # out at 0.54-0.70 against wake at 0.58-0.87 -- overlapping, and useless. A normal night
        # in bed is mostly asleep, so a quantile in the sleep-typical direction is a far better
        # zero point, and it still personalises to the night.
        stats: Dict[str, Tuple[float, float]] = {}
        for name, direction in ([(n, +1) for n in _WAKE_UP_FEATURES]
                                + [(n, -1) for n in _WAKE_DOWN_FEATURES]):
            vals = sorted(w[name] for w in usable if name in w and w[name] is not None)
            if not vals:
                continue
            # For a feature that RISES on waking, sleep sits low -> anchor low. For one that
            # FALLS on waking, sleep sits high -> anchor high.
            q = self.sleep_quantile if direction > 0 else (1.0 - self.sleep_quantile)
            anchor = vals[min(len(vals) - 1, max(0, int(q * len(vals))))]
            stats[name] = (anchor, _mad(vals, _median(vals) or anchor))
        # A name that matches NOTHING scores every epoch identically and silently -- which is
        # exactly what happened on the first run of this module, when the feature keys here were
        # missing their `ibi_` prefix and the whole autonomic channel quietly returned 0.0 for
        # the entire night with no error anywhere. Refuse to pretend that is a measurement.
        if not stats:
            raise ValueError(
                "no HRV feature names matched the extractor's output; expected any of "
                f"{sorted(set(_WAKE_UP_FEATURES) | set(_WAKE_DOWN_FEATURES))}, "
                f"got {sorted(usable[0].keys())[:8]}...")
        out: List[Optional[float]] = []
        for w in windows:
            if not w:
                out.append(None)
                continue
            num = den = 0.0
            for name, weight in _WAKE_UP_FEATURES.items():
                if name in stats and w.get(name) is not None:
                    med, scale = stats[name]
                    num += weight * _squash((w[name] - med) / scale)
                    den += weight
            for name, weight in _WAKE_DOWN_FEATURES.items():
                if name in stats and w.get(name) is not None:
                    med, scale = stats[name]
                    num += weight * _squash((med - w[name]) / scale)
                    den += weight
            out.append(num / den if den > 0 else None)
        return out

    # -- the fusion -----------------------------------------------------------
    def score_night(self, hrv_windows: Sequence[Dict[str, float]],
                    counts: Optional[Sequence[float]] = None,
                    hr: Optional[Sequence[Optional[float]]] = None) -> List[SleepWakeResult]:
        """Score a whole night. Any channel may be absent; the result records how many voted.

        A missing channel is dropped from the average rather than scored as zero -- treating
        "no accelerometer tonight" as positive evidence of sleep is how a sensor outage becomes
        a report of perfect sleep, which is a failure mode this project has already lived through.
        """
        n = len(hrv_windows)
        auto = (self._autonomic_scores(hrv_windows) if self.use_autonomic else [None] * n)

        motion: List[Optional[float]] = [None] * n
        if counts is not None and len(counts) and any(c for c in counts):
            # Cole-Kripke WITH Webster rescoring: the PSG-validated pairing, run in the modality
            # it was validated for (the armband's own counts). Webster is not optional garnish
            # here -- it exists to correct actigraphy's habit of scoring quiet wakefulness as
            # sleep, which is the precise blind spot found on 2026-08-27, where 16 of 17 missed
            # wake minutes sat within five minutes of an awakening we had already detected.
            from sleepctl.eval.reference_stagers import (calibrate_scale, cole_kripke,
                                                         webster_rescore)
            c = list(counts)[:n]
            ck = webster_rescore(cole_kripke(c, scale=calibrate_scale(c)))
            motion = [(0.0 if asleep else 1.0) for asleep in ck] + [None] * max(0, n - len(ck))
            motion = motion[:n]

        cardiac: List[Optional[float]] = [None] * n
        if hr is not None:
            vals = [v for v in hr if v is not None]
            if len(vals) >= _MIN_WINDOWS:
                sv = sorted(vals)
                anchor = sv[min(len(sv) - 1, max(0, int(self.sleep_quantile * len(sv))))]
                scale = _mad(vals, _median(vals) or anchor)
                cardiac = [None if v is None else _squash((v - anchor) / scale) for v in hr[:n]]
                cardiac += [None] * max(0, n - len(cardiac))
                cardiac = cardiac[:n]

        results: List[SleepWakeResult] = []
        for i in range(n):
            parts: List[Tuple[str, float, float]] = []
            if auto[i] is not None:
                parts.append(("autonomic", auto[i], 1.0))
            if motion[i] is not None:
                parts.append(("motion", motion[i], self.motion_weight))
            if cardiac[i] is not None:
                parts.append(("cardiac", cardiac[i], 0.6))
            if not parts:
                results.append(SleepWakeResult(False, 0.0, {}, ["no channels"], 0))
                continue
            score = sum(v * w for _, v, w in parts) / sum(w for _, _, w in parts)
            reasons = [name for name, v, _ in parts if v >= 0.6]
            results.append(SleepWakeResult(
                awake=score >= self.threshold, score=score,
                channels={"autonomic": auto[i], "motion": motion[i], "cardiac": cardiac[i]},
                reasons=reasons, n_channels=len(parts)))
        return results


def _squash(z: float) -> float:
    """Robust z -> [0,1]. A logistic keeps a single wild epoch from dominating, which a linear
    scaling would let it do."""
    try:
        return 1.0 / (1.0 + math.exp(-1.2 * z))
    except OverflowError:
        return 0.0 if z < 0 else 1.0


def windows_from_ibis(rr: Sequence[Tuple[float, float]], epoch_s: float = 60.0,
                      window_s: float = 300.0) -> Tuple[List[Dict[str, float]], List[float]]:
    """Slice a flat ``[(epoch_seconds, rr_ms)]`` series into per-epoch HRV feature windows.

    Each epoch is characterised by the ``window_s`` of intervals ENDING at it, mirroring how the
    detector runs live (only past data is available). Returns the features and the epoch start
    times, so callers can align them with actigraphy and heart rate.
    """
    if not rr:
        return [], []
    rr = sorted(rr, key=lambda x: x[0])
    t0, t1 = rr[0][0], rr[-1][0]
    starts: List[float] = []
    feats: List[Dict[str, float]] = []
    t = t0
    while t <= t1:
        lo = t - window_s
        win = [(ts, v) for ts, v in rr if lo <= ts <= t]
        starts.append(t)
        feats.append(hrv_features([w[0] for w in win], [w[1] for w in win]) if len(win) >= 8
                     else {})
        t += epoch_s
    return feats, starts
