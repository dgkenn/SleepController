"""Per-person thermal wake maneuver: learn the wake temperature that wakes you least groggy."""

from sleepctl.learning.thermal_wake import learn_thermal_wake, next_wake_f


def _recs(pairs):
    return [{"wake_thermal_f": f, "grogginess": g} for f, g in pairs]


def test_too_few_nights_holds_default():
    t = learn_thermal_wake(_recs([(74, 3)] * 4), base_f=74, min_nights=8)
    assert t.is_personalized is False and t.wake_f == 74


def test_learns_cooler_wake_when_it_is_less_groggy():
    # warm wakes (78) leave high grogginess; cool wakes (72) leave low -> learn cooler.
    pairs = [(78, 8), (78, 7), (78, 8), (74, 5), (74, 5), (74, 5),
             (72, 2), (72, 1), (72, 2), (72, 2), (72, 1), (72, 2)]
    t = learn_thermal_wake(_recs(pairs), base_f=74, min_nights=8)
    assert t.is_personalized is True and t.wake_f < 74 and t.direction == "cooler"


def test_learns_warmer_wake_when_it_is_less_groggy():
    pairs = [(72, 8), (72, 7), (72, 8), (74, 5), (74, 5), (74, 5),
             (78, 2), (78, 1), (78, 2), (78, 2), (78, 1), (78, 2)]
    t = learn_thermal_wake(_recs(pairs), base_f=74, min_nights=8)
    assert t.is_personalized is True and t.wake_f > 74 and t.direction == "warmer"


def test_bounds_respected():
    pairs = [(86, 1)] * 12
    t = learn_thermal_wake(_recs(pairs), base_f=74, min_nights=8)
    assert 70 <= t.wake_f <= 86


def test_exploration_jitter_rotates_and_clamps():
    vals = {next_wake_f(74, i) for i in range(3)}
    assert vals == {72.0, 74.0, 76.0}                 # samples around the best
    assert next_wake_f(86, 1) <= 86                    # clamped to the safe ceiling
