"""Onset learner: personalize the induction warm nudge from measured onset latency, per-mode."""

from sleepctl.learning.onset_tuning import learn_onset, next_onset_warm_f


def _recs(warm_to_latency, mode="normal", reps=3):
    out = []
    for warm, lat in warm_to_latency:
        for _ in range(reps):
            out.append({"onset_warm_f": warm, "onset_latency_min": lat, "night_type": mode})
    return out


def test_learns_the_fastest_onset_warm_nudge():
    # +1.5 °F gives the fastest onset -> learner moves toward it (shrunk).
    recs = _recs([(0.0, 22.0), (1.0, 16.0), (1.5, 9.0)])
    m = learn_onset(recs, base_f=1.0, min_nights=6)
    assert m.is_personalized is True and m.direction == "warmer"
    assert 1.0 < m.onset_warm_f <= 1.5


def test_holds_default_until_enough_nights():
    m = learn_onset(_recs([(1.0, 15.0)], reps=2), base_f=1.0, min_nights=8)
    assert m.is_personalized is False and m.onset_warm_f == 1.0 and "learning" in m.rationale


def test_segments_by_mode_with_pooled_fallback():
    # Constrained nights prefer a smaller nudge (fast onset), normal nights a bigger one.
    recs = (_recs([(0.5, 8.0), (2.0, 18.0)], mode="constrained")
            + _recs([(0.5, 20.0), (2.0, 9.0)], mode="normal"))
    constrained = learn_onset(recs, base_f=1.0, min_nights=6, mode="constrained")
    normal = learn_onset(recs, base_f=1.0, min_nights=6, mode="normal")
    assert constrained.onset_warm_f < normal.onset_warm_f      # different optima per mode
    # A mode with too little data falls back to the pooled set (still returns something usable).
    sparse = learn_onset(_recs([(1.0, 12.0)], mode="recovery", reps=1) + recs,
                         base_f=1.0, min_nights=6, mode="recovery")
    assert sparse.n >= 6      # pooled, not the lone recovery night


def test_exploration_jitter_is_bounded_and_deterministic():
    vals = {next_onset_warm_f(1.0, i) for i in range(6)}
    assert all(0.0 <= v <= 2.5 for v in vals)
    assert next_onset_warm_f(1.0, 0) == next_onset_warm_f(1.0, 3)   # rotates, deterministic
    assert len(vals) >= 2                                            # actually explores
