"""Published PSG-validated actigraphy algorithms, used as an EXTERNAL check on our staging.

Our stager is HR-led; these are motion-only and were validated against polysomnography by other
people on other subjects, so they fail differently. That is what makes agreement meaningful and
disagreement diagnostic.
"""
import pytest

from sleepctl.eval.reference_stagers import calibrate_scale, cole_kripke, compare, sadeh


def test_sustained_stillness_scores_as_sleep():
    assert all(cole_kripke([0.0] * 40))
    assert all(sadeh([0.0] * 40))


def test_sustained_movement_scores_as_wake():
    assert not any(cole_kripke([400.0] * 20))
    assert not any(sadeh([400.0] * 20))


def test_cole_kripke_uses_a_window_not_just_the_current_minute():
    """The whole point of the weighting: a large burst marks its NEIGHBOURS awake too.

    The burst has to be big enough to clear the threshold through the neighbour weights (74 and
    76) rather than the centre weight (230) -- roughly 1300 counts, not 800.
    """
    counts = [0.0] * 10 + [3000.0] + [0.0] * 10
    out = cole_kripke(counts)
    assert not out[10], "the burst minute itself must score as wake"
    assert not out[9] and not out[11], "the window must carry wake to the neighbours"


def test_a_burst_too_small_for_the_neighbour_weights_only_marks_its_own_minute():
    counts = [0.0] * 10 + [800.0] + [0.0] * 10
    out = cole_kripke(counts)
    assert not out[10] and out[9] and out[11]


def test_each_algorithm_gets_its_own_scale():
    """Cole-Kripke is a linear sum; Sadeh mixes a mean, an SD, a LOG and an absolute 50-100 band,
    so the same scale means different things. Sharing one produced 69% spurious disagreement."""
    counts = [0.0] * 30 + [200.0] * 5 + [0.0] * 30
    res = compare([True] * 65, counts)
    assert res["scale_cole_kripke"] != res["scale_sadeh"]


def test_the_two_references_largely_agree_once_each_is_scaled():
    """A sanity check on the calibration: independently scaled, they should mostly concur.
    Wild reference-vs-reference disagreement means the scaling is wrong, not the sleeper."""
    counts = [0.0] * 60 + [300.0] * 8 + [0.0] * 60
    res = compare([True] * 128, counts)
    assert res["references_disagree"] / res["n"] < 0.25


def test_disagreement_is_reported_asymmetrically():
    """Actigraphy cannot see quiet wakefulness, so 'we said awake, motion quiet' is weak evidence
    against us while 'motion says awake, we said asleep' is strong. A single accuracy number
    would flatter us in exactly the direction the references are known to be wrong."""
    counts = [0.0] * 20 + [500.0] * 6 + [0.0] * 20
    res = compare([True] * 46, counts)
    a = res["cole_kripke"]
    assert a["missed_wake_we_called_sleep"] > 0
    assert a["we_called_wake_ref_quiet"] == 0


def test_unlabelled_minutes_are_excluded_not_counted_as_agreement():
    counts = [0.0] * 20
    assert compare([None] * 20, counts)["n"] == 0
    assert compare([True] * 10 + [None] * 10, counts)["n"] == 10


def test_an_empty_or_flat_night_does_not_raise():
    assert calibrate_scale([]) == 1.0
    assert calibrate_scale([0.0] * 10) == 1.0
    assert compare([], [])["n"] == 0


@pytest.mark.parametrize("bad", [None, -5.0])
def test_missing_or_negative_counts_are_tolerated(bad):
    assert len(cole_kripke([bad, 0.0, bad])) == 3
    assert len(sadeh([bad, 0.0, bad])) == 3
