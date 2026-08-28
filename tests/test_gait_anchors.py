"""Rhythmic locomotion as an INDEPENDENT objective wake anchor.

The validation layer (`eval/wake_anchors`) needs known-awake instants this system did not
produce. Every wake signal we have -- `_actigraphy_wake`, `movement_spike`, `low_motion_break`
-- reads motion AMPLITUDE, so amplitude cannot serve as the anchor without validating the
detector against its own input.

Cadence is read by nothing. And amplitude genuinely cannot do this job: measured below, a single
postural turn in bed registers a LARGER amplitude than walking does, while carrying almost no
periodicity. Sustained gait is near-certain evidence of being awake and out of bed.
"""
import math
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import polar_pmd as pmd  # noqa: E402

FS = 52.0


def _walk(secs=10.0, cadence=1.9, noise=0.03, amp=0.2, seed=1):
    random.seed(seed)
    return [1.0 + amp * math.sin(2 * math.pi * cadence * (i / FS)) + random.gauss(0, noise)
            for i in range(int(FS * secs))]


def _turn(secs=10.0, seed=2):
    """One large postural change -- bigger in amplitude than a walk, but not rhythmic."""
    random.seed(seed)
    return [1.0 + (0.5 if int(FS * 3) < i < int(FS * 4) else 0.0) + random.gauss(0, 0.03)
            for i in range(int(FS * secs))]


def test_walking_is_detected():
    r = pmd.locomotion_features(_walk(), FS)
    assert r["gait"] is True
    assert pmd.GAIT_LO_HZ <= r["cadence_hz"] <= pmd.GAIT_HI_HZ


def test_the_full_realistic_cadence_range_is_covered():
    for cadence in (1.3, 1.6, 1.9, 2.2, 2.6):
        assert pmd.locomotion_features(_walk(cadence=cadence), FS)["gait"] is True, cadence


def test_a_bigger_but_arrhythmic_movement_is_not_gait():
    """The measurement that justifies this whole approach: the turn has HIGHER amplitude."""
    walk = pmd.locomotion_features(_walk(), FS)
    turn = pmd.locomotion_features(_turn(), FS)
    assert turn["amp_g"] > walk["amp_g"], "the turn should be the larger movement"
    assert turn["gait"] is False and walk["gait"] is True


def test_restless_turning_breathing_and_tremor_are_all_rejected():
    random.seed(9)
    restless = [1.0 + random.gauss(0, 0.12) for _ in range(int(FS * 10))]
    breathing = [1.0 + 0.05 * math.sin(2 * math.pi * 0.25 * (i / FS)) for i in range(int(FS * 10))]
    tremor = [1.0 + 0.15 * math.sin(2 * math.pi * 6.0 * (i / FS)) for i in range(int(FS * 10))]
    for sig in (restless, breathing, tremor):
        assert pmd.locomotion_features(sig, FS)["gait"] is False


def test_stillness_produces_no_anchor():
    random.seed(3)
    still = [1.0 + random.gauss(0, 0.004) for _ in range(int(FS * 10))]
    assert pmd.locomotion_features(still, FS)["gait"] is False


def test_a_short_window_reports_too_short_rather_than_guessing():
    """Spectral concentration is not length-normalised -- the same walk scores 0.101 over 2 s and
    0.591 over 12 s -- so a fixed threshold silently encodes a duration requirement. It is made
    explicit, and a short window declines to answer instead of answering wrongly."""
    r = pmd.locomotion_features(_walk(secs=4.0), FS)
    assert r["gait"] is False and r.get("too_short") is True


def test_the_minimum_window_is_where_the_margin_is_large():
    walk = pmd.locomotion_features(_walk(secs=pmd.GAIT_MIN_WINDOW_S), FS)
    turn = pmd.locomotion_features(_turn(secs=pmd.GAIT_MIN_WINDOW_S), FS)
    assert walk["concentration"] > 4 * max(turn["concentration"], 0.01)


def test_empty_or_degenerate_input_does_not_raise():
    for bad in ([], None, [1.0] * 5, [None] * 600):
        assert pmd.locomotion_features(bad, FS)["gait"] is False
    assert pmd.locomotion_features(_walk(), 0.0)["gait"] is False
