"""The metrics that produce every accuracy number this project's decisions rest on.

`cohen_kappa`, `binary_prf` and `per_class_recall` in ``sleepctl/ml/sleep_staging/train.py`` are
what the docs quote (κ 0.436/0.450), what the leave-subjects-out CV compares arms with, and what
decided that motion features hurt the stager. They had no tests at all. A silently wrong metric
does not fail anything — it just makes every one of those conclusions wrong in the same direction,
invisibly.

Expected values here are hand-computed from the confusion matrix, so these tests depend on nothing
but the definitions. They were additionally cross-checked once against scikit-learn's
``cohen_kappa_score`` / ``precision_recall_fscore_support`` / ``recall_score`` over several
thousand randomized cases; agreement was exact to floating point (max delta 3.6e-16). sklearn is
not a project dependency, so that cross-check is recorded here rather than imported.
"""

from __future__ import annotations

import math

import pytest

from sleepctl.ml.sleep_staging.train import binary_prf, cohen_kappa, per_class_recall


# ------------------------------------------------------------------ cohen_kappa
def test_perfect_agreement_is_one():
    assert cohen_kappa([0, 0, 0, 1], [0, 0, 0, 1], 2) == pytest.approx(1.0)


def test_chance_level_agreement_is_zero():
    """cm=[[1,1],[1,1]]: po=0.5, pe=0.5 -> kappa 0. Half right by coin flip is worth nothing."""
    assert cohen_kappa([0, 0, 1, 1], [0, 1, 0, 1], 2) == pytest.approx(0.0)


def test_perfect_disagreement_is_minus_one():
    assert cohen_kappa([0, 0, 1, 1], [1, 1, 0, 0], 2) == pytest.approx(-1.0)


def test_a_worked_four_class_example():
    y_true = [0, 0, 1, 1, 2, 3]
    y_pred = [0, 1, 1, 2, 2, 0]
    # agreements at idx 0, 2, 4          -> po = 3/6 = 0.5
    # true marginals [2, 2, 1, 1]; pred marginals [2, 2, 2, 0]
    # pe = (2*2 + 2*2 + 2*1 + 0*1) / 36  = 10/36
    expected = (0.5 - 10 / 36) / (1 - 10 / 36)          # = 8/26
    assert cohen_kappa(y_true, y_pred, 4) == pytest.approx(expected)
    assert expected == pytest.approx(8 / 26)


def test_degenerate_single_class_scores_zero_not_one():
    """THE property that matters for a sleep stager: predicting one class for everything, on a
    night that happens to be all that class, is 100% 'accurate' and worth zero agreement. If this
    returned 1.0, a stager stuck on 'light' would look excellent on a light-dominated night."""
    assert cohen_kappa([1, 1, 1, 1], [1, 1, 1, 1], 4) == pytest.approx(0.0)


def test_empty_input_is_zero_not_a_crash():
    assert cohen_kappa([], [], 4) == 0.0


def test_kappa_is_bounded():
    for yt, yp in ([[0, 1, 2, 3], [0, 1, 2, 3]], [[0, 1, 2, 3], [3, 2, 1, 0]],
                   [[0, 0, 1, 1], [0, 1, 1, 0]]):
        k = cohen_kappa(yt, yp, 4)
        assert -1.0 - 1e-9 <= k <= 1.0 + 1e-9, k


def test_kappa_rewards_a_real_model_over_a_constant_one():
    """The comparison the CV actually makes: a stager with signal must outscore a constant one."""
    y_true = [0, 1, 2, 3] * 10
    good = [0, 1, 2, 3] * 10                       # perfect
    lazy = [1] * 40                                # always predicts the modal class
    assert cohen_kappa(y_true, good, 4) > cohen_kappa(y_true, lazy, 4)


# ------------------------------------------------------------------ binary_prf
def test_binary_prf_worked_example():
    #        tp=2 (idx 0,1)   fp=1 (idx 4)   fn=1 (idx 2)
    y_true = [0, 0, 0, 1, 1]
    y_pred = [0, 0, 1, 1, 0]
    p, r, f = binary_prf(y_true, y_pred, positive=0)
    assert p == pytest.approx(2 / 3)
    assert r == pytest.approx(2 / 3)
    assert f == pytest.approx(2 / 3)


def test_binary_prf_precision_and_recall_are_not_the_same_quantity():
    """A regression guard: swapping fp/fn in the implementation would pass a symmetric case."""
    y_true = [0, 0, 0, 0, 1]
    y_pred = [0, 1, 1, 1, 1]        # tp=1 (idx 0), fp=0, fn=3 (idx 1,2,3)
    p, r, f = binary_prf(y_true, y_pred, positive=0)
    assert p == pytest.approx(1.0), "every positive prediction was right -> precision 1"
    assert r == pytest.approx(1 / 4), "but 3 of 4 real positives were missed -> recall 0.25"
    assert p != r, "swapping fp/fn in the implementation would make these equal"


def test_binary_prf_no_positive_predictions_is_zero_not_a_crash():
    assert binary_prf([0, 0, 1], [1, 1, 1], positive=0) == (0.0, 0.0, 0.0)


def test_binary_prf_no_positive_truths_is_zero_not_a_crash():
    p, r, f = binary_prf([1, 1, 1], [0, 1, 1], positive=0)
    assert (p, r, f) == (0.0, 0.0, 0.0)


def test_binary_prf_honours_the_positive_class():
    y_true, y_pred = [0, 1, 1], [0, 1, 0]
    assert binary_prf(y_true, y_pred, positive=0) != binary_prf(y_true, y_pred, positive=1)


def test_binary_prf_perfect_is_all_ones():
    assert binary_prf([0, 1, 0], [0, 1, 0], positive=0) == (1.0, 1.0, 1.0)


# ------------------------------------------------------------------ per_class_recall
def test_per_class_recall_worked_example():
    y_true = [0, 0, 1, 1, 2, 2]
    y_pred = [0, 1, 1, 1, 2, 0]
    assert per_class_recall(y_true, y_pred, 3) == pytest.approx([0.5, 1.0, 0.5])


def test_per_class_recall_is_nan_for_an_absent_class():
    """NaN, not 0.0: a class that never occurred has an UNDEFINED recall, and reporting 0
    would drag a mean down as though the model had failed at something it was never asked."""
    out = per_class_recall([0, 0, 1], [0, 0, 1], 4)
    assert out[0] == pytest.approx(1.0) and out[1] == pytest.approx(1.0)
    assert math.isnan(out[2]) and math.isnan(out[3])


def test_per_class_recall_returns_one_entry_per_class():
    assert len(per_class_recall([0, 1], [0, 1], 4)) == 4


def test_per_class_recall_ignores_predictions_of_absent_classes():
    """Predicting a class that never occurs costs recall on the true class, not on the phantom."""
    out = per_class_recall([0, 0, 0], [0, 0, 3], 4)
    assert out[0] == pytest.approx(2 / 3)
    assert math.isnan(out[3])
