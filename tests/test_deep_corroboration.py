"""Deep corroboration (the heuristic upgrading a model LIGHT to DEEP) is off by default.

It was added when the stager essentially never emitted deep. The BIDSleep-retrained stager
emits deep at about the EEG rate, and replayed through the live path on held-out BIDSleep
nights only 7% of the upgrades were EEG deep (58% light, 26% REM; scripts/eval_live_staging.py).
"""
from datetime import datetime, timedelta

from sleepctl.config import AppConfig
from sleepctl.controller import state_estimator as se
from sleepctl.models import SensorFrame, SleepStage


class _Est:
    stage_label, confidence, variant = "light", 0.6, "hr"


class _Stager:
    available = True

    def predict(self, *a, **k):
        return _Est()


def _still_low_hr(monkeypatch, cfg):
    """Everything the heuristic wants for DEEP: a settled baseline, HR 5 below it, and four
    still frames -- with the model saying LIGHT."""
    se._RESCORER = None
    monkeypatch.setattr(se, "_get_stager", lambda: _Stager())
    now = datetime(2026, 9, 9, 1, 0)
    recent = [SensorFrame(timestamp=now - timedelta(seconds=30 * k), stage=SleepStage.LIGHT,
                          stage_confidence=0.6, heart_rate=60.0, movement=0.01, presence=None)
              for k in range(8, 0, -1)]
    f = SensorFrame(timestamp=now, stage=SleepStage.UNKNOWN, heart_rate=55.0, movement=0.01,
                    presence=None)
    f.hr_history = [(now.timestamp() - 30 * k, 55.0 + (k % 3)) for k in range(60)]
    return se.estimate_sleep_stage(f, 60.0, recent, cfg, minutes_since_start=120,
                                   minutes_since_onset=100)


def test_the_models_light_stands_by_default(monkeypatch):
    out = _still_low_hr(monkeypatch, AppConfig.default())
    assert out[0] is SleepStage.LIGHT and out[2] == "model"


def test_corroboration_can_still_be_switched_on(monkeypatch):
    cfg = AppConfig.default()
    cfg.tunables.deep_corroboration = True
    out = _still_low_hr(monkeypatch, cfg)
    assert out[0] is SleepStage.DEEP and out[2] == "model+deep"
