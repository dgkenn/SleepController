"""Learn the ideal architecture itself from the morning subjective survey — bounded to evidence."""

from datetime import datetime

from sleepctl.benchmarks import NightMode, targets_for
from sleepctl.learning.ideal_architecture import learn_ideal_architecture
from sleepctl.models import ContextRecord, NightSummary


class _Repo:
    """Tiny in-memory repo: nights + their morning-survey contexts."""
    def __init__(self, rows):
        self._nights = [n for n, _ in rows]
        self._ctx = {n.date: c for n, c in rows}

    def recent_nights(self, n):
        return self._nights[-n:]

    def get_context(self, date):
        return self._ctx.get(date)


def _row(i, deep_pct, rem_pct, felt, tst=420):
    date = f"2026-06-{i+1:02d}"
    n = NightSummary(date=date, total_sleep_min=tst,
                     deep_min=deep_pct * tst, rem_min=rem_pct * tst)
    c = ContextRecord(date=date)
    c.subjective_quality = felt
    return (n, c)


def test_thin_data_returns_the_evidence_prior():
    base = targets_for(NightMode.NORMAL)
    repo = _Repo([_row(i, 0.20, 0.22, 7) for i in range(5)])    # < min_nights
    out = learn_ideal_architecture(repo, NightMode.NORMAL)
    assert out["deep_pct_ideal"] == base.deep_pct_ideal and out["rem_pct_ideal"] == base.rem_pct_ideal


def test_learns_toward_the_architecture_of_your_best_nights():
    base = targets_for(NightMode.NORMAL)
    # nights you rated HIGH had MORE deep; low-rated nights had less -> ideal should move UP
    rows = []
    for i in range(20):
        if i % 2 == 0:
            rows.append(_row(i, 0.25, 0.22, 9))     # lots of deep, felt great
        else:
            rows.append(_row(i, 0.13, 0.22, 3))     # little deep, felt bad
    out = learn_ideal_architecture(_Repo(rows), NightMode.NORMAL)
    assert out["deep_pct_ideal"] > base.deep_pct_ideal              # moved toward your best
    assert out["deep_pct_min"] > base.deep_pct_min                  # floor shifts in lockstep


def test_never_drifts_far_from_evidence_even_with_extreme_data():
    base = targets_for(NightMode.NORMAL)
    # absurd: every good night had 40% deep — the learner must still stay bounded near evidence
    rows = [_row(i, 0.40 if i % 2 == 0 else 0.10, 0.22, 9 if i % 2 == 0 else 1) for i in range(40)]
    out = learn_ideal_architecture(_Repo(rows), NightMode.NORMAL)
    assert out["deep_pct_ideal"] <= base.deep_pct_ideal + 0.045     # bounded shift
    assert out["deep_pct_ideal"] <= 0.26                           # hard evidence band


def test_no_felt_variation_returns_prior():
    base = targets_for(NightMode.NORMAL)
    rows = [_row(i, 0.10 + 0.01 * i, 0.22, 7) for i in range(20)]   # same rating every night
    out = learn_ideal_architecture(_Repo(rows), NightMode.NORMAL)
    assert out["deep_pct_ideal"] == base.deep_pct_ideal


def test_personalized_targets_apply_the_learned_levels():
    from sleepctl.learning.perfect_weights import personalized_targets
    rows = [_row(i, 0.25 if i % 2 == 0 else 0.13, 0.22, 9 if i % 2 == 0 else 3) for i in range(20)]
    tgt = personalized_targets(_Repo(rows), NightMode.NORMAL)
    assert tgt.deep_pct_ideal > targets_for(NightMode.NORMAL).deep_pct_ideal
