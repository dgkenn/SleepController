"""Stage calls are judged against their physiological signatures, not against themselves."""
import random
from datetime import datetime, timedelta

from sleepctl.eval.stage_consistency import build_epochs, stage_consistency

OFF = -14400
T0 = datetime(2026, 9, 19, 22, 0)


def _night(stage_plan, rem_irregular=True, deep_low_hr=True, dup=False, seed=1):
    """stage_plan: list of (stage, minutes). Builds raw_samples/hrv_windows/actigraphy."""
    random.seed(seed)
    raw, hrv, acti = [], [], []
    t = T0
    for stage, minutes in stage_plan:
        for _ in range(minutes):
            hr = {"awake": 78, "light": 64, "deep": (56 if deep_low_hr else 66), "rem": 66}[stage]
            resp = 14.0 + (random.gauss(0, 2.0) if (stage == "rem" and rem_irregular) or stage == "awake"
                           else random.gauss(0, 0.3))
            mov = 0.6 if stage == "awake" else 0.02
            rows = 2 if dup else 1
            for _k in range(rows):
                raw.append({"ts": t.isoformat(), "stage": stage, "heart_rate": hr + random.gauss(0, 1),
                            "movement": mov, "respiratory_rate": resp, "controller_state": "maintenance"})
            ut = (t - timedelta(seconds=OFF)).timestamp()
            rms = {"awake": 25, "light": 35, "deep": 55, "rem": 40}[stage]
            hrv.append({"t": ut, "ibi_rmssd": rms + random.gauss(0, 2), "ibi_hf": rms * 10})
            acti.append({"t": int(ut // 30 * 30), "pim_mean": 3.0 if stage == "awake" else 0.4})
            t += timedelta(seconds=61 if dup else 60)
    return {"local_utc_offset_s": OFF, "raw_samples": raw, "hrv_windows": hrv, "actigraphy_epochs": acti}


PLAN = [("light", 60), ("deep", 40), ("light", 60), ("rem", 50), ("awake", 25), ("light", 60), ("rem", 40)]


def test_a_physiologically_coherent_night_is_supported():
    res = stage_consistency(_night(PLAN))
    assert res["verdicts"]["deep"]["verdict"] == "supported", res["verdicts"]["deep"]
    assert res["verdicts"]["rem"]["verdict"] == "supported", res["verdicts"]["rem"]
    assert res["verdicts"]["awake"]["verdict"] == "supported", res["verdicts"]["awake"]
    assert res["unsupported"] == []


def test_rem_with_regular_breathing_is_unsupported():
    """2026-09-19: 104 min of REM whose breathing was MORE regular than light sleep's."""
    res = stage_consistency(_night(PLAN, rem_irregular=False))
    v = res["verdicts"]["rem"]
    assert v["verdict"] == "unsupported" and any("breathing" in f for f in v["failed"])
    assert "rem" in res["unsupported"] and "rem" in res["summary"]


def test_deep_at_a_high_heart_rate_is_unsupported():
    res = stage_consistency(_night(PLAN, deep_low_hr=False))
    v = res["verdicts"]["deep"]
    assert v["verdict"] == "unsupported" and any("heart rate" in f for f in v["failed"])


def test_a_stage_with_too_few_minutes_is_not_judged():
    res = stage_consistency(_night([("light", 120), ("deep", 3), ("rem", 4)]))
    assert res["verdicts"]["deep"]["verdict"] == "insufficient"
    assert res["verdicts"]["rem"]["verdict"] == "insufficient"


def test_minutes_are_duration_weighted_not_row_counted():
    """The daemon logs two samples per Pod poll (~61 s apart) under the same timestamp."""
    single = stage_consistency(_night(PLAN, dup=False))["profiles"]["rem"]["minutes"]
    double = stage_consistency(_night(PLAN, dup=True))["profiles"]["rem"]["minutes"]
    assert abs(single - 90.0) <= 3.0
    assert abs(double - 90.0) <= 5.0


def test_shares_and_norms():
    res = stage_consistency(_night([("light", 300), ("rem", 30)]))
    assert res["shares"]["deep"] == 0.0
    assert any("deep is 0%" in n for n in res["outside_norms"])


def test_empty_or_broken_export_never_raises():
    empty = stage_consistency({})
    assert empty["n_epochs"] == 0 and empty["unsupported"] == []
    assert stage_consistency({"raw_samples": [{"ts": "garbage", "stage": "light"}]})["n_epochs"] == 0
    assert build_epochs({"raw_samples": None}) == []
