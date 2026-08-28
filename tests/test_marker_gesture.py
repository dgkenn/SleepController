"""A DELIBERATE arm-shake as a user-initiated event marker.

This is established practice, not an invention: Philips Actiwatch ships a physical event-marker
button pressed at lights-off and at awakenings. A gesture is the better instrument here because
no screen, light or unlocking is involved -- using a phone at 3 a.m. is itself arousing, which
contaminates the event being marked.

It is also the only anchor that is DECLARED rather than inferred, which is what makes it able to
settle a disagreement between two inferences -- the exact situation on 2026-08-27, where our wake
voter and our stager disagreed on 38 of 51 moments.
"""
import math
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import polar_pmd as pmd  # noqa: E402

FS = 52.0


def _osc(freq, amp, secs=3.0, noise=0.03, seed=1):
    random.seed(seed)
    return [1.0 + amp * math.sin(2 * math.pi * freq * (i / FS)) + random.gauss(0, noise)
            for i in range(int(FS * secs))]


def test_a_deliberate_shake_is_detected_across_the_realistic_range():
    for freq in (4.0, 4.5, 5.0, 6.0, 7.0):
        for amp in (0.3, 0.5, 0.8):
            r = pmd.marker_gesture(_osc(freq, amp), FS)
            assert r["marker"] is True, f"{freq} Hz at {amp} g was missed"


def test_walking_is_not_a_marker():
    """Gait lives at 1.2-2.8 Hz, below the marker band -- the two anchors must not collide."""
    assert pmd.marker_gesture(_osc(1.9, 0.25), FS)["marker"] is False


def test_tremor_is_not_a_marker():
    """Tremor overlaps in FREQUENCY (4-12 Hz) but is an order of magnitude weaker. Frequency
    alone would confuse the two; frequency plus amplitude does not."""
    assert pmd.marker_gesture(_osc(6.0, 0.05), FS)["marker"] is False


def test_a_big_roll_over_is_not_a_marker():
    random.seed(4)
    lurch = [1.0 + (0.6 if 40 < i < 90 else 0.0) + random.gauss(0, 0.05)
             for i in range(int(FS * 3))]
    assert pmd.marker_gesture(lurch, FS)["marker"] is False


def test_restless_turning_does_not_produce_a_false_marker():
    """The measured near-miss: broadband turning occasionally lands a lucky in-band peak and
    reached 0.166 concentration, which produced a FALSE marker at a lower gate. A fabricated
    'known awake' instant poisons the ground truth the whole validation rests on, so the gate is
    set well above it."""
    false_markers = 0
    for seed in range(100):
        random.seed(seed)
        sig = [1.0 + random.gauss(0, 0.20) for _ in range(int(FS * 3))]
        if pmd.marker_gesture(sig, FS)["marker"]:
            false_markers += 1
    assert false_markers == 0


def test_stillness_produces_no_marker():
    random.seed(7)
    still = [1.0 + random.gauss(0, 0.005) for _ in range(int(FS * 3))]
    assert pmd.marker_gesture(still, FS)["marker"] is False


def test_a_burst_too_short_declines_to_answer():
    r = pmd.marker_gesture(_osc(5.0, 0.5, secs=0.5), FS)
    assert r["marker"] is False and r.get("too_short") is True


def test_the_gate_sits_between_the_measured_populations():
    """Confounders topped out at 0.155; true shakes bottomed out at 0.583."""
    assert 0.155 < pmd.MARKER_MIN_CONCENTRATION < 0.583


def test_the_marker_and_gait_bands_do_not_overlap():
    assert pmd.GAIT_HI_HZ < pmd.MARKER_LO_HZ


def test_degenerate_input_does_not_raise():
    for bad in ([], None, [1.0] * 3, [None] * 300):
        assert pmd.marker_gesture(bad, FS)["marker"] is False
    assert pmd.marker_gesture(_osc(5.0, 0.5), 0.0)["marker"] is False
