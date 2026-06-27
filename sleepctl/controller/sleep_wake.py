"""Calibrated multi-signal sleep/wake classifier + an AUC validation harness.

Sleep maintenance is the #1 problem, so the awakening LABEL must be trustworthy — a noisy
wake log can't be backtested into a personalized wake-trajectory. This fuses every converging
signal the cloud exposes (all piezo/BCG-derived) into a single calibrated P(wake) you can rank
and threshold, and ships a rank-based AUC harness so "solid" is a measured number, not a hope.

Honest ceiling: at ~60s cloud resolution, cardiorespiratory + actigraphic wake detection tops out
around ~0.85–0.90 AUC in the literature (wake is the hardest state to separate; Fonseca 2016
doi:10.1109/JBHI.2016.2550104, Kwon 2021 doi:10.1109/JBHI.2021.3072644). The raw piezo WAVEFORM
that would push higher needs rooting (out of scope). This maximizes what the cloud allows and
measures it; the signal vector per detection is exposed so awakenings can be cataloged + backtested.

Converging signals (each evidence toward wake, in log-odds):
  bed-exit (presence False) -> near-certain | AWAKE stage | stage regression (deep/REM->light/awake)
  HR elevation vs sleep baseline + rising HR trend | HRV drop (sympathetic shift)
  respiratory-rate variability | body movement + movement DENSITY over the window
Deep/REM with calm physiology is negative evidence (more confident asleep).
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from typing import List, Optional

from sleepctl.models import SensorFrame, SleepStage


def _mean(xs):
    vals = [v for v in xs if v is not None]
    return statistics.fmean(vals) if vals else None


def _slope_per_min(vals: List[float]) -> float:
    """Least-squares slope of a 1/min series (units per minute)."""
    pts = [(i, v) for i, v in enumerate(vals) if v is not None]
    if len(pts) < 3:
        return 0.0
    n = len(pts)
    mx = sum(x for x, _ in pts) / n
    my = sum(y for _, y in pts) / n
    den = sum((x - mx) ** 2 for x, _ in pts)
    return 0.0 if den == 0 else sum((x - mx) * (y - my) for x, y in pts) / den


@dataclass
class WakeProb:
    p: float                          # calibrated P(wake) in [0, 1]
    label: str                        # "wake" | "sleep" at the threshold
    signals: dict = field(default_factory=dict)   # signal -> log-odds contribution (the vector)


# Evidence-grounded log-odds weights (bias = baseline log-odds of being asleep).
_W = {
    "bias": -2.6,
    "awake_stage": 4.0,
    "stage_regression": 1.0,
    "light_stage": 0.5,
    "deep_rem": -1.1,        # calm deep/REM -> more confident asleep
    "hr_elev": 2.2,          # per ~10 bpm over baseline (clamped 0..1)
    "hr_trend": 1.0,         # rising HR slope (clamped)
    "hrv_drop": 1.6,         # fractional drop vs baseline (clamped 0..1)
    "rr_var": 1.0,           # respiratory variability (clamped)
    "movement": 2.6,         # instantaneous motion (clamped 0..1)
    "move_density": 1.6,     # fraction of the window in motion
}


class SleepWakeClassifier:
    """Fuses converging signals into a calibrated P(wake). Pure, stateless per call."""

    def __init__(self, cfg=None, threshold: float = 0.5, weights: Optional[dict] = None) -> None:
        self.threshold = threshold
        self.w = dict(_W)
        if weights:
            self.w.update(weights)
        t = getattr(cfg, "tunables", None)
        self.move_floor = getattr(t, "arousal_movement", 0.4)

    def probability(self, frame: SensorFrame, recent: List[SensorFrame],
                    sleep_hr_baseline: Optional[float] = None,
                    sleep_hrv_baseline: Optional[float] = None) -> WakeProb:
        # Bed exit is near-certain wake regardless of physiology.
        if frame.presence is False:
            return WakeProb(0.99, "wake", {"bed_exit": 6.0})

        w = self.w
        window = (recent or [])[-12:]
        contrib: dict = {"bias": w["bias"]}
        z = w["bias"]

        # --- stage evidence ---
        prev = window[-1].stage if window else SleepStage.UNKNOWN
        if frame.stage is SleepStage.AWAKE:
            z += w["awake_stage"]; contrib["awake_stage"] = w["awake_stage"]
        elif frame.stage is SleepStage.LIGHT:
            z += w["light_stage"]; contrib["light_stage"] = w["light_stage"]
        elif frame.stage in (SleepStage.DEEP, SleepStage.REM):
            z += w["deep_rem"]; contrib["deep_rem"] = w["deep_rem"]
        if frame.stage in (SleepStage.LIGHT, SleepStage.AWAKE) and prev in (
                SleepStage.DEEP, SleepStage.REM):
            z += w["stage_regression"]; contrib["stage_regression"] = w["stage_regression"]

        # --- cardiac: elevation vs the sleep baseline + a rising trend ---
        base_hr = sleep_hr_baseline if sleep_hr_baseline is not None else _mean(
            [f.heart_rate for f in window[:-2]])
        if frame.heart_rate is not None and base_hr:
            ev = max(0.0, min(1.0, (frame.heart_rate - base_hr) / 10.0))
            if ev > 0:
                c = w["hr_elev"] * ev; z += c; contrib["hr_elev"] = round(c, 3)
        hr_series = [f.heart_rate for f in window] + [frame.heart_rate]
        slope = _slope_per_min(hr_series)
        if slope > 0:
            ev = min(1.0, slope / 3.0)
            c = w["hr_trend"] * ev; z += c; contrib["hr_trend"] = round(c, 3)

        # --- HRV drop (sympathetic shift) ---
        base_hrv = sleep_hrv_baseline if sleep_hrv_baseline is not None else _mean(
            [f.hrv for f in window[:-2]])
        if frame.hrv is not None and base_hrv:
            ev = max(0.0, min(1.0, (base_hrv - frame.hrv) / base_hrv))
            if ev > 0:
                c = w["hrv_drop"] * ev; z += c; contrib["hrv_drop"] = round(c, 3)

        # --- respiratory variability ---
        rrs = [f.respiratory_rate for f in window if f.respiratory_rate is not None]
        if len(rrs) >= 4 and frame.respiratory_rate is not None:
            sd = statistics.pstdev(rrs + [frame.respiratory_rate])
            ev = max(0.0, min(1.0, (sd - 1.0) / 2.0))
            if ev > 0:
                c = w["rr_var"] * ev; z += c; contrib["rr_var"] = round(c, 3)

        # --- movement: instantaneous + density over the window ---
        if frame.movement is not None:
            ev = max(0.0, min(1.0, frame.movement))
            c = w["movement"] * ev; z += c; contrib["movement"] = round(c, 3)
        movers = [f.movement for f in window if f.movement is not None]
        if movers:
            density = sum(1 for m in movers if m >= self.move_floor) / len(movers)
            if density > 0:
                c = w["move_density"] * density; z += c; contrib["move_density"] = round(c, 3)

        p = 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, z))))
        return WakeProb(round(p, 4), "wake" if p >= self.threshold else "sleep", contrib)


# ------------------------------------------------------------------ AUC validation
def auc(scores: List[float], labels: List[int]) -> Optional[float]:
    """Rank-based (Mann–Whitney) AUC for P(wake) vs binary wake labels. None if one class only."""
    pos = sum(1 for l in labels if l)
    neg = len(labels) - pos
    if pos == 0 or neg == 0:
        return None
    order = sorted(range(len(scores)), key=lambda i: scores[i])
    ranks = [0.0] * len(scores)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and scores[order[j + 1]] == scores[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1.0          # average 1-based rank for ties
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    sum_pos = sum(r for r, l in zip(ranks, labels) if l)
    return (sum_pos - pos * (pos + 1) / 2.0) / (pos * neg)


def score_night(classifier: SleepWakeClassifier, frames: List[SensorFrame], labels: List[int]):
    """Run the classifier across a night, returning (scores, labels) aligned for AUC."""
    scores = []
    recent: List[SensorFrame] = []
    for f in frames:
        wp = classifier.probability(f, recent)
        scores.append(wp.p)
        recent.append(f)
        if len(recent) > 20:
            recent.pop(0)
    return scores, list(labels)


def catalog_awakening_signals(repo, classifier: Optional[SleepWakeClassifier] = None,
                              nights: int = 7, limit: int = 40) -> List[dict]:
    """Catalog each mid-sleep awakening with the CONVERGING SIGNAL VECTOR that flagged it — the
    record the maintenance backtest needs to hone a personalized wake-trajectory.

    Replays each night's stored samples through the classifier and records every sleep→wake
    transition (after the first sleep onset) with its P(wake) and the ranked signals that fired.
    """
    clf = classifier or SleepWakeClassifier()
    out: List[dict] = []
    for night in repo.recent_nights(nights):
        try:
            frames = repo.samples_for_night(night.date)
        except Exception:
            continue
        recent: List[SensorFrame] = []
        prev_wake = False
        saw_sleep = False
        for f in frames:
            wp = clf.probability(f, recent)
            is_wake = wp.label == "wake"
            if not is_wake:
                saw_sleep = True
            elif not prev_wake and saw_sleep:           # onset of a mid-sleep awakening
                ranked = sorted(((k, v) for k, v in wp.signals.items()
                                 if k != "bias" and v > 0), key=lambda kv: kv[1], reverse=True)
                out.append({
                    "night": night.date,
                    "time": f.timestamp.isoformat() if f.timestamp else None,
                    "p_wake": wp.p,
                    "converging": [k for k, _ in ranked],
                    "top_signal": ranked[0][0] if ranked else None,
                    "signals": {k: v for k, v in wp.signals.items()},
                })
            prev_wake = is_wake
            recent.append(f)
            if len(recent) > 20:
                recent.pop(0)
    return out[-limit:]
