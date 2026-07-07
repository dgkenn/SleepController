"""Regression test for the missing-``Tunables``-fields bug: ``arousal.py``, ``wake_risk.py``,
and ``sleep_wake.py`` read tuning knobs via ``getattr(t, "<name>", <default>)``, but if those
fields don't exist on the ``Tunables`` dataclass, ``AppConfig.from_yaml`` silently drops any
YAML override (it only copies keys that are in ``fields(section_obj)``) and the detectors
always fall back to the hardcoded default -- no error, just quietly-ignored config."""

from __future__ import annotations

import os
import tempfile

import yaml

from sleepctl.config import AppConfig
from sleepctl.controller.arousal import ArousalDetector
from sleepctl.controller.wake_risk import WakeRiskAssessor
from sleepctl.models import SensorFrame, SleepStage


def _frame(now, **overrides):
    defaults = dict(
        timestamp=now, stage=SleepStage.LIGHT, stage_confidence=0.9,
        heart_rate=58.0, hrv=65.0, respiratory_rate=14.0, movement=0.05,
        presence=True, bed_temp_f=70.0, room_temp_f=68.0, data_age_seconds=5.0,
    )
    defaults.update(overrides)
    return SensorFrame(**defaults)


def test_tunables_yaml_overrides_are_not_dropped():
    """Every one of the previously-missing fields must round-trip through from_yaml."""
    overrides = {
        "arousal_hr_surge_bpm": 9.5,
        "arousal_hrv_drop_frac": 0.33,
        "arousal_movement": 0.55,
        "arousal_persistence_samples": 7,
        "wake_min_signals": 5,
        "wake_risk_hr_creep_bpm": 11.0,
        "wake_risk_movement": 0.66,
        "wake_risk_warm_margin_f": 3.5,
        "wake_risk_preempt_threshold": 0.9,
    }
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "config.yaml")
        with open(path, "w", encoding="utf-8") as fh:
            yaml.safe_dump({"tunables": overrides}, fh)
        cfg = AppConfig.from_yaml(path)

    for key, value in overrides.items():
        assert getattr(cfg.tunables, key) == value, f"{key} override was dropped"


def test_arousal_detector_actually_uses_the_overridden_hr_surge_threshold():
    """Functional proof (not just a field check): a HR delta below the DEFAULT surge threshold
    but above an OVERRIDDEN (lower) threshold must flip the detector's behavior."""
    from datetime import datetime, timedelta

    now = datetime(2026, 6, 24, 2, 0)
    baseline = 58.0
    window = [_frame(now - timedelta(minutes=m), heart_rate=baseline) for m in range(6, 0, -1)]
    # +3 bpm: below the 6.0 default surge bar, above a 2.0 bpm override.
    frame = _frame(now, heart_rate=baseline + 3.0)

    default_cfg = AppConfig.default()
    default_detector = ArousalDetector(default_cfg)
    default_assessment = default_detector.assess(frame, window, now, sleep_hr_baseline=baseline)
    assert "hr_surge" not in default_assessment.signals

    overridden_cfg = AppConfig.default()
    overridden_cfg.tunables.arousal_hr_surge_bpm = 2.0
    overridden_detector = ArousalDetector(overridden_cfg)
    assert overridden_detector.hr_surge == 2.0
    overridden_assessment = overridden_detector.assess(
        frame, window, now, sleep_hr_baseline=baseline)
    assert "hr_surge" in overridden_assessment.signals


def test_wake_risk_assessor_actually_uses_the_overridden_hr_creep_threshold():
    """Same functional proof for ``WakeRiskAssessor.hr_creep`` (wake_risk.py)."""
    from datetime import datetime, timedelta

    now = datetime(2026, 6, 24, 2, 0)
    baseline = 58.0
    window = [_frame(now - timedelta(minutes=m), heart_rate=baseline) for m in range(6, 0, -1)]
    # +1.5 bpm: below the 4.0 default creep bar, above a 1.0 bpm override.
    frame = _frame(now, heart_rate=baseline + 1.5)

    default_cfg = AppConfig.default()
    default_assessor = WakeRiskAssessor(default_cfg)
    default_risk = default_assessor.assess(frame, window, now, sleep_hr_baseline=baseline)
    assert "hr_creep" not in default_risk.reasons

    overridden_cfg = AppConfig.default()
    overridden_cfg.tunables.wake_risk_hr_creep_bpm = 1.0
    overridden_assessor = WakeRiskAssessor(overridden_cfg)
    assert overridden_assessor.hr_creep == 1.0
    overridden_risk = overridden_assessor.assess(frame, window, now, sleep_hr_baseline=baseline)
    assert "hr_creep" in overridden_risk.reasons
