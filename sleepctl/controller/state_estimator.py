"""Vitals-based coarse sleep-stage estimate — lets an external HR sensor STEER the controller.

The Eight Sleep Pod normally supplies ``SensorFrame.stage`` (LIGHT/DEEP/REM/AWAKE). When that is
unavailable — the sleep-tracking membership is inactive, the sensors aren't reporting, or the only
physiology source is an external band (a **Polar Verity Sense**) — ``stage`` arrives as
``UNKNOWN``. Onset detection and the state machine hard-require a real sleep stage, so a stage-less
feed would get stuck in INDUCTION and none of the maintenance-time steering (arousal / wake-risk /
precursor / architecture) would ever run.

This module derives a **coarse** stage from heart rate, HRV and movement so that pipeline can
engage off the wearable alone. It is deliberately conservative:

  * It only ever returns AWAKE / LIGHT / DEEP — never REM (REM is not reliably separable from
    light sleep by cardiorespiratory + actigraphy signal alone; claiming it would mislead the
    REM-aware steering).
  * Confidence is capped well below a real Pod stage (``est_stage_max_conf``) so every downstream
    consumer treats it as softer evidence — the onset detector still needs its own multi-signal
    persistence gate to *confirm* onset, so an estimated LIGHT never fabricates sleep on its own.
  * It requires a heart-rate reading; depth grading (DEEP) additionally requires a sleep HR
    baseline and *sustained* quiescence, so a single still moment can't be mistaken for deep sleep.

The caller overlays the result onto ``frame.stage`` only when the real stage is ``UNKNOWN``; a Pod
stage always wins.
"""
from __future__ import annotations

from typing import Optional

from sleepctl.models import SensorFrame, SleepStage

_LABEL_TO_STAGE = {
    "wake": SleepStage.AWAKE,
    "light": SleepStage.LIGHT,
    "deep": SleepStage.DEEP,
    "rem": SleepStage.REM,
}

# Lazily-loaded singleton learned stager (None if its weights/module aren't available).
_STAGER = None
_STAGER_LOADED = False


def _get_stager():
    global _STAGER, _STAGER_LOADED
    if not _STAGER_LOADED:
        _STAGER_LOADED = True
        try:
            from sleepctl.ml.sleep_staging.infer import SleepStager
            s = SleepStager.load()
            _STAGER = s if getattr(s, "available", False) else None
        except Exception:
            _STAGER = None
    return _STAGER


def _hr_series(recent, frame) -> list:
    """Trailing (t_seconds, bpm) HR history from the controller's recent-frame buffer + the
    current frame, for the learned stager's trailing-window features."""
    out = []
    for f in list(recent or []) + [frame]:
        hr = getattr(f, "heart_rate", None)
        ts = getattr(f, "timestamp", None)
        if hr is not None and ts is not None:
            try:
                out.append((ts.timestamp(), float(hr)))
            except Exception:
                continue
    return out


def estimate_sleep_stage(frame, sleep_hr_base, recent, cfg, *,
                         minutes_since_start=None, minutes_since_onset=None):
    """Best available coarse sleep-stage estimate for a stage-less (wearable) feed.

    Returns ``(SleepStage, confidence, source)`` or ``None``. Prefers the LEARNED wearable stager
    (``sleepctl.ml.sleep_staging`` — trained on PhysioNet sleep-accel: wrist HR → PSG stages) when
    its weights are bundled and enough recent HR exists; otherwise falls back to the interpretable
    ``estimate_stage_from_vitals`` heuristic. HR-only (works with the Verity alone). Confidence is
    capped below a real Pod stage in both paths.
    """
    t = cfg.tunables
    if getattr(t, "use_learned_stager", True):
        stager = _get_stager()
        if stager is not None:
            hr_samples = _hr_series(recent, frame)
            if len(hr_samples) >= getattr(t, "stager_min_hr_samples", 5):
                try:
                    est = stager.predict(
                        hr_samples, activity_samples=None,
                        minutes_since_start=minutes_since_start,
                        minutes_since_onset=minutes_since_onset)
                except Exception:
                    est = None
                if est is not None:
                    stage = _LABEL_TO_STAGE.get(est.stage_label, SleepStage.LIGHT)
                    conf = min(float(est.confidence), getattr(t, "est_model_conf_cap", 0.7))
                    return (stage, round(conf, 3), "model")

    heur = estimate_stage_from_vitals(
        frame, sleep_hr_base, recent,
        awake_movement=t.est_stage_awake_movement,
        awake_hr_delta=t.est_stage_awake_hr_delta,
        deep_hr_delta=t.est_stage_deep_hr_delta,
        deep_movement=t.est_stage_deep_movement,
        deep_sustain=t.est_stage_deep_sustain,
        max_conf=t.est_stage_max_conf)
    if heur is None:
        return None
    return (heur[0], heur[1], "heuristic")


def estimate_stage_from_vitals(
    frame: SensorFrame,
    sleep_hr_base: Optional[float],
    recent: list,
    *,
    awake_movement: float = 0.25,
    awake_hr_delta: float = 6.0,
    deep_hr_delta: float = -3.0,
    deep_movement: float = 0.06,
    deep_sustain: int = 4,
    max_conf: float = 0.5,
) -> Optional[tuple]:
    """Estimate ``(SleepStage, confidence)`` from HR/HRV/movement, or ``None`` if there isn't
    enough signal (no heart rate) to estimate at all.

    ``sleep_hr_base`` is the controller's settled-sleep HR baseline (``_sleep_baseline``), which
    falls back to the measured resting HR, so it's usually available even on night one; depth
    grading is skipped when it isn't.
    """
    hr = frame.heart_rate
    if hr is None:
        return None  # no cardiac signal -> nothing to estimate; caller leaves stage UNKNOWN
    move = frame.movement if frame.movement is not None else 0.0

    # --- AWAKE: clear motion, or HR well above the sleep baseline --------------------------------
    if move >= awake_movement:
        return (SleepStage.AWAKE, round(max_conf, 3))
    if sleep_hr_base is not None and hr >= sleep_hr_base + awake_hr_delta:
        return (SleepStage.AWAKE, round(max_conf * 0.9, 3))

    # --- asleep: grade DEEP vs LIGHT (DEEP only with a baseline + SUSTAINED quiescence) ----------
    if sleep_hr_base is not None:
        tail = (recent or [])[-deep_sustain:]
        tail_moves = [(f.movement if f.movement is not None else 0.0) for f in tail]
        sustained_still = (
            len(tail_moves) >= deep_sustain
            and all(m <= deep_movement for m in tail_moves)
            and move <= deep_movement
        )
        if sustained_still and hr <= sleep_hr_base + deep_hr_delta:
            return (SleepStage.DEEP, round(max_conf * 0.9, 3))

    # default asleep bucket
    return (SleepStage.LIGHT, round(max_conf * 0.8, 3))
