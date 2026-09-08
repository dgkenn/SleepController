"""Predictive pre-emption: trend-based precursor detection."""

from datetime import datetime, timedelta

from sleepctl.config import AppConfig
from sleepctl.controller.precursor import PrecursorDetector
from sleepctl.models import SensorFrame, SleepStage


def _series(specs, stage=SleepStage.LIGHT, start=None):
    """specs: list of dicts of frame fields, spaced 30s apart."""
    base = start or datetime(2026, 6, 27, 3, 0)
    frames = []
    for i, s in enumerate(specs):
        frames.append(SensorFrame(timestamp=base + timedelta(seconds=30 * i), stage=stage,
                                  presence=True, **s))
    return frames


def _det():
    return PrecursorDetector(AppConfig.default())


def test_stable_sleep_no_preempt():
    # Flat HR/HRV/movement -> no drift -> no pre-empt.
    frames = _series([{"heart_rate": 56, "hrv": 60, "movement": 0.05, "bed_temp_f": 88,
                       "respiratory_rate": 14} for _ in range(10)])
    a = _det().detect(frames[-1], frames[:-1], frames[-1].timestamp, 56, 60)
    assert a.should_preempt is False and a.score == 0.0


def test_rising_hr_and_restlessness_preempts():
    # HR creeping up + movement building over ~4 min -> leading-edge pre-empt.
    specs = []
    for i in range(10):
        specs.append({"heart_rate": 55 + i * 0.8, "hrv": 62 - i * 0.9,
                      "movement": 0.04 + i * 0.02, "bed_temp_f": 88.0,
                      "respiratory_rate": 14})
    frames = _series(specs)
    a = _det().detect(frames[-1], frames[:-1], frames[-1].timestamp,
                      sleep_hr_baseline=55, sleep_hrv_baseline=62)
    assert a.should_preempt is True
    assert "hr_creep" in a.reasons and "restlessness_building" in a.reasons


def test_never_preempts_in_deep_sleep():
    specs = [{"heart_rate": 55 + i * 0.9, "hrv": 62 - i, "movement": 0.04 + i * 0.02,
              "bed_temp_f": 88, "respiratory_rate": 14} for i in range(10)]
    frames = _series(specs, stage=SleepStage.DEEP)
    a = _det().detect(frames[-1], frames[:-1], frames[-1].timestamp, 55, 62)
    assert a.should_preempt is False  # protect slow-wave even if drift is present


def test_high_movement_gates_autonomic_signals():
    # Movement above the BCG-reliability threshold: HR/HRV trends must be ignored, but the
    # restlessness trend itself can still register.
    specs = [{"heart_rate": 55 + i * 2, "hrv": 62 - i * 2, "movement": 0.5,
              "respiratory_rate": 14} for i in range(10)]
    frames = _series(specs)
    a = _det().detect(frames[-1], frames[:-1], frames[-1].timestamp, 55, 62)
    assert "hr_creep" not in a.reasons and "hrv_decay" not in a.reasons


def test_bed_warming_trend_contributes():
    specs = [{"heart_rate": 55, "hrv": 60, "movement": 0.05,
              "bed_temp_f": 86 + i * 0.25, "respiratory_rate": 14} for i in range(10)]
    frames = _series(specs)
    a = _det().detect(frames[-1], frames[:-1], frames[-1].timestamp, 55, 60)
    assert "bed_warming" in a.reasons


def test_to_dict_shape():
    frames = _series([{"heart_rate": 55, "hrv": 60, "movement": 0.05} for _ in range(6)])
    d = _det().detect(frames[-1], frames[:-1], frames[-1].timestamp, 55, 60).to_dict()
    assert set(d) == {"score", "should_preempt", "reasons", "signals"}


def test_a_restlessness_ramp_in_the_dense_counts_is_a_precursor():
    """Movement density rising against the night's own baseline leads the heart-rate creep."""
    from sleepctl.controller.actigraphy_epochs import EPOCH_S
    frames = _series([{"heart_rate": 56, "hrv": 60, "movement": 0.05, "bed_temp_f": 88,
                       "respiratory_rate": 14} for _ in range(10)])
    f = frames[-1]
    now_t = f.timestamp.timestamp()
    hist = []
    t = now_t - 3000
    while t <= now_t:
        hist.append((t, 6.0 if any(abs((now_t - t) - b) < 1.0 for b in (10, 60, 120, 200, 270)) else 0.5))
        t += 2.0
    f.activity_history = hist
    f.activity_units = "counts"
    a = _det().detect(f, frames[:-1], f.timestamp, 56, 60)
    assert "restlessness_ramp" in a.reasons
    assert a.signals["ramp_density"] >= 4
    f.activity_units = "index"          # the phone's scale is not counts: no ramp judgement
    a = _det().detect(f, frames[:-1], f.timestamp, 56, 60)
    assert "restlessness_ramp" not in a.reasons
