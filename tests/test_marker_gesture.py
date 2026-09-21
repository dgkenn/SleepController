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


# ------------------------------------------------------------------ the SNAP (double tap) marker
# 2026-09-19: the user marked every awakening by flicking or snapping the band and the shake
# detector recorded none of them. A snap is an impulse, not a rhythm.

def _quiet(secs=4.0, noise=0.01, seed=11):
    random.seed(seed)
    return [1.0 + random.gauss(0, noise) for _ in range(int(FS * secs))]


def _tap(sig, at_s, amp=1.2, width=2):
    i0 = int(at_s * FS)
    for k in range(width):
        if i0 + k < len(sig):
            sig[i0 + k] += amp * (1.0 if k == 0 else 0.6)
    return sig


def test_two_sharp_taps_on_a_quiet_arm_are_a_marker():
    for gap in (0.25, 0.4, 0.6, 1.0):
        for amp in (0.9, 1.5, 3.0):
            sig = _tap(_tap(_quiet(), 1.0, amp), 1.0 + gap, amp)
            r = pmd.snap_gesture(sig, FS)
            assert r["marker"] is True, f"gap {gap}s at {amp} g was missed: {r}"
            assert r["kind"] == "snap" and abs(r["gap_s"] - gap) < 0.05


def test_one_tap_is_a_bump_not_a_marker():
    """A single impulse is what a knock against the headboard looks like."""
    assert pmd.snap_gesture(_tap(_quiet(), 1.5, 2.0), FS)["marker"] is False


def test_a_tap_ringing_is_still_one_tap():
    sig = _tap(_tap(_quiet(), 1.0, 1.5), 1.0 + 0.08, 1.2)      # 80 ms later: the band bouncing
    assert pmd.snap_gesture(sig, FS)["marker"] is False


def test_two_taps_too_far_apart_are_two_bumps():
    assert pmd.snap_gesture(_tap(_tap(_quiet(), 0.5, 1.5), 2.5, 1.5), FS)["marker"] is False


def test_taps_during_a_turn_are_not_a_marker():
    """Restless turning runs 0.2 g across the whole window; spikes inside it are not declared."""
    random.seed(5)
    sig = [1.0 + random.gauss(0, 0.20) for _ in range(int(FS * 4))]
    sig = _tap(_tap(sig, 1.0, 1.5), 1.4, 1.5)
    r = pmd.snap_gesture(sig, FS)
    assert r["marker"] is False and r["quiet_g"] > pmd.SNAP_QUIET_G


def test_a_big_roll_over_is_not_a_snap():
    random.seed(4)
    lurch = [1.0 + (1.0 if 40 < i < 90 else 0.0) + random.gauss(0, 0.02) for i in range(int(FS * 4))]
    assert pmd.snap_gesture(lurch, FS)["marker"] is False
    two = [1.0 + (1.0 if (40 < i < 70 or 90 < i < 120) else 0.0) + random.gauss(0, 0.02)
           for i in range(int(FS * 4))]
    assert pmd.snap_gesture(two, FS)["marker"] is False       # two slow lurches: too wide


def test_restless_turning_never_produces_a_false_snap():
    false_markers = 0
    for seed in range(200):
        random.seed(seed)
        sig = [1.0 + random.gauss(0, 0.20) for _ in range(int(FS * 4))]
        if pmd.snap_gesture(sig, FS)["marker"]:
            false_markers += 1
    assert false_markers == 0


def test_a_shake_is_not_a_snap_and_a_snap_is_not_a_shake_but_either_is_a_marker():
    shake = _osc(5.0, 0.5, secs=4.0)
    snap = _tap(_tap(_quiet(), 1.0, 1.5), 1.4, 1.5)
    assert pmd.snap_gesture(shake, FS)["marker"] is False
    assert pmd.marker_gesture(snap, FS)["marker"] is False
    assert pmd.any_marker(shake, FS)["kind"] == "shake"
    assert pmd.any_marker(snap, FS)["kind"] == "snap"
    assert pmd.any_marker(_quiet(), FS)["marker"] is False


def test_snap_degenerate_input_does_not_raise():
    for bad in ([], None, [1.0] * 3, [None] * 300):
        assert pmd.snap_gesture(bad, FS)["marker"] is False
    assert pmd.snap_gesture(_quiet(), 0.0)["marker"] is False


# ------------------------------------------------- the gesture this user actually performs
# 2026-09-21, in their words: "I shook the sensor on the arm band by snapping it on my skin
# and shaking it." Measured, that combination was missed by BOTH detectors above and fell
# between them -- the snap spread the spectrum so the shake's single-peak concentration read
# 0.23-0.32 against a 0.35 gate, while the shake kept the arm from being quiet so the snap's
# quiet-arm test read 0.33-0.36 g against a 0.12 limit.

def _shaken(sig, start_s, dur_s, freq, amp):
    for i in range(int(start_s * FS), min(len(sig), int((start_s + dur_s) * FS))):
        sig[i] += amp * math.sin(2 * math.pi * freq * (i - int(start_s * FS)) / FS)
    return sig


def test_a_snap_followed_by_a_shake_is_a_marker():
    for freq in (3.0, 4.0, 5.0, 6.5):
        for amp in (0.35, 0.6, 1.0):
            sig = _shaken(_tap(_quiet(), 1.0, 1.5), 1.3, 1.5, freq, amp)
            r = pmd.any_marker(sig, FS)
            assert r["marker"] is True, f"{freq} Hz at {amp} g was missed: {r}"


def test_a_short_shake_is_a_marker():
    """Spectral concentration is length-dependent, so the sustained-shake detector needs 1.5 s.
    Half-asleep, the shake is shorter than that."""
    for dur in (0.8, 1.0, 1.2):
        assert pmd.any_marker(_shaken(_quiet(), 1.0, dur, 5.0, 0.6), FS)["marker"] is True


def test_a_slow_deliberate_shake_is_a_marker_and_still_is_not_walking():
    assert pmd.marker_gesture(_osc(3.0, 0.6), FS)["marker"] is True
    assert pmd.GAIT_HI_HZ < pmd.MARKER_LO_HZ          # still no overlap with gait
    assert pmd.any_marker(_osc(1.9, 0.25), FS)["marker"] is False


def test_the_burst_detector_rejects_everything_a_sleeping_body_does():
    """Restless turning is large and broadband but STATIONARY; a gesture starts and stops."""
    for sd in (0.20, 0.35, 0.50):
        false_markers = 0
        for seed in range(200):
            random.seed(seed)
            sig = [1.0 + random.gauss(0, sd) for _ in range(int(FS * 4))]
            if pmd.any_marker(sig, FS)["marker"]:
                false_markers += 1
        assert false_markers == 0, f"{sd} g turning produced {false_markers} false markers"
    random.seed(4)
    lurch = [1.0 + (1.0 if 40 < i < 120 else 0.0) + random.gauss(0, 0.03)
             for i in range(int(FS * 4))]
    assert pmd.any_marker(lurch, FS)["marker"] is False
    assert pmd.burst_gesture(_osc(6.0, 0.05), FS)["marker"] is False      # tremor: too small


def test_the_burst_detector_reports_what_it_measured():
    r = pmd.burst_gesture(_shaken(_tap(_quiet(), 1.0, 1.5), 1.3, 1.5, 5.0, 0.6), FS)
    assert r["kind"] == "burst" and r["marker"] is True
    assert r["amp_g"] >= pmd.BURST_MIN_AMPLITUDE_G
    assert r["band_fraction"] >= pmd.BURST_MIN_BAND_FRACTION
    assert r["burst_ratio"] >= pmd.BURST_MIN_RATIO
    for bad in ([], None, [1.0] * 3, [None] * 300):
        assert pmd.burst_gesture(bad, FS)["marker"] is False
    assert pmd.burst_gesture(_quiet(), 0.0)["marker"] is False
