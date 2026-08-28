"""iPhone Screen Time PICKUPS as coarse independent wake evidence.

Screen Time's headline charts measure APP USAGE, which needs an unlock and an app -- and this
user's overnight behaviour is pressing the side button to read the lock-screen clock, so those
charts correctly show a flat line and carry no information.

PICKUPS are different: a pickup is recorded whenever the device wakes from idle, INCLUDING
waking the screen without unlocking. That is precisely the behaviour in question.
"""
from sleepctl.eval.pickup_anchors import evaluate_pickups, format_report


def _rows(awake_hours=(), hours=range(0, 7), unstaged=()):
    out = []
    for h in hours:
        if h in unstaged:
            continue
        for m in range(0, 60, 10):
            out.append({"ts": f"2026-08-28T{h:02d}:{m:02d}:00",
                        "stage": "awake" if h in awake_hours else "light"})
    return out


def test_an_hour_we_called_entirely_asleep_with_a_pickup_in_it_is_a_miss():
    res = evaluate_pickups(_rows(), {3: 1})
    assert res["missed_hours"] == [3] and res["miss_rate"] == 1.0


def test_an_hour_we_partly_called_awake_is_caught():
    res = evaluate_pickups(_rows(awake_hours=(3,)), {3: 2})
    assert res["caught_hours"] == [3] and res["miss_rate"] == 0.0


def test_hours_without_pickups_are_not_judged():
    """Absence of a pickup proves nothing -- most awakenings do not involve the phone."""
    res = evaluate_pickups(_rows(), {3: 0, 4: 0})
    assert res["n_judged"] == 0 and res["hours_with_pickups"] == []


def test_an_unstaged_hour_is_a_sensor_gap_not_a_miss():
    """Scoring an hour we never labelled would be inventing a result."""
    res = evaluate_pickups(_rows(unstaged=(3,)), {3: 1})
    assert res["unstaged_hours"] == [3]
    assert res["missed_hours"] == [] and res["n_judged"] == 0


def test_unknown_labels_do_not_count_as_staged():
    rows = [{"ts": "2026-08-28T03:10:00", "stage": "unknown"} for _ in range(5)]
    res = evaluate_pickups(rows, {3: 1})
    assert res["unstaged_hours"] == [3]


def test_the_labels_of_a_missed_hour_are_reported():
    """WHICH wrong label matters -- calling a pickup hour DEEP is a different bug from LIGHT."""
    rows = [{"ts": f"2026-08-28T03:{m:02d}:00", "stage": "deep"} for m in range(0, 60, 10)]
    res = evaluate_pickups(rows, {3: 1})
    assert res["labels_in_missed_hours"][3] == ["deep"]


def test_the_night_window_can_be_restricted():
    """Daytime pickups are not evidence about sleep."""
    res = evaluate_pickups(_rows(hours=range(0, 24)), {3: 1, 14: 9}, night_hours=range(0, 7))
    assert res["hours_with_pickups"] == [3]


def test_a_night_with_no_pickups_is_reported_as_uninformative():
    out = format_report(evaluate_pickups(_rows(), {}))
    assert "uninformative" in out and "not reassuring" in out


def test_the_report_states_its_own_limitations():
    out = format_report(evaluate_pickups(_rows(), {3: 1}))
    assert "hour resolution" in out
    assert "measures misses only" in out
