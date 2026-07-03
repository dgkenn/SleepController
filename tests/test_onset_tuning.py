"""Onset learner: personalize the induction warm nudge from measured onset latency, per-mode."""

from sleepctl.learning.onset_tuning import (
    COLD_SETTLE_BOUNDS, decide_warm_pulse, learn_cold_settle, learn_onset,
    next_cold_settle_f, next_onset_warm_f)


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


# ---- cold-settle depth learner -------------------------------------------
def _cold_recs(cold_to_latency, mode="normal", reps=3):
    out = []
    for cold, lat in cold_to_latency:
        for _ in range(reps):
            out.append({"onset_cold_settle_f": cold, "onset_latency_min": lat,
                        "warm_pulse_on": True, "night_type": mode})
    return out


def test_learns_the_fastest_cold_settle_depth():
    # 58 °F (colder) gives the fastest onset -> learner moves toward it (shrunk, clamped).
    recs = _cold_recs([(66.0, 22.0), (62.0, 16.0), (58.0, 9.0)])
    m = learn_cold_settle(recs, base_f=60.0, min_nights=6)
    assert m.is_personalized is True and m.direction == "colder"
    assert 58.0 <= m.onset_cold_settle_f < 60.0
    lo, hi = COLD_SETTLE_BOUNDS
    assert lo <= m.onset_cold_settle_f <= hi


def test_cold_settle_holds_default_until_enough_nights():
    m = learn_cold_settle(_cold_recs([(60.0, 15.0)], reps=2), base_f=60.0, min_nights=8)
    assert m.is_personalized is False and m.onset_cold_settle_f == 60.0 and "learning" in m.rationale


def test_cold_settle_jitter_is_bounded_and_deterministic():
    lo, hi = COLD_SETTLE_BOUNDS
    vals = {next_cold_settle_f(60.0, i) for i in range(6)}
    assert all(lo <= v <= hi for v in vals)
    assert next_cold_settle_f(60.0, 0) == next_cold_settle_f(60.0, 3)   # rotates, deterministic
    assert len(vals) >= 2                                               # actually explores


# ---- warm-pulse A/B ------------------------------------------------------
def _pulse_recs(on_lat, off_lat, reps=4):
    out = []
    for _ in range(reps):
        out.append({"onset_latency_min": on_lat, "warm_pulse_on": True, "night_type": "normal"})
        out.append({"onset_latency_min": off_lat, "warm_pulse_on": False, "night_type": "normal"})
    return out


def test_warm_pulse_picks_the_faster_arm():
    # Pulse-on onset is clearly faster -> run the pulse.
    run, _ = decide_warm_pulse(_pulse_recs(on_lat=9.0, off_lat=18.0), night_index=0)
    assert run is True
    # Pulse-off is clearly faster -> skip the pulse.
    run, _ = decide_warm_pulse(_pulse_recs(on_lat=18.0, off_lat=9.0), night_index=0)
    assert run is False


def test_warm_pulse_explores_when_undecided():
    # A wash (equal arms) -> explore, alternating deterministically by night index.
    recs = _pulse_recs(on_lat=12.0, off_lat=12.0)
    assert decide_warm_pulse(recs, night_index=0)[0] is True    # even night leans to the pulse
    assert decide_warm_pulse(recs, night_index=1)[0] is False
    # Too little data -> also explores (never fixes an arm prematurely).
    assert decide_warm_pulse([], night_index=0)[0] is True
