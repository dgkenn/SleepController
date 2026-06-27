"""Calibrated multi-signal sleep/wake classifier + measured AUC."""

from datetime import datetime

from sleepctl.adapters.simulator import ScriptedNight
from sleepctl.controller.sleep_wake import SleepWakeClassifier, auc, score_night
from sleepctl.models import SensorFrame, SleepStage


def _labeled_night(scenario, seed=7):
    night = ScriptedNight(scenario=scenario, start=datetime(2026, 6, 27, 23, 0), seed=seed)
    frames = [night.frame_at(m, None) for m in range(len(night.stages))]
    labels = [1 if night.stages[m] is SleepStage.AWAKE else 0 for m in range(len(night.stages))]
    return frames, labels


def test_auc_metric_is_correct():
    assert auc([0.9, 0.8, 0.2, 0.1], [1, 1, 0, 0]) == 1.0   # perfect separation
    assert auc([0.5, 0.5, 0.5, 0.5], [1, 1, 0, 0]) == 0.5   # no information (all ties)
    assert auc([1, 1], [1, 1]) is None                       # one class only


def test_classifier_is_solid_on_labeled_nights():
    clf = SleepWakeClassifier()
    aucs = []
    for scenario in ("normal", "clustered_awakenings", "short_sleep"):
        frames, labels = _labeled_night(scenario)
        if sum(labels) == 0 or sum(labels) == len(labels):
            continue
        scores, labs = score_night(clf, frames, labels)
        a = auc(scores, labs)
        assert a is not None
        aucs.append(a)
    # multi-signal fusion should separate wake from sleep strongly on clean labeled data
    assert min(aucs) >= 0.85
    assert sum(aucs) / len(aucs) >= 0.90


def test_bed_exit_is_near_certain_wake():
    clf = SleepWakeClassifier()
    f = SensorFrame(timestamp=datetime(2026, 6, 27, 3, 0), stage=SleepStage.AWAKE, presence=False)
    assert clf.probability(f, []).p >= 0.95


def test_calm_deep_sleep_is_low_wake_prob():
    clf = SleepWakeClassifier()
    calm = SensorFrame(timestamp=datetime(2026, 6, 27, 2, 0), stage=SleepStage.DEEP,
                       heart_rate=55, hrv=60, respiratory_rate=14, movement=0.02, presence=True)
    window = [calm] * 10
    assert clf.probability(calm, window, sleep_hr_baseline=55, sleep_hrv_baseline=60).p < 0.15


def test_converging_signals_raise_probability_and_expose_the_vector():
    clf = SleepWakeClassifier()
    base = [SensorFrame(timestamp=datetime(2026, 6, 27, 2, m), stage=SleepStage.DEEP,
                        heart_rate=55, hrv=60, respiratory_rate=14, movement=0.02, presence=True)
            for m in range(10)]
    awakening = SensorFrame(timestamp=datetime(2026, 6, 27, 3, 0), stage=SleepStage.AWAKE,
                            heart_rate=68, hrv=45, respiratory_rate=18, movement=0.7, presence=True)
    wp = clf.probability(awakening, base, sleep_hr_baseline=55, sleep_hrv_baseline=60)
    assert wp.p > 0.85 and wp.label == "wake"
    # the converging signal vector is exposed for cataloging/backtest
    assert {"awake_stage", "hr_elev", "hrv_drop", "movement"} <= set(wp.signals)


def test_calibrated_classifier_beats_the_binary_voter_on_auc():
    from sleepctl.controller.wake_detection import WakeDetector
    clf = SleepWakeClassifier()
    voter = WakeDetector(min_signals=3)
    frames, labels = _labeled_night("clustered_awakenings")
    cont, _ = score_night(clf, frames, labels)
    # binary voter output as a degenerate score
    binv, recent = [], []
    for f in frames:
        binv.append(1.0 if voter.evaluate(f, recent) is not None else 0.0)
        recent.append(f)
    assert auc(cont, labels) >= auc(binv, labels)


def test_catalog_awakening_signals_records_the_converging_vector(tmp_path):
    import tempfile
    from datetime import timedelta

    from sleepctl.controller.sleep_wake import catalog_awakening_signals
    from sleepctl.models import NightSummary
    from sleepctl.storage.repository import Repository

    repo = Repository(tempfile.mktemp(suffix=".db"))
    night = "2026-06-27"
    repo.save_night_summary(NightSummary(date=night))
    t0 = datetime(2026, 6, 27, 23, 0)
    # 10 min of calm deep sleep, then a clear mid-sleep awakening
    for m in range(10):
        f = SensorFrame(timestamp=t0 + timedelta(minutes=m), stage=SleepStage.DEEP,
                        heart_rate=55, hrv=60, respiratory_rate=14, movement=0.02, presence=True)
        repo.log_sample(f, "maintenance", False, night)
    wake = SensorFrame(timestamp=t0 + timedelta(minutes=11), stage=SleepStage.AWAKE,
                       heart_rate=70, hrv=44, respiratory_rate=18, movement=0.7, presence=True)
    repo.log_sample(wake, "maintenance", True, night)

    cat = catalog_awakening_signals(repo)
    assert len(cat) >= 1
    ev = cat[-1]
    assert ev["night"] == night and ev["p_wake"] > 0.8
    assert "movement" in ev["converging"] and "awake_stage" in ev["converging"]
    repo.close()
