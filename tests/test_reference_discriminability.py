"""How much motion evidence the reference's own wake calls actually rest on.

`calibrate_scale` fits the count scale so a night reaches a target sleep fraction, which forces
roughly 12% of epochs to be scored wake whether or not any contain motion. On this sleeper's
compressed distributions (median 27, p95 ~103 on a scale saturating at 1000) the threshold lands
inside the noise: on 2026-08-27 and 2026-08-30, 34% and 26% of the reference's wake calls sit at
or below the night's median movement.
"""

from sleepctl.eval.performance import reference_discriminability


def test_wake_calls_without_motion_evidence_are_counted():
    counts = [10.0] * 8 + [500.0, 600.0]
    # The reference calls wake on two quiet epochs and both loud ones.
    ref = [True, False, True, True, False, True, True, True, False, False]
    d = reference_discriminability(counts, ref)
    assert d["reference_wake_epochs"] == 4
    assert d["wake_calls_at_or_below_median_motion"] == 2
    assert d["wake_calls_without_motion_evidence_frac"] == 0.5


def test_a_clean_reference_scores_zero_unevidenced_calls():
    counts = [10.0] * 8 + [500.0, 600.0]
    ref = [True] * 8 + [False, False]
    d = reference_discriminability(counts, ref)
    assert d["wake_calls_without_motion_evidence_frac"] == 0.0


def test_a_compressed_distribution_is_reported():
    """A flat movement signal is what makes the reference's threshold arbitrary."""
    d = reference_discriminability([27.0] * 100, [True] * 99 + [False])
    assert d["motion_dynamic_range"] == 1.0


def test_no_wake_calls_at_all_is_reported_not_divided_by():
    d = reference_discriminability([10.0, 20.0], [True, True])
    assert d["reference_wake_epochs"] == 0
    assert "wake_calls_without_motion_evidence_frac" not in d


def test_empty_input_is_safe():
    assert reference_discriminability([], []) == {"n": 0}


def test_unlabelled_epochs_are_excluded():
    d = reference_discriminability([10.0, 20.0, 30.0], [True, None, False])
    assert d["n"] == 2
