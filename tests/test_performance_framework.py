"""Standardized tracker evaluation, per Menghini et al. (SLEEP 2021;44:zsaa170).

Replaces an ad-hoc "strong/weak disagreement" scheme this project had grown, which was an
informal re-derivation of sensitivity and specificity with the summary-measure and agreement
analyses missing entirely.
"""
from sleepctl.eval.performance import (bland_altman, discrepancy, epoch_by_epoch, evaluate,
                                       summary_measures)


def test_a_detector_that_never_reports_wake_is_exposed_by_kappa_not_accuracy():
    """The reason the framework insists on more than accuracy: on a 90%-sleep night, a detector
    that always says sleep scores 0.90 accuracy. Kappa and specificity both say 0.0."""
    res = epoch_by_epoch([True] * 100, [True] * 90 + [False] * 10)
    assert res["accuracy"] == 0.9
    assert res["kappa"] == 0.0
    assert res["specificity"] == 0.0
    assert res["sensitivity"] == 1.0


def test_perfect_agreement():
    ref = [True] * 80 + [False] * 20
    res = epoch_by_epoch(list(ref), ref)
    assert res["accuracy"] == 1.0 and res["kappa"] == 1.0
    assert res["sensitivity"] == 1.0 and res["specificity"] == 1.0


def test_sleep_is_the_positive_class():
    """Framework convention. Getting this backwards silently swaps sensitivity and specificity,
    which is the difference between 'detects sleep well' and 'detects wake well'."""
    res = epoch_by_epoch([True, True, False, False], [True, True, True, False])
    assert res["tp_both_sleep"] == 2
    assert res["fp_device_sleep_ref_wake"] == 0
    assert res["fn_device_wake_ref_sleep"] == 1


def test_unlabelled_epochs_are_excluded_not_scored_as_agreement():
    """Scoring a sensor dropout as a correct call is how an outage becomes a perfect night."""
    res = epoch_by_epoch([None] * 50 + [True] * 10, [True] * 60)
    assert res["n_epochs"] == 10


def test_summary_measures_follow_the_standard_definitions():
    # 5 wake, then 10 sleep, 3 wake, 12 sleep, 2 wake
    night = [False] * 5 + [True] * 10 + [False] * 3 + [True] * 12 + [False] * 2
    m = summary_measures(night)
    assert m["tst_min"] == 22.0
    assert m["sol_min"] == 5.0, "SOL is the wake before the first sleep epoch"
    assert m["waso_min"] == 3.0, "WASO excludes wake before onset and after final awakening"
    assert m["se_pct"] == round(100 * 22 / 32, 1)


def test_a_night_with_no_sleep_does_not_raise():
    m = summary_measures([False] * 20)
    assert m["tst_min"] == 0.0 and m["waso_min"] == 0.0


def test_discrepancy_is_device_minus_reference():
    dev = summary_measures([True] * 30 + [False] * 10)
    ref = summary_measures([True] * 20 + [False] * 20)
    d = discrepancy(dev, ref)
    assert d["tst_min"] == 10.0


def test_bland_altman_reports_limits_not_just_bias():
    """A mean bias near zero with wide limits is the dangerous pattern: unbiased on average,
    unreliable on any individual night -- and one night is the unit this controller acts on."""
    ba = bland_altman([-20.0, 20.0, -18.0, 19.0])
    assert abs(ba["bias"]) < 2.0
    assert ba["loa_lower"] < -20.0 and ba["loa_upper"] > 20.0


def test_proportional_bias_is_detected():
    """A slope means the error grows with what is measured, so a single bias figure does not
    describe the device."""
    means = [100.0, 200.0, 300.0, 400.0]
    diffs = [5.0, 10.0, 15.0, 20.0]
    assert bland_altman(diffs, means)["proportional_bias_slope"] > 0.04


def test_no_proportional_bias_when_error_is_flat():
    ba = bland_altman([5.0, 5.0, 5.0, 5.0], [100.0, 200.0, 300.0, 400.0])
    assert abs(ba["proportional_bias_slope"]) < 1e-6


def test_evaluate_returns_all_three_analyses():
    res = evaluate([True] * 50 + [False] * 10, [True] * 45 + [False] * 15)
    assert set(res) == {"epoch_by_epoch", "device_summary", "reference_summary", "discrepancy"}
    assert res["epoch_by_epoch"]["n_epochs"] == 60


def test_an_empty_comparison_reports_zero_rather_than_raising():
    assert evaluate([], [])["epoch_by_epoch"]["n_epochs"] == 0
    assert bland_altman([])["n"] == 0
