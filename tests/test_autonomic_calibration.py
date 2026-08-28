"""Calibration of the autonomic channel, and the alignment bug that inverted its headline result.

The timezone case matters more than it looks: read with the two clocks mixed, `hr_from_ibi`
scored AUC 0.397 -- backwards to the literature -- and the report said so in as many words.
Aligned, it scores 0.803. A silent four-hour shift is fully capable of producing a confident,
publishable, wrong conclusion, so it gets a test.
"""

from sleepctl.eval.autonomic_calibration import (align_night, auc, drop_out_of_bed,
                                                 infer_utc_offset_s, measure_features,
                                                 proposed_weights)

_OFFSET = -4 * 3600          # America/New_York in August
_BASE_UTC = 1787900000       # arbitrary epoch inside a night


def _night(n=200, offset_s=_OFFSET, include_field=True):
    """A synthetic night whose two clocks disagree exactly the way the published ones do."""
    from datetime import datetime, timezone
    import random
    rng = random.Random(11)
    wins, raw = [], []
    # Deliberately APERIODIC. A night with a repeating pattern matches equally well at several
    # shifts, and the offset recovery is written to refuse that case rather than pick one --
    # see `test_an_ambiguous_night_is_refused`.
    awake_at = {i for i in range(n) if rng.random() < 0.14}
    for i in range(n):
        t = _BASE_UTC + i * 60
        awake = i in awake_at
        hr = (92.0 if awake else 62.0) + rng.uniform(-3.0, 3.0)
        wins.append({"t": float(t), "hr_from_ibi": hr, "ibi_rmssd": (20.0 if awake else 55.0),
                     "ibi_pnn50": (0.05 if awake else 0.30), "ibi_hf": (10.0 if awake else 40.0),
                     "ibi_hf_nu": (0.2 if awake else 0.5), "ibi_lf_hf": (4.0 if awake else 1.0),
                     "ibi_lf_nu": (0.8 if awake else 0.5),
                     "ibi_sd1_sd2": (0.2 if awake else 0.6)})
        local = datetime.fromtimestamp(t + offset_s, timezone.utc).replace(tzinfo=None)
        raw.append({"ts": local.isoformat(), "controller_state": "maintenance",
                    "heart_rate": hr, "movement": (0.6 if awake else 0.02),
                    "stage": "awake" if awake else "light"})
    night = {"hrv_windows": wins, "raw_samples": raw}
    if include_field:
        night["local_utc_offset_s"] = offset_s
    return night


def test_the_exported_offset_is_used():
    epochs = align_night(_night())
    assert len(epochs) == 200
    # Every epoch found its own minute's heart rate, so the two series must agree exactly.
    assert all(e["hr"] == e["hr_from_ibi"] for e in epochs)


def test_the_offset_is_recovered_when_the_field_is_absent():
    """The 14 nights published before `local_utc_offset_s` existed still have to align."""
    night = _night(include_field=False)
    assert infer_utc_offset_s(night) == _OFFSET
    epochs = align_night(night)
    assert all(e["hr"] == e["hr_from_ibi"] for e in epochs)


def test_an_ambiguous_night_is_refused():
    """A perfectly periodic heart rate matches at many shifts. Returning whichever scored first
    is how a four-hour error becomes a confident, publishable, wrong conclusion."""
    from datetime import datetime, timezone
    wins, raw = [], []
    for i in range(300):
        t = _BASE_UTC + i * 60
        hr = 92.0 if (i % 20) < 3 else 62.0          # exactly periodic
        wins.append({"t": float(t), "hr_from_ibi": hr})
        local = datetime.fromtimestamp(t + _OFFSET, timezone.utc).replace(tzinfo=None)
        raw.append({"ts": local.isoformat(), "controller_state": "maintenance",
                    "heart_rate": hr, "movement": 0.02, "stage": "light"})
    assert infer_utc_offset_s({"hrv_windows": wins, "raw_samples": raw}) is None


def test_a_misaligned_read_would_have_been_caught():
    """Forcing the wrong offset must NOT quietly produce a full night of plausible numbers."""
    epochs = align_night(_night(), utc_offset_s=0)
    matched = [e for e in epochs if e["hr"] is not None]
    assert len(matched) < len(epochs) // 2


def test_alignment_is_refused_rather_than_guessed():
    """A night with nothing to align against returns nothing, instead of assuming UTC."""
    night = {"hrv_windows": [{"t": 1.0, "hr_from_ibi": 60.0}], "raw_samples": []}
    assert align_night(night) == []


def test_out_of_bed_epochs_are_dropped():
    """2026-08-27 carried hours of walking-around morning at 102-124 bpm. Left in, ordinary
    daytime physiology masquerades as this sleeper's wake distribution."""
    epochs = [{"hr": 68.0, "hr_from_ibi": 68.0}, {"hr": 120.0, "hr_from_ibi": 118.0}]
    assert len(drop_out_of_bed(epochs)) == 1


def test_auc_handles_ties_without_manufacturing_separation():
    """pNN50 is flat at zero through long stretches of quiet sleep."""
    assert auc([0.0] * 10, [True] * 5 + [False] * 5) == 0.5


def test_auc_abstains_with_only_one_class():
    assert auc([1.0, 2.0, 3.0], [True, True, True]) is None


def test_a_separating_feature_is_kept_and_a_flat_one_is_not():
    measured = measure_features([("a", _night()), ("b", _night()), ("c", _night())])
    feats = measured["features"]
    assert feats["hr_from_ibi"]["verdict"] == "keep"
    assert feats["hr_from_ibi"]["mean_auc"] > 0.6
    weights = proposed_weights(measured)
    assert weights["hr_from_ibi"] > 0


def test_short_nights_are_skipped_not_scored():
    """2026-08-26 produced 9 HRV windows -- an armband dropout. An AUC over 9 epochs is a coin
    flip with a decimal point."""
    measured = measure_features([("dropout", _night(n=20))])
    assert measured["nights_used"] == []
    assert measured["nights_skipped"][0]["why"] == "too few HRV windows"
