"""The live path: a low-confidence LIGHT from the model becomes DEEP on strong beat-interval
evidence, a confident label is left alone, and the reading is stashed for the decision log."""
import math

from sleepctl.config import AppConfig
from sleepctl.controller import state_estimator as se
from sleepctl.models import SensorFrame, SleepStage
from datetime import datetime, timedelta


class _Est:
    def __init__(self, label, conf):
        self.stage_label, self.confidence = label, conf


class _Stager:
    available = True
    _hrmotion_ok = False

    def __init__(self, label, conf):
        self._e = _Est(label, conf)

    def predict(self, *a, **k):
        return self._e


def _rr(t0, seconds, rsa, lf, seed=0):
    import random
    rnd = random.Random(seed)
    out, t = [], t0
    while t < t0 + seconds:
        rr = 980 + rsa * math.sin(2 * math.pi * 0.25 * (t - t0)) + lf * math.sin(2 * math.pi * 0.1 * (t - t0)) + rnd.gauss(0, 3)
        out.append((t, rr)); t += rr / 1000.0
    return out


def _frame(now, rr):
    f = SensorFrame(timestamp=now, stage=SleepStage.UNKNOWN, heart_rate=61.0, presence=None, data_age_seconds=1.0)
    f.hr_history = [(now.timestamp() - 30 * k, 61.0 + (k % 3)) for k in range(60)]
    f.rr_history = rr
    return f


def test_strong_autonomic_evidence_moves_only_a_low_confidence_label(monkeypatch):
    cfg = AppConfig.default()
    se._RESCORER = None
    monkeypatch.setattr(se, "_get_stager", lambda: _Stager("light", 0.45))
    now = datetime(2026, 9, 9, 2, 0)
    recent = [SensorFrame(timestamp=now - timedelta(seconds=30 * k), stage=SleepStage.LIGHT, stage_confidence=0.6,
                          heart_rate=61.0, presence=None) for k in range(10)]
    t0 = now.timestamp() - 320
    # build the night's distribution with ordinary epochs
    for i in range(25):
        se.estimate_sleep_stage(_frame(now, _rr(t0 - 30 * (25 - i), 320, 30, 30, seed=i)), 61.0, recent, cfg,
                                minutes_since_start=120, minutes_since_onset=90)
    out = se.estimate_sleep_stage(_frame(now, _rr(t0, 320, 90, 5, seed=7)), 61.0, recent, cfg,
                                  minutes_since_start=120, minutes_since_onset=90)
    assert out[0] is SleepStage.DEEP and out[2] == "model+autonomic"
    # a confident label is left alone even on the same evidence
    monkeypatch.setattr(se, "_get_stager", lambda: _Stager("light", 0.66))
    out = se.estimate_sleep_stage(_frame(now, _rr(t0, 320, 90, 5, seed=8)), 61.0, recent, cfg,
                                  minutes_since_start=120, minutes_since_onset=90)
    assert out[0] is SleepStage.LIGHT and out[2] == "model"


def test_pending_onset_resets_the_nights_distribution(monkeypatch):
    se._RESCORER = None
    monkeypatch.setattr(se, "_get_stager", lambda: _Stager("light", 0.45))
    now = datetime(2026, 9, 9, 2, 0)
    f = _frame(now, _rr(now.timestamp() - 320, 320, 30, 30))
    se.estimate_sleep_stage(f, 61.0, [], AppConfig.default(), minutes_since_start=10, minutes_since_onset=None)
    assert getattr(f, "stage_autonomic", None) is None
