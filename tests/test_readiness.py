"""Morning readiness / clinical-safety score."""

from sleepctl.benchmarks import NightMode
from sleepctl.models import NightSummary
from sleepctl.readiness import morning_readiness


def _night(tst, deep, rem, wake, waso, eff, hrv):
    return NightSummary(date="2026-06-27", total_sleep_min=tst, deep_min=deep, rem_min=rem,
                        light_min=max(0, tst - deep - rem), wake_events=wake, waso_min=waso,
                        sleep_efficiency=eff, avg_hrv=hrv, sleep_onset_latency_min=12)


def test_good_night_high_readiness():
    n = _night(465, 100, 110, 0, 6, 0.94, 75)
    r = morning_readiness(n, [n] * 7, NightMode.NORMAL, baseline_hrv=70)
    assert r.score >= 75 and r.band in ("adequate", "prime")
    assert not any(f["severity"] == "high" for f in r.flags)


def test_bad_short_fragmented_night_flags_impairment():
    bad = _night(300, 35, 40, 4, 40, 0.74, 48)
    recent = [_night(330, 40, 45, 3, 35, 0.78, 50)] * 6 + [bad]
    r = morning_readiness(bad, recent, NightMode.NORMAL, baseline_hrv=70)
    assert r.score < 50 and r.band in ("impaired", "compromised")
    flags = {f["flag"] for f in r.flags}
    assert "severe_short_sleep" in flags and "impairment_risk" in flags
    assert any(f["severity"] == "high" for f in r.flags)


def test_sleep_debt_penalizes_and_flags():
    # Adequate single night but a week of deficits -> debt flag + lower score.
    night = _night(430, 90, 100, 1, 12, 0.90, 68)
    debt_week = [_night(300, 40, 45, 1, 12, 0.85, 60)] * 7
    r = morning_readiness(night, debt_week, NightMode.NORMAL, baseline_hrv=70)
    assert any(f["flag"] == "sleep_debt" for f in r.flags)
    assert r.debt_min > 0


def test_missing_hrv_falls_back_to_quality():
    n = _night(450, 95, 105, 1, 10, 0.92, None)
    r = morning_readiness(n, [n] * 5, NightMode.NORMAL, baseline_hrv=None)
    assert 0 <= r.score <= 100
    assert r.components["recovery"] == r.components["sleep_quality"]


def test_to_dict_shape():
    n = _night(450, 95, 105, 1, 10, 0.92, 70)
    d = morning_readiness(n, [n] * 5, NightMode.NORMAL, baseline_hrv=70).to_dict()
    assert set(d) == {"score", "band", "components", "debt_min", "flags", "recommendation"}
