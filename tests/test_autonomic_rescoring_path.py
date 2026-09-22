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


def _rr(t0, seconds, rsa, lf, seed=0, mean=980.0):
    import random
    rnd = random.Random(seed)
    out, t = [], t0
    while t < t0 + seconds:
        rr = mean + rsa * math.sin(2 * math.pi * 0.25 * (t - t0)) + lf * math.sin(2 * math.pi * 0.1 * (t - t0)) + rnd.gauss(0, 3)
        out.append((t, rr)); t += rr / 1000.0
    return out


def _frame(now, rr):
    f = SensorFrame(timestamp=now, stage=SleepStage.UNKNOWN, heart_rate=61.0, presence=None, data_age_seconds=1.0)
    f.hr_history = [(now.timestamp() - 30 * k, 61.0 + (k % 3)) for k in range(60)]
    f.rr_history = rr
    return f


def _still(f, now):
    f.activity_history = [(now.timestamp() - 10 * k, 0.4) for k in range(40)]
    f.activity_units = "counts"
    return f


def _night(monkeypatch, label, conf):
    se._RESCORER = None
    monkeypatch.setattr(se, "_get_stager", lambda: _Stager(label, conf))
    now = datetime(2026, 9, 9, 1, 0)
    cfg = AppConfig.default()
    recent = [SensorFrame(timestamp=now - timedelta(seconds=30 * k), stage=SleepStage.LIGHT,
                          stage_confidence=0.6, heart_rate=61.0, presence=None) for k in range(10)]
    # the night's ordinary distribution, one epoch a minute
    for i in range(25):
        t_end = now.timestamp() + 60 * i
        se.estimate_sleep_stage(_still(_frame(now, _rr(t_end - 320, 320, 30, 30, seed=i)), now),
                                61.0, recent, cfg, minutes_since_start=60 + i,
                                minutes_since_onset=40 + i)
    return now, cfg, recent


def _sustained_vagal(now, cfg, recent, minutes, start_min=25, still=True):
    out = None
    for k in range(minutes):
        t_end = now.timestamp() + 60 * (start_min + k)
        f = _frame(now, _rr(t_end - 320, 320, 90, 5, seed=500 + k, mean=1080.0))
        if still:
            _still(f, now)
        out = se.estimate_sleep_stage(f, 61.0, recent, cfg, minutes_since_start=90 + k,
                                      minutes_since_onset=70 + k)
    return out


def test_a_held_vagal_state_early_in_the_night_is_called_deep(monkeypatch):
    now, cfg, recent = _night(monkeypatch, "light", 0.62)
    out = _sustained_vagal(now, cfg, recent, 13)
    assert out[0] is SleepStage.DEEP and out[2] == "model+autonomic" and out[1] <= 0.5


def test_no_deep_call_without_positive_stillness(monkeypatch):
    now, cfg, recent = _night(monkeypatch, "light", 0.62)
    out = _sustained_vagal(now, cfg, recent, 13, still=False)
    assert out[0] is SleepStage.LIGHT


def test_a_confident_rem_label_in_a_vagal_state_is_vetoed(monkeypatch):
    """The model's REM sits at the 0.7 cap; the veto is not confidence-gated."""
    now, cfg, recent = _night(monkeypatch, "rem", 0.7)
    out = _sustained_vagal(now, cfg, recent, 9, still=False)
    assert out[0] is SleepStage.LIGHT and out[2] == "model+autonomic"


def test_pending_onset_resets_the_nights_distribution(monkeypatch):
    se._RESCORER = None
    monkeypatch.setattr(se, "_get_stager", lambda: _Stager("light", 0.45))
    now = datetime(2026, 9, 9, 2, 0)
    f = _frame(now, _rr(now.timestamp() - 320, 320, 30, 30))
    se.estimate_sleep_stage(f, 61.0, [], AppConfig.default(), minutes_since_start=10, minutes_since_onset=None)
    assert getattr(f, "stage_autonomic", None) is None


def test_the_planned_night_length_reaches_the_stagers_clock(monkeypatch):
    """Training normalised the clock by each night's true length; inference assumed 480 min."""
    seen = {}

    class _Rec(_Stager):
        def predict(self, *a, **k):
            seen.update(k)
            return self._e

    se._RESCORER = None
    monkeypatch.setattr(se, "_get_stager", lambda: _Rec("light", 0.6))
    now = datetime(2026, 9, 9, 1, 0)
    se.estimate_sleep_stage(_frame(now, []), 61.0, [], AppConfig.default(),
                            minutes_since_start=60, minutes_since_onset=40,
                            planned_night_min=390.0)
    assert seen.get("total_minutes") == 390.0
